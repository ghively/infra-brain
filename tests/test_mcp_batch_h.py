"""Batch H MCP tools — GitLab/IaC/CI-CD read-only query tools (issue #52)."""

import contextlib
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain import mcp_server
from infra_brain.db.models import (
    AnsibleInventoryGroup,
    AnsibleInventoryHost,
    CiPipelineRun,
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
def patched_session(engine):
    @contextlib.contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.mcp_server.get_session", _get_session):
        yield engine


def _seed(engine, *objs):
    with Session(engine) as s:
        s.add_all(objs)
        s.commit()


def test_get_cicd_overview_success(patched_session):
    _seed(
        patched_session,
        GitlabProject(gitlab_project_id=1, name="proj-a"),
        GitlabProject(gitlab_project_id=2, name="proj-b"),
        CiPipelineRun(gitlab_project_id=1, pipeline_id=10, status="success"),
        CiPipelineRun(gitlab_project_id=1, pipeline_id=11, status="failed"),
        CiPipelineRun(gitlab_project_id=2, pipeline_id=12, status="success"),
        IacFile(gitlab_project_id=1, path=".gitlab-ci.yml", file_type="gitlab_ci", ref="main"),
        IacFile(gitlab_project_id=1, path="main.tf", file_type="terraform", ref="main"),
    )
    ov = mcp_server.get_cicd_overview()
    assert ov["gitlab_projects"] == 2
    assert ov["pipelines_by_status"] == {"success": 2, "failed": 1}
    assert ov["iac_files_by_type"] == {"gitlab_ci": 1, "terraform": 1}


def test_get_cicd_overview_empty(patched_session):
    ov = mcp_server.get_cicd_overview()
    assert ov == {
        "gitlab_projects": 0,
        "pipelines_by_status": {},
        "iac_files_by_type": {},
    }


def test_get_iac_files_success(patched_session):
    _seed(
        patched_session,
        IacFile(gitlab_project_id=1, path=".gitlab-ci.yml", file_type="gitlab_ci", ref="main"),
        IacFile(gitlab_project_id=1, path="main.tf", file_type="terraform", ref="main"),
        IacFile(gitlab_project_id=2, path="playbook.yml", file_type="playbook", ref="main"),
    )
    rows = mcp_server.get_iac_files()
    assert len(rows) == 3


def test_get_iac_files_filters(patched_session):
    _seed(
        patched_session,
        IacFile(gitlab_project_id=1, path="main.tf", file_type="terraform", ref="main"),
        IacFile(gitlab_project_id=2, path="playbook.yml", file_type="playbook", ref="main"),
    )
    rows = mcp_server.get_iac_files(file_type="terraform", project_id=1)
    assert [r["path"] for r in rows] == ["main.tf"]


def test_get_iac_files_empty(patched_session):
    assert mcp_server.get_iac_files() == []


def test_get_ci_schedules_success_surfaces_resource_name(patched_session):
    with Session(patched_session) as s:
        r = Resource(domain="iac", type="file", name=".gitlab-ci.yml", source="CicdAgent")
        s.add(r)
        s.flush()
        s.add(
            CiSchedule(
                resource_id=r.id,
                project_id=1,
                schedule_id=100,
                description="nightly",
                ref="main",
                cron="0 4 * * *",
                active=True,
            )
        )
        s.commit()
    rows = mcp_server.get_ci_schedules()
    assert len(rows) == 1
    assert rows[0]["schedule_id"] == 100
    assert rows[0]["resource_name"] == ".gitlab-ci.yml"


def test_get_ci_schedules_active_filter(patched_session):
    with Session(patched_session) as s:
        r = Resource(domain="iac", type="file", name="ci.yml", source="CicdAgent")
        s.add(r)
        s.flush()
        s.add_all(
            [
                CiSchedule(resource_id=r.id, project_id=1, schedule_id=1, active=True),
                CiSchedule(resource_id=r.id, project_id=1, schedule_id=2, active=False),
            ]
        )
        s.commit()
    rows = mcp_server.get_ci_schedules(active=True)
    assert [r["schedule_id"] for r in rows] == [1]


def test_get_ci_schedules_empty(patched_session):
    assert mcp_server.get_ci_schedules() == []


def _seed_iac_file(engine, gitlab_project_id=1):
    with Session(engine) as s:
        f = IacFile(
            gitlab_project_id=gitlab_project_id, path="f", file_type="terraform", ref="main"
        )
        s.add(f)
        s.commit()
        return f.id


def test_get_parsed_iac_resources_terraform(patched_session):
    fid = _seed_iac_file(patched_session)
    _seed(
        patched_session,
        TerraformResource(iac_file_id=fid, resource_type="aws_instance", resource_name="web"),
        TerraformResource(iac_file_id=fid, resource_type="aws_s3_bucket", resource_name="assets"),
    )
    rows = mcp_server.get_parsed_iac_resources(kind="terraform")
    assert {r["resource_name"] for r in rows} == {"web", "assets"}


def test_get_parsed_iac_resources_compose_dispatch(patched_session):
    fid = _seed_iac_file(patched_session)
    _seed(
        patched_session,
        ComposeService(iac_file_id=fid, service_name="db", image="postgres:16"),
    )
    rows = mcp_server.get_parsed_iac_resources(kind="compose")
    assert rows[0]["service_name"] == "db"


def test_get_parsed_iac_resources_k8s_manifest_dispatch(patched_session):
    fid = _seed_iac_file(patched_session)
    _seed(
        patched_session,
        K8sManifestResource(iac_file_id=fid, kind="Deployment", name="api", namespace="default"),
    )
    rows = mcp_server.get_parsed_iac_resources(kind="k8s_manifest")
    assert rows[0]["name"] == "api"


def test_get_parsed_iac_resources_invalid_kind(patched_session):
    result = mcp_server.get_parsed_iac_resources(kind="ansible")
    assert isinstance(result, dict) and "error" in result


def test_get_parsed_iac_resources_empty(patched_session):
    assert mcp_server.get_parsed_iac_resources(kind="terraform") == []


def test_get_ansible_inventory_success(patched_session):
    fid = _seed_iac_file(patched_session)
    with Session(patched_session) as s:
        g1 = AnsibleInventoryGroup(iac_file_id=fid, name="web")
        g2 = AnsibleInventoryGroup(iac_file_id=fid, name="db")
        s.add_all([g1, g2])
        s.flush()
        s.add_all(
            [
                AnsibleInventoryHost(group_id=g1.id, name="web-1"),
                AnsibleInventoryHost(group_id=g1.id, name="web-2"),
                AnsibleInventoryHost(group_id=g2.id, name="db-1"),
            ]
        )
        s.commit()
    rows = mcp_server.get_ansible_inventory()
    by_group = {r["group"]: r for r in rows}
    assert set(by_group) == {"web", "db"}
    assert sorted(by_group["web"]["hosts"]) == ["web-1", "web-2"]
    assert by_group["web"]["host_count"] == 2


def test_get_ansible_inventory_group_with_no_hosts_still_returned(patched_session):
    fid = _seed_iac_file(patched_session)
    _seed(patched_session, AnsibleInventoryGroup(iac_file_id=fid, name="empty-group"))
    rows = mcp_server.get_ansible_inventory()
    assert len(rows) == 1
    assert rows[0]["group"] == "empty-group"
    assert rows[0]["hosts"] == []
    assert rows[0]["host_count"] == 0


def test_get_ansible_inventory_group_filter(patched_session):
    fid = _seed_iac_file(patched_session)
    with Session(patched_session) as s:
        g1 = AnsibleInventoryGroup(iac_file_id=fid, name="web")
        g2 = AnsibleInventoryGroup(iac_file_id=fid, name="db")
        s.add_all([g1, g2])
        s.commit()
    rows = mcp_server.get_ansible_inventory(group="web")
    assert [r["group"] for r in rows] == ["web"]


def test_get_ansible_inventory_empty(patched_session):
    assert mcp_server.get_ansible_inventory() == []
