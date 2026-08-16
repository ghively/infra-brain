"""
CoverageAgent — detects uncovered infrastructure domains and proposes collection strategies.

discover() compares the set of known agent domains (AGENT_REGISTRY) against active
scan_points to find gaps, uses the LLM to generate a collection strategy per gap,
records an IntegrationProposal, and returns the gap list.

wire(proposal_id) activates an approved proposal by creating a ScanPoint and seeding
Instinct rows from the LLM-generated strategy.  Because IntegrationProposal has no
description column, the strategy is re-generated from the LLM at wire-time using the
same prompt (strategies are deterministic enough for this to be safe).

This agent is standalone — no infra-ops dependency, no skill imports.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone, timedelta

from infra_brain.agents.llm_base import LLMAgent
from infra_brain.config import get_settings
from infra_brain.db.models import Instinct, IntegrationProposal, ScanPoint
from infra_brain.db.session import get_session
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)

# TRK-047: the former hand-maintained ``DEFAULT_SCHEDULES`` dict (a shadow
# cadence table that had already drifted from the agents' real cron
# schedules) is gone — the expected cadence for a wired domain is now derived
# from ``spec.schedule`` via ``etl.spec.schedule_by_domain()``. Domains with
# no registered agent (the whole point of a coverage gap) fall back to the
# generic 4-hourly default below.
_FALLBACK_SCHEDULE = "0 */4 * * *"

_COVERAGE_PROMPT_TEMPLATE = (
    "You are analyzing an infrastructure monitoring gap. "
    "Domain '{domain}' is available for collection but has no scan point configured. "
    "Based on what you know about this type of infrastructure source, generate a collection "
    "strategy covering: "
    "(1) key resource types and fields to collect, "
    "(2) recommended collection frequency, "
    "(3) drift patterns to watch for, "
    "(4) how this domain relates to other already-collected domains: {known_domains}. "
    "Return a JSON object with keys: resource_types, frequency_hint, drift_patterns, "
    "related_domains, notes."
)


def _parse_strategy(strategy_text: str, domain: str) -> dict:
    """Parse the LLM's JSON strategy response; fall back to raw-text notes."""
    try:
        # Accept ```json...``` fence or bare JSON.
        raw = strategy_text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            raw = raw.rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "[coverage] LLM did not return valid JSON for domain=%s; storing raw text as notes.",
            domain,
        )
        return {"notes": strategy_text}


def _instinct_rows_from_strategy(domain: str, strategy: dict, proposal_id) -> list[tuple[str, str]]:
    """One Instinct (pattern, citation) row per key insight in *strategy*."""
    rows: list[tuple[str, str]] = []
    if strategy.get("resource_types"):
        rt = strategy["resource_types"]
        rt_str = ", ".join(rt) if isinstance(rt, list) else str(rt)
        rows.append(
            (
                f"Collect {domain} resource types: {rt_str}",
                f"coverage-strategy:resource_types:{proposal_id}",
            )
        )
    if strategy.get("frequency_hint"):
        rows.append(
            (
                f"Recommended collection frequency for {domain}: {strategy['frequency_hint']}",
                f"coverage-strategy:frequency_hint:{proposal_id}",
            )
        )
    if strategy.get("drift_patterns"):
        dp = strategy["drift_patterns"]
        dp_str = ", ".join(dp) if isinstance(dp, list) else str(dp)
        rows.append(
            (
                f"Watch for drift in {domain}: {dp_str}",
                f"coverage-strategy:drift_patterns:{proposal_id}",
            )
        )
    if strategy.get("related_domains"):
        rd = strategy["related_domains"]
        rd_str = ", ".join(rd) if isinstance(rd, list) else str(rd)
        rows.append(
            (
                f"{domain} relates to domains: {rd_str}",
                f"coverage-strategy:related_domains:{proposal_id}",
            )
        )
    if strategy.get("notes") and not rows:
        # Fallback when strategy is raw text only.
        notes = str(strategy["notes"])[:500]
        rows.append(
            (
                f"Coverage strategy for {domain}: {notes}",
                f"coverage-strategy:notes:{proposal_id}",
            )
        )
    return rows


