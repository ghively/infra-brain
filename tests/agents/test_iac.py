from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.iac import IaCAgent, _classify_yaml
from infra_brain.etl.base import CollectOutcome
from infra_brain.db.models import (
    AnsibleInventoryGroup,
    AnsibleInventoryHost,
    AnsiblePlaybookPlay,
    ComposeService,
    DriftEvent,
    GitlabProject,
    IacFile,
    K8sManifestResource,
    Resource,
    TerraformResource,
)
from infra_brain.tools.iac_reader import (
    parse_ansible_playbook_plays_tool,
    parse_compose_services_tool,
    parse_k8s_resources_tool,
)

# --- _classify_yaml unit tests ---


def test_classify_gitlab_ci():
    assert _classify_yaml(".gitlab-ci.yml", ".gitlab-ci.yml", "") == "gitlab_ci_pipeline"


def test_classify_docker_compose_exact():
    assert _classify_yaml("docker-compose.yml", "docker-compose.yml", "") == "docker_compose"


def test_classify_docker_compose_variant():
    assert (
        _classify_yaml("docker/docker-compose.deploy.yml", "docker-compose.deploy.yml", "")
        == "docker_compose"
    )


def test_classify_k8s_by_path():
    assert _classify_yaml("k8s/deployment.yaml", "deployment.yaml", "") == "k8s_manifest"
    assert _classify_yaml("kubernetes/ingress.yml", "ingress.yml", "") == "k8s_manifest"


def test_classify_k8s_by_content():
    content = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"
    assert _classify_yaml("deploy/web.yaml", "web.yaml", content) == "k8s_manifest"


def test_classify_ansible_requirements():
    content = "collections:\n  - name: community.general\n"
    assert _classify_yaml("requirements.yml", "requirements.yml", content) == "ansible_requirements"


def test_classify_ansible_playbook():
    content = "---\n- hosts: all\n  roles:\n    - common\n"
    assert _classify_yaml("site.yml", "site.yml", content) == "ansible_playbook"


def test_classify_docker_compose_by_services_key():
    content = "services:\n  web:\n    image: nginx\n"
    assert _classify_yaml("infra/stack.yml", "stack.yml", content) == "docker_compose"


def test_classify_unrecognised_yaml_returns_none():
    content = "foo: bar\nbaz: 42\n"
    assert _classify_yaml("config.yml", "config.yml", content) is None


def test_classify_invalid_yaml_returns_none():
    assert _classify_yaml("bad.yml", "bad.yml", ": bad: {") is None


# --- IaCAgent.collect integration tests ---


def _make_projects(names_ids):
    return [{"id": pid, "name": name, "default_branch": "main"} for name, pid in names_ids]


def test_iac_agent_collects_playbooks():
    projects = _make_projects([("fleet-ansible", 1)])
    tree = [
        {"path": "site.yml", "name": "site.yml", "type": "blob"},
        {"path": "roles/common/tasks/main.yml", "name": "main.yml", "type": "blob"},
    ]
    file_content = "---\n- hosts: all\n  roles:\n    - common\n"

    with (
        patch("infra_brain.agents.iac.gitlab_get_paginated", return_value=projects),
        patch("infra_brain.agents.iac.gitlab_repository_tree_tool") as mock_tree,
        patch("infra_brain.agents.iac.gitlab_file_tool") as mock_file,
    ):
        mock_tree.invoke.return_value = tree
        mock_file.invoke.return_value = file_content
        items = IaCAgent().collect().items

    assert any(i["type"] == "ansible_playbook" for i in items)
    pb = next(i for i in items if i["type"] == "ansible_playbook")
    assert pb["data"]["project"] == "fleet-ansible"
    assert pb["data"]["play_count"] == 1


def test_iac_agent_collects_gitlab_ci():
    projects = _make_projects([("my-project", 10)])
    tree = [{"path": ".gitlab-ci.yml", "name": ".gitlab-ci.yml", "type": "blob"}]
    ci_content = "stages:\n  - build\n  - test\nbuild-job:\n  script:\n    - make\n"

    with (
        patch("infra_brain.agents.iac.gitlab_get_paginated", return_value=projects),
        patch("infra_brain.agents.iac.gitlab_repository_tree_tool") as mock_tree,
        patch("infra_brain.agents.iac.gitlab_file_tool") as mock_file,
    ):
        mock_tree.invoke.return_value = tree
        mock_file.invoke.return_value = ci_content
        items = IaCAgent().collect().items

    assert any(i["type"] == "gitlab_ci_pipeline" for i in items)
    ci = next(i for i in items if i["type"] == "gitlab_ci_pipeline")
    assert ci["data"]["stage_count"] == 2
    assert ci["data"]["job_count"] == 1


def test_iac_agent_collects_docker_compose():
    projects = _make_projects([("infra-brain", 42)])
    tree = [{"path": "docker/docker-compose.yml", "name": "docker-compose.yml", "type": "blob"}]
    compose_content = "services:\n  web:\n    image: nginx\n  db:\n    image: postgres\n"

    with (
        patch("infra_brain.agents.iac.gitlab_get_paginated", return_value=projects),
        patch("infra_brain.agents.iac.gitlab_repository_tree_tool") as mock_tree,
        patch("infra_brain.agents.iac.gitlab_file_tool") as mock_file,
    ):
        mock_tree.invoke.return_value = tree
        mock_file.invoke.return_value = compose_content
        items = IaCAgent().collect().items

    assert any(i["type"] == "docker_compose" for i in items)
    dc = next(i for i in items if i["type"] == "docker_compose")
    assert dc["data"]["service_count"] == 2
    assert set(dc["data"]["services"]) == {"web", "db"}


