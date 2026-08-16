"""Task F5 — DriftLearningAgent.

Aggregates historical DriftEvent rows and writes Instinct records so that
build_context() can surface them as monitoring hints to other agents.

Instinct fields written:
  domain      — the resource domain (e.g. "cloud", "linux") being analysed
  confidence  — frequency-based score in (0, 1]
  pattern     — human-readable hint, e.g. "field X on type Y drifts frequently — monitor closely"
  promoted_by — always "drift_learning"

ScanPoint cadence changes are intentionally skipped (safer).
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Any

from infra_brain.agents.base import BaseAgent
from infra_brain.callbacks.dlp import redact_pans_preserving_uuids
from infra_brain.db.models import DriftEvent, Instinct, Resource, RootCauseNote, ZONE_CORPORATE
from infra_brain.db.session import get_session
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.provenance import MANUAL_PROVENANCE_SOURCE, is_manually_authored

log = logging.getLogger(__name__)

# Minimum number of occurrences of a (resource_id, drift_type, field) combo
# before we promote it to an Instinct.
_MIN_OCCURRENCES = 2

# Cap confidence at 1.0; each occurrence above _MIN_OCCURRENCES adds this much.
_CONFIDENCE_STEP = 0.1
_BASE_CONFIDENCE = 0.6

# AA-R-19: DriftEvent grows without bound (open events are never pruned by
# retention.py) and this scan previously loaded the ENTIRE table with no
# pagination/limit. Cap the scan to the most-recently-detected N rows —
# recency is exactly what _recency_weighted_confidence() cares about, so
# capping to the newest rows (rather than an arbitrary offset window) keeps
# the learning signal intact while bounding memory/query cost.
_MAX_EVENTS_SCANNED = 50_000

# The `pattern` string built below bakes in the current run's occurrence
# count, e.g. "...drifts frequently (37x, type=config_drift)...". Since
# DriftEvent rows accumulate without pruning and this scan re-tallies up to
# _MAX_EVENTS_SCANNED rows fresh every run, that count will almost always
# differ between consecutive weekly runs for any resource/field/type combo
# that keeps drifting — exactly the case this feature exists to highlight.
# Comparing the raw `pattern` text (as the dedup check below used to) makes
# the exists-check miss on every such run and insert a fresh near-duplicate
# Instinct instead of updating the prior one. This regex normalizes out
# just the volatile count digits so dedup matching is stable across runs.
_COUNT_TOKEN_RE = re.compile(r"\(\d+x, type=")


def _pattern_dedup_key(pattern: str) -> str:
    """Stable dedup key for an Instinct.pattern string.

    Identical to `pattern` except the volatile occurrence count is
    normalized to a fixed token, so two pattern strings that differ only in
    how many times the drift was observed this run still compare equal.
    """
    return _COUNT_TOKEN_RE.sub("(Nx, type=", pattern, count=1)


def _recency_weighted_confidence(frequency: int, days_since_last_seen: int) -> float:
    """Decay confidence for stale patterns.

    Confidence is asymptotic in frequency (never reaches 1.0) and exponentially
    decays with staleness.  A pattern last seen >90 days ago gets at most 30% of
    its frequency-based score.
    """
    base_confidence = min(0.99, frequency / (frequency + 5.0))  # asymptotic
    decay = math.exp(-days_since_last_seen / 30.0)
    return round(base_confidence * max(0.3, decay), 3)


class DriftLearningAgent(BaseAgent):
    """Analyses drift history and promotes patterns to Instinct rows."""

    spec = AgentSpec(
        domain="drift_learning",
        tier=Tier.REASONER,
        schedule="0 4 * * 0",
        max_staleness=timedelta(days=8),
        skip_hook=True,
    )

    # ------------------------------------------------------------------
    # collect()
    # ------------------------------------------------------------------

    def collect(self, scope: str = "all") -> list[dict[str, Any]]:
        """Aggregate DriftEvent history and write Instinct rows.

        Returns an empty list (BaseAgent expects list[dict] from collect).
        All meaningful work is the side-effect of writing Instinct rows.
        """
        try:
            with get_session() as session:
                events = (
                    session.query(DriftEvent)
                    .order_by(DriftEvent.detected_at.desc())
                    .limit(_MAX_EVENTS_SCANNED)
                    .all()
                )
                if not events:
                    return []

                # Tally (resource_id, drift_type, field) occurrences and track
                # the most-recent detected_at per key for staleness calculation.
                counter: Counter = Counter()
                last_seen_by_key: dict = {}
                event_ids_by_key: dict = {}
                for evt in events:
                    key = (evt.resource_id, evt.drift_type, evt.field)
                    counter[key] += 1
                    event_ids_by_key.setdefault(key, []).append(evt.id)
                    evt_dt = evt.detected_at
                    if evt_dt is not None:
                        if evt_dt.tzinfo is None:
                            evt_dt = evt_dt.replace(tzinfo=timezone.utc)
                        prev = last_seen_by_key.get(key)
                        if prev is None or evt_dt > prev:
                            last_seen_by_key[key] = evt_dt

                now_utc = datetime.now(timezone.utc)

                # Fetch the most-recent RootCauseNote per contributing
                # drift_event_id in ONE query (never N+1 — no per-key
                # round-trip). A RootCauseNote is at most 1:1 with a
                # DriftEvent (uq_rootcause_drift), so building an
                # event_id -> note map here is enough to later pick, per
                # aggregated key, the most recent note among its
                # contributing event ids.
                all_event_ids = [eid for ids in event_ids_by_key.values() for eid in ids]
                notes_by_event_id: dict = {}
                if all_event_ids:
                    for note in (
                        session.query(RootCauseNote)
                        .filter(RootCauseNote.drift_event_id.in_(all_event_ids))
                        .all()
                    ):
                        notes_by_event_id[note.drift_event_id] = note

                # M-4: batch the resource lookup in ONE query (was an N+1 --
                # one `session.query(Resource).filter_by(id=...).first()`
                # PER counter key, mirroring the RootCauseNote fix above).
                resource_ids = {rid for (rid, _dt, _f) in counter}
                resources_by_id: dict = {}
                if resource_ids:
                    for res in (
                        session.query(Resource).filter(Resource.id.in_(resource_ids)).all()
                    ):
                        resources_by_id[res.id] = res

                # M-4: cache the "existing Instinct rows for this domain"
                # lookup per domain, not per counter key -- the old code
                # re-ran `session.query(Instinct).filter_by(domain=...,
                # promoted_by="drift_learning").all()` (a full per-domain
                # scan) once for EVERY (resource, drift_type, field) key,
                # even when many keys share the same domain. Newly-created
                # instincts are appended into the cache below so within-run
                # dedup against a just-created row (same domain, later key)
                # still matches -- unchanged from before, where autoflush +
                # a fresh per-key query saw them naturally.
                existing_by_domain: dict[str, list] = {}

                for (resource_id, drift_type, field), count in counter.items():
                    if count < _MIN_OCCURRENCES:
                        continue

                    # Look up the resource to get its type, domain, and zone
                    resource = resources_by_id.get(resource_id)
                    resource_type = resource.type if resource else "unknown"
                    resource_domain = resource.domain if resource else "unknown"
                    resource_zone = (
                        (getattr(resource, "zone", None) or ZONE_CORPORATE)
                        if resource
                        else ZONE_CORPORATE
                    )

                    # Compute recency-weighted confidence
                    last_seen = last_seen_by_key.get((resource_id, drift_type, field))
                    days_since = (now_utc - last_seen).days if last_seen else 0
                    confidence = _recency_weighted_confidence(count, days_since)

                    pattern = (
                        f"field {field} on type {resource_type} drifts frequently "
                        f"({count}x, type={drift_type}) — monitor closely"
                    )

                    # Append the most recent RootCauseNote explanation among
                    # this key's contributing drift_event_ids, if any exist.
                    # Deterministic — no flag, additive text only.
                    best_note = None
                    best_note_dt = None
                    for eid in event_ids_by_key.get((resource_id, drift_type, field), []):
                        note = notes_by_event_id.get(eid)
                        if note is None:
                            continue
                        note_dt = note.created_at or datetime.min.replace(tzinfo=timezone.utc)
                        if note_dt.tzinfo is None:
                            note_dt = note_dt.replace(tzinfo=timezone.utc)
                        # Strict `>` (not `>=`): on a created_at tie, the
                        # first-encountered note for this key wins rather
                        # than the later one in iteration order.
                        if best_note is None or note_dt > best_note_dt:
                            best_note = note
                            best_note_dt = note_dt
                    # Provenance of the folded-in note is carried STRUCTURALLY,
                    # on the Instinct row itself, not by hoping the
                    # "[MANUAL/MCP-authored by X]" prose banner survives the
                    # [:200] truncation below. A long author string used to
                    # erode that margin with nothing enforcing the invariant;
                    # now the [:200] truncation is purely cosmetic and the
                    # authoritative marker is Instinct.citation, derived from
                    # the note's structural correlated["source"] marker.
                    citation = None
                    if best_note is not None:
                        origin = (
                            MANUAL_PROVENANCE_SOURCE
                            if is_manually_authored(best_note.correlated)
                            else "root_cause_agent"
                        )
                        citation = f"{origin}:root_cause_note:{best_note.id}"
                    if best_note is not None and best_note.explanation:
                        # PAN scrub at point of use: RootCauseNote.explanation
                        # can be LLM-authored free text (rootcause.py LLM
                        # mode) and must be redacted before it's folded into
                        # an Instinct.pattern string, same as every other
                        # place LLM-derived text is surfaced.
                        pattern += f" — likely cause: {redact_pans_preserving_uuids(best_note.explanation)[:200]}"

                    # Avoid duplicate instincts for the same domain+field+type combo.
                    # Matched on the count-normalized dedup key (see
                    # _pattern_dedup_key), NOT the raw pattern text — the raw
                    # text embeds the volatile occurrence count, which almost
                    # always differs between weekly runs for an actively
                    # drifting field and would otherwise defeat this check.
                    dedup_key = _pattern_dedup_key(pattern)
                    if resource_domain not in existing_by_domain:
                        existing_by_domain[resource_domain] = (
                            session.query(Instinct)
                            .filter_by(domain=resource_domain, promoted_by="drift_learning")
                            .all()
                        )
                    existing = next(
                        (
                            row
                            for row in existing_by_domain[resource_domain]
                            if _pattern_dedup_key(row.pattern) == dedup_key
                        ),
                        None,
                    )
                    if existing:
                        # Update confidence if it improved
                        existing.confidence = max(existing.confidence, confidence)
                        # Backfill provenance onto rows promoted before this
                        # marker existed; never overwrite an existing citation.
                        if citation and not existing.citation:
                            existing.citation = citation
                        continue

                    instinct = Instinct(
                        id=uuid.uuid4(),
                        zone=resource_zone,
                        domain=resource_domain,
                        pattern=pattern,
                        confidence=confidence,
                        promoted_by="drift_learning",
                        citation=citation,
                    )
                    session.add(instinct)
                    # Keep the per-domain cache consistent: a later key in
                    # this same domain/run must see this instinct too (the
                    # old per-key fresh query saw it via autoflush).
                    existing_by_domain[resource_domain].append(instinct)

                session.commit()
        except Exception:
            log.warning("DriftLearningAgent.collect() failed", exc_info=True)
            raise

        return []
