import uuid
from unittest.mock import patch

import pytest

from sqlalchemy.orm import Session

from infra_brain.agents.octopus import OctopusAgent, _parse_dt
from infra_brain.etl.base import CollectorSkipped
from infra_brain.db.models import (
    OctopusChannel,
    OctopusDeployment,
    OctopusEnvironment,
    OctopusMachine,
    OctopusProject,
    OctopusRelease,
    Resource,
)

MOCK_SERVER_INFO = {"Version": "3.3.8", "Edition": "Community"}
MOCK_PROJECTS = [
    {
        "Id": "Projects-1",
        "Name": "Infra App",
        "Slug": "infra-app",
        "LifecycleId": "Lifecycles-1",
        "ProjectGroupId": "ProjectGroups-1",
    }
]
MOCK_ENVIRONMENTS = [{"Id": "Environments-1", "Name": "Production", "SortOrder": 0}]
MOCK_MACHINES = [
    {
        "Id": "Machines-1",
        "Name": "web-01",
        "Status": "Online",
        "StatusSummary": "Octopus was able to successfully establish a connection.",
        "EnvironmentIds": ["Environments-1"],
        "Roles": ["web"],
        "Endpoint": {
            "CommunicationStyle": "TentaclePassive",
            "Uri": "https://tentacle:10933",
            "TentacleVersionDetails": {"Version": "3.3.8"},
        },
        "IsDisabled": False,
    }
]


def test_octopus_agent_collect(make_agent):
    """collect() now emits generic Resource rows only (rich data lives in run())."""
    agent = make_agent(OctopusAgent)
    agent.settings.octopus_url = "https://octopus.example.com"

    with (
        patch("infra_brain.agents.octopus.octopus_server_info_tool") as mock_server,
        patch("infra_brain.agents.octopus.octopus_environments_tool") as mock_envs,
        patch("infra_brain.agents.octopus.octopus_machines_tool") as mock_machines,
        patch("infra_brain.agents.octopus.octopus_projects_tool") as mock_projects,
    ):
        mock_server.invoke.return_value = MOCK_SERVER_INFO
        mock_envs.invoke.return_value = MOCK_ENVIRONMENTS
        mock_machines.invoke.return_value = MOCK_MACHINES
        mock_projects.invoke.return_value = MOCK_PROJECTS
        outcome = agent.collect(scope="all")

    items = outcome.items
    assert outcome.errors == []
    types = {i["type"] for i in items}
    assert "octopus_project" in types
    assert "octopus_environment" in types
    assert "octopus_machine" in types
    assert "octopus_server" in types

    names = [i["name"] for i in items]
    assert "Infra App" in names
    assert "octopus-server" in names
    assert "web-01" in names
    assert "Production" in names

    # Server row stores Version only (no edition in the rich model).
    server_item = next(i for i in items if i["type"] == "octopus_server")
    assert server_item["data"]["version"] == "3.3.8"
    assert "edition" not in server_item["data"]

    # Project row carries minimal metadata; rich/resolved data goes to detail tables.
    project_item = next(i for i in items if i["type"] == "octopus_project")
    assert project_item["data"]["project_id"] == "Projects-1"
    assert "deployments" not in project_item["data"]


def test_octopus_domain(make_agent):
    assert make_agent(OctopusAgent).domain == "octopus"


def test_octopus_agent_collect_empty(make_agent):
    """Empty Octopus server: every tool returns nothing → only server row (from {})."""
    agent = make_agent(OctopusAgent)
    agent.settings.octopus_url = "https://octopus.example.com"

    with (
        patch("infra_brain.agents.octopus.octopus_server_info_tool") as mock_server,
        patch("infra_brain.agents.octopus.octopus_environments_tool") as mock_envs,
        patch("infra_brain.agents.octopus.octopus_machines_tool") as mock_machines,
        patch("infra_brain.agents.octopus.octopus_projects_tool") as mock_projects,
    ):
        mock_server.invoke.return_value = {}
        mock_envs.invoke.return_value = []
        mock_machines.invoke.return_value = []
        mock_projects.invoke.return_value = []
        outcome = agent.collect(scope="all")

    assert {i["type"] for i in outcome.items} <= {"octopus_server"}


