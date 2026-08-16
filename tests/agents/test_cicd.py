import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.base import CollectionResult
from infra_brain.agents.cicd import CICDAgent, _classify_gitlab_error, _parse_dt
from infra_brain.db.models import CiPipelineRun, GitlabProject, Resource


def _session_cm(engine):
    @contextmanager
    def _cm():
        with Session(engine) as s:
            yield s

    return _cm


MOCK_PROJECTS = [
    {
        "id": 1,
        "name": "infra-ops",
        "default_branch": "main",
        "last_activity_at": "2026-06-18T00:00:00Z",
    }
]
MOCK_PIPELINES = [
    {"id": 100, "status": "success", "ref": "main", "created_at": "2026-06-18T00:00:00Z"}
]


def test_cicd_agent_collect():
    agent = CICDAgent.__new__(CICDAgent)
    agent.settings = MagicMock()
    agent.callbacks = []

    # CICDAgent.collect calls the raw helpers directly: gitlab_get_paginated for the
    # project list and gitlab_get for each project's pipelines.
    with (
        patch(
            "infra_brain.agents.cicd.gitlab_get_paginated", return_value=MOCK_PROJECTS
        ) as mock_projects,
        patch("infra_brain.agents.cicd.gitlab_get", return_value=MOCK_PIPELINES),
    ):
        outcome = agent.collect(scope="all")

    assert len(outcome.items) == 1
    assert outcome.items[0]["name"] == "infra-ops"
    assert outcome.items[0]["data"]["last_pipeline_status"] == "success"
    assert outcome.errors == []
    mock_projects.assert_called_once()


def test_cicd_agent_collect_disambiguates_same_name_different_group():
    """L-8b: two projects sharing a bare name in different GitLab groups must
    NOT collapse into one Resource identity. The generic item's ``name`` (the
    field the base run() upserts Resource.name from -- see
    ETLConnector._resource_id's (domain, type, name) key) must be unique per
    project, keyed on ``path_with_namespace`` rather than the bare name."""
    projects = [
        {
            "id": 1,
            "name": "backend",
            "path_with_namespace": "team-a/backend",
            "default_branch": "main",
            "last_activity_at": "2026-06-18T00:00:00Z",
        },
        {
            "id": 2,
            "name": "backend",
            "path_with_namespace": "team-b/backend",
            "default_branch": "main",
            "last_activity_at": "2026-06-18T00:00:00Z",
        },
    ]
    agent = CICDAgent.__new__(CICDAgent)
    agent.settings = MagicMock()
    agent.callbacks = []

    with (
        patch("infra_brain.agents.cicd.gitlab_get_paginated", return_value=projects),
        patch("infra_brain.agents.cicd.gitlab_get", return_value=[]),
    ):
        outcome = agent.collect(scope="all")

    assert len(outcome.items) == 2
    names = {item["name"] for item in outcome.items}
    assert names == {"team-a/backend", "team-b/backend"}


def test_cicd_agent_collect_falls_back_to_bare_name_without_path():
    """Back-compat: a project dict lacking ``path_with_namespace`` (older
    mocked API shapes, or a GitLab instance that omits it) still falls back
    to the bare name -- MOCK_PROJECTS above has no path_with_namespace and
    test_cicd_agent_collect already covers the single-project case; this
    covers the fallback explicitly by name so it can't regress silently."""
    project = {
        "id": 9,
        "name": "solo-project",
        "default_branch": "main",
        "last_activity_at": "2026-06-18T00:00:00Z",
    }
    assert "path_with_namespace" not in project
    agent = CICDAgent.__new__(CICDAgent)
    agent.settings = MagicMock()
    agent.callbacks = []

    with (
        patch("infra_brain.agents.cicd.gitlab_get_paginated", return_value=[project]),
        patch("infra_brain.agents.cicd.gitlab_get", return_value=[]),
    ):
        outcome = agent.collect(scope="all")

    assert outcome.items[0]["name"] == "solo-project"