class CoverageAgent(LLMAgent):
    spec = AgentSpec(
        domain="coverage",
        tier=Tier.ON_DEMAND,
        # Staggered off discovery's 03:00 Sun slot (both were "0 3" — a dev-status
        # collision). Minute 40 avoids the */15 (netdiscovery) and */30
        # (host_reconcile) high-frequency collectors, and hour 3 has no */2 or
        # */6 job, so this slot is genuinely clear.
        schedule="40 3 * * 1",
        max_staleness=timedelta(days=8),
    )
    llm_role = "coverage"  # maps to bedrock_model_coverage in llm.py _model_for_role

    def collect(self, scope: str = "all") -> list[dict]:
        """Run discover() and return findings as resource dicts."""
        gaps = self.discover()
        return [
            {
                "name": g["domain"],
                "type": "coverage-gap",
                "data": g,
            }
            for g in gaps
        ]

    def discover(self) -> list[dict]:
        """Identify uncovered domains, generate LLM strategies, persist proposals.

        Returns a list of gap dicts, one per newly-created proposal:
          {domain, proposal_id, strategy}
        """
        # Lazy import to avoid circular dependency (supervisor imports CoverageAgent
        # via AGENT_REGISTRY after this module is defined).
        from infra_brain.etl.spec import retired_domains  # noqa: PLC0415
        from infra_brain.supervisor import AGENT_REGISTRY  # noqa: PLC0415

        # Retired domains (AgentSpec.retired) are excluded from the gap set.
        # They are registered — deliberately, so an operator can see they are
        # off — but an uncovered RETIRED domain is not a gap, it is the
        # decision working. Counting them would spend one LLM reason() call
        # each, every run, drafting an integration proposal to go build a
        # collector for a system that does not exist here, and would file the
        # result as a coverage finding for someone to triage.
        known_domains = set(AGENT_REGISTRY.keys()) - retired_domains()

        with get_session() as session:
            covered = {
                sp.domain for sp in session.query(ScanPoint).filter_by(status="active").all()
            }

        gaps = known_domains - covered
        if not gaps:
            logger.info("[coverage] No uncovered domains found.")
            return []

        known_str = ", ".join(sorted(covered)) if covered else "(none yet)"
        results: list[dict] = []
        # TRK-109: bound the sequential LLM fan-out — one reason() call per gap
        # against a local model blew straight through the 600s collect guard on
        # a fresh DB (~24 gaps), so coverage never completed a run. Proposals
        # persist per domain, so capped runs still drain the backlog. Two
        # bounds, whichever hits first:
        #   - count cap (coverage_gap_llm_cap) — the ceiling for fast models;
        #   - wall-clock budget (60% of collect_timeout_seconds) — with a slow
        #     local model (Ollama) even a handful of calls can exceed the
        #     collect guard, so stop drafting while there is still time to
        #     finish the run cleanly.
        # Each reason() call is itself bounded by the LLM client's per-call
        # request timeout (llm_request_timeout_seconds, TRK-109 residual), and a
        # call that errors or times out is caught in the loop below and its
        # domain deferred — so one hung/slow gap can no longer abort the whole
        # coverage run (the TRK-106 "collect() timed out after 600s" follow-up).
        settings = get_settings()
        llm_cap = settings.coverage_gap_llm_cap
        time_budget = settings.collect_timeout_seconds * 0.6
        started = time.monotonic()
        deferred = 0
        errored = 0

        # R-15/TRK-068: no DB session may be held open while self.reason() runs
        # an LLM tool-calling loop — the loop can take many seconds/round-trips
        # and a session held open for that long ties up a pool connection (and,
        # under PostgreSQL, a transaction) for the LLM call's entire duration.
        # Read what we need (which gaps already have a pending proposal), close
        # the session, do all the reasoning outside any session, then open a
        # fresh short-lived session per domain purely to persist the result.
        for domain in sorted(gaps):
            with get_session() as session:
                # Skip if a pending proposal already exists for this gap.
                existing = (
                    session.query(IntegrationProposal)
                    .filter_by(source=f"coverage-gap:{domain}", status="pending")
                    .first()
                )
            if existing:
                logger.debug("[coverage] Proposal already pending for domain=%s", domain)
                continue

            if len(results) >= llm_cap or (time.monotonic() - started) > time_budget:
                deferred += 1
                continue

            # Ask the LLM for a collection strategy — deliberately OUTSIDE any
            # session (see comment above). Resilience boundary (TRK-106
            # follow-up): a single hung/slow/erroring call (the per-call request
            # timeout firing, or a provider connection error) must not abort the
            # whole run — catch it, defer this domain to the next run, and keep
            # draining the backlog.
            prompt = _COVERAGE_PROMPT_TEMPLATE.format(domain=domain, known_domains=known_str)
            try:
                # T6 (rev14): the time_budget above was checked only BEFORE each
                # call, and the call itself had no wall-clock bound — a tool-less
                # reason() is one model turn, but REASON_MAX_ATTEMPTS(3) retries
                # x llm_request_timeout_seconds(120) = 360s worst case against a
                # 180s (0.6 x 300s) budget. One slow gap could therefore blow the
                # budget it was supposed to respect and get the whole run killed
                # by the external collect guard. Hand each call whatever remains,
                # so it self-terminates inside the budget and this loop's own
                # except clause defers that domain to the next run.
                strategy_text = self.reason(
                    prompt,
                    tools=[],
                    deadline_seconds=max(0.0, time_budget - (time.monotonic() - started)),
                )
            except Exception as exc:  # noqa: BLE001 — deliberate resilience boundary
                errored += 1
                logger.warning(
                    "[coverage] LLM strategy call failed for domain=%s (%s: %s); "
                    "deferring to next run.",
                    domain,
                    type(exc).__name__,
                    exc,
                )
                continue
            strategy = _parse_strategy(strategy_text, domain)

            proposal_id = uuid.uuid4()
            with get_session() as session:
                session.add(
                    IntegrationProposal(
                        id=proposal_id,
                        source=f"coverage-gap:{domain}",
                        type="domain-agent",
                        endpoint="all",
                        confidence=0.85,
                        proposed_at=datetime.now(timezone.utc),
                        status="pending",
                    )
                )
                session.commit()

            results.append(
                {
                    "domain": domain,
                    "proposal_id": str(proposal_id),
                    "strategy": strategy,
                }
            )

        if deferred or errored:
            logger.warning(
                "[coverage] LLM bound hit (cap=%d, time budget=%.0fs, elapsed=%.0fs): "
                "%d gap(s) deferred, %d gap(s) errored — all retried next run.",
                llm_cap,
                time_budget,
                time.monotonic() - started,
                deferred,
                errored,
            )
        logger.info("[coverage] Created %d new coverage proposals.", len(results))
        return results

    def wire(self, proposal_id: str) -> bool:
        """Activate an approved IntegrationProposal.

        Creates a ScanPoint for the domain and seeds Instinct rows from the
        LLM-generated strategy stored in the proposal description.

        Returns True on success, False if the proposal is not found/approved.
        """
        pid = uuid.UUID(proposal_id) if isinstance(proposal_id, str) else proposal_id

        # R-15/TRK-068: read everything the LLM call needs, close the session,
        # THEN call self.reason() — no session may be held open across an LLM
        # round-trip. Only after reason() returns do we open a fresh session
        # to persist the ScanPoint/Instinct rows and flip proposal.status.
        with get_session() as session:
            proposal = session.get(IntegrationProposal, pid)
            if not proposal:
                logger.error("[coverage] Proposal %s not found.", proposal_id)
                return False
            if proposal.status != "approved":
                logger.error(
                    "[coverage] Proposal %s has status=%s (expected 'approved').",
                    proposal_id,
                    proposal.status,
                )
                return False

            domain = proposal.source.split(":")[-1] if ":" in proposal.source else proposal.type
            endpoint = proposal.endpoint
            approved_by = getattr(proposal, "approved_by", None)

            existing_sp = (
                session.query(ScanPoint).filter_by(domain=domain, endpoint=endpoint).first()
                is not None
            )
            covered = {
                sp.domain for sp in session.query(ScanPoint).filter_by(status="active").all()
            }

        from infra_brain.etl.spec import schedule_by_domain  # noqa: PLC0415

        schedule = schedule_by_domain().get(domain, _FALLBACK_SCHEDULE)
        known_str = ", ".join(sorted(covered)) if covered else "(none yet)"

        # Re-generate the collection strategy via the LLM — deliberately
        # OUTSIDE any session (see comment above). IntegrationProposal has no
        # description column, so we regenerate at wire-time.
        prompt = _COVERAGE_PROMPT_TEMPLATE.format(domain=domain, known_domains=known_str)
        strategy_text = self.reason(prompt, tools=[])
        strategy = _parse_strategy(strategy_text, domain)

        zone = get_settings().default_zone
        now = datetime.now(timezone.utc)
        promoted_by = approved_by or "CoverageAgent"
        instinct_rows = _instinct_rows_from_strategy(domain, strategy, proposal_id)

        with get_session() as session:
            if not existing_sp:
                session.add(
                    ScanPoint(
                        id=uuid.uuid4(),
                        domain=domain,
                        method="agent",
                        endpoint=endpoint,
                        schedule=schedule,
                        status="active",
                    )
                )

            for pattern, citation in instinct_rows:
                session.add(
                    Instinct(
                        id=uuid.uuid4(),
                        zone=zone,
                        domain=domain,
                        pattern=pattern,
                        confidence=0.75,
                        promoted_by=promoted_by,
                        promoted_at=now,
                        citation=citation,
                    )
                )

            # FE-16: the dashboard's Integration Proposals page has a "Wired"
            # tab/stat that filters on status == "wired" (see
            # dashboard/static/index.html) — this used to set "active" here,
            # a status the frontend never checks, so a successfully-wired
            # proposal always fell out of every filter and the "Wired" stat
            # was permanently 0.
            proposal = session.get(IntegrationProposal, pid)
            proposal.status = "wired"
            session.commit()

        logger.info(
            "[coverage] Wired proposal %s for domain=%s; seeded %d instinct(s).",
            proposal_id,
            domain,
            len(instinct_rows),
        )
        return True
