"""Tests for the IaC deep-view endpoints (UI Batch 6 / GitLab #62): Compose
service, K8s manifest resource, Terraform resource, Ansible inventory/
playbook structure, and CI schedule listings.

Mirrors test_dashboard_fleet_software_cve.py's conventions: in-memory
SQLite, ORM schema via Base.metadata.create_all, get_session patched to the
test engine, auth-off via INFRA_BRAIN_DEV=1.
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    AnsibleInventoryGroup,
    AnsibleInventoryHost,
    AnsiblePlaybookPlay,
    CiSchedule,
    ComposeService,
    GitlabProject,
    IacFile,
    K8sManifestResource,
    Resource,
    TerraformResource,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def client(engine, monkeypatch):
    from infra_brain.config import get_settings

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    from infra_brain.api.routers.iac import iac_router

    app = FastAPI()
    app.include_router(iac_router)
    with patch("infra_brain.api.routers.iac.get_session", _get_session):
        yield TestClient(app)


def _seed(engine):
    with Session(engine) as s:
        proj = GitlabProject(gitlab_project_id=1, name="infra-repo", path_with_namespace="ops/infra-repo")
        s.add(proj)
        s.flush()
        compose_file = IacFile(
            gitlab_project_id=1, project_name="infra-repo", path="docker-compose.yml",
            file_type="compose", ref="main",
        )
        k8s_file = IacFile(
            gitlab_project_id=1, project_name="infra-repo", path="k8s/deploy.yaml",
            file_type="k8s_manifest", ref="main",
        )
        tf_file = IacFile(
            gitlab_project_id=1, project_name="infra-repo", path="main.tf",
            file_type="terraform", ref="main",
        )
        ansible_file = IacFile(
            gitlab_project_id=1, project_name="infra-repo", path="site.yml",
            file_type="playbook", ref="main",
        )
        inv_file = IacFile(
            gitlab_project_id=1, project_name="infra-repo", path="inventory.ini",
            file_type="inventory", ref="main",
        )
        s.add_all([compose_file, k8s_file, tf_file, ansible_file, inv_file])
        s.flush()

        s.add(ComposeService(iac_file_id=compose_file.id, service_name="web", image="nginx:1.27", ports=["80:80"]))
        s.add(ComposeService(iac_file_id=compose_file.id, service_name="db", image="postgres:16"))

        s.add(K8sManifestResource(iac_file_id=k8s_file.id, kind="Deployment", name="api", namespace="prod", api_version="apps/v1"))
        s.add(K8sManifestResource(iac_file_id=k8s_file.id, kind="Service", name="api-svc", namespace="prod"))

        s.add(TerraformResource(iac_file_id=tf_file.id, resource_type="aws_instance", resource_name="web01"))

        play = AnsiblePlaybookPlay(iac_file_id=ansible_file.id, play_index=0, name="Deploy web", hosts=["webservers"])
        s.add(play)

        grp = AnsibleInventoryGroup(iac_file_id=inv_file.id, name="webservers")
        s.add(grp)
        s.flush()
        s.add(AnsibleInventoryHost(group_id=grp.id, name="web01.internal"))

        res = Resource(name=".gitlab-ci.yml", domain="iac", type="ci_file", source="gitlab")
        s.add(res)
        s.flush()
        s.add(CiSchedule(
            resource_id=res.id, project_id=1, schedule_id=55,
            description="Nightly build", ref="main", cron="0 4 * * *", active=True,
        ))
        s.commit()


def test_iac_overview_file_type_counts_pushed_to_sql_group_by(client, engine):
    """M-4: iac_overview used to fetch EVERY IacFile row
    (``s.query(IacFile).all()``) across every project just to tally
    per-project/per-type counts in a Python loop. Real-behavior check: seed
    more files than needed, capture the actual SQL, and assert the
    per-project/per-type tally is pushed into SQL (a GROUP BY) rather than
    an unfiltered full-table SELECT. Also pins correctness of the resulting
    counts.
    """
    from sqlalchemy import event

    with Session(engine) as s:
        proj = GitlabProject(gitlab_project_id=1, name="infra-repo", path_with_namespace="ops/infra-repo")
        s.add(proj)
        s.flush()
        for i in range(4):
            s.add(
                IacFile(
                    gitlab_project_id=1, project_name="infra-repo",
                    path=f"compose-{i}.yml", file_type="compose", ref="main",
                )
            )
        for i in range(3):
            s.add(
                IacFile(
                    gitlab_project_id=1, project_name="infra-repo",
                    path=f"k8s-{i}.yaml", file_type="k8s_manifest", ref="main",
                )
            )
        s.commit()

    captured_sql: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured_sql.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        resp = client.get("/api/iac/overview")
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert resp.status_code == 200
    body = resp.json()
    proj_row = body["projects"][0]
    assert proj_row["files_by_type"] == {"compose": 4, "k8s_manifest": 3}
    assert proj_row["file_count"] == 7
    assert body["summary"]["file_count"] == 7
    assert body["summary"]["files_by_type"] == {"compose": 4, "k8s_manifest": 3}

    file_selects = [
        sql
        for sql in captured_sql
        if sql.strip().lower().startswith("select") and "iac_files" in sql.lower()
    ]
    assert file_selects, "expected iac_overview to query iac_files at all"
    assert any("group by" in sql.lower() for sql in file_selects), (
        "iac_overview must push the per-project/per-type file tally into SQL "
        "(GROUP BY) instead of pulling every iac_files row into Python"
    )


def test_iac_overview_pipeline_run_lookup_is_bounded_per_project(client, engine):
    """M-4: iac_overview used to fetch EVERY CiPipelineRun in the whole
    table (``s.query(CiPipelineRun).order_by(...).all()``) across ALL
    projects, just to keep the latest 5 per project — an unbounded-growth
    table (every CI run of every project, forever) fully materialized into
    Python on every dashboard load.

    Real-behavior check (not the shape of the patch): capture the actual SQL
    sent to the DB and assert the ci_pipeline_runs query pushes the
    per-project "latest N" selection into SQL (a window function), rather
    than an unfiltered/unbounded full-table SELECT. Also pins the
    unaffected, still-correct output: 5 most recent runs per project.
    """
    from sqlalchemy import event

    from infra_brain.db.models import CiPipelineRun

    with Session(engine) as s:
        proj1 = GitlabProject(gitlab_project_id=1, name="infra-repo", path_with_namespace="ops/infra-repo")
        proj2 = GitlabProject(gitlab_project_id=2, name="other-repo", path_with_namespace="ops/other-repo")
        s.add_all([proj1, proj2])
        s.flush()
        # 8 runs each for 2 projects -- more than the "latest 5" window, so
        # an unbounded full-table fetch is observably different (16 rows in
        # Python) from a bounded one (<=5 per project pushed to SQL).
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        for proj_id in (1, 2):
            for i in range(8):
                s.add(
                    CiPipelineRun(
                        gitlab_project_id=proj_id,
                        pipeline_id=proj_id * 100 + i,
                        status="success",
                        created_at=now - timedelta(hours=i),
                    )
                )
        s.commit()

    captured_sql: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured_sql.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        resp = client.get("/api/iac/overview")
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert resp.status_code == 200
    body = resp.json()
    projects_by_id = {p["gitlab_project_id"]: p for p in body["projects"]}
    # Correctness is unaffected: still exactly the 5 most recent runs per
    # project, most-recent first.
    assert len(projects_by_id[1]["recent_pipelines"]) == 5
    assert len(projects_by_id[2]["recent_pipelines"]) == 5
    assert projects_by_id[1]["recent_pipelines"][0]["pipeline_id"] == 100  # i=0 -> most recent
    assert body["summary"]["pipeline_run_count"] == 16

    ci_selects = [
        sql
        for sql in captured_sql
        if sql.strip().lower().startswith("select") and "ci_pipeline_runs" in sql.lower()
    ]
    assert ci_selects, "expected iac_overview to query ci_pipeline_runs at all"
    assert any(
        "over" in sql.lower() and "partition" in sql.lower() for sql in ci_selects
    ), (
        "iac_overview must push the per-project latest-N selection into SQL "
        "(a window function) instead of pulling every ci_pipeline_runs row "
        "for every project into Python"
    )


def test_compose_services_lists_seeded_rows(client, engine):
    _seed(engine)
    resp = client.get("/api/iac/compose-services")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    names = {r["service_name"] for r in body["items"]}
    assert names == {"web", "db"}
    assert all(r["project_name"] == "infra-repo" for r in body["items"])


def test_compose_services_empty_when_none_collected(client, engine):
    resp = client.get("/api/iac/compose-services")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0, "limit": 200, "offset": 0}


def test_k8s_resources_lists_seeded_rows(client, engine):
    _seed(engine)
    resp = client.get("/api/iac/k8s-resources")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    kinds = {r["kind"] for r in body["items"]}
    assert kinds == {"Deployment", "Service"}


def test_terraform_resources_lists_seeded_rows(client, engine):
    _seed(engine)
    resp = client.get("/api/iac/terraform-resources")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["resource_type"] == "aws_instance"
    assert body["items"][0]["resource_name"] == "web01"


def test_ansible_structure_lists_groups_hosts_and_plays(client, engine):
    _seed(engine)
    resp = client.get("/api/iac/ansible")
    assert resp.status_code == 200
    body = resp.json()
    assert body["groups"][0]["name"] == "webservers"
    assert body["groups"][0]["hosts"][0]["name"] == "web01.internal"
    assert body["plays"][0]["name"] == "Deploy web"
    assert body["plays"][0]["hosts"] == ["webservers"]


def test_ansible_structure_empty_when_none_collected(client, engine):
    resp = client.get("/api/iac/ansible")
    assert resp.status_code == 200
    assert resp.json() == {"groups": [], "plays": []}


def test_ci_schedules_lists_seeded_rows(client, engine):
    _seed(engine)
    resp = client.get("/api/iac/ci-schedules")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["schedule_id"] == 55
    assert row["cron"] == "0 4 * * *"
    assert row["description"] == "Nightly build"
    assert row["active"] is True