def test_iac_agent_collects_k8s_manifests():
    projects = _make_projects([("infra-brain", 42)])
    # Root tree must contain an IaC marker so the project isn't skipped;
    # full recursive tree returns both the marker and the k8s manifest.
    root_tree = [{"path": ".gitlab-ci.yml", "name": ".gitlab-ci.yml", "type": "blob"}]
    full_tree = [
        {"path": ".gitlab-ci.yml", "name": ".gitlab-ci.yml", "type": "blob"},
        {"path": "k8s/deployment.yaml", "name": "deployment.yaml", "type": "blob"},
    ]
    k8s_content = (
        "apiVersion: apps/v1\nkind: Deployment\n"
        "metadata:\n  name: agent-core\n  namespace: infra-brain\n"
    )
    ci_content = "stages:\n  - build\n"

    def _tree_side_effect(args, **kwargs):
        return root_tree if not args.get("recursive") else full_tree

    def _file_content(args, **kwargs):
        if args["file_path"] == ".gitlab-ci.yml":
            return ci_content
        return k8s_content

    with (
        patch("infra_brain.agents.iac.gitlab_get_paginated", return_value=projects),
        patch("infra_brain.agents.iac.gitlab_repository_tree_tool") as mock_tree,
        patch("infra_brain.agents.iac.gitlab_file_tool") as mock_file,
    ):
        mock_tree.invoke.side_effect = _tree_side_effect
        mock_file.invoke.side_effect = _file_content
        items = IaCAgent().collect().items

    assert any(i["type"] == "k8s_manifest" for i in items)
    k8s = next(i for i in items if i["type"] == "k8s_manifest")
    assert k8s["data"]["kind"] == "Deployment"
    assert k8s["data"]["resource_name"] == "agent-core"
    assert k8s["data"]["namespace"] == "infra-brain"


def test_iac_agent_collects_terraform_files():
    projects = _make_projects([("infra-tf", 2)])
    tree = [{"path": "main.tf", "name": "main.tf", "type": "blob"}]
    tf_content = 'resource "aws_instance" "web" {}'

    with (
        patch("infra_brain.agents.iac.gitlab_get_paginated", return_value=projects),
        patch("infra_brain.agents.iac.gitlab_repository_tree_tool") as mock_tree,
        patch("infra_brain.agents.iac.gitlab_file_tool") as mock_file,
        patch("infra_brain.agents.iac.parse_terraform_resources_tool") as mock_tf,
    ):
        mock_tree.invoke.return_value = tree
        mock_file.invoke.return_value = tf_content
        mock_tf.invoke.return_value = [{"resource_type": "aws_instance", "resource_name": "web"}]
        items = IaCAgent().collect().items

    assert any(i["type"] == "terraform_file" for i in items)


# --- TRK-311: per-file fetches within a project are now parallelized ---


def test_iac_agent_collects_all_files_when_fetched_in_parallel():
    """Multiple IaC files in one project must all still be collected once
    fetched concurrently (ThreadPoolExecutor), not just the first/last."""
    projects = _make_projects([("infra-tf", 2)])
    tree = [
        {"path": f"modules/mod{i}/main.tf", "name": "main.tf", "type": "blob"} for i in range(6)
    ]

    def _file_side_effect(kwargs, config=None):
        # Content keyed by path so each of the 6 concurrent fetches returns
        # something distinguishable, regardless of completion order.
        path = kwargs["file_path"]
        return f'resource "aws_instance" "{path}" {{}}'

    with (
        patch("infra_brain.agents.iac.gitlab_get_paginated", return_value=projects),
        patch("infra_brain.agents.iac.gitlab_repository_tree_tool") as mock_tree,
        patch("infra_brain.agents.iac.gitlab_file_tool") as mock_file,
        patch("infra_brain.agents.iac.parse_terraform_resources_tool") as mock_tf,
    ):
        mock_tree.invoke.return_value = tree
        mock_file.invoke.side_effect = _file_side_effect
        mock_tf.invoke.return_value = [{"resource_type": "aws_instance", "resource_name": "x"}]
        items = IaCAgent().collect().items

    tf_items = [i for i in items if i["type"] == "terraform_file"]
    assert len(tf_items) == 6
    assert {i["data"]["file_path"] for i in tf_items} == {t["path"] for t in tree}


