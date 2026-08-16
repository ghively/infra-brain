import threading

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from infra_brain.main import create_app
from infra_brain.config import get_settings

from tests.support.pg import make_engine


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_health(client):
    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
    mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    mock_redis_client = MagicMock()
    mock_redis_client.ping.return_value = True

    with (
        patch("infra_brain.webhooks.get_engine", return_value=mock_engine),
        patch("infra_brain.dedup.get_redis", return_value=mock_redis_client),
    ):
        resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_check_includes_postgres_and_redis(client):
    """Health endpoint must report per-dependency status for postgres and redis."""
    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
    mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    mock_redis_client = MagicMock()
    mock_redis_client.ping.return_value = True

    with (
        patch("infra_brain.webhooks.get_engine", return_value=mock_engine),
        patch("infra_brain.dedup.get_redis", return_value=mock_redis_client),
    ):
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert "postgres" in body
    assert "redis" in body


def test_health_degraded_when_postgres_down(client):
    """Readiness must return 503 (pulled from LB) when postgres is unreachable."""
    mock_engine = MagicMock()
    mock_engine.connect.side_effect = Exception("connection refused")
    mock_redis_client = MagicMock()
    mock_redis_client.ping.return_value = True

    with (
        patch("infra_brain.webhooks.get_engine", return_value=mock_engine),
        patch("infra_brain.dedup.get_redis", return_value=mock_redis_client),
    ):
        resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert "error" in body["postgres"]
    assert body["redis"] == "ok"


def test_health_degraded_when_redis_down(client):
    """Readiness must return 503 (pulled from LB) when redis is unreachable."""
    mock_conn = MagicMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
    mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    mock_redis_client = MagicMock()
    mock_redis_client.ping.side_effect = Exception("redis down")

    with (
        patch("infra_brain.webhooks.get_engine", return_value=mock_engine),
        patch("infra_brain.dedup.get_redis", return_value=mock_redis_client),
    ):
        resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["postgres"] == "ok"
    assert "error" in body["redis"]


def test_health_degraded_when_both_postgres_and_redis_down(client):
    """Readiness must return 503 when both dependencies are unreachable."""
    mock_engine = MagicMock()
    mock_engine.connect.side_effect = Exception("postgres down")
    mock_redis_client = MagicMock()
    mock_redis_client.ping.side_effect = Exception("redis down")

    with (
        patch("infra_brain.webhooks.get_engine", return_value=mock_engine),
        patch("infra_brain.dedup.get_redis", return_value=mock_redis_client),
    ):
        resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert "error" in body["postgres"]
    assert "error" in body["redis"]


def test_healthz_is_lightweight(client):
    """Liveness probe must return ok with zero I/O — no DB or Redis calls."""
    with (
        patch("infra_brain.webhooks.get_engine", side_effect=RuntimeError("must not call")),
    ):
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ops_heartbeat_fresh(client):
    """Fresh heartbeat: stale=False, age well under the threshold."""
    with patch(
        "infra_brain.callbacks.freshness.get_scheduler_heartbeat_status",
        return_value=(120.0, 7200.0, False),
    ):
        resp = client.get("/api/ops/heartbeat")

    assert resp.status_code == 200
    body = resp.json()
    assert body["heartbeat_age_seconds"] == 120.0
    assert body["max_age_seconds"] == 7200.0
    assert body["stale"] is False


def test_ops_heartbeat_stale(client):
    """Heartbeat older than the threshold reports stale=True."""
    with patch(
        "infra_brain.callbacks.freshness.get_scheduler_heartbeat_status",
        return_value=(10800.0, 7200.0, True),
    ):
        resp = client.get("/api/ops/heartbeat")

    assert resp.status_code == 200
    body = resp.json()
    assert body["stale"] is True
    assert body["heartbeat_age_seconds"] == 10800.0


def test_ops_heartbeat_never_recorded(client):
    """No heartbeat ever recorded: age is None, reported as stale."""
    with patch(
        "infra_brain.callbacks.freshness.get_scheduler_heartbeat_status",
        return_value=(None, 7200.0, True),
    ):
        resp = client.get("/api/ops/heartbeat")

    assert resp.status_code == 200
    body = resp.json()
    assert body["heartbeat_age_seconds"] is None
    assert body["stale"] is True


def test_ops_heartbeat_no_auth_required(client):
    """Consistent with /healthz and /health: no auth header needed."""
    with patch(
        "infra_brain.callbacks.freshness.get_scheduler_heartbeat_status",
        return_value=(60.0, 7200.0, False),
    ):
        resp = client.get("/api/ops/heartbeat")
    assert resp.status_code == 200