def test_cicd_agent_collect_raises_on_total_listing_failure():
    """When the project fetch raises, collect() must propagate (F-007: status="failed")."""
    agent = CICDAgent.__new__(CICDAgent)
    agent.settings = MagicMock()
    agent.callbacks = []

    with (
        patch(
            "infra_brain.agents.cicd.gitlab_get_paginated",
            side_effect=RuntimeError("network error"),
        ),
        pytest.raises(RuntimeError, match="network error"),
    ):
        agent.collect(scope="all")


def test_cicd_domain():
    assert CICDAgent.domain == "cicd"


def test_cicd_agent_collect_empty_when_no_projects():
    """No projects → collect() returns [] without touching the pipeline endpoint."""
    agent = CICDAgent.__new__(CICDAgent)
    agent.settings = MagicMock()
    agent.callbacks = []

    with (
        patch("infra_brain.agents.cicd.gitlab_get_paginated", return_value=[]),
        patch("infra_brain.agents.cicd.gitlab_get") as mock_pipelines,
    ):
        outcome = agent.collect(scope="all")

    assert outcome.items == []
    assert outcome.errors == []
    mock_pipelines.assert_not_called()


def test_cicd_agent_collect_survives_pipeline_detail_fetch_failure():
    """#77 follow-up: a failing PER-PIPELINE DETAIL fetch (the inner try/except
    at cicd.py's per-pipeline loop, untouched by the #77 fix) still yields the
    project row, falling back to the list-endpoint summary status rather than
    dropping the pipeline. This is the deliberate degrade the inner catch exists
    for; it is distinct from the INITIAL listing-call failure covered by
    test_pipeline_listing_failure_is_recorded_not_swallowed below, which must now
    surface via errors/partial status instead of being swallowed."""
    agent = CICDAgent.__new__(CICDAgent)
    agent.settings = MagicMock()
    agent.callbacks = []

    def _fake_get(url):
        if "/pipelines?per_page=5" in url:
            return MOCK_PIPELINES
        # Per-pipeline detail lookup fails; inner catch falls back to the
        # list-endpoint summary row instead of dropping the pipeline.
        raise RuntimeError("pipeline detail 500")

    with (
        patch("infra_brain.agents.cicd.gitlab_get_paginated", return_value=MOCK_PROJECTS),
        patch("infra_brain.agents.cicd.gitlab_get", side_effect=_fake_get),
    ):
        outcome = agent.collect(scope="all")

    assert len(outcome.items) == 1
    assert outcome.items[0]["name"] == "infra-ops"
    # Falls back to the list-endpoint summary row (MOCK_PIPELINES has status "success").
    assert outcome.items[0]["data"]["last_pipeline_status"] == "success"
    assert outcome.errors == []


def test_pipeline_listing_failure_is_recorded_not_swallowed(monkeypatch):
    """#77: a project whose initial pipeline-listing call raises must show up
    in CollectOutcome.errors (and thus drive status='partial'), not silently
    succeed with last_pipeline_status='unknown'."""
    agent = CICDAgent.__new__(CICDAgent)
    agent.settings = None
    agent.callbacks = []

    projects = [{"id": 1, "name": "broken-project", "default_branch": "main", "last_activity_at": ""}]

    def _fake_get_paginated(url):
        return projects

    def _fake_get(url):
        if "/pipelines?per_page=5" in url:
            raise RuntimeError("GitLab API 500")
        raise AssertionError(f"unexpected gitlab_get call: {url}")

    monkeypatch.setattr("infra_brain.agents.cicd.gitlab_get_paginated", _fake_get_paginated)
    monkeypatch.setattr("infra_brain.agents.cicd.gitlab_get", _fake_get)
    monkeypatch.setattr("infra_brain.agents.cicd.observe_pool", lambda *a, **k: None)

    outcome = agent.collect()

    assert outcome.errors, "expected the listing failure to be recorded in errors"
    assert any("broken-project" in e for e in outcome.errors)


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://gitlab.example.com/api/v4/projects/1/pipelines")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status} error", request=request, response=response)


