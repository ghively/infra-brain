"""DL-C-5 / DL-C-6 / AA-R-14 — Octopus detail-write transaction safety.

Environments, machines, and projects previously shared ONE try/except per
block with no SAVEPOINT: a single bad row (e.g. a flush-time constraint
violation, or ``_resource_id`` returning None into the NOT NULL
``resource_id`` FK) aborted the whole detail-write phase for that entity,
silently dropping every remaining row. This module verifies:

  1. a missing Resource row (``_resource_id`` -> None) is skipped, logged, and
     does not crash the run (the columns are NOT NULL FKs), and
  2. one bad row is guarded out by its own SAVEPOINT while sibling rows in the
     same block still commit.
"""

import uuid
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from infra_brain.agents.octopus import OctopusAgent
from infra_brain.db.models import OctopusEnvironment, OctopusMachine, OctopusProject, Resource

MODULE = "infra_brain.agents.octopus"


def _make_agent():
    agent = OctopusAgent.__new__(OctopusAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    agent._last_envs = None
    agent._last_machines = None
    agent._last_projects = None
    agent._last_server = None
    return agent


def _seed_resource(engine, type_: str, name: str) -> uuid.UUID:
    rid = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            Resource(
                id=rid,
                domain="octopus",
                type=type_,
                name=name,
                source="OctopusAgent",
                zone="corpor",
            )
        )
        s.commit()
    return rid


def _run_details(agent, envs=None, machines=None, projects=None):
    """Run _write_octopus_details with the group/lifecycle/channel/release/dashboard
    tools all stubbed to empty so only the envs/machines/projects blocks matter."""
    with (
        patch(f"{MODULE}.octopus_projectgroups_tool") as g,
        patch(f"{MODULE}.octopus_lifecycles_tool") as lc,
        patch(f"{MODULE}.octopus_environments_tool") as e,
        patch(f"{MODULE}.octopus_machines_tool") as mch,
        patch(f"{MODULE}.octopus_projects_tool") as pr,
        patch(f"{MODULE}.octopus_channels_tool") as ch,
        patch(f"{MODULE}.octopus_releases_tool") as rel,
        patch(f"{MODULE}.octopus_dashboard_tool") as dsh,
    ):
        g.invoke.return_value = []
        lc.invoke.return_value = []
        e.invoke.return_value = envs or []
        mch.invoke.return_value = machines or []
        pr.invoke.return_value = projects or []
        ch.invoke.return_value = []
        rel.invoke.return_value = {"Items": []}
        dsh.invoke.return_value = {"Items": []}
        agent._write_octopus_details("all")


# ---------------------------------------------------------------------------
# _resource_id() -> None guard (NOT NULL FK)
# ---------------------------------------------------------------------------


def test_environment_with_no_resource_row_is_skipped_not_crashed(session_patcher, caplog):
    import logging

    with session_patcher(MODULE) as engine:
        # No Resource row seeded for "Production" — _resource_id() returns None.
        agent = _make_agent()
        with caplog.at_level(logging.WARNING, logger=MODULE):
            _run_details(agent, envs=[{"Id": "Environments-1", "Name": "Production"}])

        with Session(engine) as s:
            assert s.query(OctopusEnvironment).count() == 0
        assert any("has no matching Resource row" in r.message for r in caplog.records)


def test_machine_with_no_resource_row_is_skipped_not_crashed(session_patcher, caplog):
    import logging

    with session_patcher(MODULE) as engine:
        agent = _make_agent()
        with caplog.at_level(logging.WARNING, logger=MODULE):
            _run_details(agent, machines=[{"Id": "Machines-1", "Name": "web-01"}])

        with Session(engine) as s:
            assert s.query(OctopusMachine).count() == 0
        assert any("has no matching Resource row" in r.message for r in caplog.records)


def test_project_with_no_resource_row_is_skipped_not_crashed(session_patcher, caplog):
    import logging

    with session_patcher(MODULE) as engine:
        agent = _make_agent()
        with caplog.at_level(logging.WARNING, logger=MODULE):
            _run_details(agent, projects=[{"Id": "Projects-1", "Name": "Infra App"}])

        with Session(engine) as s:
            assert s.query(OctopusProject).count() == 0
        assert any("has no matching Resource row" in r.message for r in caplog.records)


