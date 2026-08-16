"""MR5 relational-model + pagination tests for K8sAgent.

Exercises:
  * detail-write phase upserts k8s_nodes / k8s_pods / k8s_deployments with
    resolved/typed fields, keyed on their natural keys, idempotently;
  * unmapped data lands in ``details`` and resource_id links the Resource;
  * the ``_continue`` pagination fix — a paged list response is fully drained
    (later pages are NOT dropped);
  * idle (no items) writes nothing;
  * a detail-write failure is surfaced on the CollectionRun.

All tests run against in-memory SQLite via the shared fixtures; the kubernetes
client is fully mocked — no live cluster required.
"""

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.k8s import K8sAgent, _paginate
from infra_brain.db.models import (
    CollectionRun,
    K8sDeployment,
    K8sNode,
    K8sPod,
    Resource,
)

MODULE = "infra_brain.agents.k8s"


def _agent(settings=None):
    agent = K8sAgent.__new__(K8sAgent)
    agent.callbacks = []
    agent.settings = settings or SimpleNamespace(k8s_cluster_name=None)
    return agent


def _items():
    return [
        {
            "name": "node1",
            "type": "k8s_node",
            "data": {
                "labels": {"node-role.kubernetes.io/control-plane": ""},
                "roles": ["control-plane"],
                "ready": "True",
                "arch": "amd64",
                "os": "Ubuntu 22.04",
                "kubelet_version": "v1.29.0",
            },
        },
        {
            "name": "default/web-abc",
            "type": "k8s_pod",
            "data": {
                "namespace": "default",
                "phase": "Running",
                "node_name": "node1",
                "containers": ["web"],
            },
        },
        {
            "name": "default/web",
            "type": "k8s_deployment",
            "data": {
                "namespace": "default",
                "replicas": 3,
                "ready_replicas": 2,
                "available_replicas": 2,
            },
        },
    ]


@pytest.fixture
def patched(session_patcher):
    with session_patcher(MODULE) as engine:
        yield engine


def _seed_resources(engine, items):
    with Session(engine) as s:
        for it in items:
            s.add(
                Resource(
                    id=uuid.uuid4(),
                    domain="k8s",
                    type=it["type"],
                    name=it["name"],
                    source="K8sAgent",
                    zone="corpor",
                )
            )
        s.commit()


# ---------------------------------------------------------------------------
# relational detail-write
# ---------------------------------------------------------------------------


def test_upserts_node_pod_deployment(patched):
    engine = patched
    items = _items()
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    agent._write_k8s_details()

    with Session(engine) as s:
        node = s.query(K8sNode).one()
        assert node.name == "node1"
        assert node.status == "True"
        assert node.roles == ["control-plane"]
        assert node.kubelet_version == "v1.29.0"
        assert node.arch == "amd64"
        assert node.os_image == "Ubuntu 22.04"
        assert node.resource_id is not None
        assert node.cluster is None
        # labels are unmapped -> details
        assert node.details and "labels" in node.details

        pod = s.query(K8sPod).one()
        assert pod.namespace == "default"
        assert pod.name == "web-abc"  # bare name, not namespace/name
        assert pod.phase == "Running"
        assert pod.node_name == "node1"
        assert pod.details and pod.details.get("containers") == ["web"]

        dep = s.query(K8sDeployment).one()
        assert dep.namespace == "default"
        assert dep.name == "web"
        assert dep.replicas == 3
        assert dep.ready == 2
        assert dep.available == 2


def test_idempotent(patched):
    engine = patched
    items = _items()
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    agent._write_k8s_details()
    agent._write_k8s_details()
    with Session(engine) as s:
        assert s.query(K8sNode).count() == 1
        assert s.query(K8sPod).count() == 1
        assert s.query(K8sDeployment).count() == 1


def test_updates_in_place_on_change(patched):
    engine = patched
    items = _items()
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    agent._write_k8s_details()

    changed = _items()
    changed[2]["data"]["available_replicas"] = 3
    agent._last_items = changed
    agent._write_k8s_details()

    with Session(engine) as s:
        rows = s.query(K8sDeployment).all()
        assert len(rows) == 1
        assert rows[0].available == 3


