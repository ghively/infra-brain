"""Batch G MCP tools — network/cloud/k8s read-only query tools (issue #51)."""

import contextlib
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain import mcp_server
from infra_brain.db.models import (
    CloudResource,
    K8sDeployment,
    K8sNode,
    K8sPod,
    NetDevice,
    NetDiscoveryHost,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_session(engine):
    """Patch mcp_server.get_session to hand back a real session over the test engine."""

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


def test_get_network_discoveries_success(patched_session):
    _seed(
        patched_session,
        NetDiscoveryHost(
            ip="10.0.0.9", hostname="unknown-9", is_shadow_it=True, threat_level="high"
        ),
        NetDiscoveryHost(ip="10.0.0.10", hostname="known-10", is_known=True, threat_level="none"),
    )
    rows = mcp_server.get_network_discoveries()
    assert len(rows) == 2
    ips = {r["ip"] for r in rows}
    assert ips == {"10.0.0.9", "10.0.0.10"}
    # first_seen/last_seen serialize to ISO strings, not datetime objects.
    assert isinstance(rows[0]["last_seen"], str)


def test_get_network_discoveries_shadow_it_only(patched_session):
    _seed(
        patched_session,
        NetDiscoveryHost(ip="10.0.0.9", is_shadow_it=True, threat_level="high"),
        NetDiscoveryHost(ip="10.0.0.10", is_shadow_it=False, threat_level="none"),
    )
    rows = mcp_server.get_network_discoveries(shadow_it_only=True)
    assert len(rows) == 1
    assert rows[0]["ip"] == "10.0.0.9"
    assert rows[0]["is_shadow_it"] is True


def test_get_network_discoveries_threat_level_filter(patched_session):
    _seed(
        patched_session,
        NetDiscoveryHost(ip="10.0.0.9", threat_level="high"),
        NetDiscoveryHost(ip="10.0.0.10", threat_level="low"),
    )
    rows = mcp_server.get_network_discoveries(threat_level="high")
    assert [r["ip"] for r in rows] == ["10.0.0.9"]


def test_get_network_discoveries_empty(patched_session):
    assert mcp_server.get_network_discoveries() == []


def test_get_network_devices_success(patched_session):
    _seed(
        patched_session,
        NetDevice(ip="10.0.0.1", name="core-sw-1", sysname="core-sw-1", location="dc-a"),
        NetDevice(ip="10.0.0.2", name="edge-sw-2", sysname="edge-sw-2", location="dc-b"),
    )
    rows = mcp_server.get_network_devices()
    assert len(rows) == 2
    assert {r["ip"] for r in rows} == {"10.0.0.1", "10.0.0.2"}
    assert {r["name"] for r in rows} == {"core-sw-1", "edge-sw-2"}


def test_get_network_devices_empty(patched_session):
    assert mcp_server.get_network_devices() == []


def test_get_cloud_resources_success(patched_session):
    _seed(
        patched_session,
        CloudResource(
            provider="aws",
            cloud_type="ec2_instance",
            cloud_id="i-1",
            name="web-1",
            region="us-east-1",
            state="running",
        ),
        CloudResource(
            provider="aws",
            cloud_type="vpc",
            cloud_id="vpc-1",
            name="main-vpc",
            region="us-west-2",
            state="available",
        ),
    )
    rows = mcp_server.get_cloud_resources()
    assert len(rows) == 2


def test_get_cloud_resources_filters(patched_session):
    _seed(
        patched_session,
        CloudResource(
            provider="aws",
            cloud_type="ec2_instance",
            cloud_id="i-1",
            name="web-1",
            region="us-east-1",
        ),
        CloudResource(
            provider="aws",
            cloud_type="vpc",
            cloud_id="vpc-1",
            name="main-vpc",
            region="us-west-2",
        ),
    )
    rows = mcp_server.get_cloud_resources(cloud_type="vpc", region="us-west-2")
    assert [r["cloud_id"] for r in rows] == ["vpc-1"]


def test_get_cloud_resources_empty(patched_session):
    assert mcp_server.get_cloud_resources() == []


def test_get_k8s_resources_pods(patched_session):
    _seed(
        patched_session,
        K8sPod(cluster="c1", namespace="default", name="pod-a", phase="Running"),
        K8sPod(cluster="c1", namespace="kube-system", name="pod-b", phase="Running"),
    )
    rows = mcp_server.get_k8s_resources(kind="pod")
    assert len(rows) == 2
    assert {r["name"] for r in rows} == {"pod-a", "pod-b"}


def test_get_k8s_resources_kind_dispatch_nodes(patched_session):
    _seed(
        patched_session,
        K8sNode(cluster="c1", name="node-1", status="Ready"),
        K8sPod(cluster="c1", namespace="default", name="pod-a", phase="Running"),
    )
    rows = mcp_server.get_k8s_resources(kind="node")
    assert [r["name"] for r in rows] == ["node-1"]


def test_get_k8s_resources_deployments(patched_session):
    _seed(
        patched_session,
        K8sDeployment(cluster="c1", namespace="default", name="dep-1", replicas=3, ready=3),
    )
    rows = mcp_server.get_k8s_resources(kind="deployment")
    assert rows[0]["name"] == "dep-1"
    assert rows[0]["replicas"] == 3


def test_get_k8s_resources_namespace_filter(patched_session):
    _seed(
        patched_session,
        K8sPod(cluster="c1", namespace="default", name="pod-a", phase="Running"),
        K8sPod(cluster="c1", namespace="kube-system", name="pod-b", phase="Running"),
    )
    rows = mcp_server.get_k8s_resources(kind="pod", namespace="kube-system")
    assert [r["name"] for r in rows] == ["pod-b"]


def test_get_k8s_resources_invalid_kind(patched_session):
    result = mcp_server.get_k8s_resources(kind="service")
    assert isinstance(result, dict)
    assert "error" in result


def test_get_k8s_resources_empty(patched_session):
    assert mcp_server.get_k8s_resources(kind="pod") == []
