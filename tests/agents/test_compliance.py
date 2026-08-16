"""Tests for ComplianceAgent (deterministic policy-as-code)."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    ComplianceViolation,
    DriftEvent,
    EolRegistry,
    ProposedAction,
    Resource,
    VulnQueueItem,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_session(engine):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.agents.compliance.get_session", _get_session):
        yield engine


def _make_agent():
    from infra_brain.agents.compliance import ComplianceAgent

    agent = ComplianceAgent.__new__(ComplianceAgent)
    agent.settings = None
    agent.callbacks = []
    # thresholds is normally set by __init__ via _load_thresholds(); provide defaults here.
    agent.thresholds = {}
    return agent


def _make_agent_with_gap_finder(enabled: bool, fake_llm=None):
    """A ComplianceAgent with a fake settings object carrying the
    compliance_gap_finder_enabled flag, and an injectable fake LLM."""
    agent = _make_agent()
    agent.settings = MagicMock(compliance_gap_finder_enabled=enabled)
    if fake_llm is not None:
        agent.llm = fake_llm
    return agent


def _fake_gap_llm(gaps):
    """A fake chat-model stand-in whose with_structured_output(...).invoke(...)
    returns an object with a .gaps list, mirroring _GapFinderOutput."""
    from infra_brain.agents.compliance import _GapFinderOutput, _ProposedRuleGap

    structured = MagicMock()
    structured.invoke.return_value = _GapFinderOutput(gaps=[_ProposedRuleGap(**g) for g in gaps])
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


def test_detects_eol_vuln_and_stale_drift(patched_session):
    engine = patched_session
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-01", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(
            EolRegistry(
                resource_id=r.id,
                asset_name="Ubuntu 18.04",
                eol_date=now - timedelta(days=10),
                pci_risk_score=8,
            )
        )
        s.add(
            VulnQueueItem(
                resource_id=r.id,
                cve_id="CVE-X",
                severity="critical",
                sla_due=now - timedelta(days=1),
                status="open",
            )
        )
        s.add(
            DriftEvent(
                resource_id=r.id,
                drift_type="config_drift",
                field="kernel",
                status="open",
                detected_at=now - timedelta(days=20),
            )
        )
        s.commit()

    _make_agent().collect()
    with Session(engine) as s:
        rules = {c.rule for c in s.query(ComplianceViolation).filter_by(status="open")}
    assert rules == {"eol_overdue", "vuln_sla_breach", "stale_drift"}


def test_stale_drift_ignores_bookkeeping_domain_resources(patched_session):
    """GitLab #137: Rule 3 (stale_drift) must never fire on internal
    bookkeeping resources — compliance violation-shadow nodes and
    graph_maintenance self-telemetry. A stale-drift violation raised against a
    shadow named 'stale_drift:web-01' fed graph_maintenance's shadow minting
    and produced runaway 'stale_drift:stale_drift:*' rows."""
    engine = patched_session
    now = datetime.now(UTC)
    with Session(engine) as s:
        shadow = Resource(
            domain="compliance",
            type="violation",
            name="stale_drift:web-01",
            source="graph_maintenance",
        )
        telemetry = Resource(
            domain="graph_maintenance",
            type="report",
            name="graph-health",
            source="graph_maintenance",
        )
        real = Resource(domain="linux", type="host", name="web-02", source="LinuxAgent")
        s.add_all([shadow, telemetry, real])
        s.flush()
        for res in (shadow, telemetry, real):
            s.add(
                DriftEvent(
                    resource_id=res.id,
                    drift_type="state_drift",
                    field="presence",
                    status="open",
                    detected_at=now - timedelta(days=20),
                )
            )
        s.commit()

    _make_agent().collect()
    with Session(engine) as s:
        stale = s.query(ComplianceViolation).filter_by(rule="stale_drift", status="open").all()
    # Only the real fleet host is flagged — never the bookkeeping nodes.
    assert {v.host for v in stale} == {"web-02"}


def test_compliance_domain():
    from infra_brain.agents.compliance import ComplianceAgent

    assert ComplianceAgent.domain == "compliance"


def test_no_violations_when_clean(patched_session):
    """Empty inventory → no violations written, collect() returns items=[]."""
    engine = patched_session
    result = _make_agent().collect()
    assert result.items == []
    assert result.count_override == 0
    with Session(engine) as s:
        assert s.query(ComplianceViolation).count() == 0


def test_double_write_no_exception(patched_session):
    """Writing the same (rule, host) violation twice must not raise UniqueViolation.

    Simulates two concurrent compliance runs both attempting to insert the same
    open violation — the ON CONFLICT DO UPDATE upsert must absorb the duplicate
    without error and leave exactly one row in the DB.
    """
    engine = patched_session
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="dup-host", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(EolRegistry(resource_id=r.id, asset_name="Old OS", eol_date=now - timedelta(days=1)))
        s.commit()

    # First run creates the violation.
    _make_agent().collect()
    # Second run with the same EOL condition must not raise (ORM session-check
    # would normally prevent the duplicate, but the upsert makes this bulletproof).
    _make_agent().collect()

    with Session(engine) as s:
        open_count = (
            s.query(ComplianceViolation).filter_by(rule="eol_overdue", status="open").count()
        )
    assert open_count == 1


def test_idempotent_and_resolves_cleared(patched_session):
    engine = patched_session
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-01", source="LinuxAgent")
        s.add(r)
        s.flush()
        eol = EolRegistry(resource_id=r.id, asset_name="old", eol_date=now - timedelta(days=5))
        s.add(eol)
        s.commit()
        eol_id = eol.id

    _make_agent().collect()
    _make_agent().collect()  # second run: no duplicate
    with Session(engine) as s:
        assert s.query(ComplianceViolation).filter_by(status="open").count() == 1

    # Clear the EOL condition → violation should resolve on next run
    with Session(engine) as s:
        s.query(EolRegistry).filter_by(id=eol_id).delete()
        s.commit()
    _make_agent().collect()
    with Session(engine) as s:
        assert s.query(ComplianceViolation).filter_by(status="open").count() == 0
        assert s.query(ComplianceViolation).filter_by(status="resolved").count() == 1


def test_resolve_reopen_resolve_cycle_no_unique_violation(patched_session):
    """Regression: a violation that resolves, re-opens, then resolves AGAIN must
    not raise UniqueViolation on uq_compliance_rule_host_status.

    The stale 'resolved' tombstone from the first resolve would otherwise
    collide with flipping the re-opened row to 'resolved' on the second clear,
    crashing the entire compliance pass (observed in prod: eol_overdue /
    ip-10-3-128-252). The fix drops the stale tombstone before re-flipping.
    """
    engine = patched_session
    now = datetime.now(UTC)

    def _add_eol():
        with Session(engine) as s:
            r = s.query(Resource).filter_by(name="cycle-host").first() or Resource(
                domain="linux", type="host", name="cycle-host", source="LinuxAgent"
            )
            s.add(r)
            s.flush()
            s.add(EolRegistry(resource_id=r.id, asset_name="old", eol_date=now - timedelta(days=5)))
            s.commit()

    def _clear_eol():
        with Session(engine) as s:
            s.query(EolRegistry).delete()
            s.commit()

    # 1) fire → open, 2) clear → resolved (tombstone created)
    _add_eol()
    _make_agent().collect()
    _clear_eol()
    _make_agent().collect()
    with Session(engine) as s:
        assert s.query(ComplianceViolation).filter_by(status="resolved").count() == 1

    # 3) re-fire → open again (upsert), 4) clear → resolve AGAIN (previously
    # raised UniqueViolation against the existing tombstone).
    _add_eol()
    _make_agent().collect()
    _clear_eol()
    _make_agent().collect()  # must NOT raise

    with Session(engine) as s:
        assert (
            s.query(ComplianceViolation).filter_by(rule="eol_overdue", status="open").count() == 0
        )
        # Exactly one tombstone survives (latest wins) — constraint honored.
        assert (
            s.query(ComplianceViolation).filter_by(rule="eol_overdue", status="resolved").count()
            == 1
        )


def test_h2_failed_rule_does_not_mass_resolve_its_own_violations(patched_session):
    """H-2: a rule that raises during evaluation (e.g. a schema change renaming
    a column it reads) must NOT have its existing open violations mass-resolved
    just because the exception left it absent from this cycle's ``found`` set.
    Before the fix, ``compliance_rules.evaluate_rules`` caught any per-rule
    exception and skipped that rule silently — its tuples were then absent
    from ``found``, and ``ComplianceAgent._reconcile`` flipped every one of
    that rule's existing open violations to 'resolved' on the strength of
    that empty result, while the run still reported no errors.
    """
    engine = patched_session
    now = datetime.now(UTC)
    with Session(engine) as s:
        eol_host = Resource(domain="linux", type="host", name="eol-host", source="LinuxAgent")
        vuln_host = Resource(domain="linux", type="host", name="vuln-host", source="LinuxAgent")
        s.add_all([eol_host, vuln_host])
        s.flush()
        s.add(
            EolRegistry(
                resource_id=eol_host.id, asset_name="Old OS", eol_date=now - timedelta(days=5)
            )
        )
        # An unrelated rule that keeps firing cleanly this cycle, so the run
        # has real data alongside the error (R3: errors + data -> "partial").
        s.add(
            VulnQueueItem(
                resource_id=vuln_host.id,
                cve_id="CVE-H2",
                severity="critical",
                sla_due=now - timedelta(days=1),
                status="open",
            )
        )
        s.commit()

    # First run: eol_overdue evaluates cleanly -> one open violation.
    _make_agent().collect()
    with Session(engine) as s:
        assert (
            s.query(ComplianceViolation).filter_by(rule="eol_overdue", status="open").count() == 1
        )

    # Second run: simulate a schema change breaking the field_overdue shape
    # (the shape eol_overdue uses) — the real failure mode this finding
    # describes, not a synthetic stand-in exception type. ``_RULE_TYPES``
    # already holds a direct reference to ``_rule_field_overdue`` by the time
    # this runs, so the module-attribute must be patched via ``patch.dict``
    # against the registry itself — patching the module attribute alone would
    # leave the dict's stored reference untouched and the mock would never
    # actually be called.
    from infra_brain.agents import compliance_rules as _cr

    def _raise_field_overdue(session, cfg, now, thresholds):
        raise AttributeError("'EolRegistry' object has no attribute 'eol_date'")

    with patch.dict(_cr._RULE_TYPES, {"field_overdue": _raise_field_overdue}):
        result = _make_agent().collect()

    with Session(engine) as s:
        eol_open = s.query(ComplianceViolation).filter_by(rule="eol_overdue", status="open").count()
        vuln_open = (
            s.query(ComplianceViolation).filter_by(rule="vuln_sla_breach", status="open").count()
        )
    assert eol_open == 1, (
        "a raising rule's existing open violations must survive, not be mass-resolved"
    )
    assert vuln_open == 1, (
        "an unrelated rule that evaluated cleanly must still be reconciled normally"
    )
    # The failure must be visible on the run, never a silent "completed".
    assert result.status == "partial"
    assert any("eol_overdue" in e for e in result.errors)


def test_rule4_substring_collision_does_not_suppress(patched_session):
    """AA-C-5: an action for 'web10' must NOT suppress the 'web1' critical-CVE rule.

    The unanchored ``like('%host%')`` matched 'web1' inside 'web10'; the anchored
    ``:{host}`` suffix match plus the vuln_patch action_type filter fix it.
    """
    from infra_brain.db.models import ProposedAction, R7Asset

    engine = patched_session
    now = datetime.now(UTC)
    with Session(engine) as s:
        web1 = Resource(domain="vuln", type="r7_asset", name="web1", source="VulnAgent")
        s.add(web1)
        s.flush()
        s.add(R7Asset(resource_id=web1.id, r7_asset_id=1, hostname="web1", vuln_critical=3))
        # A recent vuln_patch action for a DIFFERENT host that shares a prefix.
        s.add(
            ProposedAction(
                agent="VulnTriageAgent",
                action_type="vuln_patch",
                target="cve:CVE-2026-1:web10",
                status="pending",
                created_at=now,
            )
        )
        s.commit()

    _make_agent().collect()
    with Session(engine) as s:
        rules = {c.rule for c in s.query(ComplianceViolation).filter_by(status="open")}
    # The web10 action must NOT have suppressed web1's violation.
    assert "unaddressed_critical_cve" in rules

    # Now add an action that genuinely addresses web1 → violation resolves.
    with Session(engine) as s:
        s.add(
            ProposedAction(
                agent="VulnTriageAgent",
                action_type="vuln_patch",
                target="cve:CVE-2026-2:web1",
                status="pending",
                created_at=datetime.now(UTC),
            )
        )
        s.commit()
    _make_agent().collect()
    with Session(engine) as s:
        open_rules = {c.rule for c in s.query(ComplianceViolation).filter_by(status="open")}
    assert "unaddressed_critical_cve" not in open_rules


def test_eol_check_disabled_skips_eol_rule(patched_session):
    """TRK-049: compliance.yml's ``eol_check.enabled`` must gate Rule 1 — when
    False, an overdue EolRegistry row must not produce an eol_overdue violation."""
    engine = patched_session
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-02", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(
            EolRegistry(
                resource_id=r.id,
                asset_name="Ubuntu 16.04",
                eol_date=now - timedelta(days=10),
                pci_risk_score=8,
            )
        )
        s.commit()

    agent = _make_agent()
    agent.thresholds = {"eol_check": {"enabled": False}}
    agent.collect()

    with Session(engine) as s:
        rules = {c.rule for c in s.query(ComplianceViolation).filter_by(status="open")}
    assert "eol_overdue" not in rules


def test_rule4_uses_unaddressed_critical_cve_days_not_vuln_sla_days(patched_session):
    """TRK-050: Rule 4's action-recency window must read
    ``unaddressed_critical_cve_days``, not ``vuln_sla_days`` (the two only ever
    looked identical because both default to 30)."""
    from infra_brain.db.models import ProposedAction, R7Asset

    engine = patched_session
    now = datetime.now(UTC)
    with Session(engine) as s:
        host = Resource(domain="vuln", type="r7_asset", name="web-x", source="VulnAgent")
        s.add(host)
        s.flush()
        s.add(R7Asset(resource_id=host.id, r7_asset_id=2, hostname="web-x", vuln_critical=1))
        # Action is 10 days old: outside a 5-day "addressed" window, but well
        # inside a 1000-day one. If Rule 4 mistakenly used vuln_sla_days=1000
        # this action would (wrongly) suppress the violation.
        s.add(
            ProposedAction(
                agent="VulnTriageAgent",
                action_type="vuln_patch",
                target="cve:CVE-2026-9:web-x",
                status="pending",
                created_at=now - timedelta(days=10),
            )
        )
        s.commit()

    agent = _make_agent()
    agent.thresholds = {"unaddressed_critical_cve_days": 5, "vuln_sla_days": 1000}
    agent.collect()

    with Session(engine) as s:
        rules = {c.rule for c in s.query(ComplianceViolation).filter_by(status="open")}
    assert "unaddressed_critical_cve" in rules


# ---------------------------------------------------------------------------
# Phase 3 Task 3: Compliance gap-finder (opt-in, propose-only)
# ---------------------------------------------------------------------------


def test_gap_finder_disabled_by_default_writes_nothing(patched_session):
    """With the flag off (default False, or agent.settings=None), no
    compliance_rule_gap ProposedAction rows are ever written."""
    engine = patched_session
    fake_llm = _fake_gap_llm(
        [
            {
                "rule_domain": "backup_retention",
                "condition_type": "missing_verification",
                "description": "no backup verification check exists",
            }
        ]
    )
    agent = _make_agent_with_gap_finder(enabled=False, fake_llm=fake_llm)
    agent.collect()

    with Session(engine) as s:
        assert s.query(ProposedAction).filter_by(action_type="compliance_rule_gap").count() == 0
    fake_llm.with_structured_output.assert_not_called()


def test_gap_finder_enabled_writes_proposed_action(patched_session):
    """With the flag on, a proposed gap must be written as a pending,
    inert ProposedAction row."""
    engine = patched_session
    fake_llm = _fake_gap_llm(
        [
            {
                "rule_domain": "backup_retention",
                "condition_type": "missing_verification",
                "description": "no backup verification check exists",
            }
        ]
    )
    agent = _make_agent_with_gap_finder(enabled=True, fake_llm=fake_llm)
    agent.collect()

    with Session(engine) as s:
        rows = s.query(ProposedAction).filter_by(action_type="compliance_rule_gap").all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "pending"
    assert row.target.startswith("rule-gap:")
    assert row.payload["rule_domain"] == "backup_retention"


def test_gap_finder_idempotent_rejected_not_reproposed(patched_session):
    """A previously-rejected gap must never be re-proposed — 'rejected' is
    included in the exists-check statuses."""
    from infra_brain.agents.compliance import _stable_gap_hash

    engine = patched_session
    stable_hash = _stable_gap_hash("backup_retention", "missing_verification")
    with Session(engine) as s:
        s.add(
            ProposedAction(
                agent="compliance",
                action_type="compliance_rule_gap",
                target=f"rule-gap:{stable_hash}",
                status="rejected",
                confidence=0.5,
            )
        )
        s.commit()

    fake_llm = _fake_gap_llm(
        [
            {
                "rule_domain": "backup_retention",
                "condition_type": "missing_verification",
                "description": "different wording this run",
            }
        ]
    )
    agent = _make_agent_with_gap_finder(enabled=True, fake_llm=fake_llm)
    agent.collect()

    with Session(engine) as s:
        rows = s.query(ProposedAction).filter_by(action_type="compliance_rule_gap").all()
    assert len(rows) == 1
    assert rows[0].status == "rejected"


def test_gap_finder_hash_stable_across_differing_llm_prose(patched_session):
    """The stable hash/target must be derived from canonical rule fields only
    — varying LLM prose (description) across runs must NOT mint a new
    target, or every run would insert a duplicate proposal."""
    engine = patched_session

    agent1 = _make_agent_with_gap_finder(
        enabled=True,
        fake_llm=_fake_gap_llm(
            [
                {
                    "rule_domain": "backup_retention",
                    "condition_type": "missing_verification",
                    "description": "wording A",
                }
            ]
        ),
    )
    agent1.collect()

    agent2 = _make_agent_with_gap_finder(
        enabled=True,
        fake_llm=_fake_gap_llm(
            [
                {
                    "rule_domain": "backup_retention",
                    "condition_type": "missing_verification",
                    "description": "totally different wording B",
                }
            ]
        ),
    )
    agent2.collect()

    with Session(engine) as s:
        rows = s.query(ProposedAction).filter_by(action_type="compliance_rule_gap").all()
    assert len(rows) == 1


def test_stable_gap_hash_is_order_sensitive():
    """(rule_domain, condition_type) and its swap must hash differently —
    they are semantically distinct gaps, not the same gap reordered.
    Regression test: the hash used to sort the pair before hashing, so a
    gap proposed as (backup_retention, missing_verification) collided with
    one proposed as (missing_verification, backup_retention)."""
    from infra_brain.agents.compliance import _stable_gap_hash

    forward = _stable_gap_hash("backup_retention", "missing_verification")
    swapped = _stable_gap_hash("missing_verification", "backup_retention")
    assert forward != swapped


def test_gap_finder_distinct_domain_same_condition_both_proposed(patched_session):
    """Two gaps sharing the same condition_type but filed under different
    rule_domain categories are semantically different rule gaps and must
    both be persisted, not de-duplicated onto the same target.

    (TRK-079: rule_domain is now a closed Literal vocabulary — this test
    used to construct one gap with rule_domain/condition_type SWAPPED
    relative to the other, but "missing_verification" is not a valid
    rule_domain slug post-fix. Rewritten to hold condition_type fixed and
    vary rule_domain across two real vocabulary entries instead, which
    still proves rule_domain contributes to the hash rather than being
    dominated by condition_type.)"""
    engine = patched_session

    agent1 = _make_agent_with_gap_finder(
        enabled=True,
        fake_llm=_fake_gap_llm(
            [
                {
                    "rule_domain": "backup_retention",
                    "condition_type": "missing_verification",
                    "description": "backups exist but are never verified",
                }
            ]
        ),
    )
    agent1.collect()

    agent2 = _make_agent_with_gap_finder(
        enabled=True,
        fake_llm=_fake_gap_llm(
            [
                {
                    "rule_domain": "secrets_management",
                    "condition_type": "missing_verification",
                    "description": "a distinct, differently-categorized gap",
                }
            ]
        ),
    )
    agent2.collect()

    with Session(engine) as s:
        rows = s.query(ProposedAction).filter_by(action_type="compliance_rule_gap").all()
    assert len(rows) == 2
    targets = {row.target for row in rows}
    assert len(targets) == 2


# ---------------------------------------------------------------------------
# TRK-079: closed rule_domain vocabulary + hash normalization
# ---------------------------------------------------------------------------


def test_rule_domain_vocabulary_rejects_invalid_slug():
    """The gap-finder's structured-output schema must reject any rule_domain
    slug outside the fixed vocabulary — this is what actually prevents an
    LLM run from minting a differently-worded domain for the same conceptual
    gap (TRK-079's core defect)."""
    import pydantic

    from infra_brain.agents.compliance import _ProposedRuleGap

    with pytest.raises(pydantic.ValidationError):
        _ProposedRuleGap(
            rule_domain="backup_verification",  # near-miss of the real "backup_retention" slug
            condition_type="missing_verification",
            description="should be rejected — not in the closed vocabulary",
        )


def test_rule_domain_vocabulary_accepts_every_listed_slug():
    """Every slug in the published vocabulary (including the 'other' escape)
    must actually validate — the schema and the vocabulary constant must
    never drift apart."""
    from infra_brain.agents.compliance import _RULE_DOMAIN_VOCAB, _ProposedRuleGap

    for slug in _RULE_DOMAIN_VOCAB:
        gap = _ProposedRuleGap(rule_domain=slug, condition_type="x", description="d")
        assert gap.rule_domain == slug


def test_stable_gap_hash_normalizes_case_and_separators():
    """TRK-079 layer 2: case, hyphen, and whitespace variants of the same
    conceptual slug must converge on one hash, so historical near-miss
    wording de-dupes against a canonical run instead of re-proposing."""
    from infra_brain.agents.compliance import _stable_gap_hash

    canonical = _stable_gap_hash("backup_retention", "missing_verification")
    assert canonical == _stable_gap_hash("Backup_Retention", "Missing_Verification")
    assert canonical == _stable_gap_hash("backup-retention", "missing-verification")
    assert canonical == _stable_gap_hash("  backup_retention  ", "  missing_verification  ")
    assert canonical == _stable_gap_hash("backup   retention", "missing   verification")


def test_other_category_gaps_with_different_condition_types_hash_differently():
    """TRK-079 layer 3: the 'other' escape slug must NOT silently dedupe
    every genuinely novel gap onto one blob. Two 'other'-category gaps with
    different condition_type values (the model's only remaining way to say
    what the gap actually is once no listed category fits) must hash to
    different targets."""
    from infra_brain.agents.compliance import _stable_gap_hash

    gap_a = _stable_gap_hash("other", "no_vendor_offboarding_review")
    gap_b = _stable_gap_hash("other", "no_physical_access_log_retention")
    assert gap_a != gap_b


def test_other_category_gaps_both_persisted_as_distinct_proposals(patched_session):
    """End-to-end version of the 'other' distinguishability guarantee: two
    'other'-category gap proposals from two runs must both land as separate
    ProposedAction rows, not collapse onto a single de-duped target."""
    engine = patched_session

    agent1 = _make_agent_with_gap_finder(
        enabled=True,
        fake_llm=_fake_gap_llm(
            [
                {
                    "rule_domain": "other",
                    "condition_type": "no_vendor_offboarding_review",
                    "description": "third-party vendor access is never revoked on offboarding",
                }
            ]
        ),
    )
    agent1.collect()

    agent2 = _make_agent_with_gap_finder(
        enabled=True,
        fake_llm=_fake_gap_llm(
            [
                {
                    "rule_domain": "other",
                    "condition_type": "no_physical_access_log_retention",
                    "description": "physical datacenter access logs have no retention policy",
                }
            ]
        ),
    )
    agent2.collect()

    with Session(engine) as s:
        rows = s.query(ProposedAction).filter_by(action_type="compliance_rule_gap").all()
    assert len(rows) == 2
    targets = {row.target for row in rows}
    assert len(targets) == 2


def test_gap_finder_llm_failure_does_not_affect_rules(patched_session):
    """If the LLM call raises, the 4 deterministic rules must still be
    evaluated and no exception must propagate out of collect()."""
    engine = patched_session
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-03", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(EolRegistry(resource_id=r.id, asset_name="Old OS", eol_date=now - timedelta(days=5)))
        s.commit()

    broken_llm = MagicMock()
    broken_llm.with_structured_output.side_effect = RuntimeError("LLM unavailable")
    agent = _make_agent_with_gap_finder(enabled=True, fake_llm=broken_llm)

    result = agent.collect()  # must not raise

    with Session(engine) as s:
        rules = {c.rule for c in s.query(ComplianceViolation).filter_by(status="open")}
    assert "eol_overdue" in rules
    assert result.count_override >= 1
    with Session(engine) as s:
        assert s.query(ProposedAction).filter_by(action_type="compliance_rule_gap").count() == 0


def test_gap_finder_calls_llm_with_no_session_open(patched_session):
    """TRK-068/R-15: the gap-finder's LLM call must never run while a DB
    session is open. Uses the tracking-wrapper pattern from
    tests/agents/test_coverage.py — a context manager that flags 'open' for
    the duration of the `with` block, released before the return."""
    engine = patched_session
    state = {"open": False}

    @contextmanager
    def _tracking_session():
        state["open"] = True
        try:
            with Session(engine) as s:
                yield s
        finally:
            state["open"] = False

    fake_llm = MagicMock()
    structured = MagicMock()

    def _invoke(*args, **kwargs):
        assert state["open"] is False, "LLM call made while a DB session was open"
        from infra_brain.agents.compliance import _GapFinderOutput

        return _GapFinderOutput(gaps=[])

    structured.invoke.side_effect = _invoke
    fake_llm.with_structured_output.return_value = structured

    agent = _make_agent_with_gap_finder(enabled=True, fake_llm=fake_llm)

    with patch("infra_brain.agents.compliance.get_session", _tracking_session):
        agent.collect()

    structured.invoke.assert_called_once()
    assert state["open"] is False


def test_gap_finder_rows_are_picked_up_by_remediation_executor(patched_session):
    """GitLab #108: RemediationAgent's ``_execute_approved()`` poll now
    includes ``action_type == 'compliance_rule_gap'`` (extending the same
    write-gated MR-drafting path config_fix/vuln_patch/eol_migration already
    use) — an approved gap-finder row must be picked up for execution, not
    left inert. See ``tests/agents/test_remediation.py`` for the full
    MR-opening behavior; this test only verifies the cross-module wiring
    (the row ComplianceAgent drafts is one RemediationAgent's real filter
    will select)."""
    from infra_brain.db.models import ProposedAction as _PA

    engine = patched_session
    fake_llm = _fake_gap_llm(
        [
            {
                "rule_domain": "backup_retention",
                "condition_type": "missing_verification",
                "description": "no backup verification check exists",
            }
        ]
    )
    agent = _make_agent_with_gap_finder(enabled=True, fake_llm=fake_llm)
    agent.collect()

    with Session(engine) as s:
        row = s.query(ProposedAction).filter_by(action_type="compliance_rule_gap").first()
        row.status = "approved"
        s.commit()

        # Mirror remediation.py's real _execute_approved() filter exactly.
        picked_up = (
            s.query(_PA)
            .filter(
                _PA.status == "approved",
                _PA.action_type.in_(
                    ("config_fix", "vuln_patch", "eol_migration", "compliance_rule_gap")
                ),
            )
            .all()
        )
    assert len(picked_up) == 1


# ---------------------------------------------------------------------------
# TRK-119: structured-output bounded retry for the gap-finder
# ---------------------------------------------------------------------------


def _gap_llm_with_structured(structured):
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


def test_gap_finder_retries_transient_structured_output_then_succeeds(patched_session):
    """A single transient parse failure must be retried, not silently discard
    the whole run's gap proposals — the second attempt's good output is used."""
    from langchain_core.exceptions import OutputParserException

    from infra_brain.agents.compliance import _GapFinderOutput, _ProposedRuleGap

    engine = patched_session
    good = _GapFinderOutput(
        gaps=[
            _ProposedRuleGap(
                rule_domain="backup_retention",
                condition_type="missing_verification",
                description="no backup verification check exists",
            )
        ]
    )
    structured = MagicMock()
    structured.invoke.side_effect = [OutputParserException("garbled model output"), good]
    agent = _make_agent_with_gap_finder(enabled=True, fake_llm=_gap_llm_with_structured(structured))

    agent.collect()  # must not raise

    assert structured.invoke.call_count == 2  # retried exactly once
    with Session(engine) as s:
        assert s.query(ProposedAction).filter_by(action_type="compliance_rule_gap").count() == 1


def test_gap_finder_retry_exhausted_skips_gracefully_rules_unaffected(patched_session):
    """When every attempt fails a parse/validation check, give up gracefully
    (skip this run, no proposal) after exhausting the bounded retries — and the
    4 deterministic rules must still have run."""
    from infra_brain.agents.compliance import _GapFinderOutput

    engine = patched_session
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name="web-retry", source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(EolRegistry(resource_id=r.id, asset_name="Old OS", eol_date=now - timedelta(days=5)))
        s.commit()

    def _always_validation_error(*args, **kwargs):
        # Constructing with a non-list gaps raises a real pydantic ValidationError.
        return _GapFinderOutput(gaps=123)

    structured = MagicMock()
    structured.invoke.side_effect = _always_validation_error
    agent = _make_agent_with_gap_finder(enabled=True, fake_llm=_gap_llm_with_structured(structured))

    result = agent.collect()  # must not raise

    assert structured.invoke.call_count == 2  # both bounded attempts used
    with Session(engine) as s:
        rules = {c.rule for c in s.query(ComplianceViolation).filter_by(status="open")}
        assert "eol_overdue" in rules  # deterministic rules unaffected
        assert s.query(ProposedAction).filter_by(action_type="compliance_rule_gap").count() == 0
    assert result.count_override >= 1


def test_gap_finder_non_transient_invoke_error_fails_fast(patched_session):
    """A non-parse/validation error from .invoke() (e.g. a provider/config
    error) must fail fast — NOT consume the retry budget — while still skipping
    gracefully via the outer fail-open guard."""
    engine = patched_session
    structured = MagicMock()
    structured.invoke.side_effect = RuntimeError("provider misconfigured")
    agent = _make_agent_with_gap_finder(enabled=True, fake_llm=_gap_llm_with_structured(structured))

    agent.collect()  # must not raise

    assert structured.invoke.call_count == 1  # no pointless retry
    with Session(engine) as s:
        assert s.query(ProposedAction).filter_by(action_type="compliance_rule_gap").count() == 0