def test_classify_gitlab_error_forbidden_vs_rate_limited_and_timeout():
    """TRK-191: a 403 must classify distinctly from a 429 and from a timeout —
    the whole point of the fix is that an operator can tell "needs a token
    permission fix" (forbidden) apart from "will probably resolve itself on
    retry" (rate_limited / timeout) without reading raw logs."""
    forbidden = _classify_gitlab_error(_http_error(403))
    rate_limited = _classify_gitlab_error(_http_error(429))
    timeout = _classify_gitlab_error(httpx.ReadTimeout("timed out"))

    assert forbidden == "forbidden"
    assert rate_limited == "rate_limited"
    assert timeout == "timeout"
    # The core assertion: 403 is NOT lumped in with the transient classes.
    assert forbidden != rate_limited
    assert forbidden != timeout


def test_pipeline_listing_403_is_classified_as_forbidden_not_generic(monkeypatch):
    """TRK-191 end-to-end: a 403 from the GitLab client during CICDAgent.collect()
    must be recorded with reason=forbidden, distinguishable from how a 429 or a
    timeout on the same call site would be recorded."""
    agent = CICDAgent.__new__(CICDAgent)
    agent.settings = None
    agent.callbacks = []

    projects = [
        {"id": 1, "name": "member-below-reporter", "default_branch": "main", "last_activity_at": ""}
    ]

    def _fake_get_paginated(url):
        return projects

    def _fake_get_403(url):
        if "/pipelines?per_page=5" in url:
            raise _http_error(403)
        raise AssertionError(f"unexpected gitlab_get call: {url}")

    monkeypatch.setattr("infra_brain.agents.cicd.gitlab_get_paginated", _fake_get_paginated)
    monkeypatch.setattr("infra_brain.agents.cicd.gitlab_get", _fake_get_403)
    monkeypatch.setattr("infra_brain.agents.cicd.observe_pool", lambda *a, **k: None)

    outcome_403 = agent.collect()

    assert outcome_403.errors
    assert any("reason=forbidden" in e for e in outcome_403.errors)
    assert not any("reason=rate_limited" in e for e in outcome_403.errors)
    assert not any("reason=timeout" in e for e in outcome_403.errors)

    def _fake_get_429(url):
        if "/pipelines?per_page=5" in url:
            raise _http_error(429)
        raise AssertionError(f"unexpected gitlab_get call: {url}")

    monkeypatch.setattr("infra_brain.agents.cicd.gitlab_get", _fake_get_429)
    outcome_429 = agent.collect()

    assert outcome_429.errors
    assert any("reason=rate_limited" in e for e in outcome_429.errors)
    assert not any("reason=forbidden" in e for e in outcome_429.errors)


# --- detail-write path: gitlab_projects + ci_pipeline_runs ---

_FULL_PROJECT = {
    "id": 7,
    "name": "infra-ops",
    "default_branch": "main",
    "path_with_namespace": "grp/infra-ops",
    "visibility": "private",
    "archived": False,
    "namespace": {"id": 42, "name": "grp"},
    "last_activity_at": "2026-06-18T00:00:00Z",
    "web_url": "http://gl/infra-ops",
}
_FULL_PIPELINES = [
    {
        "id": 100,
        "ref": "main",
        "sha": "abc123",
        "status": "success",
        "source": "push",
        "created_at": "2026-06-18T00:00:00Z",
        "updated_at": "2026-06-18T01:00:00Z",
        "duration": 42,
        "web_url": "http://gl/infra-ops/-/pipelines/100",
    }
]


def test_cicd_parse_dt():
    assert _parse_dt("2026-06-18T00:00:00Z") is not None
    assert _parse_dt("") is None
    assert _parse_dt(None) is None
    assert _parse_dt("not-a-date") is None