def test_iac_agent_one_file_fetch_failure_does_not_abort_the_rest():
    """A single file-content fetch failure must not drop every subsequent
    file for that project — matches the pre-TRK-311 per-file try/except
    behavior, now implemented via the as_completed error path instead of a
    sequential try/except."""
    projects = _make_projects([("infra-tf", 2)])
    tree = [
        {"path": "modules/good1/main.tf", "name": "main.tf", "type": "blob"},
        {"path": "modules/broken/main.tf", "name": "main.tf", "type": "blob"},
        {"path": "modules/good2/main.tf", "name": "main.tf", "type": "blob"},
    ]

    def _file_side_effect(kwargs, config=None):
        if "broken" in kwargs["file_path"]:
            raise RuntimeError("simulated GitLab API failure")
        return f'resource "aws_instance" "{kwargs["file_path"]}" {{}}'

    with (
        patch("infra_brain.agents.iac.gitlab_get_paginated", return_value=projects),
        patch("infra_brain.agents.iac.gitlab_repository_tree_tool") as mock_tree,
        patch("infra_brain.agents.iac.gitlab_file_tool") as mock_file,
        patch("infra_brain.agents.iac.parse_terraform_resources_tool") as mock_tf,
    ):
        mock_tree.invoke.return_value = tree
        mock_file.invoke.side_effect = _file_side_effect
        mock_tf.invoke.return_value = [{"resource_type": "aws_instance", "resource_name": "x"}]
        items = IaCAgent().collect().items

    tf_items = [i for i in items if i["type"] == "terraform_file"]
    assert len(tf_items) == 2
    paths = {i["data"]["file_path"] for i in tf_items}
    assert paths == {"modules/good1/main.tf", "modules/good2/main.tf"}


def test_iac_agent_collect_returns_collect_outcome():
    """collect() must return a typed CollectOutcome, not a bare list, so
    ETLConnector.run() can distinguish ok/partial/failed for this collector
    the same way it does for every sibling collector."""
    projects = _make_projects([("infra-brain", 42)])
    tree = [{"path": ".gitlab-ci.yml", "name": ".gitlab-ci.yml", "type": "blob"}]

    with (
        patch("infra_brain.agents.iac.gitlab_get_paginated", return_value=projects),
        patch("infra_brain.agents.iac.gitlab_repository_tree_tool") as mock_tree,
        patch("infra_brain.agents.iac.gitlab_file_tool") as mock_file,
    ):
        mock_tree.invoke.return_value = tree
        mock_file.invoke.return_value = "stages:\n  - build\n"
        outcome = IaCAgent().collect()

    assert isinstance(outcome, CollectOutcome)
    assert outcome.errors == []
    assert outcome.status == "ok"


def test_iac_agent_file_fetch_failure_surfaces_in_collect_outcome_errors():
    """GitLab #? regression guard: a per-file read failure inside the
    ThreadPoolExecutor fan-out must be recorded in CollectOutcome.errors (not
    just logged), so ETLConnector.run() reports status='partial' instead of a
    clean 'completed' when a project's files silently failed to fetch."""
    projects = _make_projects([("infra-tf", 2)])
    tree = [
        {"path": "modules/good/main.tf", "name": "main.tf", "type": "blob"},
        {"path": "modules/broken/main.tf", "name": "main.tf", "type": "blob"},
    ]

    def _file_side_effect(kwargs, config=None):
        if "broken" in kwargs["file_path"]:
            raise RuntimeError("simulated GitLab API failure")
        return f'resource "aws_instance" "{kwargs["file_path"]}" {{}}'

    with (
        patch("infra_brain.agents.iac.gitlab_get_paginated", return_value=projects),
        patch("infra_brain.agents.iac.gitlab_repository_tree_tool") as mock_tree,
        patch("infra_brain.agents.iac.gitlab_file_tool") as mock_file,
        patch("infra_brain.agents.iac.parse_terraform_resources_tool") as mock_tf,
    ):
        mock_tree.invoke.return_value = tree
        mock_file.invoke.side_effect = _file_side_effect
        mock_tf.invoke.return_value = [{"resource_type": "aws_instance", "resource_name": "x"}]
        outcome = IaCAgent().collect()

    assert len(outcome.errors) == 1
    assert "modules/broken/main.tf" in outcome.errors[0]
    assert "infra-tf" in outcome.errors[0]
    # Partial: the good file still produced data, so status must be "partial",
    # never a silent "ok" (which ETLConnector.run() maps to status="completed").
    assert outcome.status == "partial"


def test_iac_agent_root_tree_fetch_failure_surfaces_in_collect_outcome_errors():
    """A root-tree fetch failure for one project (e.g. transient GitLab 5xx)
    must be recorded in CollectOutcome.errors, not just logged and skipped."""
    projects = _make_projects([("broken-project", 1), ("good-project", 2)])
    good_root_tree = [{"path": ".gitlab-ci.yml", "name": ".gitlab-ci.yml", "type": "blob"}]

    def _tree_side_effect(kwargs, config=None):
        if kwargs["project_id"] == 1:
            raise RuntimeError("simulated GitLab 500")
        return good_root_tree

    with (
        patch("infra_brain.agents.iac.gitlab_get_paginated", return_value=projects),
        patch("infra_brain.agents.iac.gitlab_repository_tree_tool") as mock_tree,
        patch("infra_brain.agents.iac.gitlab_file_tool") as mock_file,
    ):
        mock_tree.invoke.side_effect = _tree_side_effect
        mock_file.invoke.return_value = "stages:\n  - build\n"
        outcome = IaCAgent().collect()

    assert len(outcome.errors) == 1
    assert "broken-project" in outcome.errors[0]
    assert outcome.status == "partial"


