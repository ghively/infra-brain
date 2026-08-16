"""MR9 — Octopus deep-capture collector tests.

Covers the twelve deep tables plus octopus_deployments.resource_id linkage.
The detail-write phase runs through BaseAgent._write_details (surfaced errors);
each entity is guarded by its own SAVEPOINT. The two zone-sensitive tables —
octopus_variables and octopus_accounts — must store METADATA ONLY (no Value, no
secret material). Tasks and events are time-boxed to the last-year window.
"""

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from infra_brain.agents.octopus import OctopusAgent
from infra_brain.db.models import (
    CollectionRun,
    OctopusAccount,
    OctopusActionTemplate,
    OctopusDeployment,
    OctopusDeploymentStep,
    OctopusEvent,
    OctopusFeed,
    OctopusInterruption,
    OctopusLibraryVariableSet,
    OctopusMachineRole,
    OctopusTask,
    OctopusTeam,
    OctopusUser,
    OctopusVariable,
)

MODULE = "infra_brain.agents.octopus"


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


# --- fixtures: mocked Octopus payloads --------------------------------------

MOCK_PROJECTS = [
    {
        "Id": "Projects-1",
        "Name": "Infra App",
        "DeploymentProcessId": "deploymentprocess-Projects-1",
        "VariableSetId": "variableset-Projects-1",
    }
]

MOCK_PROCESS = {
    "Id": "deploymentprocess-Projects-1",
    "Steps": [
        {
            "Name": "Deploy Web",
            "Condition": "Success",
            # On Octopus 3.3.8 TargetRoles lives on the STEP Properties (the action's
            # Properties carries no TargetRoles) — mirror that real layout here.
            "Properties": {
                "Octopus.Action.TargetRoles": "web,app",
            },
            "Actions": [
                {
                    "Name": "Run Script",
                    "Channels": ["Channels-1"],
                    "Environments": ["Environments-1"],
                    "Properties": {
                        "Octopus.Action.Type": "Octopus.Script",
                        "Octopus.Action.Package.FeedId": "Feeds-1",
                        "Octopus.Action.Package.PackageId": "MyPkg",
                        "Octopus.Action.Script.ScriptBody": "echo hi",
                    },
                }
            ],
        }
    ],
}

# A project variable set with a SENSITIVE variable. The API masks Value to null,
# but the collector must drop Value unconditionally and NEVER store it.
MOCK_PROJECT_VARSET = {
    "Id": "variableset-Projects-1",
    "Variables": [
        {
            "Name": "ApiKey",
            "Value": "super-secret-should-never-be-stored",
            "IsSensitive": True,
            "IsEditable": True,
            "Scope": {"Environment": ["Environments-1"]},
        },
        {
            "Name": "Region",
            "Value": "us-east",
            "IsSensitive": False,
            "IsEditable": True,
            "Scope": {},
        },
    ],
}

MOCK_LIBRARY_SETS = [
    {
        "Id": "LibraryVariableSets-1",
        "Name": "Shared",
        "Description": "common vars",
        "VariableSetId": "variableset-Lib-1",
        "ContentType": "Variables",
    }
]
MOCK_LIBRARY_VARSET = {
    "Id": "variableset-Lib-1",
    "Variables": [
        {"Name": "SharedToken", "Value": "x", "IsSensitive": True, "Scope": {}},
    ],
}