def test_gitlab_webhook_dispatches_cicd_agent(client, monkeypatch):
    """This test exercises dispatch/dedup wiring, not webhook auth — opt into the
    legacy-permissive WEBHOOK_AUTH_REQUIRED=false + INFRA_BRAIN_DEV=1 path so the
    empty X-Gitlab-Token doesn't 403 under F-034's fail-closed default (see the
    fail-closed tests below for the auth-required behavior itself)."""
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()
    payload = {
        "object_kind": "pipeline",
        "project": {"id": 1, "name": "infra-ops"},
        "object_attributes": {"status": "success"},
    }
    with (
        patch("infra_brain.webhooks.dispatch") as mock_dispatch,
        patch("infra_brain.webhooks.try_acquire", return_value=True),
    ):
        mock_dispatch.return_value = MagicMock(status="completed")
        resp = client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={"X-Gitlab-Token": ""},
        )
    assert resp.status_code == 202
    mock_dispatch.assert_called_once_with(domain="cicd", trigger_type="webhook", scope="all")


def test_manual_sweep_threads_dedup_token(client, monkeypatch):
    """Fix 1.2: manual_sweep must capture and pass the dedup token to _dispatch_bg.

    Not an auth test — opt into WEBHOOK_AUTH_REQUIRED=false + INFRA_BRAIN_DEV=1 so
    the unauthenticated request isn't rejected before reaching the dedup/dispatch
    logic under test.
    """
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()
    with (
        patch("infra_brain.webhooks._dispatch_bg") as mock_dispatch_bg,
        patch("infra_brain.webhooks.try_acquire", return_value="tok-123"),
    ):
        resp = client.post("/sweeps/linux")
    assert resp.status_code == 202
    # Verify _dispatch_bg was called with the token as the 4th arg
    mock_dispatch_bg.assert_called_once_with("linux", "manual", "all", "tok-123")


def test_sweep_unknown_domain_returns_404(client, monkeypatch):
    """Not an auth test — opt into WEBHOOK_AUTH_REQUIRED=false + INFRA_BRAIN_DEV=1 so
    the unauthenticated request reaches the domain-validation check under test instead
    of 403ing first."""
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()
    resp = client.post("/sweeps/unknowndomain")
    assert resp.status_code == 404


def test_dedup_returns_409_when_locked(client, monkeypatch):
    """Not an auth test — opt into WEBHOOK_AUTH_REQUIRED=false + INFRA_BRAIN_DEV=1 so
    the unauthenticated request reaches the dedup-lock check under test instead of
    403ing first."""
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()
    with patch("infra_brain.webhooks.try_acquire", return_value=False):
        resp = client.post("/sweeps/linux")
    assert resp.status_code == 409


# --- Webhook authentication tests ---


