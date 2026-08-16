"""Tests for the outbound webhook subscription/event-publish system (GitLab #112).

Uses a real (sqlite, in-memory) session — publish_event/retry_pending_deliveries
read/write WebhookSubscription/WebhookDelivery rows directly, unlike the
single-config-value tools/ops_webhook.py path, so a fully mocked session isn't
a faithful test here (matches tests/test_mcp_keys_api.py's pattern for a
router that's genuinely DB-shaped).
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    DELIVERY_DELIVERED,
    DELIVERY_EXHAUSTED,
    DELIVERY_RETRYING,
    WebhookDelivery,
    WebhookSubscription,
)
from infra_brain.tools import webhook_publish

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def session_ctx(engine, monkeypatch):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    monkeypatch.setattr(webhook_publish, "get_session", _get_session)
    return _get_session


@pytest.fixture
def _skip_ssrf_dns_lookup(monkeypatch):
    """These tests exercise matching/delivery/retry logic against fake
    *.example.com targets, not the SSRF guard itself (that has its own
    dedicated TestSsrfGuard class below, using literal IPs so it needs no
    real DNS). Real socket.getaddrinfo() would otherwise reject every test
    here in any network-isolated sandbox/CI runner (NXDOMAIN for a
    non-resolvable example.com), which is exactly the failure mode this
    fixture avoids -- default to "safe" so the guard is a no-op unless a
    test explicitly overrides it. Applied per-class (not module-wide
    autouse) so TestSsrfGuard below can exercise the real guard logic."""
    monkeypatch.setattr(webhook_publish, "_is_ssrf_safe_target", lambda url: True)


def _settings(**overrides):
    from types import SimpleNamespace

    defaults = dict(api_timeout_seconds=30)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _add_subscription(engine, **overrides) -> str:
    defaults = dict(
        name="pagerduty",
        target_url="https://hooks.example.com/pd",
        event_pattern="*",
        domain_filter=None,
        active=True,
    )
    defaults.update(overrides)
    with Session(engine) as s:
        row = WebhookSubscription(**defaults)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def _mock_gate():
    return patch("infra_brain.tools.webhook_publish.gate_external_write", return_value=None)


class TestMatching:
    def test_wildcard_matches_everything(self):
        sub = WebhookSubscription(event_pattern="*", domain_filter=None)
        assert webhook_publish._matches(sub, "drift.critical", "vsphere") is True
        assert webhook_publish._matches(sub, "collection_health", None) is True

    def test_prefix_glob_matches_prefix_only(self):
        sub = WebhookSubscription(event_pattern="drift.*", domain_filter=None)
        assert webhook_publish._matches(sub, "drift.critical", None) is True
        assert webhook_publish._matches(sub, "drift.incident", None) is True
        assert webhook_publish._matches(sub, "compliance.incident", None) is False

    def test_exact_pattern_matches_only_itself(self):
        sub = WebhookSubscription(event_pattern="drift.critical", domain_filter=None)
        assert webhook_publish._matches(sub, "drift.critical", None) is True
        assert webhook_publish._matches(sub, "drift.critical.extra", None) is False

    def test_domain_filter_is_an_and_condition(self):
        sub = WebhookSubscription(event_pattern="drift.*", domain_filter="vsphere")
        assert webhook_publish._matches(sub, "drift.critical", "vsphere") is True
        assert webhook_publish._matches(sub, "drift.critical", "linux") is False
        assert webhook_publish._matches(sub, "drift.critical", None) is False

    def test_domain_filter_case_insensitive(self):
        sub = WebhookSubscription(event_pattern="*", domain_filter="VSphere")
        assert webhook_publish._matches(sub, "drift.critical", "vsphere") is True


class TestPublishEvent:
    @pytest.fixture(autouse=True)
    def _ssrf_bypass(self, _skip_ssrf_dns_lookup):
        pass

    def test_noop_when_no_messages(self, session_ctx):
        result = webhook_publish.publish_event("drift.critical", [])
        assert result == {"matched": 0, "delivered": 0, "failed": 0}

    def test_noop_when_no_matching_subscriptions(self, engine, session_ctx):
        _add_subscription(engine, event_pattern="compliance.*")
        with patch(
            "infra_brain.config.get_settings", return_value=_settings()
        ):
            result = webhook_publish.publish_event("drift.critical", ["msg"])
        assert result == {"matched": 0, "delivered": 0, "failed": 0}

    def test_delivers_to_matching_active_subscription(self, engine, session_ctx):
        sub_id = _add_subscription(engine, event_pattern="drift.*")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with (
            patch(
                "infra_brain.config.get_settings", return_value=_settings()
            ),
            patch(
                "infra_brain.tools.webhook_publish.httpx.post", return_value=mock_resp
            ) as mock_post,
            _mock_gate(),
        ):
            result = webhook_publish.publish_event(
                "drift.critical", ["vsphere/web01 drifted"], domain="vsphere"
            )

        assert result == {"matched": 1, "delivered": 1, "failed": 0}
        assert mock_post.call_args.args[0] == "https://hooks.example.com/pd"

        with Session(engine) as s:
            rows = s.query(WebhookDelivery).filter_by(subscription_id=sub_id).all()
            assert len(rows) == 1
            assert rows[0].status == DELIVERY_DELIVERED

    def test_ignores_inactive_subscription(self, engine, session_ctx):
        _add_subscription(engine, active=False)
        with (
            patch(
                "infra_brain.config.get_settings", return_value=_settings()
            ),
            patch("infra_brain.tools.webhook_publish.httpx.post") as mock_post,
        ):
            result = webhook_publish.publish_event("drift.critical", ["msg"])

        assert result == {"matched": 0, "delivered": 0, "failed": 0}
        mock_post.assert_not_called()

    def test_failure_creates_retrying_delivery_row(self, engine, session_ctx):
        sub_id = _add_subscription(engine)
        with (
            patch(
                "infra_brain.config.get_settings", return_value=_settings()
            ),
            patch(
                "infra_brain.tools.webhook_publish.httpx.post",
                side_effect=RuntimeError("connection refused"),
            ),
            _mock_gate(),
        ):
            result = webhook_publish.publish_event("drift.critical", ["msg"])

        assert result == {"matched": 1, "delivered": 0, "failed": 1}
        with Session(engine) as s:
            row = s.query(WebhookDelivery).filter_by(subscription_id=sub_id).one()
            assert row.status == DELIVERY_RETRYING
            assert row.attempt_count == 1
            assert row.next_attempt_at is not None
            assert "connection refused" in row.last_error

    def test_one_failing_destination_does_not_block_another(self, engine, session_ctx):
        _add_subscription(engine, name="a", target_url="https://a.example.com/hook")
        _add_subscription(engine, name="b", target_url="https://b.example.com/hook")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        def _side_effect(url, **kwargs):
            if url == "https://a.example.com/hook":
                raise RuntimeError("boom")
            return mock_resp

        with (
            patch(
                "infra_brain.config.get_settings", return_value=_settings()
            ),
            patch(
                "infra_brain.tools.webhook_publish.httpx.post", side_effect=_side_effect
            ),
            _mock_gate(),
        ):
            result = webhook_publish.publish_event("drift.critical", ["msg"])

        assert result == {"matched": 2, "delivered": 1, "failed": 1}

    def test_write_gate_deny_is_recorded_as_exhausted_not_raised(self, engine, session_ctx):
        """A DLP-deny (PermissionError from gate_external_write) for one
        subscription must not abort publish_event()'s fan-out -- it should
        be recorded as a permanently-failed WebhookDelivery row (retrying
        would just trip the same gate again), matching how
        retry_pending_deliveries() already handles this same exception."""
        sub_id = _add_subscription(engine)
        with (
            patch(
                "infra_brain.config.get_settings", return_value=_settings()
            ),
            patch(
                "infra_brain.tools.webhook_publish.gate_external_write",
                side_effect=PermissionError("PAN detected"),
            ),
        ):
            result = webhook_publish.publish_event("drift.critical", ["card 4111111111111111"])

        assert result == {"matched": 1, "delivered": 0, "failed": 1}
        with Session(engine) as s:
            row = s.query(WebhookDelivery).filter_by(subscription_id=sub_id).one()
            assert row.status == DELIVERY_EXHAUSTED
            assert row.next_attempt_at is None
            assert "PAN detected" in row.last_error

    def test_write_gate_deny_on_one_subscription_does_not_block_another(
        self, engine, session_ctx
    ):
        """Reproduces the fan-out-abort bug directly: subscription 'a' trips
        the DLP gate, subscription 'b' does not -- 'b' must still receive
        the event instead of the PermissionError unwinding out of
        publish_event() before 'b' is ever reached."""
        _add_subscription(engine, name="a", target_url="https://a.example.com/hook")
        sub_b = _add_subscription(engine, name="b", target_url="https://b.example.com/hook")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        def _gate(*, system, operation, payload, agent_name):
            # Deny only the subscription whose id is embedded in `operation`
            # (see _deliver: f"publish_event:{category}:{subscription.id}").
            if str(sub_b) not in operation:
                raise PermissionError("PAN detected")
            return None

        with (
            patch(
                "infra_brain.config.get_settings", return_value=_settings()
            ),
            patch(
                "infra_brain.tools.webhook_publish.httpx.post", return_value=mock_resp
            ) as mock_post,
            patch(
                "infra_brain.tools.webhook_publish.gate_external_write",
                side_effect=_gate,
            ),
        ):
            result = webhook_publish.publish_event("drift.critical", ["msg"])

        assert result == {"matched": 2, "delivered": 1, "failed": 1}
        # Subscription b's target was actually POSTed to despite a's deny.
        assert mock_post.call_args.args[0] == "https://b.example.com/hook"


class TestRetryPendingDeliveries:
    @pytest.fixture(autouse=True)
    def _ssrf_bypass(self, _skip_ssrf_dns_lookup):
        pass

    def _add_delivery(self, engine, sub_id, **overrides):
        defaults = dict(
            subscription_id=sub_id,
            category="drift.critical",
            domain="vsphere",
            status=DELIVERY_RETRYING,
            attempt_count=1,
            max_attempts=3,
            payload_summary="msg one",
            next_attempt_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        defaults.update(overrides)
        with Session(engine) as s:
            row = WebhookDelivery(**defaults)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.id

    def test_not_yet_due_rows_are_untouched(self, engine, session_ctx):
        sub_id = _add_subscription(engine)
        self._add_delivery(
            engine, sub_id, next_attempt_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        with patch(
            "infra_brain.config.get_settings", return_value=_settings()
        ):
            result = webhook_publish.retry_pending_deliveries()
        assert result == {"attempted": 0, "delivered": 0, "exhausted": 0, "rescheduled": 0}

    def test_successful_retry_marks_delivered(self, engine, session_ctx):
        sub_id = _add_subscription(engine)
        delivery_id = self._add_delivery(engine, sub_id)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with (
            patch(
                "infra_brain.config.get_settings", return_value=_settings()
            ),
            patch("infra_brain.tools.webhook_publish.httpx.post", return_value=mock_resp),
            _mock_gate(),
        ):
            result = webhook_publish.retry_pending_deliveries()

        assert result == {"attempted": 1, "delivered": 1, "exhausted": 0, "rescheduled": 0}
        with Session(engine) as s:
            row = s.get(WebhookDelivery, delivery_id)
            assert row.status == DELIVERY_DELIVERED
            assert row.attempt_count == 2
            assert row.delivered_at is not None

    def test_repeated_failure_reschedules_with_backoff(self, engine, session_ctx):
        sub_id = _add_subscription(engine)
        delivery_id = self._add_delivery(engine, sub_id, attempt_count=1, max_attempts=5)

        with (
            patch(
                "infra_brain.config.get_settings", return_value=_settings()
            ),
            patch(
                "infra_brain.tools.webhook_publish.httpx.post",
                side_effect=RuntimeError("still down"),
            ),
            _mock_gate(),
        ):
            result = webhook_publish.retry_pending_deliveries()

        assert result == {"attempted": 1, "delivered": 0, "exhausted": 0, "rescheduled": 1}
        with Session(engine) as s:
            row = s.get(WebhookDelivery, delivery_id)
            assert row.status == DELIVERY_RETRYING
            assert row.attempt_count == 2
            # sqlite round-trips DateTime(timezone=True) as naive — compare
            # naive-to-naive (still proves the reschedule moved forward).
            next_attempt = row.next_attempt_at
            now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
            if next_attempt.tzinfo is not None:
                next_attempt = next_attempt.astimezone(timezone.utc).replace(tzinfo=None)
            assert next_attempt > now_naive

    def test_exhausts_after_max_attempts(self, engine, session_ctx):
        sub_id = _add_subscription(engine)
        delivery_id = self._add_delivery(engine, sub_id, attempt_count=4, max_attempts=5)

        with (
            patch(
                "infra_brain.config.get_settings", return_value=_settings()
            ),
            patch(
                "infra_brain.tools.webhook_publish.httpx.post",
                side_effect=RuntimeError("still down"),
            ),
            _mock_gate(),
        ):
            result = webhook_publish.retry_pending_deliveries()

        assert result == {"attempted": 1, "delivered": 0, "exhausted": 1, "rescheduled": 0}
        with Session(engine) as s:
            row = s.get(WebhookDelivery, delivery_id)
            assert row.status == DELIVERY_EXHAUSTED

    def test_inactive_subscription_exhausts_immediately(self, engine, session_ctx):
        sub_id = _add_subscription(engine, active=False)
        delivery_id = self._add_delivery(engine, sub_id)

        with (
            patch(
                "infra_brain.config.get_settings", return_value=_settings()
            ),
            patch("infra_brain.tools.webhook_publish.httpx.post") as mock_post,
        ):
            result = webhook_publish.retry_pending_deliveries()

        assert result == {"attempted": 1, "delivered": 0, "exhausted": 1, "rescheduled": 0}
        mock_post.assert_not_called()
        with Session(engine) as s:
            row = s.get(WebhookDelivery, delivery_id)
            assert row.status == DELIVERY_EXHAUSTED

    def test_write_gate_deny_exhausts_without_further_retry(self, engine, session_ctx):
        sub_id = _add_subscription(engine)
        delivery_id = self._add_delivery(engine, sub_id, attempt_count=1, max_attempts=5)

        with (
            patch(
                "infra_brain.config.get_settings", return_value=_settings()
            ),
            patch(
                "infra_brain.tools.webhook_publish.gate_external_write",
                side_effect=PermissionError("PAN detected"),
            ),
        ):
            result = webhook_publish.retry_pending_deliveries()

        assert result == {"attempted": 1, "delivered": 0, "exhausted": 1, "rescheduled": 0}
        with Session(engine) as s:
            row = s.get(WebhookDelivery, delivery_id)
            assert row.status == DELIVERY_EXHAUSTED
            assert "PAN detected" in row.last_error


class TestPayloadSummaryRoundTrip:
    """GitLab audit finding: payload_summary was built with
    " | ".join(messages)[:2000] and reconstructed on retry via a naive
    .split(" | "), which corrupts the retried payload whenever a message
    contains the literal substring " | " or the join was truncated
    mid-message. _summarize_payload/_parse_payload_summary must round-trip
    exactly for the realistic case and degrade safely (never corrupt a
    message) for the pathological long-message case."""

    def test_round_trips_message_containing_pipe_separator(self):
        messages = ["path C:\\data | archive contains a literal pipe", "second message"]
        summary = webhook_publish._summarize_payload(messages)
        assert webhook_publish._parse_payload_summary(summary) == messages

    def test_round_trips_normal_messages(self):
        messages = ["vsphere/web01 drifted", "vsphere/web02 drifted"]
        summary = webhook_publish._summarize_payload(messages)
        assert webhook_publish._parse_payload_summary(summary) == messages

    def test_oversized_payload_drops_whole_trailing_messages_not_mid_message(self):
        # Many short messages whose naive " | ".join would exceed the old
        # 2000-char cutoff mid-message. The fix must never return a message
        # that is a corrupted (partial) fragment of an original one.
        messages = [f"alert #{i}: something happened on host-{i}" for i in range(200)]
        summary = webhook_publish._summarize_payload(messages, limit=500)
        recovered = webhook_publish._parse_payload_summary(summary)
        assert len(summary) <= 500
        assert recovered == messages[: len(recovered)]
        for msg in recovered:
            assert msg in messages

    def test_single_oversized_message_still_returns_valid_json(self):
        messages = ["x" * 5000]
        summary = webhook_publish._summarize_payload(messages, limit=500)
        recovered = webhook_publish._parse_payload_summary(summary)
        assert len(summary) <= 500
        assert recovered == [messages[0][: max(500 - 20, 0)]]

    def test_legacy_pipe_joined_row_still_parses_as_best_effort(self):
        # Rows written before this fix used the raw " | ".join format —
        # parsing must not raise for those, even though it can't recover
        # the original message boundaries perfectly.
        assert webhook_publish._parse_payload_summary("msg one | msg two") == [
            "msg one",
            "msg two",
        ]

    def test_empty_and_none_parse_to_empty_list(self):
        assert webhook_publish._parse_payload_summary(None) == []
        assert webhook_publish._parse_payload_summary("") == []


class TestRetryPreservesExactOriginalPayload:
    """End-to-end: a message containing " | " must survive a failed
    publish_event -> retry_pending_deliveries round trip unchanged."""

    @pytest.fixture(autouse=True)
    def _ssrf_bypass(self, _skip_ssrf_dns_lookup):
        pass

    def test_retry_redelivers_exact_original_messages(self, engine, session_ctx):
        _add_subscription(engine)
        original_messages = [
            "disk /var/log | /var/data usage at 91%",
            "second alert, unrelated",
        ]

        with (
            patch("infra_brain.config.get_settings", return_value=_settings()),
            patch(
                "infra_brain.tools.webhook_publish.httpx.post",
                side_effect=RuntimeError("connection refused"),
            ),
            _mock_gate(),
        ):
            webhook_publish.publish_event("drift.critical", original_messages)

        # publish_event's backoff schedules next_attempt_at in the future;
        # force it due now so retry_pending_deliveries picks it up.
        with Session(engine) as s:
            row = s.query(WebhookDelivery).one()
            row.next_attempt_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            s.commit()

        captured = {}

        def _capture_and_succeed(url, json, **kwargs):
            captured["messages"] = json["messages"]
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with (
            patch("infra_brain.config.get_settings", return_value=_settings()),
            patch(
                "infra_brain.tools.webhook_publish.httpx.post",
                side_effect=_capture_and_succeed,
            ),
            _mock_gate(),
        ):
            result = webhook_publish.retry_pending_deliveries()

        assert result["delivered"] == 1
        assert captured["messages"] == original_messages


class TestSsrfGuard:
    """_is_ssrf_safe_target's own logic, using literal IPs so no real DNS
    lookup is needed (ip_address() on a literal never hits the network)."""

    def test_rejects_loopback(self):
        assert webhook_publish._is_ssrf_safe_target("http://127.0.0.1/hook") is False

    def test_rejects_link_local_metadata_endpoint(self):
        assert (
            webhook_publish._is_ssrf_safe_target("http://169.254.169.254/latest/meta-data/")
            is False
        )

    def test_rejects_private_rfc1918(self):
        assert webhook_publish._is_ssrf_safe_target("http://10.0.0.5/hook") is False
        assert webhook_publish._is_ssrf_safe_target("http://198.51.100.17/hook") is False
        assert webhook_publish._is_ssrf_safe_target("http://172.16.0.1/hook") is False

    def test_rejects_unspecified(self):
        assert webhook_publish._is_ssrf_safe_target("http://0.0.0.0/hook") is False

    def test_accepts_public_ip_literal(self):
        assert webhook_publish._is_ssrf_safe_target("http://8.8.8.8/hook") is True

    def test_rejects_non_http_scheme(self):
        assert webhook_publish._is_ssrf_safe_target("file:///etc/passwd") is False
        assert webhook_publish._is_ssrf_safe_target("ftp://8.8.8.8/hook") is False

    def test_rejects_malformed_url(self):
        assert webhook_publish._is_ssrf_safe_target("not a url") is False

    def test_deliver_refuses_private_target_without_attempting_http(self):
        """End-to-end: _deliver itself must refuse before any httpx.post,
        distinct from the matching/retry tests above which stub the guard
        out via the autouse fixture."""
        sub = MagicMock(target_url="http://127.0.0.1:9999/internal", id="x", secret_token=None)
        with patch("infra_brain.tools.webhook_publish.httpx.post") as mock_post:
            delivered, error = webhook_publish._deliver(
                sub, "drift.critical", ["msg"], None, "test", 5
            )
        assert delivered is False
        assert "private" in error or "loopback" in error or "reserved" in error
        mock_post.assert_not_called()
