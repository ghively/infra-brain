"""F-008: detail-writer agents report real counts via CollectOutcome.count_override.

Fails against the old code: all three returned a bare [] so resources_found
was always 0.
"""

from infra_brain.agents.base import CollectOutcome


def test_rootcause_collect_returns_count_override(make_agent, session_patcher):
    from infra_brain.agents.rootcause import RootCauseAgent

    with session_patcher("infra_brain.agents.rootcause"):
        agent = make_agent(RootCauseAgent)
        outcome = agent.collect()
    assert isinstance(outcome, CollectOutcome)
    assert outcome.items == []
    assert outcome.count_override == 0  # empty DB -> zero notes, truthfully


def test_compliance_collect_returns_count_override(make_agent, session_patcher):
    from infra_brain.agents.compliance import ComplianceAgent

    with session_patcher("infra_brain.agents.compliance"):
        agent = make_agent(ComplianceAgent)
        # thresholds is normally set by __init__ via _load_thresholds(); provide
        # defaults here (mirrors tests/agents/test_compliance.py::_make_agent).
        agent.thresholds = {}
        outcome = agent.collect()
    assert isinstance(outcome, CollectOutcome)
    assert outcome.count_override == 0


def test_vuln_triage_collect_returns_count_override(make_agent, session_patcher):
    from infra_brain.agents.vuln_triage import VulnTriageAgent

    with session_patcher("infra_brain.agents.vuln_triage"):
        agent = make_agent(VulnTriageAgent)
        outcome = agent.collect()
    assert isinstance(outcome, CollectOutcome)
    assert outcome.count_override == 0


def test_vuln_triage_dead_metadata_code_removed():
    import inspect

    from infra_brain.agents import vuln_triage

    src = inspect.getsource(vuln_triage)
    assert "metadata_" not in src, "dead triage_summary/metadata_ code must stay removed"