def test_iac_agent_excludes_knowledge_paths():
    """InfraOps knowledge/ and rules/ YAML must not be ingested as IaC."""
    projects = _make_projects([("InfraOps", 28)])
    tree = [
        {"path": ".gitlab-ci.yml", "name": ".gitlab-ci.yml", "type": "blob"},
        {
            "path": "knowledge/instincts/corpor/eol/eol-remediation-cadence.yml",
            "name": "eol-remediation-cadence.yml",
            "type": "blob",
        },
        {"path": "rules/enforcement/winrm.yml", "name": "winrm.yml", "type": "blob"},
        {
            "path": ".claude/agent-dependencies.yaml",
            "name": "agent-dependencies.yaml",
            "type": "blob",
        },
    ]
    ci_content = "stages:\n  - validate\n"

    with (
        patch("infra_brain.agents.iac.gitlab_get_paginated", return_value=projects),
        patch("infra_brain.agents.iac.gitlab_repository_tree_tool") as mock_tree,
        patch("infra_brain.agents.iac.gitlab_file_tool") as mock_file,
    ):
        mock_tree.invoke.return_value = tree
        mock_file.invoke.return_value = ci_content
        items = IaCAgent().collect().items

    names = [i["name"] for i in items]
    assert any(".gitlab-ci.yml" in n for n in names), "CI pipeline should be collected"
    assert not any("knowledge/" in n for n in names), "knowledge/ must be excluded"
    assert not any("rules/" in n for n in names), "rules/ must be excluded"
    assert not any(".claude/" in n for n in names), ".claude/ must be excluded"


def test_iac_agent_skips_unclassifiable_yaml():
    """Plain config YAML with no IaC structure must not be stored."""
    projects = _make_projects([("my-project", 5)])
    tree = [
        {"path": ".gitlab-ci.yml", "name": ".gitlab-ci.yml", "type": "blob"},
        {"path": "config.yml", "name": "config.yml", "type": "blob"},
    ]

    def _file_content(args, **kwargs):
        if args["file_path"] == ".gitlab-ci.yml":
            return "stages:\n  - build\n"
        return "app_name: my-app\nversion: 1.0\n"

    with (
        patch("infra_brain.agents.iac.gitlab_get_paginated", return_value=projects),
        patch("infra_brain.agents.iac.gitlab_repository_tree_tool") as mock_tree,
        patch("infra_brain.agents.iac.gitlab_file_tool") as mock_file,
    ):
        mock_tree.invoke.return_value = tree
        mock_file.invoke.side_effect = _file_content
        items = IaCAgent().collect().items

    assert not any(i["name"].endswith("config.yml") for i in items)


def test_iac_agent_scans_configured_groups():
    """When iac_group_ids is set, projects are fetched from those groups."""
    group_projects = _make_projects([("fleet-ansible", 5), ("fleet-inventory", 6)])
    tree = [{"path": ".gitlab-ci.yml", "name": ".gitlab-ci.yml", "type": "blob"}]
    ci_content = "stages:\n  - deploy\n"

    from infra_brain.config import get_settings

    get_settings.cache_clear()

    with (
        patch.dict("os.environ", {"IAC_GROUP_IDS": "35"}),
        patch(
            "infra_brain.agents.iac.gitlab_get_paginated", return_value=group_projects
        ) as mock_paged,
        patch("infra_brain.agents.iac.gitlab_repository_tree_tool") as mock_tree,
        patch("infra_brain.agents.iac.gitlab_file_tool") as mock_file,
    ):
        get_settings.cache_clear()
        mock_tree.invoke.return_value = tree
        mock_file.invoke.return_value = ci_content
        items = IaCAgent().collect().items

    get_settings.cache_clear()
    # Group endpoint was called with group id 35
    call_urls = [str(c.args[0]) for c in mock_paged.call_args_list]
    assert any("groups/35" in u for u in call_urls)
    assert any(i["data"]["project"] in ("fleet-ansible", "fleet-inventory") for i in items)


def test_iac_agent_collect_raises_on_api_error():
    """A total project-listing failure must propagate (F-007: status='failed'),
    not silently report a fake-successful empty run."""
    with (
        patch("infra_brain.agents.iac.gitlab_get_paginated", side_effect=Exception("API down")),
        pytest.raises(RuntimeError, match="API down"),
    ):
        IaCAgent().collect()


def test_iac_agent_domain():
    assert IaCAgent().domain == "iac"


# --- Stage-2 parser unit tests ---


def test_parse_compose_services():
    content = (
        "services:\n"
        "  web:\n"
        "    image: nginx:1.25\n"
        "    ports:\n"
        "      - '8080:80'\n"
        "      - 443\n"
        "    environment:\n"
        "      FOO: bar\n"
        "  db:\n"
        "    image: postgres\n"
    )
    rows = parse_compose_services_tool.invoke({"content": content})
    web = next(r for r in rows if r["service_name"] == "web")
    assert web["image"] == "nginx:1.25"
    assert web["ports"] == ["8080:80", "443"]  # int short-form normalized to str
    assert web["config"]["environment"] == {"FOO": "bar"}
    assert "image" not in web["config"] and "ports" not in web["config"]


def test_parse_compose_services_non_compose_returns_empty():
    assert parse_compose_services_tool.invoke({"content": "foo: bar\n"}) == []
    assert parse_compose_services_tool.invoke({"content": ": bad: {"}) == []


