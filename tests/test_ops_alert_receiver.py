"""In-app ops-alert receiver — POST /webhooks/ops-alert (TRK-135 / TRK-329).

The deployment had complete alert-delivery wiring pointing at nothing:
``OPS_WEBHOOK_URL`` unset, ``webhook_subscriptions`` empty. These tests pin the
DESTINATION half — that an authenticated inbound alert is persisted where the
dashboard can see it, that an unconfigured receiver is closed rather than open,
that both existing producers' payload shapes are accepted unchanged, and that
pointing a producer at this same app is a single hop with no recursion.
"""

from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.config import get_settings
from infra_brain.db.models import AuditLog

from tests.support.pg import make_engine


TOKEN = "receiver-token-abc123"

PROBER_PATH = Path(__file__).resolve().parents[1] / "docker" / "deadman-prober" / "prober.py"


def _load_prober():
    """Load the standalone prober module by path (it must never be importable
    as part of infra_brain — see tests/test_deadman_prober.py)."""
    spec = importlib.util.spec_from_file_location("deadman_prober_receiver_test", PROBER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["deadman_prober_receiver_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine():
    eng = make_engine()
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine, monkeypatch):
    """Bare TestClient over webhooks.router with a sqlite-backed get_session.

    ``record_alert_audit_row`` imports ``get_session`` from
    ``infra_brain.db.session`` at call time, so that is where it is patched.
    """
    from infra_brain.webhooks import router

    monkeypatch.setenv("OPS_WEBHOOK_RECEIVER_TOKEN", TOKEN)
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    app = FastAPI()
    app.include_router(router)
    with patch("infra_brain.db.session.get_session", _get_session):
        with TestClient(app) as c:
            yield c
    get_settings.cache_clear()


def _audit_rows(engine) -> list[AuditLog]:
    with Session(engine) as s:
        return list(s.query(AuditLog).all())


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_valid_bearer_token_persists_and_returns_200(client, engine):
    resp = client.post(
        "/webhooks/ops-alert",
        json={"category": "collection_health", "messages": ["linux: 34 consecutive failures"]},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"received": True, "category": "collection_health", "messages": 1}

    rows = _audit_rows(engine)
    assert len(rows) == 1
    assert rows[0].tool == "ops_alert_received:collection_health"
    assert rows[0].allowed is False


def test_bad_bearer_token_is_401(client, engine):
    resp = client.post(
        "/webhooks/ops-alert",
        json={"category": "collection_health", "messages": ["x"]},
        headers={"Authorization": "Bearer totally-wrong"},
    )
    assert resp.status_code == 401
    assert _audit_rows(engine) == []


def test_missing_authorization_header_is_401(client, engine):
    resp = client.post(
        "/webhooks/ops-alert",
        json={"category": "collection_health", "messages": ["x"]},
    )
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"
    assert _audit_rows(engine) == []


def test_wrong_auth_scheme_is_401(client, engine):
    """A raw token with no ``Bearer`` scheme must not authenticate."""
    resp = client.post(
        "/webhooks/ops-alert",
        json={"category": "collection_health", "messages": ["x"]},
        headers={"Authorization": TOKEN},
    )
    assert resp.status_code == 401
    assert _audit_rows(engine) == []


def test_non_ascii_bearer_value_is_401_not_500(client):
    """hmac.compare_digest over str raises TypeError on non-ASCII — comparing
    utf-8 BYTES keeps a hostile header an auth failure, not a 500.

    Exercised against the helper directly: starlette decodes inbound header
    BYTES as latin-1, so a non-ASCII byte does reach this function as a
    non-ASCII str in production, but the test client refuses to encode one.
    """
    from fastapi import HTTPException

    from infra_brain.webhooks import _verify_ops_alert_bearer

    with pytest.raises(HTTPException) as exc:
        _verify_ops_alert_bearer("Bearer tökén")
    assert exc.value.status_code == 401


def test_unset_receiver_token_is_a_hard_refusal_not_an_open_door(engine, monkeypatch):
    """The load-bearing case: an unconfigured receiver must be OFF (503), never
    unauthenticated. It must NOT honour the INFRA_BRAIN_DEV / WEBHOOK_AUTH_REQUIRED
    dev-mode escape that _verify_token's other routes have — shipping an
    accidentally-open alert sink would be worse than shipping none."""
    from infra_brain.webhooks import router

    monkeypatch.setenv("OPS_WEBHOOK_RECEIVER_TOKEN", "")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    app = FastAPI()
    app.include_router(router)
    with patch("infra_brain.db.session.get_session", _get_session), TestClient(app) as c:
        unauthenticated = c.post(
            "/webhooks/ops-alert", json={"category": "collection_health", "messages": ["x"]}
        )
        with_a_guess = c.post(
            "/webhooks/ops-alert",
            json={"category": "collection_health", "messages": ["x"]},
            headers={"Authorization": "Bearer anything"},
        )

    assert unauthenticated.status_code == 503
    assert with_a_guess.status_code == 503
    assert "OPS_WEBHOOK_RECEIVER_TOKEN" in unauthenticated.json()["detail"]
    # Nothing was accepted, so nothing was written.
    assert _audit_rows(engine) == []
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Producer payload shapes — accept what the two existing senders already send
# ---------------------------------------------------------------------------


def test_accepts_send_ops_alert_payload_shape_with_dedup_key(client, engine):
    """tools/ops_webhook.py::send_ops_alert POSTs category + messages + dedup_key."""
    resp = client.post(
        "/webhooks/ops-alert",
        json={
            "category": "collection_health",
            "messages": ["linux: last successful run 8 days ago", "windows: never run"],
            "dedup_key": "drift:res-1:cpu",
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert resp.status_code == 200
    assert resp.json()["messages"] == 2
    rows = _audit_rows(engine)
    assert len(rows) == 1
    assert rows[0].input_hash == "drift:res-1:cpu"


def test_accepts_prober_payload_shape_end_to_end(client, engine):
    """The prober builds its body with build_alert_payload() and has NO
    dedup_key. Build it with the prober's own function so this test breaks if
    that producer's shape ever drifts from what the receiver accepts."""
    prober = _load_prober()
    payload = prober.build_alert_payload(
        "endpoint_health", ["healthz: DOWN (3 consecutive failures) — connection refused"]
    )
    assert set(payload) == {"category", "messages"}  # no dedup_key, by design

    resp = client.post(
        "/webhooks/ops-alert",
        json=payload,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert resp.status_code == 200, resp.text
    rows = _audit_rows(engine)
    assert len(rows) == 1
    assert rows[0].tool == "ops_alert_received:endpoint_health"
    assert "healthz: DOWN" in rows[0].denial_reason


def test_prober_send_alert_delivers_to_the_receiver(client, engine):
    """Full prober -> receiver hop: drive the prober's real send_alert() and
    route its urllib POST into this app. Proves the Bearer header the prober
    already builds authenticates against OPS_WEBHOOK_RECEIVER_TOKEN."""
    prober = _load_prober()
    captured: dict = {}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        captured["url"] = req.full_url
        resp = client.post(
            "/webhooks/ops-alert",
            content=req.data,
            headers={k: v for k, v in req.headers.items()},
        )
        captured["status"] = resp.status_code
        return MagicMock(
            status=resp.status_code,
            __enter__=lambda s: s,
            __exit__=lambda s, *a: False,
        )

    with patch.object(prober.urllib.request, "urlopen", side_effect=fake_urlopen):
        ok = prober.send_alert(
            "http://testserver/webhooks/ops-alert",
            TOKEN,
            "heartbeat",
            ["scheduler heartbeat stale: 4h > 2h threshold"],
            5.0,
        )

    assert ok is True
    assert captured["status"] == 200
    rows = _audit_rows(engine)
    assert len(rows) == 1
    assert rows[0].tool == "ops_alert_received:heartbeat"
    assert "scheduler heartbeat stale" in rows[0].denial_reason


def test_liberal_body_bare_string_messages_and_unknown_keys(client, engine):
    """Liberal in what it accepts: a rejected alert is a LOST alert. A bare
    string where a list is expected is coerced; unknown keys are ignored."""
    resp = client.post(
        "/webhooks/ops-alert",
        json={"category": "deadman", "messages": "scheduler is gone", "source": "future-producer"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert resp.status_code == 200
    assert resp.json()["messages"] == 1
    assert "scheduler is gone" in _audit_rows(engine)[0].denial_reason


def test_missing_category_defaults_rather_than_422(client, engine):
    resp = client.post(
        "/webhooks/ops-alert",
        json={"messages": ["something happened"]},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 200
    assert _audit_rows(engine)[0].tool == "ops_alert_received:unspecified"


# ---------------------------------------------------------------------------
# Persisted row content + one shared convention with the outbound fallback
# ---------------------------------------------------------------------------


def test_audit_row_carries_category_and_every_message(client, engine):
    messages = [
        "linux: last successful run 8 days ago (SLA 8h)",
        "windows: zero runs, ever",
        "discovery: skipped — no live fleet source",
    ]
    client.post(
        "/webhooks/ops-alert",
        json={"category": "collection_health", "messages": messages},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    row = _audit_rows(engine)[0]
    assert row.agent == "ops_alert_receiver"
    assert row.tool == "ops_alert_received:collection_health"
    # allowed=False is what puts this on the dashboard Activity page via
    # get_audit_log(allowed=False) — same convention as the outbound fallback.
    assert row.allowed is False
    assert "collection_health" in row.denial_reason
    for m in messages:
        assert m in row.denial_reason


def test_receiver_and_outbound_fallback_share_one_audit_convention(engine):
    """Both halves go through record_alert_audit_row, so the dashboard query
    that surfaces one surfaces the other. Differing only in the tool prefix."""
    from infra_brain.tools.ops_webhook import _persist_undelivered_alert

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.db.session.get_session", _get_session):
        _persist_undelivered_alert("collection_health", ["linux: stale"], "NotificationAgent", None)

    row = _audit_rows(engine)[0]
    assert row.tool == "send_ops_alert:collection_health"
    assert row.allowed is False


def test_oversized_category_is_truncated_to_the_column_width(client, engine):
    """AuditLog.tool is String(128) and category comes off an inbound payload.
    sqlite would silently accept an over-long value; Postgres would raise."""
    client.post(
        "/webhooks/ops-alert",
        json={"category": "x" * 500, "messages": ["boom"]},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    row = _audit_rows(engine)[0]
    assert len(row.tool) <= 128
    assert len(row.agent) <= 64


# ---------------------------------------------------------------------------
# Failure handling + loop safety
# ---------------------------------------------------------------------------


def test_persistence_failure_returns_500_not_a_silent_200(client, engine):
    """A receiver that reports success while storing nothing would recreate the
    exact defect this endpoint exists to fix."""
    with patch(
        "infra_brain.tools.ops_webhook.record_alert_audit_row",
        side_effect=RuntimeError("db is down"),
    ):
        resp = client.post(
            "/webhooks/ops-alert",
            json={"category": "collection_health", "messages": ["x"]},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert resp.status_code == 500
    assert _audit_rows(engine) == []


def test_receiver_never_raises_a_new_alert(client, engine):
    """Loop safety: the handler must not call send_ops_alert on ANY path —
    success or failure — or an app pointed at itself could recurse."""
    import infra_brain.tools.ops_webhook as ops_webhook_mod

    with patch.object(ops_webhook_mod, "send_ops_alert") as mock_send:
        ok = client.post(
            "/webhooks/ops-alert",
            json={"category": "collection_health", "messages": ["x"]},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with patch.object(
            ops_webhook_mod, "record_alert_audit_row", side_effect=RuntimeError("db is down")
        ):
            failed = client.post(
                "/webhooks/ops-alert",
                json={"category": "collection_health", "messages": ["y"]},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )

    assert ok.status_code == 200
    assert failed.status_code == 500
    mock_send.assert_not_called()


def test_app_pointed_at_itself_is_exactly_one_hop_and_one_row(client, engine, monkeypatch):
    """OPS_WEBHOOK_URL -> this same app: send_ops_alert must deliver in ONE hop
    (returns True, no fallback row) and leave exactly one received-alert row.

    This is the configuration the .env.example recipe recommends, so the
    no-recursion claim is pinned by a test rather than asserted in a comment.
    """
    from types import SimpleNamespace

    from infra_brain.tools.ops_webhook import send_ops_alert

    settings = SimpleNamespace(
        ops_webhook_url="http://testserver/webhooks/ops-alert",
        ops_webhook_token=TOKEN,
        api_timeout_seconds=5,
    )

    def fake_httpx_post(url, json=None, headers=None, timeout=None):  # noqa: A002, ARG001
        resp = client.post(url, json=json, headers=headers)
        return SimpleNamespace(
            status_code=resp.status_code,
            raise_for_status=lambda: (
                None
                if resp.status_code < 400
                else (_ for _ in ()).throw(RuntimeError(f"HTTP {resp.status_code}"))
            ),
        )

    with (
        patch("infra_brain.tools.ops_webhook.get_settings", return_value=settings),
        patch("infra_brain.tools.ops_webhook.gate_external_write"),
        patch("infra_brain.tools.ops_webhook.httpx.post", side_effect=fake_httpx_post) as mock_post,
    ):
        delivered = send_ops_alert("collection_health", ["linux: stale"], "NotificationAgent")

    assert delivered is True
    # One hop: the producer POSTed exactly once and did not re-enter.
    assert mock_post.call_count == 1
    rows = _audit_rows(engine)
    assert len(rows) == 1
    # The received-alert row, NOT the "no webhook configured" fallback row —
    # one alert produces one row, never two.
    assert rows[0].tool == "ops_alert_received:collection_health"
