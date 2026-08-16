"""Tests for scheduler._collection_health_job (OB-1 + item 9 wiring):
records the scheduler heartbeat, runs both monitors, and delivers any
alerts through NotificationAgent.notify_ops_alerts().
"""

from unittest.mock import MagicMock, patch

from infra_brain.scheduler import _collection_health_job


def test_records_heartbeat_every_run():
    with (
        patch("infra_brain.callbacks.freshness.record_scheduler_heartbeat") as mock_heartbeat,
        patch("infra_brain.callbacks.freshness.check_collection_health", return_value=[]),
        patch("infra_brain.callbacks.anomaly.check_agent_anomalies", return_value=[]),
    ):
        _collection_health_job()

    mock_heartbeat.assert_called_once()


def test_heartbeat_failure_does_not_abort_the_rest_of_the_job():
    with (
        patch(
            "infra_brain.callbacks.freshness.record_scheduler_heartbeat",
            side_effect=RuntimeError("db down"),
        ),
        patch(
            "infra_brain.callbacks.freshness.check_collection_health", return_value=["linux: stale"]
        ),
        patch("infra_brain.callbacks.anomaly.check_agent_anomalies", return_value=[]),
        patch("infra_brain.agents.notification.NotificationAgent") as mock_notif_cls,
    ):
        _collection_health_job()

    mock_notif_cls.return_value.notify_ops_alerts.assert_called_once_with(
        "collection_health", ["linux: stale"]
    )


def test_collection_health_alerts_delivered_via_notification_agent():
    mock_agent = MagicMock()
    with (
        patch("infra_brain.callbacks.freshness.record_scheduler_heartbeat"),
        patch(
            "infra_brain.callbacks.freshness.check_collection_health",
            return_value=["linux: stale — last data 9h ago"],
        ),
        patch("infra_brain.callbacks.anomaly.check_agent_anomalies", return_value=[]),
        patch("infra_brain.agents.notification.NotificationAgent", return_value=mock_agent),
    ):
        _collection_health_job()

    mock_agent.notify_ops_alerts.assert_called_once_with(
        "collection_health", ["linux: stale — last data 9h ago"]
    )


def test_agent_anomaly_alerts_delivered_via_notification_agent():
    mock_agent = MagicMock()
    with (
        patch("infra_brain.callbacks.freshness.record_scheduler_heartbeat"),
        patch("infra_brain.callbacks.freshness.check_collection_health", return_value=[]),
        patch(
            "infra_brain.callbacks.anomaly.check_agent_anomalies",
            return_value=["QueryAgent: 12 denied tool calls (deny-verdict spike)"],
        ),
        patch("infra_brain.agents.notification.NotificationAgent", return_value=mock_agent),
    ):
        _collection_health_job()

    mock_agent.notify_ops_alerts.assert_called_once_with(
        "agent_anomaly", ["QueryAgent: 12 denied tool calls (deny-verdict spike)"]
    )


def test_no_delivery_call_when_both_monitors_are_clean():
    with (
        patch("infra_brain.callbacks.freshness.record_scheduler_heartbeat"),
        patch("infra_brain.callbacks.freshness.check_collection_health", return_value=[]),
        patch("infra_brain.callbacks.anomaly.check_agent_anomalies", return_value=[]),
        patch("infra_brain.agents.notification.NotificationAgent") as mock_notif_cls,
    ):
        _collection_health_job()

    mock_notif_cls.return_value.notify_ops_alerts.assert_not_called()


def test_delivery_failure_does_not_propagate():
    """A NotificationAgent construction/delivery failure must not crash the job."""
    with (
        patch("infra_brain.callbacks.freshness.record_scheduler_heartbeat"),
        patch(
            "infra_brain.callbacks.freshness.check_collection_health", return_value=["linux: stale"]
        ),
        patch("infra_brain.callbacks.anomaly.check_agent_anomalies", return_value=[]),
        patch(
            "infra_brain.agents.notification.NotificationAgent",
            side_effect=RuntimeError("construction failed"),
        ),
    ):
        _collection_health_job()  # must not raise


def test_anomaly_monitor_failure_does_not_abort_collection_health_delivery():
    mock_agent = MagicMock()
    with (
        patch("infra_brain.callbacks.freshness.record_scheduler_heartbeat"),
        patch(
            "infra_brain.callbacks.freshness.check_collection_health",
            return_value=["linux: stale"],
        ),
        patch(
            "infra_brain.callbacks.anomaly.check_agent_anomalies",
            side_effect=RuntimeError("query failed"),
        ),
        patch("infra_brain.agents.notification.NotificationAgent", return_value=mock_agent),
    ):
        _collection_health_job()

    mock_agent.notify_ops_alerts.assert_called_once_with("collection_health", ["linux: stale"])


def test_unconfigured_alerts_are_not_delivered_to_ops():
    """An "unconfigured (skipped)" domain must not page anyone.

    Regression: _collection_health_job logged every alert at ERROR and handed
    the whole list to the ops webhook, including entries check_collection_health
    had already classified as informational (skip_only — never unhealthy, never
    escalating). On a fleet with 11 unconfigured collectors that buried the one
    genuinely failing domain under ~11x its volume of noise every pass.
    """
    mock_agent = MagicMock()
    with (
        patch("infra_brain.callbacks.freshness.record_scheduler_heartbeat"),
        patch(
            "infra_brain.callbacks.freshness.check_collection_health",
            return_value=[
                "vsphere: unconfigured (skipped) — no vCenter hosts configured",
                "grafana: unconfigured (skipped) — grafana_url not configured",
                "wazuh: last run failed (401 Unauthorized)",
            ],
        ),
        patch("infra_brain.callbacks.anomaly.check_agent_anomalies", return_value=[]),
        patch("infra_brain.agents.notification.NotificationAgent", return_value=mock_agent),
    ):
        _collection_health_job()

    # Only the real failure reaches ops; both unconfigured entries are dropped.
    mock_agent.notify_ops_alerts.assert_called_once_with(
        "collection_health", ["wazuh: last run failed (401 Unauthorized)"]
    )


def test_no_ops_delivery_when_every_alert_is_informational():
    """A fleet whose only "alerts" are unconfigured collectors pages nobody."""
    mock_agent = MagicMock()
    with (
        patch("infra_brain.callbacks.freshness.record_scheduler_heartbeat"),
        patch(
            "infra_brain.callbacks.freshness.check_collection_health",
            return_value=[
                "vsphere: unconfigured (skipped) — no vCenter hosts configured",
                "prometheus: unconfigured (skipped) — prometheus_url not configured",
            ],
        ),
        patch("infra_brain.callbacks.anomaly.check_agent_anomalies", return_value=[]),
        patch("infra_brain.agents.notification.NotificationAgent", return_value=mock_agent),
    ):
        _collection_health_job()

    mock_agent.notify_ops_alerts.assert_not_called()