def test_octopus_agent_collect_tool_exception_degrades(make_agent):
    """A failing tool is logged and skipped; collect() still returns the rest, never raises."""
    agent = make_agent(OctopusAgent)
    agent.settings.octopus_url = "https://octopus.example.com"

    with (
        patch("infra_brain.agents.octopus.octopus_server_info_tool") as mock_server,
        patch("infra_brain.agents.octopus.octopus_environments_tool") as mock_envs,
        patch("infra_brain.agents.octopus.octopus_machines_tool") as mock_machines,
        patch("infra_brain.agents.octopus.octopus_projects_tool") as mock_projects,
    ):
        mock_server.invoke.side_effect = RuntimeError("octopus 500")
        mock_envs.invoke.return_value = MOCK_ENVIRONMENTS
        mock_machines.invoke.return_value = MOCK_MACHINES
        mock_projects.invoke.return_value = []
        outcome = agent.collect(scope="all")  # must not raise

    names = [i["name"] for i in outcome.items]
    assert "octopus-server" not in names  # failed section skipped
    assert "web-01" in names  # surviving sections still collected
    assert "Production" in names
    # F-007: the server-info failure must be recorded, not silently swallowed.
    assert len(outcome.errors) == 1
    assert "server info failed" in outcome.errors[0]


def test_parse_dt_handles_octopus_iso_and_z():
    # 3.3.8 offset form
    assert _parse_dt("2018-04-03T19:34:12.308+00:00") is not None
    # Z form (test/fixture)
    assert _parse_dt("2026-06-18T00:00:00Z") is not None
    # garbage / empty / non-string → None
    assert _parse_dt("") is None
    assert _parse_dt(None) is None


# --- TRK-245: detail_rows_written regression ------------------------------
#
# A live-data audit found the octopus domain always reported
# CollectionRun.detail_rows_written=0 despite genuinely writing real detail
# rows (392 octopus_machines, 1767 octopus_deployments, 3151 octopus_releases,
# 42 octopus_environments, all current). Root cause: `_write_octopus_details`
# and `_write_octopus_deep` fell off the end of the function with an implicit
# `None` return — `BaseAgent._write_details` only accumulates
# `result.detail_rows_written` when the detail-writer function returns an
# `int` (see etl/base.py's `_write_details` docstring), so the real writes
# never moved the counter. These tests lock in the fix: both writers must now
# return the actual row count, matching what lands in the DB.


def test_octopus_write_details_returns_row_count(make_agent, sqlite_engine, session_patcher):
    """`_write_octopus_details` must return the written-row count so
    `BaseAgent._write_details` can populate `CollectionRun.detail_rows_written`
    (the same established pattern as cicd/vuln — see their equivalent tests)."""
    agent = make_agent(OctopusAgent)
    agent._last_envs = MOCK_ENVIRONMENTS
    agent._last_machines = MOCK_MACHINES
    agent._last_projects = MOCK_PROJECTS
    agent._last_server = MOCK_SERVER_INFO

    # Pre-seed the generic Resource rows collect() would already have upserted
    # — _write_octopus_details links each detail row to its Resource via
    # _resource_id(domain, type, name); without these rows every detail write
    # is guarded out (rid is None) and never reaches the DB.
    with Session(sqlite_engine) as seed:
        seed.add_all(
            [
                Resource(
                    id=uuid.uuid4(),
                    domain="octopus",
                    type="octopus_environment",
                    name="Production",
                    source="octopus",
                ),
                Resource(
                    id=uuid.uuid4(),
                    domain="octopus",
                    type="octopus_machine",
                    name="web-01",
                    source="octopus",
                ),
                Resource(
                    id=uuid.uuid4(),
                    domain="octopus",
                    type="octopus_project",
                    name="Infra App",
                    source="octopus",
                ),
                Resource(
                    id=uuid.uuid4(),
                    domain="octopus",
                    type="octopus_server",
                    name="octopus-server",
                    source="octopus",
                ),
            ]
        )
        seed.commit()

    mock_channels = [{"Id": "Channels-1", "Name": "Default", "IsDefault": True}]
    mock_releases = {
        "Items": [
            {
                "Id": "Releases-1",
                "ProjectId": "Projects-1",
                "Version": "1.0.0",
                "ChannelId": "Channels-1",
                "Assembled": "2026-07-01T00:00:00Z",
            }
        ]
    }
    mock_dashboard = {
        "Items": [
            {
                "Id": "Deployments-1",
                "ProjectId": "Projects-1",
                "ReleaseId": "Releases-1",
                "ReleaseVersion": "1.0.0",
                "EnvironmentId": "Environments-1",
                "TaskId": "ServerTasks-1",
                "State": "Success",
                "Created": "2026-07-01T00:00:00Z",
                "CompletedTime": "2026-07-01T00:05:00Z",
                "Duration": "00:05:00",
            }
        ]
    }

    with (
        session_patcher("infra_brain.agents.octopus"),
        patch("infra_brain.agents.octopus.octopus_projectgroups_tool") as mock_groups,
        patch("infra_brain.agents.octopus.octopus_lifecycles_tool") as mock_lifecycles,
        patch("infra_brain.agents.octopus.octopus_channels_tool") as mock_channels_tool,
        patch("infra_brain.agents.octopus.octopus_releases_tool") as mock_releases_tool,
        patch("infra_brain.agents.octopus.octopus_dashboard_tool") as mock_dashboard_tool,
    ):
        mock_groups.invoke.return_value = []
        mock_lifecycles.invoke.return_value = []
        mock_channels_tool.invoke.return_value = mock_channels
        mock_releases_tool.invoke.return_value = mock_releases
        mock_dashboard_tool.invoke.return_value = mock_dashboard

        count = agent._write_octopus_details(scope="all")

    assert isinstance(count, int)
    assert count > 0

    with Session(sqlite_engine) as v:
        actual = (
            v.query(OctopusEnvironment).count()
            + v.query(OctopusMachine).count()
            + v.query(OctopusProject).count()
            + v.query(OctopusChannel).count()
            + v.query(OctopusRelease).count()
            + v.query(OctopusDeployment).count()
        )
    assert actual > 0
    assert count == actual


