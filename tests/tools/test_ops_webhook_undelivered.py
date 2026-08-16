"""An alert with no delivery channel must still land somewhere a human can see.

TRK-329. With ``OPS_WEBHOOK_URL`` unset and zero ``webhook_subscriptions`` rows,
``send_ops_alert`` used to log at INFO and return True — so every escalation
existed only inside a container log nothing consumes. That is how the ``linux``
collector stayed broken for 8 days across 34 consecutive failed runs while the
hourly freshness monitor correctly escalated every one of them (TRK-328).

An alert with nowhere to go is not an alert. These tests pin that the
unconfigured path now persists to ``AuditLog`` (dashboard-queryable) and that a
persistence failure can never take down the monitor that raised the alert.
"""

from unittest.mock import MagicMock, patch

from infra_brain.tools import ops_webhook

MODULE = "infra_brain.tools.ops_webhook"


def _settings(url: str):
    s = MagicMock()
    s.ops_webhook_url = url
    return s


def test_unconfigured_webhook_persists_the_alert_to_auditlog():
    added = []
    session = MagicMock()
    session.add.side_effect = added.append
    session.__enter__ = lambda self: session
    session.__exit__ = lambda self, *a: False

    with (
        patch(f"{MODULE}.get_settings", return_value=_settings("")),
        patch("infra_brain.db.session.get_session", return_value=session),
    ):
        ok = ops_webhook.send_ops_alert(
            "collection_health",
            ["linux: last run failed (ansible inventory path does not exist)"],
            agent_name="SchedulerMonitor",
            dedup_key="linux-health",
        )

    assert ok is True, "callers treat False as 'delivery attempted and failed'; this is not that"
    assert len(added) == 1, "the undelivered alert must be persisted, not just logged"
    row = added[0]
    assert row.allowed is False
    assert row.agent == "SchedulerMonitor"
    assert row.tool == "send_ops_alert:collection_health"
    assert "not configured" in row.denial_reason
    assert "ansible inventory path does not exist" in row.denial_reason, (
        "the alert's actual content must survive into the audit row, or the row "
        "tells an operator that something happened without telling them what"
    )


def test_persistence_failure_never_breaks_the_caller():
    """Alerting is the last line of defence — it must not take down the monitor."""
    with (
        patch(f"{MODULE}.get_settings", return_value=_settings("")),
        patch("infra_brain.db.session.get_session", side_effect=RuntimeError("db down")),
    ):
        ok = ops_webhook.send_ops_alert("collection_health", ["something broke"])

    assert ok is True, (
        "a failure to RECORD an alert must never propagate into the health check "
        "that raised it — that would turn an alerting outage into a monitoring outage"
    )


def test_configured_webhook_does_not_persist_an_audit_row():
    """The AuditLog write is the no-channel fallback, not an always-on side effect."""
    added = []
    session = MagicMock()
    session.add.side_effect = added.append
    session.__enter__ = lambda self: session
    session.__exit__ = lambda self, *a: False
    resp = MagicMock()
    resp.raise_for_status.return_value = None

    with (
        patch(f"{MODULE}.get_settings", return_value=_settings("https://hooks.example/x")),
        patch(f"{MODULE}.gate_external_write", return_value=None),
        patch(f"{MODULE}.httpx.post", return_value=resp),
        patch("infra_brain.db.session.get_session", return_value=session),
    ):
        ops_webhook.send_ops_alert("collection_health", ["delivered fine"])

    assert added == [], "a delivered alert needs no undelivered-fallback row"
