"""CapacityForecastAgent — Reasoner-tier trend/regression forecaster.

GitLab #99 ("New agent/reasoner: capacity planning and forecasting").
Ranked #6/13 of the 2026-07-23 world-class-tool roadmap pass.

Distinct from the point-in-time snapshots vSphere/Linux/Windows already collect
at each sweep: this agent runs a deterministic linear-regression trend model
over the time-series ALREADY sitting in ``vsphere_datastore_metrics``
(``VsphereDatastoreMetric`` — an append-only capacity/free sample per
datastore, per collection, written by ``VsphereAgent._upsert_datastore``) to
answer "N days until disk-full" for each datastore.

Per the issue's own design sketch:
  - No new bucket table — this computes over EXISTING history
    (``VsphereDatastoreMetric``), the same choice ``drift_learning`` made by
    writing its output to the existing ``Instinct`` table rather than adding a
    new one.
  - No new graph edge — reuses the existing "promote to Instinct" attachment
    pattern (matches ``drift_learning``'s HAS_DRIFT-style output), rather than
    adding a new ``FORECASTS`` edge/table that nothing yet consumes.
  - Gated like the existing reasoner-tier flags (``rootcause_llm_enabled``,
    ``compliance_gap_finder_enabled``): strictly opt-in via
    ``capacity_forecast_enabled`` (default False). Unlike those two, this
    agent has no non-LLM fallback behavior to preserve — when the flag is
    off, the WHOLE collector has nothing to do, so it raises
    ``CollectorSkipped`` (status="skipped") rather than completing with an
    empty result — the same convention ``backup``/``dns``/``net``/``cloud``/
    ``container_registry`` use for "nothing configured", not the
    rootcause/compliance-gap pattern (those gate an optional *enhancement*
    within an otherwise-always-running collector, so they fall back to
    real deterministic work instead of skipping).
  - Purely a regression over numbers already in Postgres — no LLM call, no
    external HTTP/API call of any kind. Read-only by construction: this
    agent only ever runs SELECT queries against infra-brain's own database
    (via the shared SQLAlchemy session) and writes Instinct rows, exactly
    like ``drift_learning``.
  - Retention note (from the issue): ``retention_snapshots_days`` (default 90)
    caps how far back any trend analysis can reach. This agent additionally
    caps its own lookback to that same setting so it never claims to reason
    over data older than what retention/pruning actually guarantees is there.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from infra_brain.agents.base import BaseAgent
from infra_brain.db.models import ZONE_CORPORATE, Instinct, VsphereDatastoreMetric
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectorSkipped
from infra_brain.etl.spec import AgentSpec, Tier

log = logging.getLogger(__name__)

# Minimum number of samples for a datastore before a trend is considered
# statistically meaningful at all (two points is a line, not a trend).
_MIN_SAMPLES = 5

# Minimum elapsed time between the first and last sample considered before
# projecting a forecast — a trend fit across a few hours of noise is not a
# capacity trend.
_MIN_SPAN_DAYS = 2.0

# Cap the number of metric rows scanned per datastore, mirroring
# drift_learning's _MAX_EVENTS_SCANNED bound (AA-R-19): bound memory/query
# cost regardless of how long a datastore has been collected.
_MAX_SAMPLES_PER_DATASTORE = 2_000

# Slope (used_pct per day) below which growth is treated as noise/flat rather
# than a real trend — avoids a near-zero (or negative) slope producing a wild
# or negative "days until threshold" projection.
_MIN_GROWTH_PCT_PER_DAY = 0.01

# Confidence is derived from the regression's R² (goodness of fit), clamped
# into the same [0.05, 0.99] band the rootcause LLM finding uses, so this
# reasoner's Instinct rows read on the same confidence scale as every other
# reasoner-tier agent's output.
_MIN_CONFIDENCE = 0.05
_MAX_CONFIDENCE = 0.99


def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Ordinary least squares fit of ys = slope * xs + intercept.

    Returns (slope, intercept, r_squared). Pure-Python (no numpy dependency)
    — this agent's inputs are at most _MAX_SAMPLES_PER_DATASTORE floats per
    datastore, well within what a plain Python loop handles cheaply.
    """
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    if ss_xx == 0:
        return 0.0, mean_y, 0.0
    slope = ss_xy / ss_xx
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot == 0:
        # Every sample identical: a perfect (degenerate) fit, flat line.
        return slope, intercept, 1.0
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r_squared = max(0.0, 1.0 - (ss_res / ss_tot))
    return slope, intercept, r_squared