@pytest.fixture(autouse=False)
def clear_settings_cache():
    """Clear lru_cache on get_settings before and after each auth test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_gitlab_webhook_rejects_wrong_token(client, monkeypatch, clear_settings_cache):
    monkeypatch.setenv("WEBHOOK_GITLAB_SECRET", "correct-secret")
    get_settings.cache_clear()
    with patch("infra_brain.webhooks.try_acquire", return_value=True):
        resp = client.post(
            "/webhooks/gitlab",
            json={},
            headers={"X-Gitlab-Token": "wrong-secret"},
        )
    assert resp.status_code == 403


def test_gitlab_webhook_accepts_correct_token(client, monkeypatch, clear_settings_cache):
    monkeypatch.setenv("WEBHOOK_GITLAB_SECRET", "correct-secret")
    get_settings.cache_clear()
    with (
        patch("infra_brain.webhooks.dispatch") as mock_dispatch,
        patch("infra_brain.webhooks.try_acquire", return_value=True),
    ):
        mock_dispatch.return_value = MagicMock(status="completed")
        resp = client.post(
            "/webhooks/gitlab",
            json={},
            headers={"X-Gitlab-Token": "correct-secret"},
        )
    assert resp.status_code == 202


def test_gitlab_webhook_allows_when_secret_unset(client, monkeypatch, clear_settings_cache):
    """C4 made this path opt-in: fail-closed is now the default when the secret is
    unset, so this legacy-permissive scenario must explicitly request it via
    WEBHOOK_AUTH_REQUIRED=false."""
    monkeypatch.setenv("WEBHOOK_GITLAB_SECRET", "")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    get_settings.cache_clear()
    with (
        patch("infra_brain.webhooks.dispatch") as mock_dispatch,
        patch("infra_brain.webhooks.try_acquire", return_value=True),
    ):
        mock_dispatch.return_value = MagicMock(status="completed")
        resp = client.post("/webhooks/gitlab", json={})
    assert resp.status_code == 202


def test_octopus_webhook_rejects_wrong_token(client, monkeypatch, clear_settings_cache):
    monkeypatch.setenv("WEBHOOK_OCTOPUS_SECRET", "octopus-secret")
    get_settings.cache_clear()
    with patch("infra_brain.webhooks.try_acquire", return_value=True):
        resp = client.post(
            "/webhooks/octopus",
            json={},
            headers={"X-Octopus-Webhook-Token": "bad-token"},
        )
    assert resp.status_code == 403


def test_ansible_webhook_rejects_wrong_token(client, monkeypatch, clear_settings_cache):
    monkeypatch.setenv("WEBHOOK_ANSIBLE_SECRET", "ansible-secret")
    get_settings.cache_clear()
    with patch("infra_brain.webhooks.try_acquire", return_value=True):
        resp = client.post(
            "/webhooks/ansible",
            json={"host": "linux-host"},
            headers={"X-Ansible-Token": "bad-token"},
        )
    assert resp.status_code == 403


# --- Octopus accept-path (coverage gap) ---


def test_octopus_webhook_accepts_correct_token(client, monkeypatch, clear_settings_cache):
    """The accept path, with octopus revived — the domain is retired in this
    environment, and retirement is checked after auth (see the 409 test below),
    so reaching 202 at all requires turning it back on."""
    monkeypatch.setenv("WEBHOOK_OCTOPUS_SECRET", "octopus-secret")
    monkeypatch.setenv("COLLECTION_REVIVED_DOMAINS", "octopus")
    get_settings.cache_clear()
    with (
        patch("infra_brain.webhooks.dispatch") as mock_dispatch,
        patch("infra_brain.webhooks.try_acquire", return_value=True),
    ):
        mock_dispatch.return_value = MagicMock(status="completed")
        resp = client.post(
            "/webhooks/octopus",
            json={},
            headers={"X-Octopus-Webhook-Token": "octopus-secret"},
        )
    assert resp.status_code == 202


def test_webhook_for_a_retired_domain_is_refused_not_silently_accepted(
    client, monkeypatch, clear_settings_cache
):
    """octopus is retired (AgentSpec.retired). Without the gate in
    _trigger_domain, this route would still answer 202 Accepted and then fail
    inside the background task, where dispatch()'s ValueError is logged and
    never reaches the caller — so the sender would keep firing a webhook that
    can never do anything. 409 says why, and names the way back."""
    monkeypatch.setenv("WEBHOOK_OCTOPUS_SECRET", "octopus-secret")
    get_settings.cache_clear()
    with (
        patch("infra_brain.webhooks.dispatch") as mock_dispatch,
        patch("infra_brain.webhooks.try_acquire", return_value=True),
    ):
        resp = client.post(
            "/webhooks/octopus",
            json={},
            headers={"X-Octopus-Webhook-Token": "octopus-secret"},
        )
    assert resp.status_code == 409
    assert "retired" in resp.json()["detail"]
    assert "COLLECTION_REVIVED_DOMAINS=octopus" in resp.json()["detail"]
    mock_dispatch.assert_not_called()


# --- Generic / cloud / sweep / approve auth tests ---


def test_generic_webhook_rejects_wrong_token(client, monkeypatch, clear_settings_cache):
    monkeypatch.setenv("WEBHOOK_GENERIC_SECRET", "generic-secret")
    get_settings.cache_clear()
    with patch("infra_brain.webhooks.try_acquire", return_value=True):
        resp = client.post(
            "/webhooks/generic",
            json={"domain": "linux"},
            headers={"X-Infra-Token": "wrong-secret"},
        )
    assert resp.status_code == 403


def test_generic_webhook_allows_when_secret_unset(client, monkeypatch, clear_settings_cache):
    """C4 made this path opt-in: fail-closed is now the default when the secret is
    unset, so this legacy-permissive scenario must explicitly request it via
    WEBHOOK_AUTH_REQUIRED=false."""
    monkeypatch.setenv("WEBHOOK_GENERIC_SECRET", "")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    get_settings.cache_clear()
    with (
        patch("infra_brain.webhooks.dispatch") as mock_dispatch,
        patch("infra_brain.webhooks.try_acquire", return_value=True),
    ):
        mock_dispatch.return_value = MagicMock(status="completed")
        resp = client.post(
            "/webhooks/generic",
            json={"domain": "linux"},
        )
    assert resp.status_code == 202


def test_approve_integration_rejects_wrong_token(client, monkeypatch, clear_settings_cache):
    monkeypatch.setenv("WEBHOOK_GENERIC_SECRET", "generic-secret")
    get_settings.cache_clear()
    import uuid as _uuid

    proposal_id = str(_uuid.uuid4())
    resp = client.post(
        f"/integrations/{proposal_id}/approve",
        json={"approved_by": "tester"},
        headers={"X-Infra-Token": "wrong-secret"},
    )
    assert resp.status_code == 403


def test_approve_integration_allows_when_secret_unset(client, monkeypatch, clear_settings_cache):
    """C4 made this path opt-in: fail-closed is now the default when the secret is
    unset, so this legacy-permissive scenario must explicitly request it via
    WEBHOOK_AUTH_REQUIRED=false."""
    monkeypatch.setenv("WEBHOOK_GENERIC_SECRET", "")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    get_settings.cache_clear()
    import uuid as _uuid
    from unittest.mock import MagicMock, patch

    proposal_id = str(_uuid.uuid4())
    mock_proposal = MagicMock()
    mock_proposal.confidence = 0.9
    mock_proposal.status = "pending"
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = mock_proposal
    mock_coverage_agent = MagicMock()
    mock_coverage_agent.wire.return_value = True
    with (
        patch("infra_brain.webhooks.get_session", return_value=mock_session),
        patch(
            "infra_brain.agents.coverage.CoverageAgent",
            return_value=mock_coverage_agent,
        ),
    ):
        resp = client.post(
            f"/integrations/{proposal_id}/approve",
            json={"approved_by": "tester"},
        )
    assert resp.status_code == 200
    mock_coverage_agent.wire.assert_called_once_with(proposal_id)


def test_scan_points_rejects_wrong_token(client, monkeypatch, clear_settings_cache):
    """GET /scan_points must be gated the same as the other webhook-secret routes --
    it was previously reachable with no auth at all."""
    monkeypatch.setenv("WEBHOOK_GENERIC_SECRET", "generic-secret")
    get_settings.cache_clear()
    resp = client.get("/scan_points", headers={"X-Infra-Token": "wrong-secret"})
    assert resp.status_code == 403


def test_scan_points_allows_when_secret_unset(client, monkeypatch, clear_settings_cache):
    """C4 made this path opt-in: fail-closed is now the default when the secret is
    unset, so this legacy-permissive scenario must explicitly request it via
    WEBHOOK_AUTH_REQUIRED=false."""
    monkeypatch.setenv("WEBHOOK_GENERIC_SECRET", "")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    get_settings.cache_clear()
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.query.return_value.filter_by.return_value.all.return_value = []
    with patch("infra_brain.webhooks.get_session", return_value=mock_session):
        resp = client.get("/scan_points")
    assert resp.status_code == 200


# --- C4: webhook_auth_required fail-closed tests ---


def test_webhook_auth_required_fails_closed_when_secret_unset(
    client, monkeypatch, clear_settings_cache
):
    """C4: when webhook_auth_required=true and secret is empty, must return 403."""
    monkeypatch.setenv("WEBHOOK_GITLAB_SECRET", "")
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    with patch("infra_brain.webhooks.try_acquire", return_value="token123"):
        resp = client.post("/webhooks/gitlab", json={})
    assert resp.status_code == 403


def test_webhook_auth_required_false_allows_empty_secret(client, monkeypatch, clear_settings_cache):
    """F-034: webhook_auth_required=false alone no longer allows an empty secret --
    INFRA_BRAIN_DEV=1 must also be set explicitly (fail-closed by default)."""
    monkeypatch.setenv("WEBHOOK_GITLAB_SECRET", "")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    get_settings.cache_clear()
    with (
        patch("infra_brain.webhooks.dispatch") as mock_dispatch,
        patch("infra_brain.webhooks.try_acquire", return_value="token123"),
    ):
        mock_dispatch.return_value = MagicMock(status="completed")
        resp = client.post("/webhooks/gitlab", json={})
    assert resp.status_code == 202


def test_sweep_fails_closed_by_default(client, monkeypatch, clear_settings_cache):
    """F-034: with no secret and no INFRA_BRAIN_DEV flag, /sweeps rejects (403)."""
    monkeypatch.setenv("WEBHOOK_GENERIC_SECRET", "")
    monkeypatch.delenv("INFRA_BRAIN_DEV", raising=False)
    monkeypatch.delenv("WEBHOOK_AUTH_REQUIRED", raising=False)
    get_settings.cache_clear()
    resp = client.post("/sweeps/linux")
    assert resp.status_code == 403


def test_dispatch_bg_caps_concurrent_sweeps_with_bounded_semaphore(monkeypatch):
    """B5: _dispatch_bg must cap concurrent webhook-triggered sweeps at
    _MAX_CONCURRENT_WEBHOOK_SWEEPS via a threading.BoundedSemaphore (NOT an
    asyncio.Semaphore — _dispatch_bg runs in Starlette's BackgroundTasks
    threadpool, not on the event loop). A call arriving when the cap is
    already held must skip dispatch() (not block) and still release the
    dedup lock so a retry can pick the sweep back up."""
    import infra_brain.webhooks as webhooks_mod

    assert type(webhooks_mod._webhook_sweep_semaphore) is type(threading.BoundedSemaphore())

    released_tokens = []

    def fake_release(domain, scope, token=None):
        released_tokens.append((domain, scope, token))

    with patch("infra_brain.webhooks.dispatch") as mock_dispatch:
        # Exhaust the cap manually, then confirm the next call is skipped.
        acquired = [
            webhooks_mod._webhook_sweep_semaphore.acquire(blocking=False)
            for _ in range(webhooks_mod._MAX_CONCURRENT_WEBHOOK_SWEEPS)
        ]
        assert all(acquired)
        try:
            with patch("infra_brain.webhooks.release", side_effect=fake_release):
                webhooks_mod._dispatch_bg("linux", "webhook", "all", token="tok-over-cap")
            mock_dispatch.assert_not_called()
            assert ("linux", "all", "tok-over-cap") in released_tokens
        finally:
            for _ in acquired:
                webhooks_mod._webhook_sweep_semaphore.release()

    # Below the cap, dispatch() runs normally and the semaphore slot is freed.
    with (
        patch("infra_brain.webhooks.dispatch") as mock_dispatch,
        patch("infra_brain.webhooks.release", side_effect=fake_release),
    ):
        webhooks_mod._dispatch_bg("linux", "webhook", "all", token="tok-under-cap")
        mock_dispatch.assert_called_once_with(domain="linux", trigger_type="webhook", scope="all")
    # Semaphore slot released — the full cap can be acquired again.
    reacquired = [
        webhooks_mod._webhook_sweep_semaphore.acquire(blocking=False)
        for _ in range(webhooks_mod._MAX_CONCURRENT_WEBHOOK_SWEEPS)
    ]
    assert all(reacquired)
    for _ in reacquired:
        webhooks_mod._webhook_sweep_semaphore.release()


# ---------------------------------------------------------------------------
# Phase 2 Task 4: sweep_graph_enabled webhook routing.
#
# scope=="all" AND sweep_graph_enabled → run_sweep_sync(), NO route-level
# Redis lock (the sweep graph's collector node owns domain:all). Scoped
# triggers (scope != "all", e.g. the ansible per-host path) always stay on
# the legacy dispatch() path, in both modes.
# ---------------------------------------------------------------------------


def test_manual_sweep_routes_to_sweep_graph_when_enabled(client, monkeypatch):
    """scope=='all' + sweep_graph_enabled=True → run_sweep_sync() called, and
    the route-level Redis lock (try_acquire) must NOT be acquired."""
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.setenv("SWEEP_GRAPH_ENABLED", "true")
    get_settings.cache_clear()
    with (
        patch("infra_brain.graph.run_sweep_sync") as mock_run_sweep_sync,
        patch("infra_brain.webhooks.try_acquire") as mock_try_acquire,
        patch("infra_brain.webhooks._dispatch_bg") as mock_dispatch_bg,
    ):
        resp = client.post("/sweeps/linux")
    assert resp.status_code == 202
    mock_try_acquire.assert_not_called()
    mock_dispatch_bg.assert_not_called()
    mock_run_sweep_sync.assert_called_once_with(
        domains=["linux"], run_reasoners=False, trigger_type="webhook"
    )
    get_settings.cache_clear()


def test_ansible_host_scope_stays_legacy_when_sweep_enabled(client, monkeypatch):
    """Scoped triggers (host != 'all') always use the legacy dispatch() path,
    even when sweep_graph_enabled=True — the graph has no scope channel."""
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.setenv("SWEEP_GRAPH_ENABLED", "true")
    get_settings.cache_clear()
    with (
        patch("infra_brain.graph.run_sweep_sync") as mock_run_sweep_sync,
        patch("infra_brain.webhooks.try_acquire", return_value="tok-host") as mock_try_acquire,
        patch("infra_brain.webhooks._dispatch_bg") as mock_dispatch_bg,
    ):
        resp = client.post(
            "/webhooks/ansible",
            json={"host": "web01.example.com"},
            headers={"X-Ansible-Token": ""},
        )
    assert resp.status_code == 202
    mock_try_acquire.assert_called_once_with("linux", "web01.example.com")
    mock_dispatch_bg.assert_called_once_with("linux", "webhook", "web01.example.com", "tok-host")
    mock_run_sweep_sync.assert_not_called()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# TRK-242 (GitLab #113): incident-management ack-in webhook.
# ---------------------------------------------------------------------------


def test_incident_ack_rejects_wrong_token(client, monkeypatch, clear_settings_cache):
    monkeypatch.setenv("WEBHOOK_INCIDENT_SECRET", "incident-secret")
    get_settings.cache_clear()
    resp = client.post(
        "/webhooks/incident/ack",
        json={"incident_key": "drift:abc:field", "action": "acknowledge"},
        headers={"X-Incident-Token": "wrong-secret"},
    )
    assert resp.status_code == 403


def test_incident_ack_fails_closed_when_secret_unset(client, monkeypatch, clear_settings_cache):
    monkeypatch.setenv("WEBHOOK_INCIDENT_SECRET", "")
    monkeypatch.delenv("INFRA_BRAIN_DEV", raising=False)
    monkeypatch.delenv("WEBHOOK_AUTH_REQUIRED", raising=False)
    get_settings.cache_clear()
    resp = client.post(
        "/webhooks/incident/ack",
        json={"incident_key": "drift:abc:field"},
    )
    assert resp.status_code == 403


def test_incident_ack_unknown_incident_key_returns_404(client, monkeypatch, clear_settings_cache):
    monkeypatch.setenv("WEBHOOK_INCIDENT_SECRET", "incident-secret")
    get_settings.cache_clear()
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.query.return_value.filter_by.return_value.all.return_value = []
    with patch("infra_brain.webhooks.get_session", return_value=mock_session):
        resp = client.post(
            "/webhooks/incident/ack",
            json={"incident_key": "drift:no-such-key:field", "action": "acknowledge"},
            headers={"X-Incident-Token": "incident-secret"},
        )
    assert resp.status_code == 404


def test_incident_ack_updates_matching_drift_event_and_compliance_violation(
    client, monkeypatch, clear_settings_cache
):
    """Success case: a shared incident_key can match rows across BOTH tables
    (unlikely in practice given the disjoint dedup_key prefixes used by
    notify_incidents, but the endpoint itself does not assume single-table
    correlation) and every matching row's status is updated."""
    monkeypatch.setenv("WEBHOOK_INCIDENT_SECRET", "incident-secret")
    get_settings.cache_clear()

    mock_drift_event = MagicMock()
    mock_drift_event.status = "open"
    mock_violation = MagicMock()
    mock_violation.status = "open"

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    def fake_query(model):
        q = MagicMock()
        if model.__name__ == "DriftEvent":
            q.filter_by.return_value.all.return_value = [mock_drift_event]
        else:
            q.filter_by.return_value.all.return_value = [mock_violation]
        return q

    mock_session.query.side_effect = fake_query

    with patch("infra_brain.webhooks.get_session", return_value=mock_session):
        resp = client.post(
            "/webhooks/incident/ack",
            json={"incident_key": "drift:res-1:cpu", "action": "acknowledge"},
            headers={"X-Incident-Token": "incident-secret"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["acknowledged"] is True
    assert body["incident_key"] == "drift:res-1:cpu"
    assert body["updated"] == 2
    assert mock_drift_event.status == "acknowledged"
    assert mock_violation.status == "acknowledged"
    mock_session.commit.assert_called_once()


def test_incident_ack_resolve_action_sets_resolved_status(client, monkeypatch, clear_settings_cache):
    monkeypatch.setenv("WEBHOOK_INCIDENT_SECRET", "incident-secret")
    get_settings.cache_clear()

    mock_violation = MagicMock()
    mock_violation.status = "open"

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    def fake_query(model):
        q = MagicMock()
        if model.__name__ == "DriftEvent":
            q.filter_by.return_value.all.return_value = []
        else:
            q.filter_by.return_value.all.return_value = [mock_violation]
        return q

    mock_session.query.side_effect = fake_query

    with patch("infra_brain.webhooks.get_session", return_value=mock_session):
        resp = client.post(
            "/webhooks/incident/ack",
            json={"incident_key": "compliance:rule-1:host-1", "action": "resolve"},
            headers={"X-Incident-Token": "incident-secret"},
        )

    assert resp.status_code == 200
    assert resp.json()["updated"] == 1
    assert mock_violation.status == "resolved"


def test_incident_ack_allows_when_secret_unset_in_dev_mode(client, monkeypatch, clear_settings_cache):
    monkeypatch.setenv("WEBHOOK_INCIDENT_SECRET", "")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    get_settings.cache_clear()

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.query.return_value.filter_by.return_value.all.return_value = []

    with patch("infra_brain.webhooks.get_session", return_value=mock_session):
        resp = client.post(
            "/webhooks/incident/ack",
            json={"incident_key": "drift:x:y"},
        )
    # Auth passes (dev mode) but the key is unknown → 404, not 403.
    assert resp.status_code == 404


def test_manual_sweep_stays_legacy_when_sweep_disabled(client, monkeypatch):
    """Default (sweep_graph_enabled=False): scope=='all' still uses the legacy
    try_acquire + _dispatch_bg path — unchanged behavior."""
    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    monkeypatch.delenv("SWEEP_GRAPH_ENABLED", raising=False)
    get_settings.cache_clear()
    with (
        patch("infra_brain.graph.run_sweep_sync") as mock_run_sweep_sync,
        patch("infra_brain.webhooks.try_acquire", return_value="tok-legacy") as mock_try_acquire,
        patch("infra_brain.webhooks._dispatch_bg") as mock_dispatch_bg,
    ):
        resp = client.post("/sweeps/linux")
    assert resp.status_code == 202
    mock_try_acquire.assert_called_once_with("linux", "all")
    mock_dispatch_bg.assert_called_once_with("linux", "manual", "all", "tok-legacy")
    mock_run_sweep_sync.assert_not_called()
    get_settings.cache_clear()


# --- Regression: webhook approve routes must not diverge from the shared
# guards (audit findings, both against src/infra_brain/webhooks.py) ---------


def _webhook_router_client(engine, monkeypatch):
    """Same helper shape as test_remediation_graph.py's ``_webhook_client`` —
    a bare TestClient wired only to webhooks.router, auth disabled via
    dev-mode, and a contextmanager ``get_session`` bound to *engine*."""
    from contextlib import contextmanager

    from fastapi import FastAPI
    from sqlalchemy.orm import Session

    from infra_brain.webhooks import router

    monkeypatch.setenv("WEBHOOK_AUTH_REQUIRED", "false")
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    app = FastAPI()
    app.include_router(router)
    return TestClient(app), _get_session


def test_approve_action_refuses_entity_resolution_review_rows(monkeypatch):
    """TRK-226 (webhook surface): the webhook's POST /actions/{id}/approve
    must reject an entity_resolution_same_as review row exactly like the
    dashboard's mirror route does (test_remediation_graph.py's
    test_dashboard_approve_refuses_entity_resolution_review_rows) -- no agent
    ever re-checks these rows after a generic approve flips their status, so
    approving here would permanently close the identity question with no
    SAME_AS edge ever written and no way to recover. Must 409, pointing at
    the real confirm route, and must NOT flip status or resume any graph."""
    import uuid as _uuid
    from unittest.mock import AsyncMock, patch
    from sqlalchemy.orm import Session

    from infra_brain.action_decisions import REVIEW_ACTION_APPROVE_DETAIL
    from infra_brain.db.models import ProposedAction

    engine = make_engine()

    with Session(engine) as s:
        review_action = ProposedAction(
            agent="graph_phase3.queue_for_review",
            action_type="entity_resolution_same_as",
            target="same-as:VsphereVM:web01",
            payload={"source_node": {"node_id": str(_uuid.uuid4())}, "candidate_matches": []},
            confidence=0.85,
            status="pending",
        )
        s.add(review_action)
        s.commit()
        review_id = str(review_action.id)

    client, get_session_cm = _webhook_router_client(engine, monkeypatch)
    resume = AsyncMock()
    with (
        patch("infra_brain.webhooks.get_session", get_session_cm),
        patch("infra_brain.webhooks.resume_remediation_action", resume),
    ):
        resp = client.post(f"/actions/{review_id}/approve", json={})

    assert resp.status_code == 409
    assert resp.json()["detail"] == REVIEW_ACTION_APPROVE_DETAIL
    resume.assert_not_awaited()
    with Session(engine) as s:
        assert s.get(ProposedAction, _uuid.UUID(review_id)).status == "pending"


def test_reject_action_refuses_entity_resolution_review_rows(monkeypatch):
    """KG-3: the generic reject route must 409 a review row, mirroring approve.

    A bare status flip here is exactly the defect: unattributed, bundle-scoped,
    and invisible to the identity emitters, so the next resolve_entities pass
    would happily auto-merge the pair the human just rejected. The row must
    stay pending and the caller must be pointed at the route that writes a real
    pair-scoped NOT_SAME_AS veto.
    """
    import uuid as _uuid
    from unittest.mock import AsyncMock, patch
    from sqlalchemy.orm import Session

    from infra_brain.action_decisions import REVIEW_ACTION_REJECT_DETAIL
    from infra_brain.db.models import ProposedAction

    engine = make_engine()

    with Session(engine) as s:
        review_action = ProposedAction(
            agent="graph_phase3.queue_for_review",
            action_type="entity_resolution_same_as",
            target="same-as:VsphereVM:web01",
            payload={"source_node": {"node_id": str(_uuid.uuid4())}, "candidate_matches": []},
            confidence=0.85,
            status="pending",
        )
        s.add(review_action)
        s.commit()
        review_id = str(review_action.id)

    client, get_session_cm = _webhook_router_client(engine, monkeypatch)
    resume = AsyncMock()
    with (
        patch("infra_brain.webhooks.get_session", get_session_cm),
        patch("infra_brain.webhooks.resume_remediation_action", resume),
    ):
        resp = client.post(f"/actions/{review_id}/reject", json={})

    assert resp.status_code == 409
    assert resp.json()["detail"] == REVIEW_ACTION_REJECT_DETAIL
    resume.assert_not_awaited()
    with Session(engine) as s:
        assert s.get(ProposedAction, _uuid.UUID(review_id)).status == "pending"


def test_approve_integration_requires_pending_status(monkeypatch):
    """A proposal that is already 'approved' (or 'wired') must not be
    re-approved/re-wired -- without this guard, a webhook caller could reset
    an already-wired proposal back to 'approved' with no wire() call, and any
    later retry through the dashboard's own approve-and-wire endpoint would
    then 409 because that route requires status=='pending'."""
    import uuid as _uuid
    from unittest.mock import MagicMock, patch
    from sqlalchemy.orm import Session

    from infra_brain.db.models import IntegrationProposal

    engine = make_engine()

    with Session(engine) as s:
        proposal = IntegrationProposal(
            source="cloud:aws",
            type="cloud",
            endpoint="https://example.invalid",
            confidence=0.9,
            status="wired",
        )
        s.add(proposal)
        s.commit()
        proposal_id = str(proposal.id)

    client, get_session_cm = _webhook_router_client(engine, monkeypatch)
    mock_coverage_agent = MagicMock()
    with (
        patch("infra_brain.webhooks.get_session", get_session_cm),
        patch("infra_brain.agents.coverage.CoverageAgent", return_value=mock_coverage_agent),
    ):
        resp = client.post(
            f"/integrations/{proposal_id}/approve", json={"approved_by": "tester"}
        )

    assert resp.status_code == 409
    mock_coverage_agent.wire.assert_not_called()
    with Session(engine) as s:
        assert s.get(IntegrationProposal, _uuid.UUID(proposal_id)).status == "wired"


def test_approve_integration_wires_on_success(monkeypatch):
    """Confirms the fix: a pending proposal approved through the webhook route
    must actually call CoverageAgent.wire() in the same request (previously it
    only flipped status='approved' and returned 200, so the proposal was
    silently never activated)."""
    from unittest.mock import MagicMock, patch
    from sqlalchemy.orm import Session

    from infra_brain.db.models import IntegrationProposal

    engine = make_engine()

    with Session(engine) as s:
        proposal = IntegrationProposal(
            source="cloud:aws",
            type="cloud",
            endpoint="https://example.invalid",
            confidence=0.9,
            status="pending",
        )
        s.add(proposal)
        s.commit()
        proposal_id = str(proposal.id)

    client, get_session_cm = _webhook_router_client(engine, monkeypatch)
    mock_coverage_agent = MagicMock()
    mock_coverage_agent.wire.return_value = True
    with (
        patch("infra_brain.webhooks.get_session", get_session_cm),
        patch("infra_brain.agents.coverage.CoverageAgent", return_value=mock_coverage_agent),
    ):
        resp = client.post(
            f"/integrations/{proposal_id}/approve", json={"approved_by": "tester"}
        )

    assert resp.status_code == 200
    mock_coverage_agent.wire.assert_called_once_with(proposal_id)


def test_approve_integration_reverts_to_pending_when_wire_fails(monkeypatch):
    """A wire() failure (clean False return) must not strand the proposal at
    status='approved' -- that dead end blocks both a retry through this route
    (no longer 'pending') and the dashboard's own approve-and-wire endpoint.
    Must revert to 'pending' and return an error status."""
    import uuid as _uuid
    from unittest.mock import MagicMock, patch
    from sqlalchemy.orm import Session

    from infra_brain.db.models import IntegrationProposal

    engine = make_engine()

    with Session(engine) as s:
        proposal = IntegrationProposal(
            source="cloud:aws",
            type="cloud",
            endpoint="https://example.invalid",
            confidence=0.9,
            status="pending",
        )
        s.add(proposal)
        s.commit()
        proposal_id = str(proposal.id)

    client, get_session_cm = _webhook_router_client(engine, monkeypatch)
    mock_coverage_agent = MagicMock()
    mock_coverage_agent.wire.return_value = False
    with (
        patch("infra_brain.webhooks.get_session", get_session_cm),
        patch("infra_brain.agents.coverage.CoverageAgent", return_value=mock_coverage_agent),
    ):
        resp = client.post(
            f"/integrations/{proposal_id}/approve", json={"approved_by": "tester"}
        )

    assert resp.status_code == 500
    with Session(engine) as s:
        reverted = s.get(IntegrationProposal, _uuid.UUID(proposal_id))
        assert reverted.status == "pending"
        assert reverted.approved_by is None
        assert reverted.approved_at is None
