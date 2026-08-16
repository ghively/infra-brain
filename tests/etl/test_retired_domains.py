"""``AgentSpec.retired`` — the first-class collector OFF switch.

This system monitors a home lab. A handful of collectors are remnants of an
enterprise estate (Rapid7, vCenter, Octopus Deploy, Kubernetes, AWS, Ansible
Windows, Okta) that will never be configured here. Before this field they were
switched off two different ad-hoc ways, neither of which is a real off switch:

  * ``dispatchable=False`` PLUS commenting the domain out of
    ``supervisor._AGENT_SPECS`` (cloud/k8s/windows). This works, but the agent
    vanishes from the registry entirely — the operator sees nothing at all, and
    turning it back on needs TWO source edits in a critical file.
  * Left fully enabled and self-skipping on missing credentials
    (vsphere/vuln/octopus/identity). Every one of these burns a scheduler slot,
    writes a ``skipped`` ``collection_runs`` row, and shows up in the roster as
    a gap rather than as a deliberate decision.

``retired=True`` is the honest middle: the domain is NOT scheduled, NOT a sweep
member, NOT freshness-monitored, and NOT dispatchable — but it stays in
``AGENT_REGISTRY``, keeps its spec (including the cron string it WOULD run on),
stays importable and testable, and reports as ``retired`` rather than
``skipped`` so an operator reads "off on purpose", not "broken".

Re-enabling needs no code change: list the domain in
``settings.collection_revived_domains``. See ``etl/spec.retired_domains()`` for
which direction wins.
"""

from unittest.mock import MagicMock, patch

import pytest

from infra_brain.etl.spec import (
    AgentSpec,
    Tier,
    agent_specs,
    is_retired,
    max_staleness_by_domain,
    retired_domains,
    schedule_by_domain,
    sweep_members,
)
from infra_brain.supervisor import AGENT_REGISTRY

from tests.support.pg import make_engine

# The confirmed remnant set (maintainer-confirmed, 2026-08-12). Kept as a
# literal here on purpose: this test is the place a future reader learns WHICH
# domains were switched off and can challenge the list, so deriving it from the
# specs would make the assertion vacuous.
RETIRED = {"cloud", "identity", "k8s", "octopus", "vsphere", "vuln", "windows"}

# Deliberately NOT retired, despite looking enterprise-shaped. Each works off
# locally-held data or has a non-enterprise input path, so retiring it would
# switch off something that can legitimately produce results here.
STILL_ON = {"vuln_triage", "licensing", "saas_inventory"}


# ---------------------------------------------------------------------------
# The field itself
# ---------------------------------------------------------------------------


def test_agent_spec_carries_a_retired_flag_defaulting_to_false():
    """Every spec written before this field existed must be unaffected."""
    spec = AgentSpec(domain="x", tier=Tier.COLLECTOR, schedule="0 * * * *", max_staleness=None)
    assert spec.retired is False
    assert (
        AgentSpec(
            domain="x", tier=Tier.COLLECTOR, schedule="0 * * * *", max_staleness=None, retired=True
        ).retired
        is True
    )


def test_the_confirmed_remnant_domains_declare_retired():
    specs = agent_specs()
    for domain in sorted(RETIRED):
        assert domain in specs, f"{domain} must stay registered — retired is not deletion"
        assert specs[domain].retired is True, f"{domain} should be retired"


def test_domains_that_only_look_enterprise_are_left_on():
    """Err toward leaving things on: a collector that works off local data must
    not be retired just because its name reads corporate."""
    specs = agent_specs()
    for domain in sorted(STILL_ON):
        assert specs[domain].retired is False, f"{domain} must NOT be retired"


def test_a_retired_agent_stays_importable_registered_and_re_enablable():
    """Retired is a switch, not a deletion — the agent must still be real."""
    for domain in sorted(RETIRED):
        cls = AGENT_REGISTRY[domain]  # resolves the module: still importable
        assert cls.spec.domain == domain
        # The cron it WOULD run on is remembered, so re-enabling restores the
        # original cadence rather than needing it re-derived.
        assert cls.spec.schedule, f"{domain} lost its schedule string"


# ---------------------------------------------------------------------------
# What retired actually switches off
# ---------------------------------------------------------------------------


def test_a_retired_domain_is_not_freshness_monitored():
    """The freshness monitor and fleet_health both read max_staleness_by_domain;
    a domain that is off on purpose can never be 'stale'."""
    monitored = set(max_staleness_by_domain())
    assert RETIRED.isdisjoint(monitored), sorted(RETIRED & monitored)


def test_a_retired_domain_has_no_expected_cadence():
    scheduled = set(schedule_by_domain())
    assert RETIRED.isdisjoint(scheduled), sorted(RETIRED & scheduled)


def test_a_retired_domain_is_not_a_sweep_member():
    members = {d for domains in sweep_members().values() for d in domains}
    assert RETIRED.isdisjoint(members), sorted(RETIRED & members)


def test_a_retired_domain_drops_out_of_the_sweep_graph_topology():
    from langgraph.checkpoint.memory import MemorySaver

    from infra_brain.graph import build_sweep_graph

    nodes = set(build_sweep_graph(checkpointer=MemorySaver()).nodes)
    assert RETIRED.isdisjoint(nodes), sorted(RETIRED & nodes)