def test_octopus_write_deep_returns_row_count(make_agent, sqlite_engine, session_patcher):
    """`_write_octopus_deep` must likewise return a non-zero int row count —
    it fell off the end with an implicit ``None`` return before the fix, so
    every one of its twelve deep tables silently contributed 0 to
    ``detail_rows_written`` even on a run that wrote real rows."""
    agent = make_agent(OctopusAgent)
    agent.settings.octopus_fetch_workers = 2
    agent.settings.octopus_history_days = 365
    agent.settings.octopus_events_cap = 50000
    agent.settings.octopus_collect_events = False
    agent._last_projects = []

    with (
        session_patcher("infra_brain.agents.octopus"),
        patch("infra_brain.agents.octopus.octopus_projects_tool") as mock_projects,
        patch("infra_brain.agents.octopus.octopus_machine_roles_tool") as mock_roles,
        patch("infra_brain.agents.octopus.octopus_feeds_tool") as mock_feeds,
        patch("infra_brain.agents.octopus.octopus_action_templates_tool") as mock_templates,
        patch("infra_brain.agents.octopus.octopus_accounts_tool") as mock_accounts,
        patch("infra_brain.agents.octopus.octopus_teams_tool") as mock_teams,
        patch("infra_brain.agents.octopus.octopus_users_tool") as mock_users,
        patch("infra_brain.agents.octopus.octopus_interruptions_tool") as mock_interruptions,
        patch(
            "infra_brain.agents.octopus.octopus_library_variable_sets_tool"
        ) as mock_libsets,
        patch("infra_brain.agents.octopus.octopus_tasks_tool") as mock_tasks,
    ):
        mock_projects.invoke.return_value = []
        mock_roles.invoke.return_value = ["web", "app"]
        mock_feeds.invoke.return_value = []
        mock_templates.invoke.return_value = []
        mock_accounts.invoke.return_value = []
        mock_teams.invoke.return_value = []
        mock_users.invoke.return_value = []
        mock_interruptions.invoke.return_value = []
        mock_libsets.invoke.return_value = []
        mock_tasks.invoke.return_value = []

        count = agent._write_octopus_deep(scope="all")

    assert isinstance(count, int)
    # Only machine_roles produced rows in this scenario (2 distinct roles) —
    # every other phase is empty, matching the real bug scenario where a
    # non-zero contribution from any single phase must surface in the total.
    assert count == 2
    assert _parse_dt("not-a-date") is None


def test_octopus_collect_self_skips_when_unconfigured(make_agent):
    """An unconfigured Octopus must self-skip, not fail.

    Regression: octopus.py was the one collector with no CollectorSkipped
    guard, so with OCTOPUS_URL unset each tool call failed on its own and
    BaseAgent.run() recorded status="failed" — 6 permanent failures in 7 days
    for an integration that was simply never set up, indistinguishable from a
    genuinely broken collector.
    """
    agent = make_agent(OctopusAgent)
    agent.settings.octopus_url = ""

    with pytest.raises(CollectorSkipped) as exc:
        agent.collect(scope="all")
    assert "octopus_url not configured" in str(exc.value)


def test_octopus_collect_self_skips_when_url_is_whitespace(make_agent):
    """A whitespace-only URL is unconfigured too, not a valid endpoint."""
    agent = make_agent(OctopusAgent)
    agent.settings.octopus_url = "   "

    with pytest.raises(CollectorSkipped):
        agent.collect(scope="all")
