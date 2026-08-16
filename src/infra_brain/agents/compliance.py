"""ComplianceAgent — policy-as-code over collected inventory (PCI-oriented).

Deterministic rules flag violations a regulated environment cares about:
EOL assets past end-of-life, vulnerabilities past their remediation SLA, and
drift left unresolved too long. Writes ``ComplianceViolation`` rows (idempotent
per rule+host) and resolves ones that no longer apply. DB-only, read-only on
infra.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import Uuid, bindparam, func, text

from infra_brain.agents.base import BaseAgent, CollectOutcome
from infra_brain.agents.compliance_rules import DEFAULT_RULES, evaluate_rules
from infra_brain.agents.llm_base import invoke_structured_with_retry
from infra_brain.db.models import ComplianceViolation, ProposedAction, Resource
from infra_brain.db.session import get_session
from infra_brain.etl.base import ReconcileScope
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)

# Phase 3 Task 3: the 4 rule names ComplianceAgent currently evaluates. Fed to
# the gap-finder LLM prompt as "rule inventory" context so it proposes NEW
# gaps rather than re-describing existing coverage. Never mutated by the
# gap-finder — these 4 rules stay pure rules-as-code.
_KNOWN_RULE_NAMES = (
    "eol_overdue",
    "vuln_sla_breach",
    "stale_drift",
    "unaddressed_critical_cve",
)

# Idempotency (review finding): a rejected gap proposal must never be
# re-proposed on the next run — the (action_type, target, status) uq
# constraint gives IntegrityError, not idempotency, so we do our own
# exists-check over every terminal/non-terminal status before inserting.
_GAP_PROPOSAL_LIVE_STATUSES = ("pending", "approved", "executed", "rejected")

# GitLab #137: Resource domains that hold internal bookkeeping/self-telemetry
# nodes, never real fleet infrastructure. Rule 3 (stale_drift) must never fire
# on them — a stale-drift violation raised against a compliance violation-shadow
# node feeds back through graph_maintenance's HAS_VIOLATION shadow minting and
# produces runaway "stale_drift:stale_drift:*" rows. Mirrors TRK-191's
# graph_maintenance exclusion in get_drift_events.
#
# GitLab #118: this exclusion now lives declaratively as the stale_drift rule's
# ``exclude_resource_domains`` in DEFAULT_RULES / compliance.yml, not as a
# Python-level constant — kept here as a doc anchor only, no longer read.


# TRK-079: closed vocabulary for the gap-finder's ``rule_domain`` field.
#
# Before this fix, ``rule_domain`` was a free-text slug the LLM invented on
# every call. Idempotency (``_stable_gap_hash``) hashes ``rule_domain`` +
# ``condition_type``, so a differently-worded run of the SAME conceptual gap
# (e.g. "backup_retention" vs "backup_verification") minted a different hash
# and re-proposed a duplicate "new" gap every run. Pydantic's
# ``with_structured_output`` machinery enforces ``Literal`` membership at the
# schema boundary (LangChain compiles it into the tool/function-call JSON
# schema's ``enum``), so the model is structurally unable to author a
# differently-worded domain slug — it must pick one of the values below.
#
# Grounded, not invented (per review): every slug maps to a domain this
# system actually collects data for (see ``domain="..."`` across
# ``src/infra_brain/agents/*.py``) or to one of ComplianceAgent's own 4
# deterministic rule concepts (``_KNOWN_RULE_NAMES`` above) — so a gap
# proposed under any of these slugs is, in principle, checkable against real
# collected data, not a hypothetical category nothing in the system produces.
#   eol_lifecycle          -> EolRegistry / domain="eol"        (existing: eol_overdue)
#   vulnerability_management -> VulnQueueItem / domain="vuln"   (existing: vuln_sla_breach)
#   critical_cve_response   -> R7Asset / domain="vuln_triage"   (existing: unaddressed_critical_cve)
#   drift_change_control    -> DriftEvent / domain="drift"      (existing: stale_drift)
#   backup_retention        -> domain="backup" (BackupAgent)
#   certificate_pki         -> domain="pki" (cert/key lifecycle)
#   secrets_management      -> domain="secrets_inventory"
#   identity_access         -> domain="identity" (local admins, privileged accounts)
#   network_segmentation    -> domain="net" / domain="loadbalancer"
#   patch_cadence           -> domain="linux" / domain="windows" pending-update lag
#   container_security      -> domain="container_registry"
#   cloud_posture           -> domain="cloud"
#   k8s_policy              -> domain="k8s"
#   logging_monitoring      -> domain="prometheus" / "grafana" / "alertmanager" / "uptime_kuma"
#   license_compliance      -> domain="licensing"
#   saas_vendor_risk        -> domain="saas_inventory"
#   shadow_asset_discovery  -> domain="discovery" / "netdiscovery" (asset-inventory completeness)
#   other                   -> escape hatch, see _write_gap_proposal's hashing note below
_RULE_DOMAIN_VOCAB: tuple[str, ...] = (
    "eol_lifecycle",
    "vulnerability_management",
    "critical_cve_response",
    "drift_change_control",
    "backup_retention",
    "certificate_pki",
    "secrets_management",
    "identity_access",
    "network_segmentation",
    "patch_cadence",
    "container_security",
    "cloud_posture",
    "k8s_policy",
    "logging_monitoring",
    "license_compliance",
    "saas_vendor_risk",
    "shadow_asset_discovery",
    "other",
)

# Single source of truth: the Literal used for schema validation is derived
# from _RULE_DOMAIN_VOCAB (PEP 646 star-unpacking, Python >=3.11 per
# pyproject.toml's requires-python) rather than duplicated as a second
# hand-maintained list that could drift out of sync with it.
_RuleDomain = Literal[*_RULE_DOMAIN_VOCAB]


class _ProposedRuleGap(BaseModel):
    """One candidate compliance-rule gap suggested by the LLM."""

    rule_domain: _RuleDomain = Field(
        description="The compliance-rule category this gap belongs to — MUST be one "
        "of the fixed vocabulary below, chosen by best fit. Use 'other' only when "
        "no listed category is a reasonable fit for the gap being proposed: "
        + ", ".join(_RULE_DOMAIN_VOCAB)
    )
    condition_type: str = Field(
        description="Short machine-friendly condition type the rule would check "
        "WITHIN rule_domain, e.g. 'missing_backup_verification'. Free text, but "
        "keep it a short snake_case-style slug, not a sentence."
    )
    description: str = Field(
        description="Human-readable explanation of the gap and why it matters "
        "for PCI/compliance posture."
    )


class _GapFinderOutput(BaseModel):
    gaps: list[_ProposedRuleGap] = Field(default_factory=list)


def _normalize_gap_slug(value: str) -> str:
    """Canonicalize a gap-hash input slug (TRK-079 layer 2): lowercase, strip,
    and collapse ANY run of whitespace/hyphens/underscores to a single '_' so
    near-miss wording (``"Backup Retention"``, ``"backup-retention"``,
    ``"backup__retention"``) converges on one hash instead of minting a new
    target per stylistic variant. Applied to both ``rule_domain`` (now a
    closed ``Literal`` so this mostly guards against case drift) and
    ``condition_type`` (still free text, where separator/case drift is the
    live risk this fixes).
    """
    collapsed = re.sub(r"[\s\-_]+", "_", value.strip().lower())
    return collapsed.strip("_")


def _stable_gap_hash(rule_domain: str, condition_type: str) -> str:
    """Hash over CANONICAL rule-semantics fields only — never over LLM prose
    (an explanation/description that varies run-to-run would mint a new
    target every time and defeat idempotency).

    Field order is preserved (NOT sorted) — rule_domain and condition_type
    are semantically distinct roles, so a gap proposed as
    (rule_domain='backup_retention', condition_type='missing_verification')
    must hash differently from one with the values swapped. Sorting the pair
    before hashing would collide those two distinct gaps onto the same
    target, defeating the idempotency de-dup this hash exists to provide.

    TRK-079 layer 2: both inputs are canonicalized via ``_normalize_gap_slug``
    (was: bare ``.strip().lower()``) before hashing, so case/hyphen/whitespace
    variants of the same slug converge. This is a hash-computation change from
    the pre-fix version for inputs containing hyphens/internal whitespace —
    accepted deliberately rather than dual-hash-compared against history:
    ``compliance_gap_finder_enabled`` defaults OFF and has never been enabled
    against a real model in production (see docs/TRACKER-ARCHIVE.md TRK-078's
    "Policy hold" note — the real-model smoke run is still gated behind
    pending Bedrock access), so no live ``rule-gap:*`` ProposedAction rows
    exist to orphan. The manual-authoring path (``mcp_server.record_compliance_gap``)
    shares this function directly on free text too, and its existing tests use
    already-clean slugs (``"backup_retention"``, ``"d"``/``"c"``) that
    normalize identically before and after this change.

    TRK-079 layer 3 (the 'other' escape hatch): ``rule_domain`` is now a
    closed ``Literal`` (see ``_RuleDomain``), so every gap the model can't fit
    into a real category collapses onto the literal string "other". If the
    hash only depended on rule_domain, every "other" gap would collide onto
    one target. It does NOT collide: ``condition_type`` stays free text and is
    ALWAYS part of this hash regardless of rule_domain, so two distinct
    "other" gaps with distinct condition_type values (which is the model's
    only way to describe what the gap actually is once "other" is chosen)
    still hash differently. See
    ``test_other_category_gaps_with_different_condition_types_hash_differently``.
    """
    canonical = [_normalize_gap_slug(rule_domain), _normalize_gap_slug(condition_type)]
    digest_input = json.dumps(canonical).encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()[:16]


# Atomic upsert for compliance violations — prevents UniqueViolation when two
# concurrent runs (e.g. scheduler overlap after restart) both try to INSERT the
# same (rule, host, 'open') tuple. ON CONFLICT updates the mutable fields and
# preserves resource_id if already set (COALESCE-style CASE guard).
#
# Compatible with both PostgreSQL (production) and SQLite 3.24+ (test in-memory
# engine). The status literal 'open' is the conflict discriminator — only
# open violations are managed here; resolved rows stay untouched.
_COMPLIANCE_UPSERT = text(
    """
    INSERT INTO compliance_violations
        (id, rule, host, severity, detail, status, resource_id, detected_at)
    VALUES
        (:id, :rule, :host, :severity, :detail, 'open', :resource_id, :detected_at)
    ON CONFLICT (rule, host, status)
    DO UPDATE SET
        severity    = excluded.severity,
        detail      = excluded.detail,
        resource_id = CASE
                        WHEN compliance_violations.resource_id IS NULL
                        THEN excluded.resource_id
                        ELSE compliance_violations.resource_id
                      END
    """
).bindparams(
    # Typed bindparams let SQLAlchemy convert uuid.UUID objects to the correct
    # format for each database backend (CHAR(32) hex in SQLite, native UUID in
    # PostgreSQL) — matching the ORM's own storage format so the identity map
    # stays consistent between raw-SQL inserts and subsequent ORM queries.
    bindparam("id", type_=Uuid(as_uuid=True)),
    bindparam("resource_id", type_=Uuid(as_uuid=True)),
)


def reconcile_compliance_resource_links(session) -> int:
    """Link unlinked ``ComplianceViolation`` rows to their matching ``Resource``
    by exact name match on ``host`` (Task 4.5 backfill/reconciliation).

    Idempotent: only touches rows with ``resource_id IS NULL``. A ``host``
    string that matches exactly one ``Resource.name`` gets linked; a ``host``
    matching zero or MORE THAN ONE resource name is left ``NULL`` (ambiguous
    matches are never guessed). Does not commit — caller controls the
    transaction boundary (mirrors ``_reconcile``'s existing pattern of a single
    commit at the end of the compliance pass).

    Returns the number of violations newly linked.
    """
    unlinked = (
        session.query(ComplianceViolation)
        .filter(ComplianceViolation.resource_id.is_(None))
        .filter(ComplianceViolation.host != "")
        .all()
    )
    if not unlinked:
        return 0

    hosts = {cv.host for cv in unlinked}
    matches = session.query(Resource.id, Resource.name).filter(Resource.name.in_(hosts)).all()
    counts: dict[str, int] = {}
    unique_id: dict[str, uuid.UUID] = {}
    for res_id, name in matches:
        counts[name] = counts.get(name, 0) + 1
        unique_id[name] = res_id

    linked = 0
    for cv in unlinked:
        if counts.get(cv.host) == 1:
            cv.resource_id = unique_id[cv.host]
            linked += 1
    return linked


class ComplianceAgent(BaseAgent):
    spec = AgentSpec(
        domain="compliance",
        tier=Tier.REASONER,
        schedule="30 6 * * *",
        max_staleness=timedelta(hours=26),
        skip_hook=True,
    )

    def __init__(self):
        super().__init__()
        self.thresholds = self._load_thresholds()

    def _load_thresholds(self) -> dict:
        yml_path = Path(__file__).parents[3] / "rules" / "enforcement" / "compliance.yml"
        try:
            with open(yml_path) as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning("compliance.yml not found — using defaults")
            return {"vuln_sla_days": 30, "stale_drift_days": 14}

    def collect(self, scope: str = "all") -> list[dict]:
        now = datetime.now(UTC)
        # (rule, host, severity, detail, resource_id) — resource_id is the
        # concrete FK the rule already holds in scope when it unambiguously
        # identifies the violation's subject resource; None when the rule has
        # no such concrete link (falls back to reconcile_compliance_resource_links's
        # name-match, which may correctly stay NULL for ambiguous multi-domain
        # host names — see review finding I-1).
        #
        # GitLab #118: rule *logic* itself is now data, evaluated by the
        # policy-as-code engine in ``agents/compliance_rules.py`` from
        # ``rules/enforcement/compliance.yml``'s ``rules:`` list. Falls back
        # to DEFAULT_RULES (the original 4 hardcoded blocks, reproduced
        # declaratively) when a config on disk predates the ``rules:`` key,
        # so upgrading this module never changes behavior for an unmodified
        # config.
        rules_cfg = self.thresholds.get("rules") or DEFAULT_RULES
        found: list[tuple[str, str, str, str, uuid.UUID | None]] = []
        # H-2: every rule is recorded as observed (evaluated cleanly) or
        # failed (raised) — _reconcile below scopes its destructive
        # resolution pass to rule_scope.safe_scope, so a rule whose evaluation
        # raised (e.g. a schema change it no longer matches) never gets its
        # existing open violations mass-resolved on the strength of an empty
        # `found` that only means "this rule didn't run this cycle".
        rule_scope = ReconcileScope(label="compliance rule")
        with get_session() as session:
            found = evaluate_rules(session, rules_cfg, self.thresholds, now, scope=rule_scope)

            self._reconcile(session, found, now, scope=rule_scope)

            # Phase 3 Task 3 / TRK-068: LLM-assisted gap-finder, strictly
            # opt-in and propose-only. Runs AFTER the 4 deterministic rules
            # have evaluated and reconciled — never affects them, and any
            # failure here (including the flag being off) is swallowed so
            # the deterministic compliance pass is unaffected.
            #
            # Only phase 1 (gather plain summary values) happens inside this
            # session block. The LLM call and the write phase happen after
            # the session closes (see below) — a DB session must never be
            # held open across an LLM round-trip (R-15/TRK-068).
            gap_finder_rows = None
            if self.settings is not None and getattr(
                self.settings, "compliance_gap_finder_enabled", False
            ):
                gap_finder_rows = self._gather_gap_finder_summary(session)
        logger.info("ComplianceAgent evaluated rules; %d violation(s) currently open", len(found))

        # Phase 2 + 3 (TRK-068): LLM call and write-back happen strictly
        # after the session above has closed. When the flag is off,
        # gap_finder_rows stays None and this is a no-op — byte-identical to
        # the flag-off path before this fix.
        if gap_finder_rows is not None:
            self._propose_compliance_gaps(gap_finder_rows, now)
        # F-008: report the real evaluated-violation count (0 is a legitimate
        # clean-fleet result and stays status="completed"). H-2: rule_scope's
        # errors (any rule that raised this cycle) feed CollectOutcome.errors
        # so the existing R3 status mapping downgrades the run to "partial"
        # (or "failed" if nothing else was found) instead of a clean
        # "completed" that hides the swallowed rule failure.
        return CollectOutcome(items=[], errors=rule_scope.errors, count_override=len(found))

    @staticmethod
    def _reconcile(
        session,
        found: list[tuple[str, str, str, str, uuid.UUID | None]],
        now: datetime,
        scope: ReconcileScope | None = None,
    ) -> None:
        """Upsert current violations (idempotent) and resolve ones that cleared.

        Uses a raw SQL ``ON CONFLICT DO UPDATE`` upsert instead of ORM
        query-then-insert to prevent ``UniqueViolation`` when two concurrent
        scheduler runs (e.g. after a container restart) both attempt to INSERT
        the same ``(rule, host, 'open')`` tuple before either commits.

        H-2: when ``scope`` is given, the resolution pass below is scoped to
        ``scope.safe_scope`` (rule *names* that evaluated cleanly this cycle)
        — an existing open violation whose rule raised (or was never declared
        this cycle) is left untouched rather than resolved on the strength of
        an empty ``found`` for that rule. ``scope=None`` preserves the
        pre-fix behavior (resolve anything not currently found) for any
        direct caller that doesn't construct a scope.
        """
        current_keys = {(rule, host) for rule, host, _sev, _detail, _rid in found}

        # Load existing open violations before executing upserts so we have ORM
        # objects to flip to "resolved" in the resolution step below.
        existing = {
            (cv.rule, cv.host): cv
            for cv in session.query(ComplianceViolation)
            .filter(ComplianceViolation.status == "open")
            .all()
        }

        # Upsert every found violation atomically.
        # ON CONFLICT preserves resource_id when already set (CASE guard in SQL).
        for rule, host, severity, detail, resource_id in found:
            session.execute(
                _COMPLIANCE_UPSERT,
                {
                    "id": uuid.uuid4(),
                    "rule": rule,
                    "host": host,
                    "severity": severity,
                    "detail": detail,
                    "resource_id": resource_id,  # uuid.UUID or None — typed bindparam handles format
                    "detected_at": now,
                },
            )

        # Resolve open violations that no longer apply. A prior
        # resolve→reopen→resolve cycle can leave a stale 'resolved' tombstone
        # for the same (rule, host); uq_compliance_rule_host_status permits only
        # one row per (rule, host, status), so flipping the current open row to
        # 'resolved' would collide with that stale tombstone (UniqueViolation,
        # crashing the whole pass). Drop the stale tombstone first (latest
        # resolved row wins — history is a single tombstone by design), then
        # flip. All deletes run BEFORE any status mutation so autoflush can't
        # push a colliding flip mid-loop.
        cleared = [
            (key, cv)
            for key, cv in existing.items()
            if key not in current_keys and (scope is None or key[0] in scope.safe_scope)
        ]
        for (rule, host), _cv in cleared:
            session.query(ComplianceViolation).filter_by(
                rule=rule, host=host, status="resolved"
            ).delete(synchronize_session=False)
        session.flush()
        for _key, cv in cleared:
            cv.status = "resolved"

        # Task 4.5: link any still-unlinked violations (new + pre-existing) to
        # their matching Resource before the single commit for this pass.
        reconcile_compliance_resource_links(session)
        session.commit()

    def _gather_gap_finder_summary(self, session) -> list[tuple[str, int]] | None:
        """Phase 1 (TRK-068): summarize open violations into plain (rule, count)
        tuples while the caller's session is still open. Returns ``None`` on
        query failure so the caller skips phases 2/3 entirely (identical to
        the prior behavior of returning immediately without an LLM call).
        """
        try:
            return (
                session.query(ComplianceViolation.rule, func.count(ComplianceViolation.id))
                .filter(ComplianceViolation.status == "open")
                .group_by(ComplianceViolation.rule)
                .all()
            )
        except Exception:
            logger.warning(
                "ComplianceAgent gap-finder: failed to summarize open violations",
                exc_info=True,
            )
            return None

    def _propose_compliance_gaps(self, rows: list[tuple[str, int]], now: datetime) -> None:
        """LLM-assisted, propose-only compliance rule-gap finder.

        Composition, not inheritance: ComplianceAgent stays a plain
        ``BaseAgent`` with its 4 deterministic rules; this method is an
        additive, opt-in helper that uses ``self.llm`` (BaseAgent's lazy
        chat-model property) for one structured-output call. Any failure —
        LLM unavailable, malformed output, DB error — is logged and
        swallowed here so the deterministic rules above are NEVER affected.

        TRK-068 (R-15): called AFTER the ``with get_session()`` block in
        ``collect()`` has already closed — ``rows`` is plain data captured by
        ``_gather_gap_finder_summary`` while that session was open, so no DB
        session is held across the LLM call below. A NEW session is opened
        only for the write phase (``_write_gap_proposal``).
        """
        if rows:
            summary = "\n".join(f"- {rule}: {count} open violation(s)" for rule, count in rows)
        else:
            summary = "- (no open violations currently)"

        prompt = (
            "You are a PCI/regulatory compliance analyst reviewing an "
            "infrastructure compliance system.\n\n"
            "The system currently enforces exactly these deterministic rules:\n"
            + "\n".join(f"- {name}" for name in _KNOWN_RULE_NAMES)
            + "\n\nSummary of currently open violations by rule:\n"
            + summary
            + "\n\nEach gap you propose must be filed under one rule_domain category "
            "from this fixed list (pick the best fit; use 'other' only when nothing "
            "else fits):\n"
            + "\n".join(f"- {slug}" for slug in _RULE_DOMAIN_VOCAB)
            + "\n\nPropose NEW compliance rule gaps — checks the system does NOT "
            "currently perform but should, given common PCI/regulatory concerns "
            "and the violation patterns above. Do not restate the existing "
            "rules. If you have no confident suggestion, return an empty list."
        )

        try:
            structured = self.llm.with_structured_output(_GapFinderOutput)
            # TRK-119: a single transient parse/validation hiccup from the model
            # used to discard the ENTIRE run's gap proposals via the bare
            # except below. Retry those transient failures (bounded) first; a
            # genuine config error (no model configured) still fails fast and is
            # caught by the outer except, preserving the existing "give up
            # gracefully, skip this run" fail-open posture.
            result = invoke_structured_with_retry(
                structured,
                prompt,
                config={"callbacks": self.callbacks},
                label="ComplianceAgent gap-finder",
                schema=_GapFinderOutput,
            )
        except Exception:
            logger.warning("ComplianceAgent gap-finder: LLM call failed — skipping", exc_info=True)
            return

        gaps = getattr(result, "gaps", None) or []
        if not gaps:
            return

        wrote_any = False
        with get_session() as session:
            for gap in gaps:
                try:
                    if self._write_gap_proposal(session, gap, now):
                        wrote_any = True
                except Exception:
                    logger.warning(
                        "ComplianceAgent gap-finder: failed to persist a proposed gap",
                        exc_info=True,
                    )
            if wrote_any:
                session.commit()

    def _write_gap_proposal(self, session, gap: _ProposedRuleGap, now: datetime) -> bool:
        """Idempotently write one ``ProposedAction`` row for a proposed gap.

        Idempotency: the (action_type, target, status) uq constraint gives
        IntegrityError on a literal duplicate, NOT idempotency — a re-run
        that mints the same target with a *different* status would still
        insert a second row. So we exists-check across every status the
        constraint's status column can hold — 'rejected' is deliberately
        included so an operator-rejected gap is never re-proposed.
        """
        stable_hash = _stable_gap_hash(gap.rule_domain, gap.condition_type)
        target = f"rule-gap:{stable_hash}"

        existing = (
            session.query(ProposedAction)
            .filter(
                ProposedAction.action_type == "compliance_rule_gap",
                ProposedAction.target == target,
                ProposedAction.status.in_(_GAP_PROPOSAL_LIVE_STATUSES),
            )
            .first()
        )
        if existing is not None:
            return False

        action = ProposedAction(
            id=uuid.uuid4(),
            agent="compliance",
            action_type="compliance_rule_gap",
            target=target,
            payload={
                "rule_domain": gap.rule_domain,
                "condition_type": gap.condition_type,
                "description": gap.description,
            },
            confidence=0.5,
            status="pending",
            created_at=now,
        )
        session.add(action)
        return True