def test_idle_no_writes_when_no_items(patched):
    engine = patched
    agent = _agent()
    agent._last_items = []
    agent._write_k8s_details()
    with Session(engine) as s:
        assert s.query(K8sNode).count() == 0
        assert s.query(K8sPod).count() == 0
        assert s.query(K8sDeployment).count() == 0


def test_bad_item_skips_without_aborting_rest(patched):
    engine = patched
    good = _items()[0]
    # a pod with no namespace+name still upserts (namespace defaults ""), so use
    # a type with a hard requirement: deployment whose upsert raises on bad data.
    bad = {"name": "x", "type": "k8s_unknown", "data": {}}
    items = [bad, good]
    _seed_resources(engine, [good])
    agent = _agent()
    agent._last_items = items
    agent._write_k8s_details()
    with Session(engine) as s:
        assert s.query(K8sNode).count() == 1


# ---------------------------------------------------------------------------
# _continue pagination fix
# ---------------------------------------------------------------------------


def _page(items, continue_token):
    page = MagicMock()
    page.items = items
    page.metadata._continue = continue_token
    return page


def test_paginate_follows_continue_token():
    """_paginate drains every page; later pages must NOT be dropped."""
    page1 = _page(["a", "b"], "TOKEN1")
    page2 = _page(["c", "d"], "TOKEN2")
    page3 = _page(["e"], None)
    pages = [page1, page2, page3]
    calls = []

    def list_fn(**kwargs):
        calls.append(kwargs)
        return pages[len(calls) - 1]

    out = list(_paginate(list_fn))
    assert out == ["a", "b", "c", "d", "e"]
    # first call has no _continue; subsequent calls feed the prior token back
    assert "_continue" not in calls[0]
    assert calls[0]["limit"] == 500
    assert calls[1]["_continue"] == "TOKEN1"
    assert calls[2]["_continue"] == "TOKEN2"


def test_paginate_single_page():
    out = list(_paginate(lambda **kw: _page(["only"], None)))
    assert out == ["only"]


def test_collect_drains_paged_nodes():
    """End-to-end: a paged list_node response yields ALL nodes, not just page 1."""
    mock_k8s = MagicMock()
    mock_k8s.config.load_incluster_config = MagicMock()

    def _mk_node(name):
        n = MagicMock()
        n.metadata.name = name
        n.metadata.labels = {}
        n.status.conditions = []
        n.status.node_info.architecture = "amd64"
        n.status.node_info.os_image = "linux"
        n.status.node_info.kubelet_version = "v1.29.0"
        return n

    node_pages = [
        _page([_mk_node("n1"), _mk_node("n2")], "MORE"),
        _page([_mk_node("n3")], None),
    ]
    node_calls = []

    def list_node(**kwargs):
        node_calls.append(kwargs)
        return node_pages[len(node_calls) - 1]

    core = mock_k8s.client.CoreV1Api.return_value
    core.list_node.side_effect = list_node
    core.list_pod_for_all_namespaces.return_value = _page([], None)
    mock_k8s.client.AppsV1Api.return_value.list_deployment_for_all_namespaces.return_value = _page(
        [], None
    )

    agent = _agent()
    with patch("infra_brain.agents.k8s.kubernetes", mock_k8s):
        result = agent.collect("all")

    node_items = [i for i in result if i["type"] == "k8s_node"]
    assert {i["name"] for i in node_items} == {"n1", "n2", "n3"}
    assert len(node_calls) == 2  # second page was fetched via the continue token


def test_detail_write_failure_marks_run_failed(sqlite_engine):
    engine = sqlite_engine
    run_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id,
                domain="k8s",
                trigger_type="scheduled",
                trigger_source="all",
                status="completed",
            )
        )
        s.commit()

    from infra_brain.agents.base import CollectionResult

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _agent()
    result = CollectionResult(
        run_id=run_id, domain="k8s", resources_found=1, drift_count=0, status="completed"
    )

    def _boom():
        raise RuntimeError("simulated k8s relational write failure")

    with patch("infra_brain.etl.base.get_session", _get_session):
        agent._write_details(result, _boom)

    assert result.status == "failed"
    assert any("simulated k8s relational write failure" in e for e in result.errors)
    with Session(engine) as s:
        run = s.get(CollectionRun, run_id)
        assert run.status == "failed"