def test_parse_k8s_resources_multidoc():
    content = (
        "apiVersion: apps/v1\nkind: Deployment\n"
        "metadata:\n  name: web\n  namespace: prod\n  labels:\n    app: web\n"
        "---\napiVersion: v1\nkind: Service\nmetadata:\n  name: web-svc\n"
    )
    rows = parse_k8s_resources_tool.invoke({"content": content})
    assert len(rows) == 2
    dep = next(r for r in rows if r["kind"] == "Deployment")
    assert dep["namespace"] == "prod"
    assert dep["labels"] == {"app": "web"}
    svc = next(r for r in rows if r["kind"] == "Service")
    assert svc["namespace"] is None


def test_parse_ansible_playbook_plays_hosts_normalized():
    content = "---\n- name: play one\n  hosts: webservers\n- hosts:\n    - db1\n    - db2\n"
    rows = parse_ansible_playbook_plays_tool.invoke({"content": content})
    assert rows[0] == {"play_index": 0, "name": "play one", "hosts": ["webservers"]}
    assert rows[1] == {"play_index": 1, "name": None, "hosts": ["db1", "db2"]}


def test_parse_ansible_playbook_plays_non_list_returns_empty():
    assert parse_ansible_playbook_plays_tool.invoke({"content": "foo: bar\n"}) == []


# --- IaCAgent detail-write path ---


def _iac_item(gen_type, path, extra=None):
    data = {"project": "proj", "project_id": 9, "file_path": path, "ref": "main", "size_bytes": 10}
    if extra:
        data.update(extra)
    return {"name": f"proj/{path}", "type": gen_type, "data": data}


def _seed_iac_agent(make_agent):
    agent = make_agent(IaCAgent)
    agent.domain = "iac"
    agent._last_projects = {9: {"id": 9, "name": "proj", "default_branch": "main"}}
    agent._last_files = [
        {
            "item": _iac_item("docker_compose", "docker-compose.yml", {"services": ["web"]}),
            "content": "services:\n  web:\n    image: nginx\n    ports: ['80:80']\n",
        },
        {
            "item": _iac_item("k8s_manifest", "k8s/dep.yaml", {"kind": "Deployment"}),
            "content": (
                "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: ns\n"
            ),
        },
        {
            "item": _iac_item("terraform_file", "main.tf"),
            "content": 'resource "aws_instance" "web" {}\n',
        },
        {
            "item": _iac_item("ansible_inventory_file", "inventory/hosts.yml"),
            "content": (
                "all:\n  children:\n    web:\n      hosts:\n        h1: {ansible_host: 1.2.3.4}\n"
            ),
        },
        {
            "item": _iac_item("ansible_playbook", "site.yml"),
            "content": "---\n- name: p\n  hosts: all\n",
        },
        {
            "item": _iac_item("gitlab_ci_pipeline", ".gitlab-ci.yml", {"stages": ["build"]}),
            "content": "stages: [build]\n",
        },
    ]
    return agent


def test_iac_write_details_populates_files_and_children(make_agent, sqlite_engine, session_patcher):
    agent = _seed_iac_agent(make_agent)
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()

    with Session(sqlite_engine) as v:
        files = {f.file_type: f for f in v.query(IacFile).all()}
        assert set(files) == {
            "compose",
            "k8s_manifest",
            "terraform",
            "inventory",
            "playbook",
            "gitlab_ci",
        }
        # gitlab_ci / requirements keep their summary in iac_files.details (no child table)
        assert files["gitlab_ci"].details["stages"] == ["build"]

        svc = v.query(ComposeService).one()
        assert svc.service_name == "web" and svc.ports == ["80:80"]
        k8s = v.query(K8sManifestResource).one()
        assert k8s.kind == "Deployment" and k8s.namespace == "ns"
        tf = v.query(TerraformResource).one()
        assert tf.resource_type == "aws_instance" and tf.resource_name == "web"
        grp = v.query(AnsibleInventoryGroup).one()
        assert grp.name == "web"
        host = v.query(AnsibleInventoryHost).one()
        assert host.name == "h1" and host.vars == {"ansible_host": "1.2.3.4"}
        play = v.query(AnsiblePlaybookPlay).one()
        assert play.play_index == 0 and play.hosts == ["all"]

        gp = v.query(GitlabProject).filter_by(gitlab_project_id=9).one()
        assert gp.name == "proj" and gp.default_branch == "main"


def test_iac_write_details_returns_row_count(make_agent, sqlite_engine, session_patcher):
    """TRK-053: `_write_iac_details` must return the written-row count so
    `BaseAgent._write_details` can populate `CollectionRun.detail_rows_written`
    (same pattern as linux.py/windows.py — see the octopus/linux comparisons)."""
    agent = _seed_iac_agent(make_agent)
    with session_patcher("infra_brain.agents.iac"):
        count = agent._write_iac_details()

    assert isinstance(count, int)
    assert count > 0


def test_iac_write_details_idempotent_no_dupes(make_agent, sqlite_engine, session_patcher):
    agent = _seed_iac_agent(make_agent)
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()
        agent._write_iac_details()

    with Session(sqlite_engine) as v:
        assert v.query(IacFile).count() == 6
        assert v.query(ComposeService).count() == 1
        assert v.query(AnsibleInventoryHost).count() == 1
        assert v.query(AnsiblePlaybookPlay).count() == 1


