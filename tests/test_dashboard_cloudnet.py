"""Tests for the Cloud/K8s/Net typed-view endpoint (UI Batch 7 / GitLab #63).

These domains (cloud/k8s/net) are POC-disabled by default (TRK-041) — the
realistic production state is all-empty. Both the empty-state and a seeded-
data case are covered so the endpoint is proven correct in both states.

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
    CloudResource,
    K8sDeployment,
    K8sNode,
    K8sPod,
    NetDevice,
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

    from infra_brain.api.routers.cloudnet import cloudnet_router

    app = FastAPI()
    app.include_router(cloudnet_router)
    with patch("infra_brain.api.routers.cloudnet.get_session", _get_session):
        yield TestClient(app)


def test_overview_empty_when_domains_disabled(client, engine):
    """The realistic default: cloud/k8s/net collectors are idle (TRK-041)."""
    resp = client.get("/api/cloudnet/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cloud_resources"] == []
    assert body["k8s_nodes"] == []
    assert body["k8s_pods"] == []
    assert body["k8s_deployments"] == []
    assert body["net_devices"] == []
    assert body["summary"] == {
        "cloud_resource_count": 0,
        "k8s_node_count": 0,
        "k8s_pod_count": 0,
        "k8s_deployment_count": 0,
        "net_device_count": 0,
    }


def test_overview_returns_seeded_rows_when_domains_enabled(client, engine):
    """If/when TRK-041 is revisited and these domains start collecting, the
    same endpoint must surface real typed data — proven here with seeded
    rows standing in for a live collector."""
    with Session(engine) as s:
        s.add(CloudResource(provider="aws", cloud_type="ec2_instance", cloud_id="i-0123", name="web-01", region="us-east-1", state="running"))
        s.add(K8sNode(cluster="prod", name="node-1", status="Ready", roles=["worker"], kubelet_version="v1.29.0", arch="amd64"))
        s.add(K8sPod(cluster="prod", namespace="default", name="api-7f9c", phase="Running", node_name="node-1"))
        s.add(K8sDeployment(cluster="prod", namespace="default", name="api", replicas=3, ready=3, available=3))
        s.add(NetDevice(ip="10.0.0.1", name="core-switch-01", sysname="sw01.internal", location="rack-3"))
        s.commit()

    resp = client.get("/api/cloudnet/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cloud_resources"][0]["cloud_id"] == "i-0123"
    assert body["k8s_nodes"][0]["name"] == "node-1"
    assert body["k8s_pods"][0]["name"] == "api-7f9c"
    assert body["k8s_deployments"][0]["name"] == "api"
    assert body["net_devices"][0]["ip"] == "10.0.0.1"
    assert body["summary"] == {
        "cloud_resource_count": 1,
        "k8s_node_count": 1,
        "k8s_pod_count": 1,
        "k8s_deployment_count": 1,
        "net_device_count": 1,
    }