def test_environments_mixed_missing_and_present_resource_rows(session_patcher):
    """One environment has no Resource row (skipped); the other still writes."""
    with session_patcher(MODULE) as engine:
        _seed_resource(engine, "octopus_environment", "Staging")
        agent = _make_agent()
        _run_details(
            agent,
            envs=[
                {"Id": "Environments-1", "Name": "Production"},  # no Resource -> skipped
                {"Id": "Environments-2", "Name": "Staging"},  # has Resource -> written
            ],
        )

        with Session(engine) as s:
            rows = {r.octopus_id: r for r in s.query(OctopusEnvironment).all()}
            assert set(rows) == {"Environments-2"}


# ---------------------------------------------------------------------------
# Per-row SAVEPOINT: one bad row does not abort its siblings
# ---------------------------------------------------------------------------


def test_bad_environment_row_guarded_out_siblings_survive(session_patcher):
    with session_patcher(MODULE) as engine:
        _seed_resource(engine, "octopus_environment", "Production")
        _seed_resource(engine, "octopus_environment", "Staging")
        agent = _make_agent()
        real_upsert = agent._upsert

        def _flaky(session, model, octopus_id, **fields):
            if model is OctopusEnvironment and octopus_id == "Environments-BAD":
                raise RuntimeError("simulated bad environment row")
            return real_upsert(session, model, octopus_id, **fields)

        with patch.object(agent, "_upsert", side_effect=_flaky):
            _run_details(
                agent,
                envs=[
                    {"Id": "Environments-1", "Name": "Production"},
                    {"Id": "Environments-BAD", "Name": "Staging"},
                    {"Id": "Environments-2", "Name": "Staging"},
                ],
            )

        with Session(engine) as s:
            ids = {r.octopus_id for r in s.query(OctopusEnvironment).all()}
            # The bad row is guarded out; both good rows (sharing "Staging" or not)
            # still commit.
            assert ids == {"Environments-1", "Environments-2"}


def test_bad_machine_row_guarded_out_siblings_survive(session_patcher):
    with session_patcher(MODULE) as engine:
        _seed_resource(engine, "octopus_machine", "web-01")
        _seed_resource(engine, "octopus_machine", "web-02")
        agent = _make_agent()
        real_upsert = agent._upsert

        def _flaky(session, model, octopus_id, **fields):
            if model is OctopusMachine and octopus_id == "Machines-BAD":
                raise RuntimeError("simulated bad machine row")
            return real_upsert(session, model, octopus_id, **fields)

        with patch.object(agent, "_upsert", side_effect=_flaky):
            _run_details(
                agent,
                machines=[
                    {"Id": "Machines-1", "Name": "web-01"},
                    {"Id": "Machines-BAD", "Name": "web-02"},
                ],
            )

        with Session(engine) as s:
            ids = {r.octopus_id for r in s.query(OctopusMachine).all()}
            assert ids == {"Machines-1"}


def test_bad_project_row_guarded_out_siblings_survive(session_patcher):
    with session_patcher(MODULE) as engine:
        _seed_resource(engine, "octopus_project", "Infra App")
        _seed_resource(engine, "octopus_project", "Billing App")
        agent = _make_agent()
        real_upsert = agent._upsert

        def _flaky(session, model, octopus_id, **fields):
            if model is OctopusProject and octopus_id == "Projects-BAD":
                raise RuntimeError("simulated bad project row")
            return real_upsert(session, model, octopus_id, **fields)

        with patch.object(agent, "_upsert", side_effect=_flaky):
            _run_details(
                agent,
                projects=[
                    {"Id": "Projects-1", "Name": "Infra App"},
                    {"Id": "Projects-BAD", "Name": "Billing App"},
                ],
            )

        with Session(engine) as s:
            ids = {r.octopus_id for r in s.query(OctopusProject).all()}
            assert ids == {"Projects-1"}
