"""
Task F4 — inject prior learning into agent prompts.

build_context(agent, domain) reads back Instinct patterns (high-confidence),
Observation tool-usage patterns, and reusable GeneratedScript summaries for
the given agent/domain, and renders a compact preamble string.

All queries are best-effort: any failure returns "".
Total output is bounded to ~2000 chars to avoid prompt bloat.
"""

import logging

from infra_brain.db.models import (
    ZONE_CORPORATE,
    CollectionRun,
    GeneratedScript,
    Instinct,
    Observation,
)
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)

# Caps for each section
_MAX_INSTINCTS = 5
_MAX_OBSERVATIONS = 5
_MAX_SCRIPTS = 3
_MAX_TOTAL_CHARS = 2000
_MIN_CONFIDENCE = 0.7


def build_context(agent: str, domain: str, zone: str = ZONE_CORPORATE) -> str:
    """
    Return a compact "## What you've learned before" preamble for the agent.

    Reads:
    - Top high-confidence Instinct.pattern rows for the domain, filtered by zone
    - Top Observation.pattern rows for this agent+domain (by count desc)
    - Top reusable GeneratedScript summaries for this agent+domain

    Returns "" if nothing learned or on any error.
    """
    try:
        with get_session() as session:
            instincts = _read_instincts(session, domain, zone)
            observations = _read_observations(session, agent, domain)
            scripts = _read_scripts(session, agent, domain)
    except Exception:
        logger.debug("build_context: DB read failed (best-effort)", exc_info=True)
        return ""

    if not instincts and not observations and not scripts:
        return ""

    lines: list[str] = ["## What you've learned before"]

    if instincts:
        lines.append("### Instincts")
        for pattern in instincts:
            lines.append(f"- {pattern}")

    if observations:
        lines.append("### Tool patterns")
        for pattern in observations:
            lines.append(f"- {pattern}")

    if scripts:
        lines.append("### Reusable scripts")
        for name, purpose in scripts:
            lines.append(f"- {name}: {purpose}")

    result = "\n".join(lines)
    # Bound total length
    if len(result) > _MAX_TOTAL_CHARS:
        result = result[: _MAX_TOTAL_CHARS - 3] + "..."
    return result


def _read_instincts(session, domain: str, zone: str = ZONE_CORPORATE) -> list[str]:
    try:
        rows = (
            session.query(Instinct)
            .filter(
                Instinct.zone == zone,
                Instinct.domain == domain,
                Instinct.confidence >= _MIN_CONFIDENCE,
            )
            .order_by(Instinct.confidence.desc())
            .limit(_MAX_INSTINCTS)
            .all()
        )
        return [r.pattern for r in rows]
    except Exception:
        # Zone column may not exist pre-migration 0019 — fall back to unfiltered.
        logger.debug(
            "build_context: zone-filtered instincts query failed, retrying unfiltered",
            exc_info=True,
        )
        try:
            rows = (
                session.query(Instinct)
                .filter(
                    Instinct.domain == domain,
                    Instinct.confidence >= _MIN_CONFIDENCE,
                )
                .order_by(Instinct.confidence.desc())
                .limit(_MAX_INSTINCTS)
                .all()
            )
            return [r.pattern for r in rows]
        except Exception:
            logger.debug("build_context: instincts query failed", exc_info=True)
            return []


def _read_observations(session, agent: str, domain: str) -> list[str]:
    try:
        rows = (
            session.query(Observation)
            .filter(
                Observation.agent == agent,
                Observation.domain == domain,
            )
            .order_by(Observation.count.desc())
            .limit(_MAX_OBSERVATIONS)
            .all()
        )
        return [r.pattern for r in rows]
    except Exception:
        logger.debug("build_context: observations query failed", exc_info=True)
        return []


# ── Feedback loop: outcomes → confidence (Phase 7) ───────────────────────────

_RUN_LOOKBACK = 10
_REINFORCE = 0.02
_DECAY = 0.05
_MIN_SCRIPT_RUNS = 3


def record_domain_outcomes() -> int:
    """Nudge Instinct.confidence per domain based on recent collection outcomes.

    Domains whose recent runs mostly succeed get a small reinforcement; those
    mostly failing decay. Confidence stays clamped to [0, 1]. Returns the number
    of instincts adjusted. Best-effort: any failure returns 0.
    """
    changed = 0
    try:
        with get_session() as session:
            domains = {d for (d,) in session.query(Instinct.domain).distinct()}
            for domain in domains:
                runs = (
                    session.query(CollectionRun)
                    .filter(CollectionRun.domain == domain)
                    .order_by(CollectionRun.started_at.desc())
                    .limit(_RUN_LOOKBACK)
                    .all()
                )
                if not runs:
                    continue
                ratio = sum(1 for r in runs if r.status == "completed") / len(runs)
                delta = _REINFORCE if ratio >= 0.8 else (-_DECAY if ratio < 0.5 else 0.0)
                if delta == 0.0:
                    continue
                for inst in session.query(Instinct).filter(Instinct.domain == domain).all():
                    new = max(0.0, min(1.0, inst.confidence + delta))
                    if new != inst.confidence:
                        inst.confidence = new
                        changed += 1
            session.commit()
    except Exception:
        logger.debug("record_domain_outcomes failed (best-effort)", exc_info=True)
    return changed


def prune_unreliable_scripts() -> int:
    """Mark repeatedly-failing generated scripts as non-reusable so build_context
    stops surfacing them. Returns the number pruned. Best-effort."""
    pruned = 0
    try:
        with get_session() as session:
            scripts = (
                session.query(GeneratedScript)
                .filter(
                    GeneratedScript.reusable.is_(True),
                    GeneratedScript.run_count >= _MIN_SCRIPT_RUNS,
                )
                .all()
            )
            for sc in scripts:
                if sc.last_returncode not in (0, None):
                    sc.reusable = False
                    pruned += 1
            session.commit()
    except Exception:
        logger.debug("prune_unreliable_scripts failed (best-effort)", exc_info=True)
    return pruned


def _read_scripts(session, agent: str, domain: str) -> list[tuple[str, str]]:
    try:
        rows = (
            session.query(GeneratedScript)
            .filter(
                GeneratedScript.created_by_agent == agent,
                GeneratedScript.domain == domain,
                GeneratedScript.reusable.is_(True),
            )
            .order_by(GeneratedScript.run_count.desc())
            .limit(_MAX_SCRIPTS)
            .all()
        )
        return [(r.name, r.purpose[:120]) for r in rows]
    except Exception:
        logger.debug("build_context: scripts query failed", exc_info=True)
        return []