def test_iac_write_details_merges_gitlab_project_without_clobber(
    make_agent, sqlite_engine, session_patcher
):
    """IaC must only fill name/default_branch, not overwrite cicd's richer columns."""
    with Session(sqlite_engine) as s:
        s.add(
            GitlabProject(
                gitlab_project_id=9,
                name="proj",
                visibility="private",
                archived=True,
                group_id=42,
            )
        )
        s.commit()

    agent = _seed_iac_agent(make_agent)
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()

    with Session(sqlite_engine) as v:
        gp = v.query(GitlabProject).filter_by(gitlab_project_id=9).one()
        # cicd-populated columns survive
        assert gp.visibility == "private"
        assert gp.archived is True
        assert gp.group_id == 42


def test_iac_write_details_bad_file_skipped_others_survive(
    make_agent, sqlite_engine, session_patcher
):
    """One file whose child-write raises must not abort the rest of the run."""
    agent = _seed_iac_agent(make_agent)
    with session_patcher("infra_brain.agents.iac"):
        # Make the compose parser blow up only for the compose file.
        real = parse_compose_services_tool.invoke

        def _boom(args, **kwargs):
            raise RuntimeError("parse exploded")

        with patch("infra_brain.agents.iac.parse_compose_services_tool") as mock_compose:
            mock_compose.invoke.side_effect = _boom
            agent._write_iac_details()
        assert real  # silence unused

    with Session(sqlite_engine) as v:
        # Compose file row rolled back by its per-file savepoint; others present.
        types = {f.file_type for f in v.query(IacFile).all()}
        assert "compose" not in types
        assert {"k8s_manifest", "terraform", "playbook", "inventory", "gitlab_ci"} <= types
    # Task 4: _write_iac_details migrated to the shared _write_each helper,
    # which tracks the skip count on the agent.
    assert agent._iac_files_skipped == 1


def test_ci_schedule_rebuild_failure_does_not_wipe_existing_schedules(
    make_agent, sqlite_engine, session_patcher
):
    """M-2 priority site: ``_collect_ci_schedules`` used to DELETE every
    existing ``CiSchedule`` row for a project, then build replacement rows
    from the freshly fetched schedule list — and if that build step raised
    partway through (e.g. a malformed schedule dict missing ``"id"``), the
    delete was already staged in the SAME session and got committed by
    ``_write_iac_details``'s single end-of-phase ``session.commit()``,
    silently wiping the table and logging only at DEBUG. The delete+insert
    must be atomic (rolled back together on failure) and the failure must be
    reported through ``ETLConnector._record_partial_errors`` so the run
    downgrades away from "completed" instead of reporting clean success.
    """
    import uuid as _uuid

    from infra_brain.db.models import CiSchedule, Resource
    from infra_brain.etl.base import CollectionResult

    agent = make_agent(IaCAgent)
    agent.domain = "iac"
    agent._last_projects = {9: {"id": 9, "name": "proj", "default_branch": "main"}}
    agent._last_files = []

    with Session(sqlite_engine) as s:
        res = Resource(domain="iac", name="proj", type="gitlab_project", source="gitlab")
        s.add(res)
        s.flush()
        s.add(GitlabProject(gitlab_project_id=9, name="proj", resource_id=res.id))
        s.add(
            CiSchedule(
                resource_id=res.id,
                project_id=9,
                schedule_id=101,
                description="nightly build",
                cron="0 2 * * *",
            )
        )
        s.commit()

    # The upstream fetch succeeds (so the delete DOES run) but the second
    # schedule in the response is malformed (missing "id") — this used to
    # raise partway through the rebuild loop, AFTER the delete had already
    # executed in the same session.
    bad_schedules = [
        {"id": 201, "description": "ok", "cron": "0 3 * * *"},
        {"description": "malformed - no id key"},
    ]

    result = CollectionResult(
        run_id=_uuid.uuid4(),
        domain="iac",
        resources_found=0,
        drift_count=0,
        status="completed",
        errors=[],
    )

    with (
        session_patcher("infra_brain.agents.iac"),
        session_patcher("infra_brain.etl.base"),
        patch("infra_brain.agents.iac.gitlab_get_paginated", return_value=bad_schedules),
    ):
        agent._write_iac_details(result=result)

    with Session(sqlite_engine) as s:
        schedule_ids = {r.schedule_id for r in s.query(CiSchedule).filter_by(project_id=9).all()}

    assert 101 in schedule_ids, (
        "the pre-existing schedule must survive a failed rebuild — the delete "
        "and the (failed) reinsert must be atomic"
    )
    assert result.status != "completed", (
        "a project whose CI-schedule rebuild failed must not read 'completed'"
    )
    assert result.errors


# --- secret-scan drift-event dedup (DL-C-8) ---


def _secret_scan_agent(
    make_agent, sqlite_engine, path="secrets.tf", content=None, gen_type="terraform_file"
):
    """A single-file IaC agent whose file content trips the "password" pattern,
    with a Resource row pre-seeded so ``_scan_for_secrets`` has a resource_id."""
    agent = make_agent(IaCAgent)
    agent.domain = "iac"
    agent._last_projects = {9: {"id": 9, "name": "proj", "default_branch": "main"}}
    item = _iac_item(gen_type, path)
    agent._last_files = [
        {
            "item": item,
            "content": content or 'variable "db" {\n  default = "password: hunter2"\n}\n',
        }
    ]
    with Session(sqlite_engine) as s:
        s.add(
            Resource(
                domain="iac",
                type=gen_type,
                name=item["name"],
                source="IaCAgent",
                zone="corpor",
            )
        )
        s.commit()
    return agent


