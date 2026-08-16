"""Phase 1 Task 5 (TRK-047): AgentSpec registry completeness tests.

Every class in supervisor.AGENT_REGISTRY must declare a well-formed frozen
``spec: ClassVar[AgentSpec]``, and the legacy class attributes
(domain/schedule/skip_hook/dispatchable) must be exactly what the spec says
— they are derived by ``ETLConnector.__init_subclass__`` so supervisor.py
and scheduler.py keep working unmodified.
"""

import dataclasses
import re
from datetime import timedelta
from pathlib import Path

import pytest

from infra_brain.etl.spec import (
    AgentSpec,
    Tier,
    agent_specs,
    max_staleness_by_domain,
    schedule_by_domain,
)
from infra_brain.supervisor import AGENT_REGISTRY

_CRON_RE = re.compile(r"^\S+ \S+ \S+ \S+ \S+$")


def test_agent_spec_is_frozen():
    spec = AgentSpec(domain="x", tier=Tier.COLLECTOR, schedule=None, max_staleness=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.domain = "y"


def test_every_registered_agent_has_a_well_formed_spec():
    """Completeness: every AGENT_REGISTRY class declares a spec whose domain
    matches its registry key, with valid types on every field."""
    problems: list[str] = []
    for domain, cls in AGENT_REGISTRY.items():
        spec = getattr(cls, "spec", None)
        if not isinstance(spec, AgentSpec):
            problems.append(f"{domain}: no AgentSpec (got {spec!r})")
            continue
        if spec.domain != domain:
            problems.append(f"{domain}: spec.domain={spec.domain!r} mismatch")
        if not isinstance(spec.tier, Tier):
            problems.append(f"{domain}: tier={spec.tier!r} is not a Tier")
        if spec.schedule is not None and not _CRON_RE.match(spec.schedule):
            problems.append(f"{domain}: schedule={spec.schedule!r} not 5-field cron")
        if spec.max_staleness is not None and (
            not isinstance(spec.max_staleness, timedelta) or spec.max_staleness <= timedelta(0)
        ):
            problems.append(f"{domain}: bad max_staleness={spec.max_staleness!r}")
    assert problems == []


def test_legacy_class_attributes_are_derived_from_spec():
    """Backward-compat contract: supervisor.py/scheduler.py read the legacy
    class attributes; __init_subclass__ must have copied them from spec."""
    for domain, cls in AGENT_REGISTRY.items():
        spec = cls.spec
        assert cls.domain == spec.domain, domain
        assert cls.schedule == spec.schedule, domain
        assert cls.skip_hook == spec.skip_hook, domain
        assert cls.dispatchable == spec.dispatchable, domain


def test_collect_timeout_override_helper():
    """TRK-117: collect_timeout_override_for_domain returns the spec override
    (graph_maintenance=1200) or None for domains without one, and None for
    unknown domains."""
    from infra_brain.etl.spec import collect_timeout_override_for_domain

    assert collect_timeout_override_for_domain("graph_maintenance") == 1200
    assert collect_timeout_override_for_domain("linux") is None
    assert collect_timeout_override_for_domain("does_not_exist") is None


def test_graph_maintenance_declares_timeout_override_and_offset_schedule():
    """TRK-117: guard the two Phase-0 spec changes so they can't silently
    regress — the 1200s per-domain timeout and the off-":00" cron minute."""
    spec = AGENT_REGISTRY["graph_maintenance"].spec
    assert spec.collect_timeout_seconds == 1200
    minute, hour = spec.schedule.split()[0], spec.schedule.split()[1]
    assert minute == "50", f"graph_maintenance must be off :00, got minute {minute!r}"
    assert hour == "*/2", f"graph_maintenance keeps its 2-hourly cadence, got {hour!r}"


def test_spec_helpers_agree_with_registry():
    specs = agent_specs()
    assert set(specs) == set(AGENT_REGISTRY.keys())
    for domain, sched in schedule_by_domain().items():
        assert specs[domain].schedule == sched
    for domain, age in max_staleness_by_domain().items():
        assert specs[domain].max_staleness == age
    # scheduled or freshness-monitored domains never overlap with None-spec'd
    assert "drift" not in schedule_by_domain()
    assert "drift" not in max_staleness_by_domain()


def test_query_is_on_demand_not_freshness_monitored():
    """TRK-111: QueryAgent is the on-demand NL->SQL chat agent, not a data
    collector. It has no declarative schedule (schedule=None), so it must NOT be
    freshness-monitored -- otherwise check_collection_health() alerts
    "query: no collection run has ever started" forever. It stays registered and
    dispatchable so on-demand query() and the bespoke weekly health probe still
    work; it is simply exempt from freshness alerting, like drift/notification."""
    assert "query" in AGENT_REGISTRY
    assert "query" not in schedule_by_domain()
    assert "query" not in max_staleness_by_domain()
    assert AGENT_REGISTRY["query"].dispatchable is True


def test_freshness_monitored_domains_are_all_declaratively_scheduled():
    """TRK-111 regression guard. Invariant: a domain is freshness-monitored (has
    max_staleness) only if it is also declaratively scheduled (has a cron
    schedule). check_collection_health() iterates the freshness-monitored set and
    alerts "never ran" on any domain with zero CollectionRun rows, so a
    monitored-but-unscheduled domain alerts forever. query violated this
    (max_staleness set, schedule=None) until it was made on-demand-only."""
    monitored = set(max_staleness_by_domain())
    scheduled = set(schedule_by_domain())
    monitored_but_unscheduled = monitored - scheduled
    assert not monitored_but_unscheduled, (
        "Domains freshness-monitored but not declaratively scheduled "
        "(check_collection_health will alert 'never ran' forever): "
        f"{sorted(monitored_but_unscheduled)}"
    )


def test_agents_md_matches_generator_output():
    """The committed AGENTS.md must be byte-identical to the generator output
    (modulo trailing whitespace) — regenerate with scripts/gen_agents_md.py."""
    import importlib.util

    repo_root = Path(__file__).resolve().parents[2]
    gen_path = repo_root / "scripts" / "gen_agents_md.py"
    spec = importlib.util.spec_from_file_location("gen_agents_md", gen_path)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    committed = (repo_root / "AGENTS.md").read_text(encoding="utf-8")
    assert committed == gen.render(), (
        "AGENTS.md is stale — run: .venv/Scripts/python.exe scripts/gen_agents_md.py"
    )


def test_agents_md_roster_count_matches_registry():
    """The 'N dispatchable domains' claim and per-domain rows must match the
    live registry. Domain regex is [a-z0-9_]+ — 'k8s' has a digit."""
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / "AGENTS.md").read_text(encoding="utf-8")
    m = re.search(r"(\d+) dispatchable domains", text)
    assert m, "AGENTS.md missing the 'N dispatchable domains' line"
    rows = re.findall(r"^\| ([a-z0-9_]+) \|", text, flags=re.MULTILINE)
    assert int(m.group(1)) == len(AGENT_REGISTRY) == len(rows)
    assert set(rows) == set(AGENT_REGISTRY.keys())