def test_a_retired_domain_is_not_reported_as_a_coverage_gap():
    """CoverageAgent.discover() calls the LLM once per uncovered domain to draft
    an integration proposal. An uncovered RETIRED domain is not a gap — it is
    the decision working — so counting it would spend a reason() call every run
    proposing a collector for a system that does not exist, and file the result
    for a human to triage."""
    from infra_brain.agents.coverage import CoverageAgent

    agent = CoverageAgent.__new__(CoverageAgent)  # no __init__: no LLM client
    with (
        patch("infra_brain.agents.coverage.get_session") as get_session,
        patch.object(CoverageAgent, "reason") as reason,
    ):
        session = get_session.return_value.__enter__.return_value
        session.query.return_value.filter_by.return_value.all.return_value = []
        gaps = agent.discover()

    gap_domains = {g["domain"] for g in gaps}
    assert RETIRED.isdisjoint(gap_domains), sorted(RETIRED & gap_domains)
    reason.assert_not_called()


def test_the_scheduler_registers_no_cron_for_a_retired_domain():
    from infra_brain.scheduler import SchedulerService

    svc = SchedulerService()
    with patch.object(svc._scheduler, "start"):
        svc.start()
    job_ids = {j.id for j in svc._scheduler.get_jobs()}
    for domain in sorted(RETIRED):
        assert f"infra_brain_{domain}_all" not in job_ids, f"{domain} was scheduled"


def test_the_vsphere_pulse_job_is_not_registered_while_vsphere_is_retired():
    """The 15-minute pulse is added by a hardcoded add_job_scoped() call that
    bypasses _DEFAULT_SCHEDULES entirely — retiring vsphere must reach it too,
    or the single noisiest job on the box survives the switch."""
    from infra_brain.scheduler import SchedulerService

    svc = SchedulerService()
    with patch.object(svc._scheduler, "start"):
        svc.start()
    job_ids = {j.id for j in svc._scheduler.get_jobs()}
    assert "infra_brain_vsphere_pulse" not in job_ids


def test_dispatch_refuses_a_retired_domain_and_writes_no_run_row():
    """A retired domain must produce NO collection_runs row at all. Writing a
    'skipped' row (what the dispatchable__ pause lever does, correctly, for a
    temporary pause) is exactly the daily noise this field exists to remove."""
    from infra_brain import supervisor

    with (
        patch.object(supervisor, "record_paused_run") as record,
        patch.object(supervisor, "_post_collection_hook") as hook,
        pytest.raises(ValueError, match="retired"),
    ):
        supervisor.dispatch(domain="vsphere", trigger_type="manual", scope="all")
    record.assert_not_called()
    hook.assert_not_called()


# ---------------------------------------------------------------------------
# Turning one back ON without a code change
# ---------------------------------------------------------------------------


def test_the_revived_override_wins_and_turns_a_retired_domain_back_on():
    """settings.collection_revived_domains re-enables a retired domain. The
    override only ever turns things ON — see retired_domains()'s docstring."""
    from infra_brain import config

    live = MagicMock()
    live.collection_revived_domains = "vsphere, octopus"
    with patch.object(config, "get_settings", return_value=live):
        still_retired = retired_domains()

    assert "vsphere" not in still_retired
    assert "octopus" not in still_retired
    assert "vuln" in still_retired  # untouched by the override


def test_is_retired_agrees_with_retired_domains_without_importing_the_fleet():
    """dispatch() and the webhook gate use is_retired(), not retired_domains(),
    so a per-dispatch call resolves ONE agent class instead of importing all ~46
    agent modules and undoing _LazyAgentRegistry. The two must never disagree."""
    fleet = retired_domains()
    for domain in sorted(set(agent_specs())):
        assert is_retired(domain) is (domain in fleet), domain
    # An unknown domain is not retired — the caller owns that error.
    assert is_retired("no_such_domain") is False


def test_is_retired_honours_the_revive_override_too():
    from infra_brain import config

    live = MagicMock()
    live.collection_revived_domains = "vsphere"
    with patch.object(config, "get_settings", return_value=live):
        assert is_retired("vsphere") is False
        assert is_retired("vuln") is True


def test_a_settings_read_failure_leaves_a_retired_domain_retired():
    """Fail-safe direction: if the override cannot be read, retired stays
    retired. A DB blip must never silently start dispatching a dead collector."""
    from infra_brain import config

    with patch.object(config, "get_settings", side_effect=RuntimeError("no db")):
        assert RETIRED <= retired_domains()


# ---------------------------------------------------------------------------
# What the operator sees
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_db():
    """An empty-but-migrated sqlite DB, patched into both roster surfaces.

    Empty is the point: with no collection_runs rows at all, the ONLY thing
    that can make a row read as retired is the spec flag.
    """
    import contextlib

    from sqlalchemy.orm import Session


    eng = make_engine()

    @contextlib.contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    with (
        patch("infra_brain.mcp_server.get_session", _get_session),
        patch("infra_brain.api.routers.governance_ops.get_session", _get_session),
    ):
        yield eng


def test_the_mcp_roster_reports_retired_distinctly_from_paused(empty_db):
    """'retired' and 'paused' are different facts: paused is a temporary
    operator toggle on a live collector, retired is a standing decision."""
    from infra_brain import mcp_server

    rows = {r["domain"]: r for r in mcp_server.get_agent_roster()}
    for domain in sorted(RETIRED):
        assert rows[domain]["retired"] is True, f"{domain} not reported retired"
        assert rows[domain]["paused"] is False
    assert rows["linux"]["retired"] is False


def test_the_dashboard_agents_page_shows_retired_not_skipped(empty_db):
    """An operator must read 'off on purpose', not 'unconfigured' or 'broken'."""
    from infra_brain.api.routers.governance_ops import list_agents

    rows = {a.domain: a for a in list_agents(limit=500).items}
    for domain in sorted(RETIRED):
        assert rows[domain].status == "retired", f"{domain} shown as {rows[domain].status}"
    # ...and a live collector with no runs yet is still plain "idle", so
    # "retired" is not just swallowing every quiet domain.
    assert rows["linux"].status == "idle"
