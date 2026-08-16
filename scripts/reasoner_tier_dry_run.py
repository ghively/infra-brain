"""Offline preview of infra-brain's three reasoner-tier LLM features.

`rootcause_llm_enabled`, `compliance_gap_finder_enabled`, and the LLM drafting
path in RemediationAgent (`_draft_plan`, gated in production by
`remediation_interrupt_enabled`/real Bedrock credentials) are all disabled by
default pending real LLM key provisioning (AWS Bedrock procurement in
progress; a plain Anthropic key would also unblock per config.py's
`llm_provider` default). Each feature already has solid mocked-LLM unit test
coverage proving its parse+write logic is correct in isolation
(tests/agents/test_rootcause.py, test_compliance.py, test_remediation.py).

What's missing before ever flipping one of these flags against a real model is
a quick, human-readable answer to "what would this actually produce?" — this
script runs each agent's REAL `collect()` entrypoint against an isolated
in-memory SQLite session seeded with realistic sample data, feeding in a
canned fake LLM response (both a clean happy-path response and a
malformed/transient-failure response that exercises the same bounded-retry /
fallback path the real code already has), and pretty-prints exactly what got
written to the database.

100% offline: no real database, no real global config, no API key, no network
calls. Every session is a private in-memory SQLite engine created and
discarded within this script's own run — re-running it has no side effects
anywhere else.

Usage:
    .venv/bin/python scripts/reasoner_tier_dry_run.py
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langchain_core.exceptions import OutputParserException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from infra_brain.db.models import (
    Base,
    CollectionRun,
    ComplianceViolation,
    DriftEvent,
    EolRegistry,
    ProposedAction,
    Resource,
    RootCauseNote,
)

SEP = "=" * 78


def _rule(title: str) -> None:
    print(f"\n{SEP}\n{title}\n{SEP}")


def _make_engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    return eng


@contextmanager
def _session_factory(engine):
    with Session(engine) as s:
        yield s


def _pretty_row(obj, fields: list[str]) -> str:
    lines = []
    for f in fields:
        lines.append(f"    {f} = {getattr(obj, f, None)!r}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 1. RootCauseAgent (rootcause_llm_enabled)
# --------------------------------------------------------------------------- #


def _seed_rootcause_event(engine, *, name: str):
    """Mirrors tests/agents/test_rootcause.py::_seed_event — one open drift
    event on a host, plus an Octopus deploy CollectionRun 1h before detection
    whose trigger_source names a concrete Resource (pipeline-x)."""
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name=name, source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(
            Resource(domain="octopus", type="project", name="pipeline-x", source="OctopusAgent")
        )
        s.add(
            CollectionRun(
                domain="octopus",
                trigger_type="webhook",
                trigger_source="pipeline-x",
                started_at=now - timedelta(hours=1),
                status="completed",
            )
        )
        de = DriftEvent(
            resource_id=r.id,
            drift_type="config_drift",
            field="version",
            old_value={"v": "1.2.3"},
            new_value={"v": "1.3.0"},
            status="open",
            detected_at=now,
        )
        s.add(de)
        s.flush()
        event_id = de.id
        s.commit()
    return event_id


def run_rootcause_case(*, malformed_retry: bool) -> dict:
    from infra_brain.agents.rootcause import RootCauseAgent, RootCauseFinding

    engine = _make_engine()
    host = "web-retry-01" if malformed_retry else "web-happy-01"
    _seed_rootcause_event(engine, name=host)

    agent = RootCauseAgent.__new__(RootCauseAgent)
    agent.settings = MagicMock(rootcause_llm_enabled=True, rootcause_llm_max_events_per_run=20)
    agent.callbacks = []
    agent._llm = None
    agent.reason = MagicMock(return_value="Deploy of pipeline-x preceded the version drift.")

    finding = RootCauseFinding(
        explanation="Deploy of pipeline-x changed the version.",
        trigger_source="pipeline-x",
        confidence=0.97,
        correlated_run_ids=["run-1"],
    )
    model = MagicMock()
    if malformed_retry:
        # First attempt is malformed (OutputParserException) — the real code's
        # bounded retry (invoke_structured_with_retry, TRK-119) retries once
        # and the second attempt succeeds.
        model.with_structured_output.return_value.invoke.side_effect = [
            OutputParserException("garbled structured output from model"),
            finding,
        ]
    else:
        model.with_structured_output.return_value.invoke.return_value = finding

    with (
        patch("infra_brain.agents.rootcause.get_session", lambda: _session_factory(engine)),
        patch("infra_brain.agents.rootcause.get_chat_model", return_value=model),
    ):
        outcome = agent.collect()

    with Session(engine) as s:
        note = s.query(RootCauseNote).first()

    label = "malformed-then-retry" if malformed_retry else "happy-path"
    print(f"\n--- rootcause_llm_enabled: {label} case ---")
    print(f"  fake LLM response(s) fed in: "
          f"{'[OutputParserException, RootCauseFinding(...)]' if malformed_retry else '[RootCauseFinding(...)]'}")
    print(f"  invoke() call count: {model.with_structured_output.return_value.invoke.call_count}")
    print(f"  CollectOutcome: count_override={outcome.count_override} errors={outcome.errors} "
          f"status={outcome.status}")
    if note is not None:
        print("  RootCauseNote written:")
        print(_pretty_row(note, ["drift_event_id", "explanation", "correlated"]))

    ok = (
        note is not None
        and note.explanation == finding.explanation
        and outcome.errors == []
        and outcome.count_override == 1
    )
    verdict = "PASS" if ok else "FAIL"
    print(f"  verdict: {verdict}")
    return {"verdict": verdict, "note": note, "outcome": outcome}


# --------------------------------------------------------------------------- #
# 2. ComplianceAgent (compliance_gap_finder_enabled)
# --------------------------------------------------------------------------- #


def _seed_compliance_violation(engine, *, host: str):
    """Mirrors tests/agents/test_compliance.py's inline EolRegistry seeding —
    one overdue asset so the 4 deterministic rules produce an eol_overdue
    violation the gap-finder prompt can summarize."""
    now = datetime.now(UTC)
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name=host, source="LinuxAgent")
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
        s.commit()


def run_compliance_case(*, malformed_retry: bool) -> dict:
    from infra_brain.agents.compliance import ComplianceAgent, _GapFinderOutput, _ProposedRuleGap

    engine = _make_engine()
    host = "web-retry-02" if malformed_retry else "web-happy-02"
    _seed_compliance_violation(engine, host=host)

    agent = ComplianceAgent.__new__(ComplianceAgent)
    agent.settings = MagicMock(compliance_gap_finder_enabled=True)
    agent.thresholds = {}
    agent.callbacks = []

    good = _GapFinderOutput(
        gaps=[
            _ProposedRuleGap(
                rule_domain="backup_retention",
                condition_type="missing_verification",
                description="no backup verification check exists for critical hosts",
            )
        ]
    )
    structured = MagicMock()
    if malformed_retry:
        structured.invoke.side_effect = [
            OutputParserException("garbled gap-finder output"),
            good,
        ]
    else:
        structured.invoke.return_value = good
    fake_llm = MagicMock()
    fake_llm.with_structured_output.return_value = structured
    agent.llm = fake_llm

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.agents.compliance.get_session", _get_session):
        outcome = agent.collect()

    with Session(engine) as s:
        rules = {c.rule for c in s.query(ComplianceViolation).filter_by(status="open")}
        gap_row = s.query(ProposedAction).filter_by(action_type="compliance_rule_gap").first()

    label = "malformed-then-retry" if malformed_retry else "happy-path"
    print(f"\n--- compliance_gap_finder_enabled: {label} case ---")
    print(f"  fake LLM response(s) fed in: "
          f"{'[OutputParserException, _GapFinderOutput(gaps=[...])]' if malformed_retry else '[_GapFinderOutput(gaps=[...])]'}")
    print(f"  invoke() call count: {structured.invoke.call_count}")
    print(f"  CollectOutcome: count_override={outcome.count_override} status={outcome.status}")
    print(f"  deterministic rules still evaluated: {sorted(rules)}")
    if gap_row is not None:
        print("  ProposedAction (compliance_rule_gap) written:")
        print(_pretty_row(gap_row, ["target", "status", "payload"]))

    ok = (
        "eol_overdue" in rules
        and gap_row is not None
        and gap_row.status == "pending"
        and gap_row.payload["rule_domain"] == "backup_retention"
    )
    verdict = "PASS" if ok else "FAIL"
    print(f"  verdict: {verdict}")
    return {"verdict": verdict, "gap_row": gap_row, "outcome": outcome}


def run_compliance_retry_exhausted_case() -> dict:
    """Extra malformed case: every attempt fails validation — the retry
    budget is exhausted and the gap-finder skips gracefully (no proposal)
    while the 4 deterministic rules remain unaffected. Mirrors
    test_gap_finder_retry_exhausted_skips_gracefully_rules_unaffected."""
    from infra_brain.agents.compliance import ComplianceAgent, _GapFinderOutput

    engine = _make_engine()
    _seed_compliance_violation(engine, host="web-retry-exhausted")

    agent = ComplianceAgent.__new__(ComplianceAgent)
    agent.settings = MagicMock(compliance_gap_finder_enabled=True)
    agent.thresholds = {}
    agent.callbacks = []

    def _always_validation_error(*args, **kwargs):
        return _GapFinderOutput(gaps=123)  # invalid type -> pydantic ValidationError

    structured = MagicMock()
    structured.invoke.side_effect = _always_validation_error
    fake_llm = MagicMock()
    fake_llm.with_structured_output.return_value = structured
    agent.llm = fake_llm

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.agents.compliance.get_session", _get_session):
        outcome = agent.collect()

    with Session(engine) as s:
        rules = {c.rule for c in s.query(ComplianceViolation).filter_by(status="open")}
        gap_count = s.query(ProposedAction).filter_by(action_type="compliance_rule_gap").count()

    print("\n--- compliance_gap_finder_enabled: retry-exhausted (both attempts fail) case ---")
    print("  fake LLM response(s) fed in: [ValidationError, ValidationError] (retry budget exhausted)")
    print(f"  invoke() call count: {structured.invoke.call_count}")
    print(f"  deterministic rules still evaluated: {sorted(rules)}")
    print(f"  compliance_rule_gap rows written: {gap_count} (expected 0 — graceful skip)")

    ok = "eol_overdue" in rules and gap_count == 0 and structured.invoke.call_count == 2
    verdict = "PASS" if ok else "FAIL"
    print(f"  verdict: {verdict}")
    return {"verdict": verdict, "outcome": outcome}


# --------------------------------------------------------------------------- #
# 3. RemediationAgent LLM drafting (_draft_plan / reason())
# --------------------------------------------------------------------------- #


def _seed_remediation_drift(engine, *, name: str):
    """Mirrors tests/agents/test_remediation.py::_seed_open_drift."""
    with Session(engine) as s:
        r = Resource(domain="linux", type="host", name=name, source="LinuxAgent")
        s.add(r)
        s.flush()
        s.add(
            DriftEvent(
                resource_id=r.id,
                drift_type="config_drift",
                field="kernel",
                old_value={"v": "5.15"},
                new_value={"v": "5.19"},
                status="open",
            )
        )
        s.commit()


def run_remediation_case(*, malformed_retry: bool) -> dict:
    """RemediationAgent's LLM drafting path has no structured-output retry of
    its own (unlike rootcause/compliance) — a reason() failure falls straight
    back to the static template (see test_draft_plan_falls_back_to_template_
    on_llm_failure). That fallback IS this feature's "malformed input doesn't
    crash the pipeline" guarantee, so the second case here exercises it."""
    from types import SimpleNamespace

    from infra_brain.agents.remediation import RemediationAgent

    engine = _make_engine()
    host = "web-retry-03" if malformed_retry else "web-happy-03"
    _seed_remediation_drift(engine, name=host)

    settings = SimpleNamespace(
        remediation_project_id=0,
        remediation_branch="main",
        gitlab_url="https://gitlab.example.com",
    )
    agent = RemediationAgent.__new__(RemediationAgent)
    agent.settings = settings
    agent.callbacks = []
    agent._llm = None

    if malformed_retry:
        reason_mock = MagicMock(side_effect=RuntimeError("model unavailable (simulated)"))
    else:
        reason_mock = MagicMock(
            return_value=f"LLM-generated remediation plan: roll back kernel on {host}."
        )

    with (
        patch("infra_brain.agents.remediation.get_session", lambda: _session_factory(engine)),
        patch("infra_brain.agents.llm_base.get_session", lambda: _session_factory(engine)),
        patch.object(RemediationAgent, "reason", reason_mock),
    ):
        agent.collect()

    with Session(engine) as s:
        action = s.query(ProposedAction).filter_by(action_type="config_fix").first()

    label = "reason()-raises-falls-back-to-template" if malformed_retry else "happy-path"
    fake_response_desc = (
        "RuntimeError('model unavailable')" if malformed_retry else repr(reason_mock.return_value)
    )
    print(f"\n--- RemediationAgent LLM drafting: {label} case ---")
    print(f"  fake reason() response fed in: {fake_response_desc}")
    print(f"  reason() call count: {reason_mock.call_count}")
    if action is not None:
        print("  ProposedAction (config_fix) written:")
        print(_pretty_row(action, ["target", "status", "payload"]))

    if malformed_retry:
        ok = (
            action is not None
            and action.status == "pending"
            and "kernel" in action.payload["plan"]
            and host in action.payload["plan"]
            and "LLM-generated" not in action.payload["plan"]  # template fallback, not the mock text
        )
    else:
        ok = (
            action is not None
            and action.status == "pending"
            and "LLM-generated remediation plan" in action.payload["plan"]
        )
    verdict = "PASS" if ok else "FAIL"
    print(f"  verdict: {verdict}")
    return {"verdict": verdict, "action": action}


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def main() -> int:
    _rule("REASONER-TIER LLM DRY RUN — offline preview, no DB/API touched")
    print(
        "Validates parse+write correctness of the three reasoner-tier LLM\n"
        "features ahead of real LLM key provisioning. Every run below uses an\n"
        "isolated in-memory SQLite engine and a canned fake chat model — no\n"
        "network calls, no real config, no real database."
    )

    results = {}

    _rule("FEATURE 1: rootcause_llm_enabled  (src/infra_brain/agents/rootcause.py)")
    results["rootcause_happy"] = run_rootcause_case(malformed_retry=False)
    results["rootcause_retry"] = run_rootcause_case(malformed_retry=True)

    _rule("FEATURE 2: compliance_gap_finder_enabled  (src/infra_brain/agents/compliance.py)")
    results["compliance_happy"] = run_compliance_case(malformed_retry=False)
    results["compliance_retry"] = run_compliance_case(malformed_retry=True)
    results["compliance_retry_exhausted"] = run_compliance_retry_exhausted_case()

    _rule("FEATURE 3: RemediationAgent LLM drafting  (src/infra_brain/agents/remediation.py)")
    results["remediation_happy"] = run_remediation_case(malformed_retry=False)
    results["remediation_retry"] = run_remediation_case(malformed_retry=True)

    _rule("SUMMARY")
    rows = [
        (
            "rootcause_llm_enabled",
            "rootcause_llm_enabled",
            results["rootcause_happy"]["verdict"],
            results["rootcause_retry"]["verdict"],
        ),
        (
            "compliance_gap_finder_enabled",
            "compliance_gap_finder_enabled",
            results["compliance_happy"]["verdict"],
            results["compliance_retry"]["verdict"]
            + "/"
            + results["compliance_retry_exhausted"]["verdict"],
        ),
        (
            "RemediationAgent LLM drafting",
            "(gated by remediation_interrupt_enabled / real LLM key in prod)",
            results["remediation_happy"]["verdict"],
            results["remediation_retry"]["verdict"],
        ),
    ]
    header = f"{'feature':<32} | {'flag':<45} | {'happy-path':<12} | {'malformed/retry':<20}"
    print(header)
    print("-" * len(header))
    for feature, flag, happy, retry in rows:
        print(f"{feature:<32} | {flag:<45} | {happy:<12} | {retry:<20}")

    all_ok = all(
        v["verdict"] == "PASS"
        for k, v in results.items()
        if isinstance(v, dict) and "verdict" in v
    )
    _rule("OVERALL: " + ("ALL PASS" if all_ok else "AT LEAST ONE FAILURE — see verdicts above"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