MOCK_ACTION_TEMPLATES = [
    {"Id": "ActionTemplates-1", "Name": "Tpl", "ActionType": "Octopus.Script", "Version": 3}
]
MOCK_MACHINE_ROLES = ["web", "app", "web"]  # dupe must collapse
MOCK_FEEDS = [{"Id": "Feeds-1", "Name": "nuget", "FeedType": "NuGet", "FeedUri": "https://feed"}]
MOCK_INTERRUPTIONS = [
    {
        "Id": "Interruptions-1",
        "Form": {"Title": "Approve prod"},
        "IsPending": True,
        "TaskId": "ServerTasks-1",
        "ResponsibleTeamIds": ["teams-1"],
    }
]
MOCK_ACCOUNTS = [
    {
        "Id": "Accounts-1",
        "Name": "deploy-svc",
        "AccountType": "UsernamePassword",
        "Username": "svc",
        "Password": {"HasValue": True},  # must NOT be stored
        "EnvironmentIds": ["Environments-1"],
    }
]
MOCK_TEAMS = [{"Id": "teams-1", "Name": "Ops", "MemberUserIds": ["users-1"], "ProjectIds": []}]
MOCK_USERS = [
    {
        "Id": "users-1",
        "Username": "alice",
        "DisplayName": "Alice",
        "EmailAddress": "alice@x",
        "IsActive": True,
        "IsService": False,
    }
]


def _tasks_payload():
    recent = _now() - timedelta(days=10)
    old = _now() - timedelta(days=400)
    return [
        {
            "Id": "ServerTasks-1",
            "Name": "Deploy",
            "Description": "Deploy Infra App",
            "State": "Success",
            "CompletedTime": _iso(recent),
            "Duration": "2m",
            "FinishedSuccessfully": True,
            "HasWarningsOrErrors": False,
        },
        {
            "Id": "ServerTasks-OLD",
            "Name": "Deploy",
            "State": "Success",
            "CompletedTime": _iso(old),  # older than cutoff → walk stops here
        },
    ]


def _events_payload():
    recent = _now() - timedelta(days=5)
    return [
        {
            "Id": "Events-1",
            "Category": "Deployed",
            "Username": "alice",
            "Occurred": _iso(recent),
            "Message": "deployed",
            "RelatedDocumentIds": ["Projects-1"],
        }
    ]


def _patch_tools():
    """Patch every deep tool imported into the octopus agent module."""
    targets = {
        "octopus_projects_tool": MOCK_PROJECTS,
        "octopus_deployment_process_tool": MOCK_PROCESS,
        "octopus_variableset_tool": None,  # set per-call below
        "octopus_library_variable_sets_tool": MOCK_LIBRARY_SETS,
        "octopus_action_templates_tool": MOCK_ACTION_TEMPLATES,
        "octopus_machine_roles_tool": MOCK_MACHINE_ROLES,
        "octopus_feeds_tool": MOCK_FEEDS,
        "octopus_interruptions_tool": MOCK_INTERRUPTIONS,
        "octopus_accounts_tool": MOCK_ACCOUNTS,
        "octopus_teams_tool": MOCK_TEAMS,
        "octopus_users_tool": MOCK_USERS,
        "octopus_tasks_tool": _tasks_payload(),
        "octopus_events_tool": _events_payload(),
    }
    patchers = []
    mocks = {}
    for name, retval in targets.items():
        p = patch(f"{MODULE}.{name}")
        m = p.start()
        patchers.append(p)
        mocks[name] = m
        if name == "octopus_variableset_tool":

            def _varset(args, config=None):
                vid = args["variable_set_id"]
                return MOCK_PROJECT_VARSET if "Projects" in vid else MOCK_LIBRARY_VARSET

            m.invoke.side_effect = _varset
        elif name == "octopus_deployment_process_tool":
            m.invoke.return_value = MOCK_PROCESS
        else:
            m.invoke.return_value = retval
    return patchers, mocks