def test_iac_secret_scan_dedupes_across_runs(make_agent, sqlite_engine, session_patcher):
    """DL-C-8: a stable secret finding must not create a new DriftEvent on every
    scan — the same unbounded-growth bug drift.py's dedup-before-insert pattern
    already guards against for ordinary drift detection."""
    agent = _secret_scan_agent(make_agent, sqlite_engine)
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()
        agent._write_iac_details()  # second scan — same finding, same file

    with Session(sqlite_engine) as v:
        events = v.query(DriftEvent).filter_by(drift_type="potential_secret_in_iac").all()
        assert len(events) == 1
        assert events[0].new_value["secret_type"] == "password"


def test_iac_secret_scan_reopens_if_previously_closed(make_agent, sqlite_engine, session_patcher):
    """A finding that was closed (status != "open") re-fires — dedup only
    suppresses re-creating an event that is still open, matching drift.py."""
    agent = _secret_scan_agent(make_agent, sqlite_engine)
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()

    with Session(sqlite_engine) as v:
        ev = v.query(DriftEvent).filter_by(drift_type="potential_secret_in_iac").one()
        ev.status = "resolved"
        v.commit()

    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()

    with Session(sqlite_engine) as v:
        events = v.query(DriftEvent).filter_by(drift_type="potential_secret_in_iac").all()
        assert len(events) == 2
        assert {e.status for e in events} == {"resolved", "open"}


def test_iac_secret_scan_sets_collection_run_id(make_agent, sqlite_engine, session_patcher):
    """DL-C-8: collection_run_id must be stamped (was left NULL), matching the
    convention host_reconcile.py/drift.py use for every DriftEvent they write."""
    import uuid

    from infra_brain.db.models import CollectionRun

    run_id = uuid.uuid4()
    # A REAL collection_runs row: DriftEvent.collection_run_id is a genuine FK,
    # which SQLite ignores and PostgreSQL enforces — without the parent, the
    # detail-write is skipped and the assertion below sees no row at all.
    # (agent-orm-check gate, TRK-356.)
    with Session(sqlite_engine) as s:
        s.add(
            CollectionRun(
                id=run_id, domain="iac", trigger_type="scheduled", status="completed"
            )
        )
        s.commit()
    agent = _secret_scan_agent(make_agent, sqlite_engine)
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details(run_id)

    with Session(sqlite_engine) as v:
        ev = v.query(DriftEvent).filter_by(drift_type="potential_secret_in_iac").one()
        assert ev.collection_run_id == run_id


# --- GitLab #164: dedup_key, value-confidence tiering, k8s Secret awareness ---


def _secret_findings(sqlite_engine):
    """All open secret-scan findings as {secret_type: confidence_tier}."""
    with Session(sqlite_engine) as v:
        return {
            e.new_value["secret_type"]: e.new_value["confidence_tier"]
            for e in v.query(DriftEvent).filter_by(drift_type="potential_secret_in_iac").all()
        }


def test_secret_scan_ignores_variable_reference(make_agent, sqlite_engine, session_patcher):
    """GitLab #164 defect 2 (a): ``POSTGRES_PASSWORD: ${PG_PASS}`` is a REFERENCE
    to a secret held elsewhere — the correct practice — and must produce zero
    findings. The old blunt ``password\\s*[=:]\\s*\\S+`` matcher fired on it
    identically to a hard-coded literal, which is what trained operators to
    ignore this signal."""
    agent = _secret_scan_agent(
        make_agent,
        sqlite_engine,
        path="docker-compose.yml",
        content="services:\n  db:\n    environment:\n      POSTGRES_PASSWORD: ${PG_PASS}\n",
        gen_type="docker_compose",
    )
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()

    assert _secret_findings(sqlite_engine) == {}


def test_secret_scan_flags_literal_value(make_agent, sqlite_engine, session_patcher):
    """GitLab #164 defect 2 (b): a genuine hard-coded value is still reported,
    at the ordinary ``literal`` tier."""
    agent = _secret_scan_agent(
        make_agent,
        sqlite_engine,
        path="docker-compose.yml",
        content="services:\n  db:\n    environment:\n      POSTGRES_PASSWORD: hunter2literal\n",
        gen_type="docker_compose",
    )
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()

    assert _secret_findings(sqlite_engine) == {"password": "literal"}


def test_secret_scan_flags_known_token_format_as_high(make_agent, sqlite_engine, session_patcher):
    """GitLab #164 defect 2 (c): a self-identifying credential format
    (``glpat-``) is the highest confidence tier — checked BEFORE the placeholder
    test so a redacted-looking but real-format token is not dismissed."""
    agent = _secret_scan_agent(
        make_agent,
        sqlite_engine,
        path=".gitlab-ci.yml",
        content="variables:\n  CI_TOKEN: glpat-xxxxxAbC123deF456\n",
        gen_type="gitlab_ci_pipeline",
    )
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()

    assert _secret_findings(sqlite_engine) == {"token": "literal_high"}


