"""Unit tests for docker/deadman-prober/prober.py.

prober.py is a STANDALONE module (must never import infra_brain — see its
module docstring), so it is loaded here by file path rather than as a package
import. Network calls (urllib.request.urlopen) are mocked; nothing in this
test file makes a real HTTP request.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

PROBER_PATH = Path(__file__).resolve().parents[1] / "docker" / "deadman-prober" / "prober.py"


def _load_prober():
    spec = importlib.util.spec_from_file_location("deadman_prober", PROBER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["deadman_prober"] = module
    spec.loader.exec_module(module)
    return module


prober = _load_prober()


def test_prober_module_does_not_import_infra_brain():
    """Standalone-by-design guarantee: no `import infra_brain` anywhere in the
    file (the docstring mentions the package by name to explain why — that's
    fine; an actual import statement is not)."""
    text = PROBER_PATH.read_text()
    assert "import infra_brain" not in text
    assert "from infra_brain" not in text


# ---------------------------------------------------------------------------
# EndpointState — consecutive-failure counting + edge-triggered alerting
# ---------------------------------------------------------------------------


def test_endpoint_state_no_alert_before_threshold():
    state = prober.EndpointState("healthz")
    assert state.record_failure("boom") is None
    assert state.record_failure("boom") is None
    assert state.consecutive_failures == 2


def test_endpoint_state_alerts_on_third_consecutive_failure():
    state = prober.EndpointState("healthz")
    assert state.record_failure("boom") is None
    assert state.record_failure("boom") is None
    msg = state.record_failure("boom")
    assert msg is not None
    assert "3 consecutive failures" in msg
    assert state.alerting is True


def test_endpoint_state_does_not_spam_after_already_alerting():
    state = prober.EndpointState("healthz")
    for _ in range(3):
        state.record_failure("boom")
    assert state.alerting is True
    # A 4th, 5th failure must not re-alert.
    assert state.record_failure("boom") is None
    assert state.record_failure("boom") is None


def test_endpoint_state_success_resets_counter():
    state = prober.EndpointState("healthz")
    state.record_failure("boom")
    state.record_failure("boom")
    state.record_success()
    assert state.consecutive_failures == 0
    # Two more failures after a reset must not immediately re-trigger.
    assert state.record_failure("boom") is None


def test_endpoint_state_recovery_message_only_after_alerting():
    state = prober.EndpointState("healthz")
    # Success while never alerting produces no recovery message.
    assert state.record_success() is None
    for _ in range(3):
        state.record_failure("boom")
    assert state.alerting is True
    msg = state.record_success()
    assert msg == "healthz: recovered"
    assert state.alerting is False


# ---------------------------------------------------------------------------
# HeartbeatState — alerts ONLY on stale=True, never on mere age
# ---------------------------------------------------------------------------


def test_heartbeat_state_no_alert_when_fresh():
    state = prober.HeartbeatState()
    assert state.observe(stale=False, age_seconds=300.0, max_age_seconds=7200.0) is None


def test_heartbeat_state_alerts_on_stale():
    state = prober.HeartbeatState()
    msg = state.observe(stale=True, age_seconds=10800.0, max_age_seconds=7200.0)
    assert msg is not None
    assert "stale" in msg
    assert state.alerting is True


def test_heartbeat_state_never_recorded_still_alerts_with_stale_true():
    """age_seconds=None (never recorded) must still be reported via stale=True,
    never inferred independently by the prober itself."""
    state = prober.HeartbeatState()
    msg = state.observe(stale=True, age_seconds=None, max_age_seconds=7200.0)
    assert msg is not None
    assert "never recorded" in msg


def test_heartbeat_state_does_not_spam_while_still_stale():
    state = prober.HeartbeatState()
    state.observe(stale=True, age_seconds=10800.0, max_age_seconds=7200.0)
    assert state.observe(stale=True, age_seconds=10900.0, max_age_seconds=7200.0) is None


def test_heartbeat_state_recovery_message():
    state = prober.HeartbeatState()
    state.observe(stale=True, age_seconds=10800.0, max_age_seconds=7200.0)
    msg = state.observe(stale=False, age_seconds=60.0, max_age_seconds=7200.0)
    assert msg == "scheduler heartbeat: recovered"
    assert state.alerting is False


# ---------------------------------------------------------------------------
# Webhook payload shape
# ---------------------------------------------------------------------------


def test_build_alert_payload_shape_matches_ops_webhook():
    payload = prober.build_alert_payload("endpoint_health", ["healthz: down"])
    assert payload == {"category": "endpoint_health", "messages": ["healthz: down"]}


def test_send_alert_includes_bearer_token_when_configured():
    captured = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        captured["url"] = req.full_url
        return FakeResp()

    with patch.object(prober.urllib.request, "urlopen", side_effect=fake_urlopen):
        ok = prober.send_alert(
            "http://ops.example/webhook", "secret-token", "endpoint_health", ["down"], timeout=5.0
        )

    assert ok is True
    assert captured["headers"]["Authorization"] == "Bearer secret-token"


def test_send_alert_no_token_omits_auth_header():
    captured = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        return FakeResp()

    with patch.object(prober.urllib.request, "urlopen", side_effect=fake_urlopen):
        prober.send_alert(
            "http://ops.example/webhook", None, "endpoint_health", ["down"], timeout=5.0
        )

    assert "Authorization" not in captured["headers"]


def test_send_alert_unconfigured_webhook_is_a_noop_success():
    ok = prober.send_alert("", "token", "endpoint_health", ["down"], timeout=5.0)
    assert ok is True


def test_send_alert_delivery_failure_returns_false():
    with patch.object(prober.urllib.request, "urlopen", side_effect=OSError("refused")):
        ok = prober.send_alert("http://ops.example/webhook", None, "cat", ["msg"], timeout=5.0)
    assert ok is False


# ---------------------------------------------------------------------------
# probe_heartbeat — endpoint failure vs. staleness are independent signals
# ---------------------------------------------------------------------------


def test_probe_heartbeat_ok_and_not_stale():
    with patch.object(
        prober,
        "_http_get_json",
        return_value=(
            200,
            {"stale": False, "heartbeat_age_seconds": 60.0, "max_age_seconds": 7200.0},
        ),
    ):
        ok, detail, body = prober.probe_heartbeat("http://app:8000", timeout=5.0)
    assert ok is True
    assert body["stale"] is False


def test_probe_heartbeat_endpoint_down_counts_as_endpoint_failure_not_staleness():
    with patch.object(prober, "_http_get_json", side_effect=OSError("connection refused")):
        ok, detail, body = prober.probe_heartbeat("http://app:8000", timeout=5.0)
    assert ok is False
    assert body is None


def test_probe_heartbeat_malformed_body_counts_as_endpoint_failure():
    with patch.object(prober, "_http_get_json", return_value=(200, {"unexpected": "shape"})):
        ok, detail, body = prober.probe_heartbeat("http://app:8000", timeout=5.0)
    assert ok is False
    assert body is None


# ---------------------------------------------------------------------------
# Prober.run_once — end-to-end wiring of the three probes + alert delivery
# ---------------------------------------------------------------------------


def _make_prober():
    return prober.Prober(app_url="http://app:8000", webhook_url="", webhook_token=None)


def test_run_once_all_healthy_sends_nothing():
    p = _make_prober()
    with (
        patch.object(prober, "probe_endpoint", return_value=(True, "HTTP 200")),
        patch.object(
            prober,
            "probe_heartbeat",
            return_value=(
                True,
                "HTTP 200",
                {"stale": False, "heartbeat_age_seconds": 60.0, "max_age_seconds": 7200.0},
            ),
        ),
    ):
        sent = p.run_once()
    assert sent == []


def test_run_once_endpoint_alerts_only_after_three_cycles():
    p = _make_prober()
    with (
        patch.object(prober, "probe_endpoint", return_value=(False, "HTTP 500")),
        patch.object(
            prober,
            "probe_heartbeat",
            return_value=(
                True,
                "HTTP 200",
                {"stale": False, "heartbeat_age_seconds": 60.0, "max_age_seconds": 7200.0},
            ),
        ),
    ):
        assert p.run_once() == []
        assert p.run_once() == []
        sent = p.run_once()
    # Both healthz and health independently cross the threshold on cycle 3.
    assert any("healthz" in m for m in sent)
    assert any("health" in m for m in sent)


def test_run_once_heartbeat_stale_alerts_immediately_not_after_three_cycles():
    """Staleness is not gated by the 3-consecutive-failure rule — it fires on
    the first cycle it's observed, because it's not a connectivity failure."""
    p = _make_prober()
    with (
        patch.object(prober, "probe_endpoint", return_value=(True, "HTTP 200")),
        patch.object(
            prober,
            "probe_heartbeat",
            return_value=(
                True,
                "HTTP 200",
                {"stale": True, "heartbeat_age_seconds": 10800.0, "max_age_seconds": 7200.0},
            ),
        ),
    ):
        sent = p.run_once()
    assert any("stale" in m for m in sent)


def test_run_once_recovery_after_incident():
    p = _make_prober()
    with (
        patch.object(prober, "probe_endpoint", return_value=(False, "HTTP 500")),
        patch.object(
            prober,
            "probe_heartbeat",
            return_value=(
                True,
                "HTTP 200",
                {"stale": False, "heartbeat_age_seconds": 60.0, "max_age_seconds": 7200.0},
            ),
        ),
    ):
        p.run_once()
        p.run_once()
        p.run_once()  # alert fires here

    with (
        patch.object(prober, "probe_endpoint", return_value=(True, "HTTP 200")),
        patch.object(
            prober,
            "probe_heartbeat",
            return_value=(
                True,
                "HTTP 200",
                {"stale": False, "heartbeat_age_seconds": 60.0, "max_age_seconds": 7200.0},
            ),
        ),
    ):
        sent = p.run_once()

    assert any("recovered" in m for m in sent)