def _make_agent():
    agent = OctopusAgent.__new__(OctopusAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    agent._active_run_id = uuid.uuid4()
    return agent


def _run_deep(engine):
    """Run _write_octopus_deep against a sqlite engine with tools mocked."""

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    settings = MagicMock()
    settings.octopus_fetch_workers = 4
    settings.octopus_history_days = 365
    settings.octopus_events_cap = 50000

    patchers, mocks = _patch_tools()
    try:
        with (
            patch(f"{MODULE}.get_session", _get_session),
            patch(f"{MODULE}.get_settings", return_value=settings),
        ):
            _make_agent()._write_octopus_deep("all")
    finally:
        for p in patchers:
            p.stop()
    return mocks


# --- tests ------------------------------------------------------------------


def test_deployment_steps_from_process(sqlite_engine):
    _run_deep(sqlite_engine)
    with Session(sqlite_engine) as s:
        steps = s.query(OctopusDeploymentStep).all()
        assert len(steps) == 1
        st = steps[0]
        assert st.project_octopus_id == "Projects-1"
        assert st.step_name == "Deploy Web"
        assert st.action_name == "Run Script"
        assert st.action_type == "Octopus.Script"
        assert st.target_roles == ["web", "app"]
        assert st.feed_id == "Feeds-1"
        assert st.package_id == "MyPkg"
        assert st.condition == "Success"
        # leftover (non-promoted) props land in details, never as a typed column
        assert "Octopus.Action.Script.ScriptBody" in (st.details or {})
        assert "Octopus.Action.Type" not in (st.details or {})


def test_variables_metadata_only_no_value(sqlite_engine):
    """SECURITY: variable Value is NEVER stored; is_sensitive preserved."""
    _run_deep(sqlite_engine)
    with Session(sqlite_engine) as s:
        proj_vars = s.query(OctopusVariable).filter_by(owner_type="project").all()
        names = {v.name for v in proj_vars}
        assert names == {"ApiKey", "Region"}
        api = next(v for v in proj_vars if v.name == "ApiKey")
        assert api.is_sensitive is True
        assert api.scope == {"Environment": ["Environments-1"]}
        # The model has no value column at all — and no attribute leaks the secret.
        assert not hasattr(api, "value")
        for v in proj_vars:
            for attr in vars(v).values():
                assert "super-secret-should-never-be-stored" != attr
        # Library variables captured with owner_type="library"
        lib_vars = s.query(OctopusVariable).filter_by(owner_type="library").all()
        assert {v.name for v in lib_vars} == {"SharedToken"}
        assert lib_vars[0].is_sensitive is True


def test_accounts_metadata_only_no_secret(sqlite_engine):
    """SECURITY: account secret/key material is NEVER stored."""
    _run_deep(sqlite_engine)
    with Session(sqlite_engine) as s:
        acct = s.query(OctopusAccount).one()
        assert acct.name == "deploy-svc"
        assert acct.account_type == "UsernamePassword"
        assert acct.username == "svc"
        assert acct.environment_ids == ["Environments-1"]
        # No password/secret attribute exists on the row.
        assert not hasattr(acct, "password")
        for attr in vars(acct).values():
            assert not (isinstance(attr, dict) and "HasValue" in attr)


def test_tasks_last_year_cutoff_excludes_old(sqlite_engine):
    _run_deep(sqlite_engine)
    with Session(sqlite_engine) as s:
        tasks = s.query(OctopusTask).all()
        ids = {t.octopus_id for t in tasks}
        assert "ServerTasks-1" in ids
        assert "ServerTasks-OLD" not in ids  # older than 365d → walk stopped
        t = next(t for t in tasks if t.octopus_id == "ServerTasks-1")
        assert t.task_type == "Deploy"
        assert t.state == "Success"
        assert t.finished_successfully is True


def test_feeds_roles_templates_teams_users_interruptions_libsets(sqlite_engine):
    _run_deep(sqlite_engine)
    with Session(sqlite_engine) as s:
        assert s.query(OctopusFeed).count() == 1
        assert s.query(OctopusFeed).one().feed_type == "NuGet"
        # dupe role collapses to 2 distinct
        roles = {r.role_name for r in s.query(OctopusMachineRole).all()}
        assert roles == {"web", "app"}
        assert s.query(OctopusActionTemplate).one().action_type == "Octopus.Script"
        assert s.query(OctopusActionTemplate).one().version == "3"
        assert s.query(OctopusTeam).one().name == "Ops"
        assert s.query(OctopusUser).one().email == "alice@x"
        i = s.query(OctopusInterruption).one()
        assert i.title == "Approve prod"
        assert i.is_pending is True
        lvs = s.query(OctopusLibraryVariableSet).one()
        assert lvs.name == "Shared"


def test_events_last_year_captured(sqlite_engine):
    _run_deep(sqlite_engine)
    with Session(sqlite_engine) as s:
        ev = s.query(OctopusEvent).one()
        assert ev.octopus_id == "Events-1"
        assert ev.category == "Deployed"
        assert ev.username == "alice"


def test_events_skipped_when_flag_off(sqlite_engine):
    """octopus_collect_events=False must skip the audit-events phase entirely:
    the events tool is never invoked and no OctopusEvent rows are written. The
    target's audit events embed cardholder-shaped data (Visa-IIN PANs) that DLP
    fail-closes on — see docs/READONLY-MODEL.md 'PCI/DLP in action'."""

    @contextmanager
    def _get_session():
        with Session(sqlite_engine) as s:
            yield s

    settings = MagicMock()
    settings.octopus_fetch_workers = 4
    settings.octopus_history_days = 365
    settings.octopus_events_cap = 50000
    settings.octopus_collect_events = False  # the gate under test

    patchers, mocks = _patch_tools()
    try:
        with (
            patch(f"{MODULE}.get_session", _get_session),
            patch(f"{MODULE}.get_settings", return_value=settings),
        ):
            _make_agent()._write_octopus_deep("all")
    finally:
        for p in patchers:
            p.stop()

    mocks["octopus_events_tool"].invoke.assert_not_called()
    with Session(sqlite_engine) as s:
        assert s.query(OctopusEvent).count() == 0
        # Non-event phases still ran (proves we skipped ONLY events).
        assert s.query(OctopusDeploymentStep).count() >= 1


def test_events_from_filter_passed(sqlite_engine):
    mocks = _run_deep(sqlite_engine)
    # The events tool must be invoked with a from_iso bounding date.
    call = mocks["octopus_events_tool"].invoke.call_args
    assert "from_iso" in call.args[0]
    from_iso = call.args[0]["from_iso"]
    assert from_iso  # non-empty ISO date


def test_events_from_filter_is_tz_naive_second_precision(sqlite_engine):
    """3.3.8 mis-parses the ``+00:00`` tz suffix (``+`` -> space), silently voiding
    ``from=`` -> full-history pagination -> timeout -> 0 events. The collector must
    send a TIMEZONE-NAIVE, second-precision value so the filter actually bounds."""
    mocks = _run_deep(sqlite_engine)
    from_iso = mocks["octopus_events_tool"].invoke.call_args.args[0]["from_iso"]
    # No timezone offset / "Z" — a "+" in the query is decoded as a space by 3.3.8.
    assert "+" not in from_iso
    assert not from_iso.endswith("Z")
    # Second precision, no microseconds (e.g. 2025-06-24T00:00:00).
    assert "." not in from_iso
    # Parses as a naive datetime (fromisoformat yields tzinfo is None).
    assert datetime.fromisoformat(from_iso).tzinfo is None


def test_events_fetch_failure_is_surfaced_not_swallowed(sqlite_engine):
    """A fetch-level events failure must RE-RAISE so _write_details flips the run to
    failed — not be swallowed as a silent 0 (the regression-hiding bug)."""
    import pytest

    agent = _make_agent()
    with (
        patch(f"{MODULE}.octopus_events_tool") as ev,
        Session(sqlite_engine) as session,
    ):
        ev.invoke.side_effect = RuntimeError("events endpoint timed out")
        cutoff = _now() - timedelta(days=365)
        with pytest.raises(RuntimeError, match="events endpoint timed out"):
            agent._deep_events(session, {"callbacks": []}, cutoff, 50000)


def test_deep_idempotent(sqlite_engine):
    _run_deep(sqlite_engine)
    _run_deep(sqlite_engine)  # second pass must not duplicate
    with Session(sqlite_engine) as s:
        assert s.query(OctopusDeploymentStep).count() == 1
        assert s.query(OctopusVariable).filter_by(owner_type="project").count() == 2
        assert s.query(OctopusFeed).count() == 1
        assert s.query(OctopusTask).count() == 1
        assert s.query(OctopusEvent).count() == 1
        assert s.query(OctopusAccount).count() == 1


def test_events_cap_truncates(sqlite_engine, caplog):
    """A tiny cap stops ingestion early and logs the truncation."""
    import logging

    caplog.set_level(logging.INFO, logger=MODULE)

    @contextmanager
    def _get_session():
        with Session(sqlite_engine) as s:
            yield s

    many = [
        {
            "Id": f"Events-{i}",
            "Category": "X",
            "Occurred": _iso(_now() - timedelta(days=1)),
            "Message": "m",
        }
        for i in range(5)
    ]
    settings = MagicMock()
    settings.octopus_fetch_workers = 2
    settings.octopus_history_days = 365
    settings.octopus_events_cap = 2  # cap below the 5 available

    patchers, mocks = _patch_tools()
    mocks["octopus_events_tool"].invoke.return_value = many
    try:
        with (
            patch(f"{MODULE}.get_session", _get_session),
            patch(f"{MODULE}.get_settings", return_value=settings),
        ):
            _make_agent()._write_octopus_deep("all")
    finally:
        for p in patchers:
            p.stop()

    with Session(sqlite_engine) as s:
        assert s.query(OctopusEvent).count() == 2  # capped
    assert any("truncated=True" in r.message for r in caplog.records)

    from infra_brain.db.models import AgentActionLog

    with Session(sqlite_engine) as s:
        rows = s.query(AgentActionLog).filter_by(tool="truncation").all()
        assert len(rows) == 1
        row = rows[0]
        assert row.verdict == "allow"
        assert row.status == "ok"
        assert row.domain == "octopus"
        summary = json.loads(row.args_summary)
        assert summary["cap"] == 2
        assert summary["dropped_count"] == 3  # 5 available - 2 captured
        assert summary["entity"] == "octopus_events"
        assert row.run_id is not None


def test_events_cap_drop_count_excludes_out_of_window_events(sqlite_engine, caplog):
    """dropped_count must only count events that pass the cutoff-window filter
    AND were cut by the cap — not every remaining raw row. Mixes window-excluded
    events (older than cutoff) in among the cap-dropped events to prove the count
    isn't the naive `len(events) - n` (the 92k-event Octopus bug scenario)."""
    import logging

    caplog.set_level(logging.INFO, logger=MODULE)

    @contextmanager
    def _get_session():
        with Session(sqlite_engine) as s:
            yield s

    # Feed is newest-first: 4 in-window events, then 10 events older than the
    # cutoff (window-excluded). Cap=2 means only 2 in-window events are kept;
    # 2 more in-window events are dropped by the cap; the 10 stale/out-of-window
    # events must NOT be counted as "dropped by the cap".
    in_window = [
        {
            "Id": f"Events-fresh-{i}",
            "Category": "X",
            "Occurred": _iso(_now() - timedelta(days=1)),
            "Message": "m",
        }
        for i in range(4)
    ]
    out_of_window = [
        {
            "Id": f"Events-stale-{i}",
            "Category": "X",
            "Occurred": _iso(_now() - timedelta(days=800)),
            "Message": "m",
        }
        for i in range(10)
    ]
    many = in_window + out_of_window

    settings = MagicMock()
    settings.octopus_fetch_workers = 2
    settings.octopus_history_days = 365
    settings.octopus_events_cap = 2  # cap below the 4 in-window events

    patchers, mocks = _patch_tools()
    mocks["octopus_events_tool"].invoke.return_value = many
    try:
        with (
            patch(f"{MODULE}.get_session", _get_session),
            patch(f"{MODULE}.get_settings", return_value=settings),
        ):
            _make_agent()._write_octopus_deep("all")
    finally:
        for p in patchers:
            p.stop()

    with Session(sqlite_engine) as s:
        assert s.query(OctopusEvent).count() == 2  # capped

    from infra_brain.db.models import AgentActionLog

    with Session(sqlite_engine) as s:
        rows = s.query(AgentActionLog).filter_by(tool="truncation").all()
        assert len(rows) == 1
        summary = json.loads(rows[0].args_summary)
        assert summary["cap"] == 2
        # Only the 2 remaining in-window events are dropped by the cap — the
        # 10 out-of-window events must not inflate the count.
        assert summary["dropped_count"] == 2
        assert rows[0].run_id is not None


def test_events_under_cap_writes_no_truncation_row(sqlite_engine):
    """No AgentActionLog truncation row is written when events stay under cap."""
    from infra_brain.db.models import AgentActionLog

    @contextmanager
    def _get_session():
        with Session(sqlite_engine) as s:
            yield s

    settings = MagicMock()
    settings.octopus_fetch_workers = 2
    settings.octopus_history_days = 365
    settings.octopus_events_cap = 50000  # well above available events

    patchers, mocks = _patch_tools()
    try:
        with (
            patch(f"{MODULE}.get_session", _get_session),
            patch(f"{MODULE}.get_settings", return_value=settings),
        ):
            _make_agent()._write_octopus_deep("all")
    finally:
        for p in patchers:
            p.stop()

    with Session(sqlite_engine) as s:
        assert s.query(AgentActionLog).filter_by(tool="truncation").count() == 0


def test_deployment_resource_id_set_from_project_map(session_patcher):
    """octopus_deployments.resource_id is linked from the project's Resource."""
    from infra_brain.db.models import Resource

    with session_patcher(MODULE) as engine:
        # Seed the project's Resource row so _resource_id resolves it.
        with Session(engine) as s:
            s.add(
                Resource(
                    id=uuid.uuid4(),
                    domain="octopus",
                    type="octopus_project",
                    name="Infra App",
                    source="OctopusAgent",
                    zone="corpor",
                    last_seen=_now(),
                    metadata_={},
                )
            )
            s.commit()

        agent = _make_agent()
        agent._last_projects = MOCK_PROJECTS
        agent._last_envs = []
        agent._last_machines = []
        agent._last_server = None

        dash = {
            "Items": [
                {
                    "Id": "Deployments-1",
                    "ProjectId": "Projects-1",
                    "EnvironmentId": "Environments-1",
                    "State": "Success",
                }
            ]
        }
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
            e.invoke.return_value = []
            mch.invoke.return_value = []
            pr.invoke.return_value = MOCK_PROJECTS
            ch.invoke.return_value = []
            rel.invoke.return_value = {"Items": []}
            dsh.invoke.return_value = dash
            agent._write_octopus_details("all")

        with Session(engine) as s:
            dep = s.query(OctopusDeployment).one()
            proj_res = (
                s.query(Resource)
                .filter_by(domain="octopus", type="octopus_project", name="Infra App")
                .one()
            )
            assert dep.resource_id == proj_res.id


def test_write_details_surfaces_deep_failure(sqlite_engine):
    """A failure in the deep phase must flip the CollectionRun to failed."""
    from infra_brain.agents.base import CollectionResult

    engine = sqlite_engine
    run_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id,
                domain="octopus",
                trigger_type="scheduled",
                trigger_source="all",
                status="completed",
            )
        )
        s.commit()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _make_agent()
    result = CollectionResult(
        run_id=run_id, domain="octopus", resources_found=1, drift_count=0, status="completed"
    )

    def _boom():
        raise RuntimeError("simulated deep failure")

    with patch("infra_brain.etl.base.get_session", _get_session):
        agent._write_details(result, _boom)

    assert result.status == "failed"
    assert any("simulated deep failure" in e for e in result.errors)
    with Session(engine) as s:
        assert s.get(CollectionRun, run_id).status == "failed"