def test_secret_scan_k8s_secret_manifest_is_high_confidence(
    make_agent, sqlite_engine, session_patcher
):
    """GitLab #164 defect 3 (reframed): the k8s parser always handled
    ``kind: Secret`` fine — the SCANNER had no kind-awareness. A populated
    ``stringData`` block in a real Secret manifest must be literal_high, not the
    same score as a ``${VAR}`` in a compose file."""
    agent = _secret_scan_agent(
        make_agent,
        sqlite_engine,
        path="k8s/db-secret.yaml",
        content=(
            "apiVersion: v1\n"
            "kind: Secret\n"
            "metadata:\n  name: db\n"
            "type: Opaque\n"
            "stringData:\n  password: hunter2\n"
        ),
        gen_type="k8s_manifest",
    )
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()

    findings = _secret_findings(sqlite_engine)
    assert findings.get("k8s_secret_data") == "literal_high"
    # The literal inside a real Secret manifest is upgraded too — it is a
    # credential committed alongside the manifest that deploys it.
    assert findings.get("password") == "literal_high"


def test_secret_scan_ignores_secret_key_ref_in_non_secret_manifest(
    make_agent, sqlite_engine, session_patcher
):
    """GitLab #164 defect 3: a Deployment that REFERENCES a Secret via
    ``secretKeyRef`` is doing the right thing and must produce zero findings."""
    agent = _secret_scan_agent(
        make_agent,
        sqlite_engine,
        path="k8s/api-deploy.yaml",
        content=(
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n  name: api\n"
            "spec:\n  template:\n    spec:\n      containers:\n"
            "        - name: api\n"
            "          env:\n"
            "            - name: DB_PASSWORD\n"
            "              valueFrom:\n"
            "                secretKeyRef: {name: db, key: password}\n"
        ),
        gen_type="k8s_manifest",
    )
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()

    assert _secret_findings(sqlite_engine) == {}


def test_secret_scan_second_scan_bumps_last_seen_not_a_new_row(
    make_agent, sqlite_engine, session_patcher
):
    """GitLab #164 defect 1: the regression test for the duplicate rows seen in
    production. Two scans of identical content must leave exactly ONE row, with
    ``last_seen_at`` advanced and ``detected_at`` unchanged."""
    agent = _secret_scan_agent(
        make_agent,
        sqlite_engine,
        path="docker-compose.yml",
        content="services:\n  db:\n    environment:\n      POSTGRES_PASSWORD: hunter2literal\n",
        gen_type="docker_compose",
    )
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()

    with Session(sqlite_engine) as v:
        row = v.query(DriftEvent).filter_by(drift_type="potential_secret_in_iac").one()
        first_detected = row.detected_at
        first_seen = row.last_seen_at
        assert row.dedup_key == "secret:password:docker-compose.yml"
        assert first_seen is not None

    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()

    with Session(sqlite_engine) as v:
        rows = v.query(DriftEvent).filter_by(drift_type="potential_secret_in_iac").all()
        assert len(rows) == 1, "a re-scan of identical content must not duplicate the finding"
        assert rows[0].detected_at == first_detected
        assert rows[0].last_seen_at > first_seen


def test_secret_scan_tracks_each_secret_type_independently(
    make_agent, sqlite_engine, session_patcher
):
    """GitLab #164 defect 1: the old trailing ``break`` ("one event per file is
    enough") meant a file with two different hard-coded credential kinds
    reported only the first — and fixing that one made the other look new."""
    agent = _secret_scan_agent(
        make_agent,
        sqlite_engine,
        path="docker-compose.yml",
        content=(
            "services:\n  db:\n    environment:\n"
            "      POSTGRES_PASSWORD: hunter2literal\n"
            "      API_KEY: abcd1234literal\n"
        ),
        gen_type="docker_compose",
    )
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()

    assert _secret_findings(sqlite_engine) == {"password": "literal", "api_key": "literal"}


def test_secret_scan_skips_example_and_fixture_paths(make_agent, sqlite_engine, session_patcher):
    """GitLab #164 defect 2: sample/fixture files are where placeholder
    credentials legitimately live; findings there can never be "fixed"."""
    agent = _secret_scan_agent(
        make_agent,
        sqlite_engine,
        path="docker-compose.yml.example",
        content="services:\n  db:\n    environment:\n      POSTGRES_PASSWORD: hunter2literal\n",
        gen_type="docker_compose",
    )
    with session_patcher("infra_brain.agents.iac"):
        agent._write_iac_details()

    assert _secret_findings(sqlite_engine) == {}


def test_classify_secret_value_tiers():
    """Direct unit coverage of the tier classifier, including the line-level
    indirection markers that no regex in _SECRET_PATTERNS happens to trip."""
    from infra_brain.agents.iac import _classify_secret_value

    assert _classify_secret_value("${PG_PASS}") == "reference"
    assert _classify_secret_value("$PG_PASS") == "reference"
    assert _classify_secret_value("{{ vault_db_password }}") == "reference"
    assert _classify_secret_value("!vault") == "reference"
    assert _classify_secret_value("anything", line="  secretKeyRef: {key: password}") == "reference"
    assert _classify_secret_value("anything", line="  envFrom: x") == "reference"
    assert _classify_secret_value("CHANGEME") == "placeholder"
    assert _classify_secret_value("") == "placeholder"
    assert _classify_secret_value("xxxx") == "placeholder"
    assert _classify_secret_value("<redacted>") == "placeholder"
    assert _classify_secret_value("ghp_abcdef123456") == "literal_high"
    assert _classify_secret_value("AKIAIOSFODNN7EXAMPLE") == "literal_high"
    assert _classify_secret_value("-----BEGIN") == "literal_high"
    assert _classify_secret_value("hunter2") == "literal"