def test_cicd_write_details_populates_typed_tables(make_agent, sqlite_engine, session_patcher):
    agent = make_agent(CICDAgent)
    agent._last_items = [
        {
            "name": "infra-ops",
            "type": "gitlab_project",
            "data": {},
            "_project": _FULL_PROJECT,
            "_pipelines": _FULL_PIPELINES,
        }
    ]
    # Seed the generic Resource so the detail rows pick up resource_id.
    # L-8b: Resource identity is keyed on path_with_namespace (not the bare
    # project name) so two same-named projects in different groups don't
    # collapse — must match _FULL_PROJECT's "grp/infra-ops" here too.
    with Session(sqlite_engine) as s:
        s.add(
            Resource(
                domain="cicd", type="gitlab_project", name="grp/infra-ops", source="CICDAgent"
            )
        )
        s.commit()

    with session_patcher("infra_brain.agents.cicd"):
        agent._write_cicd_details()

    with Session(sqlite_engine) as v:
        gp = v.query(GitlabProject).one()
        assert gp.gitlab_project_id == 7
        assert gp.group_id == 42
        assert gp.visibility == "private"
        assert gp.path_with_namespace == "grp/infra-ops"
        assert gp.last_pipeline_id == 100
        assert gp.last_activity_at is not None
        assert gp.resource_id is not None
        assert gp.details["web_url"] == "http://gl/infra-ops"

        cp = v.query(CiPipelineRun).one()
        assert cp.pipeline_id == 100
        assert cp.sha == "abc123"
        assert cp.source == "push"
        assert cp.duration == 42
        assert cp.created_at is not None
        assert cp.updated_at is not None


def test_cicd_write_details_idempotent(make_agent, sqlite_engine, session_patcher):
    agent = make_agent(CICDAgent)
    agent._last_items = [
        {
            "name": "infra-ops",
            "type": "gitlab_project",
            "data": {},
            "_project": _FULL_PROJECT,
            "_pipelines": _FULL_PIPELINES,
        }
    ]
    with session_patcher("infra_brain.agents.cicd"):
        agent._write_cicd_details()
        agent._write_cicd_details()

    with Session(sqlite_engine) as v:
        assert v.query(GitlabProject).count() == 1
        assert v.query(CiPipelineRun).count() == 1


def test_cicd_write_details_returns_row_count(make_agent, sqlite_engine, session_patcher):
    """TRK-052: `_write_cicd_details` must return the written-row count so
    `BaseAgent._write_details` can populate `CollectionRun.detail_rows_written`
    (see the octopus/linux/windows agents for the established pattern)."""
    agent = make_agent(CICDAgent)
    agent._last_items = [
        {
            "name": "infra-ops",
            "type": "gitlab_project",
            "data": {},
            "_project": _FULL_PROJECT,
            "_pipelines": _FULL_PIPELINES,
        }
    ]
    with session_patcher("infra_brain.agents.cicd"):
        count = agent._write_cicd_details()

    assert isinstance(count, int)
    assert count > 0


def test_cicd_run_surfaces_detail_write_failure(make_agent, sqlite_engine, session_patcher):
    """A structural detail-write failure must flip the run to failed, not stay silent."""
    agent = make_agent(CICDAgent)
    result = CollectionResult(
        run_id=uuid.uuid4(), domain="cicd", resources_found=0, drift_count=0, status="completed"
    )

    def _boom():
        raise RuntimeError("db exploded")

    # _write_details is the BaseAgent guard; a boom must surface on the result and
    # be recorded on the CollectionRun (here the run row is absent → still no raise).
    with patch("infra_brain.etl.base.get_session", _session_cm(sqlite_engine)):
        agent._write_details(result, _boom)

    assert result.status == "failed"
    assert any("db exploded" in e for e in result.errors)
