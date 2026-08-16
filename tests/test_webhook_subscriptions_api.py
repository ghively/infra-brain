"""Webhook subscription dashboard routes (session/admin gated; dev-mode open) —
GitLab #112."""

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import WebhookDelivery

from tests.support.pg import make_engine


@pytest.fixture
def client(monkeypatch):
    # Dev-mode opens require_session/require_admin so we can exercise the
    # routes without a signed cookie (mirrors tests/test_mcp_keys_api.py).
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    from infra_brain.config import get_settings

    get_settings.cache_clear()

    eng = make_engine()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    import infra_brain.api.routers.webhook_subscriptions as mod

    monkeypatch.setattr(mod, "get_session", _get_session)
    monkeypatch.setattr("infra_brain.tools.webhook_publish.get_session", _get_session)

    app = FastAPI()
    app.include_router(mod.webhook_subscriptions_router)
    return TestClient(app)


def test_create_then_list_subscription(client):
    created = client.post(
        "/api/dashboard/webhook-subscriptions",
        json={
            "name": "pagerduty-vsphere",
            "target_url": "https://events.pagerduty.com/v2/enqueue",
            "secret_token": "s3cr3t",
            "event_pattern": "drift.*",
            "domain_filter": "vsphere",
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["name"] == "pagerduty-vsphere"
    assert body["has_secret"] is True
    # Raw secret is never echoed back.
    assert "secret_token" not in body
    sub_id = body["id"]

    listed = client.get("/api/dashboard/webhook-subscriptions")
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert any(i["id"] == sub_id for i in items)
    assert all("secret_token" not in i for i in items)


def test_create_rejects_overlong_name(client):
    r = client.post(
        "/api/dashboard/webhook-subscriptions",
        json={"name": "x" * 129, "target_url": "https://hooks.example.com/a"},
    )
    assert r.status_code == 422


def test_create_rejects_empty_target_url(client):
    r = client.post(
        "/api/dashboard/webhook-subscriptions",
        json={"name": "a", "target_url": ""},
    )
    assert r.status_code == 422


def test_update_amends_fields_in_place(client):
    created = client.post(
        "/api/dashboard/webhook-subscriptions",
        json={"name": "a", "target_url": "https://hooks.example.com/a"},
    )
    sub_id = created.json()["id"]

    updated = client.patch(
        f"/api/dashboard/webhook-subscriptions/{sub_id}",
        json={"active": False, "event_pattern": "compliance.*"},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["active"] is False
    assert body["event_pattern"] == "compliance.*"
    # Untouched fields survive.
    assert body["name"] == "a"


def test_update_unknown_id_is_404(client):
    import uuid

    r = client.patch(
        f"/api/dashboard/webhook-subscriptions/{uuid.uuid4()}",
        json={"active": False},
    )
    assert r.status_code == 404


def test_update_rejects_malformed_id(client):
    r = client.patch("/api/dashboard/webhook-subscriptions/not-a-uuid", json={"active": False})
    assert r.status_code == 422


def test_delete_subscription(client):
    created = client.post(
        "/api/dashboard/webhook-subscriptions",
        json={"name": "a", "target_url": "https://hooks.example.com/a"},
    )
    sub_id = created.json()["id"]

    deleted = client.delete(f"/api/dashboard/webhook-subscriptions/{sub_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"id": sub_id, "deleted": True}

    listed = client.get("/api/dashboard/webhook-subscriptions")
    assert all(i["id"] != sub_id for i in listed.json()["items"])


def test_delete_unknown_id_is_404(client):
    import uuid

    r = client.delete(f"/api/dashboard/webhook-subscriptions/{uuid.uuid4()}")
    assert r.status_code == 404


def test_send_test_delivery_success(client, monkeypatch):
    from unittest.mock import MagicMock, patch

    created = client.post(
        "/api/dashboard/webhook-subscriptions",
        json={"name": "a", "target_url": "https://hooks.example.com/a"},
    )
    sub_id = created.json()["id"]

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    with (
        patch("infra_brain.tools.webhook_publish.httpx.post", return_value=mock_resp),
        patch("infra_brain.tools.webhook_publish.gate_external_write", return_value=None),
        # Testing delivery/retry logic, not the SSRF guard itself (which has
        # its own dedicated tests in test_webhook_publish.py using literal
        # IPs) -- hooks.example.com doesn't resolve in a network-isolated
        # sandbox/CI runner, which would otherwise reject this fake target.
        patch("infra_brain.tools.webhook_publish._is_ssrf_safe_target", return_value=True),
    ):
        r = client.post(
            f"/api/dashboard/webhook-subscriptions/{sub_id}/test",
            json={"category": "test", "message": "hello"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["delivered"] is True
    assert body["error"] is None


def test_send_test_delivery_reports_failure(client):
    from unittest.mock import patch

    created = client.post(
        "/api/dashboard/webhook-subscriptions",
        json={"name": "a", "target_url": "https://hooks.example.com/a"},
    )
    sub_id = created.json()["id"]

    with (
        patch(
            "infra_brain.tools.webhook_publish.httpx.post",
            side_effect=RuntimeError("connection refused"),
        ),
        patch("infra_brain.tools.webhook_publish.gate_external_write", return_value=None),
        patch("infra_brain.tools.webhook_publish._is_ssrf_safe_target", return_value=True),
    ):
        r = client.post(
            f"/api/dashboard/webhook-subscriptions/{sub_id}/test",
            json={"category": "test", "message": "hello"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["delivered"] is False
    assert "connection refused" in body["error"]


def test_send_test_delivery_unknown_id_is_404(client):
    import uuid

    r = client.post(
        f"/api/dashboard/webhook-subscriptions/{uuid.uuid4()}/test",
        json={"category": "test", "message": "hello"},
    )
    assert r.status_code == 404


def test_list_deliveries_returns_history(client, monkeypatch):
    from datetime import datetime, timezone

    created = client.post(
        "/api/dashboard/webhook-subscriptions",
        json={"name": "a", "target_url": "https://hooks.example.com/a"},
    )
    sub_id = created.json()["id"]

    from infra_brain.db.session import get_session as real_get_session  # noqa: F401

    import infra_brain.api.routers.webhook_subscriptions as mod

    import uuid

    with mod.get_session() as s:
        s.add(
            WebhookDelivery(
                subscription_id=uuid.UUID(sub_id),
                category="drift.critical",
                domain="vsphere",
                status="delivered",
                attempt_count=1,
                delivered_at=datetime.now(timezone.utc),
            )
        )
        s.commit()

    r = client.get(f"/api/dashboard/webhook-subscriptions/{sub_id}/deliveries")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["category"] == "drift.critical"
    assert body["items"][0]["status"] == "delivered"


def test_list_deliveries_total_reflects_full_count_not_page_size(client, monkeypatch):
    """Regression test: ``total`` must be the true row count, not
    ``len(items)`` after ``.limit()`` is applied — matching the pagination
    contract documented in ``api/_envelope.py::paginate()`` and followed by
    every other paged endpoint (hosts.py/iac.py/netdiscovery.py/vuln.py).
    With 5 delivery rows and a page ``limit`` of 2, the response must report
    ``total=5`` (the true count) while still only returning 2 items."""
    from datetime import datetime, timezone
    import uuid

    created = client.post(
        "/api/dashboard/webhook-subscriptions",
        json={"name": "a", "target_url": "https://hooks.example.com/a"},
    )
    sub_id = created.json()["id"]

    import infra_brain.api.routers.webhook_subscriptions as mod

    with mod.get_session() as s:
        for i in range(5):
            s.add(
                WebhookDelivery(
                    subscription_id=uuid.UUID(sub_id),
                    category=f"drift.event-{i}",
                    domain="vsphere",
                    status="delivered",
                    attempt_count=1,
                    delivered_at=datetime.now(timezone.utc),
                )
            )
        s.commit()

    r = client.get(
        f"/api/dashboard/webhook-subscriptions/{sub_id}/deliveries",
        params={"limit": 2},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["total"] == 5