class CapacityForecastAgent(BaseAgent):
    """Forecasts days-until-threshold for datastore capacity via linear trend."""

    spec = AgentSpec(
        domain="capacity_forecast",
        tier=Tier.REASONER,
        schedule="0 8 * * 0",
        max_staleness=timedelta(days=8),
        skip_hook=True,
    )

    # ------------------------------------------------------------------
    # collect()
    # ------------------------------------------------------------------

    def collect(self, scope: str = "all") -> list[dict[str, Any]]:
        """Fit a trend per datastore and promote forecasts to Instinct rows.

        Strictly opt-in (``capacity_forecast_enabled``, default False). When
        off, the whole collector has nothing to do, so it raises
        CollectorSkipped (status="skipped") — see the module docstring for
        why this differs from rootcause_llm_enabled/compliance_gap_finder_enabled.
        All meaningful work when enabled is the side-effect of writing
        Instinct rows, same as drift_learning.
        """
        if not getattr(self.settings, "capacity_forecast_enabled", False):
            raise CollectorSkipped("capacity_forecast_enabled is off")

        threshold_pct = getattr(self.settings, "capacity_forecast_used_pct_threshold", 90.0)
        retention_days = getattr(self.settings, "retention_snapshots_days", 90)

        try:
            with get_session() as session:
                cutoff = datetime.now(UTC) - timedelta(days=retention_days)
                rows = (
                    session.query(VsphereDatastoreMetric)
                    .filter(VsphereDatastoreMetric.collected_at >= cutoff)
                    .filter(VsphereDatastoreMetric.used_pct.isnot(None))
                    .order_by(
                        VsphereDatastoreMetric.vcenter,
                        VsphereDatastoreMetric.moref,
                        VsphereDatastoreMetric.collected_at.asc(),
                    )
                    .all()
                )
                if not rows:
                    return []

                # Key is (vcenter, moref) to avoid collisions across vCenters —
                # MoRefs are only unique within a single vCenter instance (see
                # VsphereDatastore's UniqueConstraint("vcenter", "moref", ...)
                # and the same convention used by agents/vsphere.py, GitLab #184).
                by_vcenter_moref: dict[tuple[str, str], list[VsphereDatastoreMetric]] = (
                    defaultdict(list)
                )
                for row in rows:
                    samples = by_vcenter_moref[(row.vcenter, row.moref)]
                    if len(samples) < _MAX_SAMPLES_PER_DATASTORE:
                        samples.append(row)

                now_utc = datetime.now(UTC)

                for (vcenter, moref), samples in by_vcenter_moref.items():
                    if len(samples) < _MIN_SAMPLES:
                        continue

                    first_dt = samples[0].collected_at
                    last_dt = samples[-1].collected_at
                    if first_dt.tzinfo is None:
                        first_dt = first_dt.replace(tzinfo=UTC)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=UTC)
                    span_days = (last_dt - first_dt).total_seconds() / 86400.0
                    if span_days < _MIN_SPAN_DAYS:
                        continue

                    xs = []
                    for s in samples:
                        dt = s.collected_at
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        xs.append((dt - first_dt).total_seconds() / 86400.0)
                    ys = [float(s.used_pct) for s in samples]

                    slope, intercept, r_squared = _linear_regression(xs, ys)
                    if slope < _MIN_GROWTH_PCT_PER_DAY:
                        # Flat or shrinking usage — nothing to forecast.
                        continue

                    current_used_pct = ys[-1]
                    if current_used_pct >= threshold_pct:
                        days_until_threshold = 0.0
                    else:
                        days_until_threshold = (threshold_pct - current_used_pct) / slope
                    days_until_threshold = round(max(0.0, days_until_threshold), 1)

                    confidence = round(min(_MAX_CONFIDENCE, max(_MIN_CONFIDENCE, r_squared)), 3)

                    name = samples[-1].name
                    pattern = (
                        f"datastore {name} ({moref}, vcenter={vcenter}) is projected to reach "
                        f"{threshold_pct:.0f}% used in ~{days_until_threshold:.1f} day(s) "
                        f"at current growth rate ({slope:.3f}%/day, "
                        f"currently {current_used_pct:.1f}%)"
                    )
                    citation = f"capacity_forecast:vsphere_datastore_metrics:{vcenter}:{moref}"

                    existing = (
                        session.query(Instinct)
                        .filter_by(domain="vsphere", promoted_by="capacity_forecast")
                        .filter(Instinct.citation == citation)
                        .first()
                    )
                    if existing:
                        existing.pattern = pattern
                        existing.confidence = confidence
                        existing.promoted_at = now_utc
                        continue

                    instinct = Instinct(
                        id=uuid.uuid4(),
                        zone=ZONE_CORPORATE,
                        domain="vsphere",
                        pattern=pattern,
                        confidence=confidence,
                        promoted_by="capacity_forecast",
                        citation=citation,
                    )
                    session.add(instinct)

                session.commit()
        except Exception:
            log.warning("CapacityForecastAgent.collect() failed", exc_info=True)
            raise

        return []
