import uuid
from unittest.mock import MagicMock, patch

from infra_brain.agents.notification import NotificationAgent
from infra_brain.db.models import AuditLog, ComplianceViolation, DriftEvent


def _query_side_effect(*, drift_events=None, violations=None):
    """Build a `session.query(Model)` side_effect that returns a distinct
    `.filter_by(...).all()` result per model, so a test that only cares about
    one of the two open-item loops in `notify_all()` doesn't accidentally
    feed its fixtures into the other loop as well (both loops share the same
    mocked `session.query`).

    M-4: notify_all() now bounds each loop's fetch via
    `.filter_by(status="open").count()` (real total) and
    `.filter_by(status="open").order_by(...).limit(cap).all()` (the capped
    page) instead of a bare `.filter_by(...).all()`. Both chains are wired
    here to the same fixture list so existing tests (which pass a
    small/uncapped fixture list) see identical behavior to before — only a
    genuinely-oversized fixture list would ever observe the cap.
    """
    drift_events = drift_events if drift_events is not None else []
    violations = violations if violations is not None else []

    def _query(model):
        items = drift_events if model is DriftEvent else violations if model is ComplianceViolation else []
        q = MagicMock()
        q.filter_by.return_value.all.return_value = items
        q.filter_by.return_value.count.return_value = len(items)
        q.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value = items
        return q

    return _query


def test_notification_agent_init_calls_super():
    """NotificationAgent.__init__ must call super().__init__() so self.llm is set."""
    with (
        patch("infra_brain.etl.base.get_settings") as mock_settings,
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.agents.base.get_chat_model"),
        patch("infra_brain.agents.notification.build_callbacks", return_value=[]),
        patch("infra_brain.agents.notification.get_settings") as mock_notif_settings,
    ):
        mock_settings.return_value = MagicMock(
            anthropic_api_key="test-key",
            jira_url="https://jira.example.com",
            confluence_url="https://confluence.example.com",
        )
        mock_notif_settings.return_value = mock_settings.return_value
        agent = NotificationAgent()
    assert hasattr(agent, "llm"), "self.llm must be set by super().__init__()"


def test_notification_agent_filters_unset_urls_from_whitelist():
    """Regression test for GitLab #(latent OB-1 whitelist bug): unset
    (None) settings URLs must never reach `build_callbacks`' whitelisted_post
    list. `ReadOnlyToolValidator.on_tool_start` does
    `url.startswith(prefix)` for every entry in that list, and
    `str.startswith(None)` raises TypeError — so a deployment with fewer
    than all three of jira_url/confluence_url/ops_webhook_url configured
    must not pass None through, matching the sibling
    `RemediationAgent` pattern (`whitelisted_post=[self.settings.gitlab_url]
    if self.settings.gitlab_url else []`).
    """
    with (
        patch("infra_brain.etl.base.get_settings") as mock_settings,
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.agents.base.get_chat_model"),
        patch("infra_brain.agents.notification.build_callbacks") as mock_build_callbacks,
        patch("infra_brain.agents.notification.get_settings") as mock_notif_settings,
    ):
        mock_build_callbacks.return_value = []
        mock_settings.return_value = MagicMock(
            anthropic_api_key="test-key",
            jira_url="https://jira.example.com",
            confluence_url=None,
            ops_webhook_url=None,
        )
        mock_notif_settings.return_value = mock_settings.return_value
        NotificationAgent()

    _, kwargs = mock_build_callbacks.call_args
    assert kwargs["whitelisted_post"] == ["https://jira.example.com"]
    assert None not in kwargs["whitelisted_post"]


def test_notify_all_creates_tickets_for_open_events():
    drift_id = uuid.uuid4()
    resource_id = uuid.uuid4()

    mock_event = MagicMock()
    mock_event.id = drift_id
    mock_event.resource_id = resource_id
    mock_event.drift_type = "config_drift"
    mock_event.field = "instance_type"
    mock_event.old_value = {"v": "t3.small"}
    mock_event.new_value = {"v": "t3.medium"}
    mock_event.jira_key = None

    mock_resource = MagicMock()
    mock_resource.name = "web01"
    mock_resource.domain = "cloud"

    mock_session = MagicMock()
    mock_session.query.side_effect = _query_side_effect(drift_events=[mock_event])
    mock_session.get.return_value = mock_resource

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch(
            "infra_brain.agents.notification.create_jira_ticket", return_value="INFRA-1"
        ) as mock_jira,
        patch("infra_brain.agents.notification.create_compliance_jira_ticket") as mock_compliance,
        patch("infra_brain.agents.notification.upsert_confluence_page", return_value="page-1"),
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.jira_url = "https://jira.example.com"
        agent.settings.confluence_url = "https://confluence.example.com"
        agent.callbacks = []
        agent.notify_all()

    mock_jira.assert_called_once()
    assert mock_event.jira_key == "INFRA-1"
    assert mock_event.status == "acknowledged"
    mock_compliance.assert_not_called()


def test_notify_all_drift_loop_is_capped_and_records_truncation_when_backlog_exceeds_cap():
    """M-4: notify_all() used to fetch and Jira-ticket EVERY open DriftEvent
    with zero bound (one HTTP call + one commit per row) -- a drift flood
    could attempt tens of thousands of webhook/Jira calls in a single sweep.
    `notification_max_per_sweep` must cap the number of tickets one sweep
    attempts, and hitting the cap must be recorded as an explicit
    audit-trail WARNING row (never a silent partial notify -- the finding's
    explicit "do not let it silently notify less either").

    Distinguishes the fixed SQL-side-capped fetch
    (`.filter_by(status="open").order_by(...).limit(cap).all()` -> 2 rows)
    from the old unbounded fetch (`.filter_by(status="open").all()` -> 5
    rows): only the capped-fetch code path can produce `mock_jira.call_count
    == 2` here: the unbounded path would ticket all 5.
    """
    capped_page = [
        MagicMock(
            id=uuid.uuid4(),
            resource_id=uuid.uuid4(),
            drift_type="config_drift",
            field="f",
            old_value={},
            new_value={},
            jira_key=None,
            status="open",
        )
        for _ in range(2)
    ]
    # What the OLD unbounded `.filter_by(status="open").all()` call would
    # have fetched: the full 5-row backlog, not just the capped page.
    unbounded_backlog = capped_page + [
        MagicMock(
            id=uuid.uuid4(),
            resource_id=uuid.uuid4(),
            drift_type="config_drift",
            field="f",
            old_value={},
            new_value={},
            jira_key=None,
            status="open",
        )
        for _ in range(3)
    ]

    mock_resource = MagicMock()
    mock_resource.name = "web01"
    mock_resource.domain = "cloud"

    def _query(model):
        q = MagicMock()
        if model is DriftEvent:
            q.filter_by.return_value.all.return_value = unbounded_backlog
            q.filter_by.return_value.count.return_value = 5
            q.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value = (
                capped_page
            )
        else:
            q.filter_by.return_value.all.return_value = []
            q.filter_by.return_value.count.return_value = 0
            q.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value = []
        return q

    mock_session = MagicMock()
    mock_session.query.side_effect = _query
    mock_session.get.return_value = mock_resource

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch(
            "infra_brain.agents.notification.create_jira_ticket", return_value="INFRA-1"
        ) as mock_jira,
        patch("infra_brain.agents.notification.create_compliance_jira_ticket"),
        patch("infra_brain.agents.notification.upsert_confluence_page", return_value="page-1"),
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 2
        agent.settings.jira_url = "https://jira.example.com"
        agent.settings.confluence_url = "https://confluence.example.com"
        agent.callbacks = []
        agent.notify_all()

    # Only the capped page (2) was ticketed -- not the full 5-row backlog.
    assert mock_jira.call_count == 2, (
        f"expected exactly 2 Jira calls (the capped page), got {mock_jira.call_count} -- "
        "notify_all must fetch via the SQL-side-limited query, not the unbounded "
        "`.filter_by(status='open').all()`"
    )

    # The truncation must be recorded as an explicit AuditLog row -- never silent.
    truncation_rows = [
        call.args[0]
        for call in mock_session.add.call_args_list
        if isinstance(call.args[0], AuditLog) and call.args[0].tool == "notify_all:jira_drift"
    ]
    assert truncation_rows, (
        "expected an AuditLog row recording the drift-notification truncation "
        "when the open-event backlog (5) exceeds notification_max_per_sweep (2)"
    )
    assert "5" in truncation_rows[0].denial_reason
    assert "2" in truncation_rows[0].denial_reason


# --- C5: Resilient per-event notification tests ---


def test_notify_all_commits_first_event_even_if_second_raises():
    """C5: if 2nd event's Jira creation raises, 1st event must still be committed."""
    resource_id_1 = uuid.uuid4()
    resource_id_2 = uuid.uuid4()

    mock_event1 = MagicMock()
    mock_event1.id = uuid.uuid4()
    mock_event1.resource_id = resource_id_1
    mock_event1.drift_type = "config_drift"
    mock_event1.field = "instance_type"
    mock_event1.old_value = {"v": "t3.small"}
    mock_event1.new_value = {"v": "t3.medium"}
    mock_event1.jira_key = None
    mock_event1.status = "open"

    mock_event2 = MagicMock()
    mock_event2.id = uuid.uuid4()
    mock_event2.resource_id = resource_id_2
    mock_event2.drift_type = "config_drift"
    mock_event2.field = "instance_type"
    mock_event2.old_value = {"v": "t3.small"}
    mock_event2.new_value = {"v": "t3.large"}
    mock_event2.jira_key = None
    mock_event2.status = "open"

    mock_resource1 = MagicMock()
    mock_resource1.name = "web01"
    mock_resource1.domain = "cloud"
    mock_resource2 = MagicMock()
    mock_resource2.name = "web02"
    mock_resource2.domain = "cloud"

    def mock_session_get(model, resource_id):
        if resource_id == resource_id_1:
            return mock_resource1
        return mock_resource2

    mock_session = MagicMock()
    mock_session.query.side_effect = _query_side_effect(drift_events=[mock_event1, mock_event2])
    mock_session.get.side_effect = mock_session_get

    commit_count = []

    def record_commit():
        commit_count.append(1)

    mock_session.commit.side_effect = record_commit

    def jira_side_effect(summary, description, event_id):
        if event_id == mock_event2.id:
            raise RuntimeError("Jira API error for event 2")
        return "INFRA-10"

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch("infra_brain.agents.notification.create_jira_ticket", side_effect=jira_side_effect),
        patch("infra_brain.agents.notification.upsert_confluence_page", return_value="page-1"),
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.jira_url = "https://jira.example.com"
        agent.settings.confluence_url = "https://confluence.example.com"
        agent.callbacks = []
        agent.notify_all()

    # First event must be committed
    assert mock_event1.jira_key == "INFRA-10", "First event must be acked even if second fails"
    assert mock_event1.status == "acknowledged"
    # Second event must NOT be acknowledged
    assert mock_event2.status != "acknowledged", "Second event should not be acked if Jira raised"
    # At least one commit must have happened (for event1)
    assert len(commit_count) >= 1, "Must commit incrementally per event"


def test_notify_all_continues_after_single_event_failure():
    """C5: loop must continue processing events even after one fails."""
    resource_id_1 = uuid.uuid4()
    resource_id_2 = uuid.uuid4()

    mock_event1 = MagicMock()
    mock_event1.id = uuid.uuid4()
    mock_event1.resource_id = resource_id_1
    mock_event1.drift_type = "config_drift"
    mock_event1.field = "cpu"
    mock_event1.old_value = {}
    mock_event1.new_value = {}
    mock_event1.jira_key = None
    mock_event1.status = "open"

    mock_event2 = MagicMock()
    mock_event2.id = uuid.uuid4()
    mock_event2.resource_id = resource_id_2
    mock_event2.drift_type = "config_drift"
    mock_event2.field = "mem"
    mock_event2.old_value = {}
    mock_event2.new_value = {}
    mock_event2.jira_key = None
    mock_event2.status = "open"

    mock_resource1 = MagicMock()
    mock_resource1.name = "host1"
    mock_resource1.domain = "linux"
    mock_resource2 = MagicMock()
    mock_resource2.name = "host2"
    mock_resource2.domain = "linux"

    def mock_session_get(model, resource_id):
        if resource_id == resource_id_1:
            return mock_resource1
        return mock_resource2

    mock_session = MagicMock()
    mock_session.query.side_effect = _query_side_effect(drift_events=[mock_event1, mock_event2])
    mock_session.get.side_effect = mock_session_get

    jira_call_count = []

    def jira_side_effect(summary, description, event_id):
        jira_call_count.append(event_id)
        if event_id == mock_event1.id:
            raise RuntimeError("Jira error for event 1")
        return "INFRA-99"

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch("infra_brain.agents.notification.create_jira_ticket", side_effect=jira_side_effect),
        patch("infra_brain.agents.notification.upsert_confluence_page", return_value="p"),
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.jira_url = "https://jira.example.com"
        agent.settings.confluence_url = "https://confluence.example.com"
        agent.callbacks = []
        agent.notify_all()

    # Both events must have been attempted
    assert len(jira_call_count) == 2, "Must attempt Jira for all events even if one fails"
    # Second event must succeed
    assert mock_event2.jira_key == "INFRA-99"
    assert mock_event2.status == "acknowledged"


# --- Skip notification sinks when their URLs are unconfigured ---


def test_notify_all_skips_jira_when_url_unconfigured():
    """When jira_url is empty, notify_all must NOT call create_jira_ticket and must not raise."""
    mock_session = MagicMock()
    mock_session.query.side_effect = _query_side_effect()

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch("infra_brain.agents.notification.create_jira_ticket") as mock_jira,
        patch("infra_brain.agents.notification.upsert_confluence_page") as mock_conf,
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.jira_url = ""
        agent.settings.confluence_url = ""
        agent.callbacks = []
        agent.notify_all()  # must not raise

    mock_jira.assert_not_called()
    mock_conf.assert_not_called()


def test_notify_all_skips_jira_when_url_blank_whitespace():
    """A whitespace-only jira_url is treated as unconfigured."""
    mock_session = MagicMock()
    mock_session.query.side_effect = _query_side_effect()

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch("infra_brain.agents.notification.create_jira_ticket") as mock_jira,
        patch("infra_brain.agents.notification.upsert_confluence_page"),
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.jira_url = "   "
        agent.settings.confluence_url = "https://confluence.example.com"
        agent.callbacks = []
        agent.notify_all()

    mock_jira.assert_not_called()


def test_notify_all_skips_confluence_when_url_unconfigured():
    """Jira configured but Confluence empty: tickets created, no Confluence upsert."""
    drift_id = uuid.uuid4()
    resource_id = uuid.uuid4()

    mock_event = MagicMock()
    mock_event.id = drift_id
    mock_event.resource_id = resource_id
    mock_event.drift_type = "config_drift"
    mock_event.field = "instance_type"
    mock_event.old_value = {"v": "t3.small"}
    mock_event.new_value = {"v": "t3.medium"}
    mock_event.jira_key = None

    mock_resource = MagicMock()
    mock_resource.name = "web01"
    mock_resource.domain = "cloud"

    mock_session = MagicMock()
    mock_session.query.side_effect = _query_side_effect(drift_events=[mock_event])
    mock_session.get.return_value = mock_resource

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch(
            "infra_brain.agents.notification.create_jira_ticket", return_value="INFRA-1"
        ) as mock_jira,
        patch("infra_brain.agents.notification.create_compliance_jira_ticket"),
        patch("infra_brain.agents.notification.upsert_confluence_page") as mock_conf,
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.jira_url = "https://jira.example.com"
        agent.settings.confluence_url = ""
        agent.callbacks = []
        agent.notify_all()

    # Jira still ticketed (existing behavior preserved when configured)
    mock_jira.assert_called_once()
    assert mock_event.jira_key == "INFRA-1"
    # Confluence skipped because unconfigured
    mock_conf.assert_not_called()


# ---------------------------------------------------------------------------
# GitLab #106 — ComplianceViolation ticketing (parallel path to DriftEvent)
# ---------------------------------------------------------------------------


def test_notify_all_creates_ticket_for_open_compliance_violation():
    """An open ComplianceViolation must get a Jira ticket via the same
    write-gated helper, and its status flips to acknowledged (the mechanism
    that keeps it from being re-ticketed on the next run)."""
    violation_id = uuid.uuid4()
    resource_id = uuid.uuid4()

    mock_violation = MagicMock()
    mock_violation.id = violation_id
    mock_violation.rule = "no-open-ssh-to-internet"
    mock_violation.severity = "high"
    mock_violation.host = "web01"
    mock_violation.detail = "port 22 open to 0.0.0.0/0"
    mock_violation.resource_id = resource_id
    mock_violation.status = "open"

    mock_resource = MagicMock()
    mock_resource.name = "web01"
    mock_resource.domain = "cloud"

    mock_session = MagicMock()
    mock_session.query.side_effect = _query_side_effect(violations=[mock_violation])
    mock_session.get.return_value = mock_resource

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch("infra_brain.agents.notification.create_jira_ticket") as mock_jira,
        patch(
            "infra_brain.agents.notification.create_compliance_jira_ticket",
            return_value="INFRA-COMPLIANCE-1",
        ) as mock_compliance,
        patch("infra_brain.agents.notification.upsert_confluence_page", return_value="page-1"),
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.jira_url = "https://jira.example.com"
        agent.settings.confluence_url = "https://confluence.example.com"
        agent.callbacks = []
        agent.notify_all()

    mock_compliance.assert_called_once()
    summary, description = mock_compliance.call_args.args
    assert "no-open-ssh-to-internet" in summary
    assert "high" in description
    assert mock_violation.status == "acknowledged"
    mock_jira.assert_not_called()


def test_notify_all_skips_already_acknowledged_compliance_violations():
    """Dedup: notify_all only ever queries status='open' violations, so an
    already-ticketed (acknowledged) violation is never re-ticketed. This test
    proves the query-level dedup by returning an empty violations list (as the
    real `filter_by(status="open")` would for an already-acked row) and
    asserting the ticket helper is never invoked."""
    mock_session = MagicMock()
    mock_session.query.side_effect = _query_side_effect(violations=[])

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch("infra_brain.agents.notification.create_jira_ticket"),
        patch("infra_brain.agents.notification.create_compliance_jira_ticket") as mock_compliance,
        patch("infra_brain.agents.notification.upsert_confluence_page") as mock_conf,
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.jira_url = "https://jira.example.com"
        agent.settings.confluence_url = "https://confluence.example.com"
        agent.callbacks = []
        agent.notify_all()  # must not raise

    mock_compliance.assert_not_called()
    mock_conf.assert_not_called()


def test_notify_all_continues_when_compliance_ticket_creation_raises():
    """C5-style resilience: a failing compliance ticket must be logged and
    skipped (violation left un-acknowledged, so it is retried next run)
    without aborting the rest of notify_all."""
    violation_id = uuid.uuid4()

    mock_violation = MagicMock()
    mock_violation.id = violation_id
    mock_violation.rule = "no-root-login"
    mock_violation.severity = "medium"
    mock_violation.host = "db01"
    mock_violation.detail = "root login enabled"
    mock_violation.resource_id = None
    mock_violation.status = "open"

    mock_session = MagicMock()
    mock_session.query.side_effect = _query_side_effect(violations=[mock_violation])
    mock_session.get.return_value = None

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch("infra_brain.agents.notification.create_jira_ticket"),
        patch(
            "infra_brain.agents.notification.create_compliance_jira_ticket",
            side_effect=RuntimeError("Jira API error"),
        ) as mock_compliance,
        patch("infra_brain.agents.notification.upsert_confluence_page") as mock_conf,
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.jira_url = "https://jira.example.com"
        agent.settings.confluence_url = "https://confluence.example.com"
        agent.callbacks = []
        agent.notify_all()  # must not raise

    mock_compliance.assert_called_once()
    assert mock_violation.status == "open", "must not be acknowledged if ticketing failed"
    mock_conf.assert_not_called()


def test_notify_all_handles_no_open_compliance_violations():
    """Empty-result case: no open violations means no compliance tickets and
    no crash — mirrors the drift path's behavior with an empty event list."""
    mock_session = MagicMock()
    mock_session.query.side_effect = _query_side_effect()

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch("infra_brain.agents.notification.create_jira_ticket"),
        patch("infra_brain.agents.notification.create_compliance_jira_ticket") as mock_compliance,
        patch("infra_brain.agents.notification.upsert_confluence_page") as mock_conf,
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.jira_url = "https://jira.example.com"
        agent.settings.confluence_url = "https://confluence.example.com"
        agent.callbacks = []
        agent.notify_all()  # must not raise

    mock_compliance.assert_not_called()
    mock_conf.assert_not_called()


# ---------------------------------------------------------------------------
# notify_ops_alerts (OB-1) — collection-health / dead-man / anomaly delivery
# ---------------------------------------------------------------------------


def test_notify_ops_alerts_delivers_via_send_ops_alert():
    agent = NotificationAgent.__new__(NotificationAgent)
    agent.settings = MagicMock()
    agent.settings.notification_max_per_sweep = 1000

    with patch("infra_brain.agents.notification.send_ops_alert", return_value=True) as mock_send:
        result = agent.notify_ops_alerts("collection_health", ["linux: stale"])

    assert result is True
    mock_send.assert_called_once_with(
        "collection_health", ["linux: stale"], agent_name="NotificationAgent"
    )


def test_notify_ops_alerts_returns_false_on_delivery_failure_without_raising():
    agent = NotificationAgent.__new__(NotificationAgent)
    agent.settings = MagicMock()
    agent.settings.notification_max_per_sweep = 1000

    with patch("infra_brain.agents.notification.send_ops_alert", return_value=False):
        result = agent.notify_ops_alerts("agent_anomaly", ["QueryAgent: deny spike"])

    assert result is False


# ---------------------------------------------------------------------------
# notify_incidents (TRK-242 / GitLab #113) — correlation field population
# ---------------------------------------------------------------------------


def test_notify_incidents_noop_when_ops_webhook_unconfigured():
    """Clean no-op — mirrors the Jira/Confluence 'unconfigured' pattern."""
    mock_session = MagicMock()

    with patch("infra_brain.agents.notification.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.ops_webhook_url = ""
        result = agent.notify_incidents()

    assert result == 0
    mock_session.query.assert_not_called()


def test_notify_incidents_correlates_open_drift_event():
    resource_id = uuid.uuid4()
    mock_event = MagicMock()
    mock_event.id = uuid.uuid4()
    mock_event.resource_id = resource_id
    mock_event.field = "instance_type"
    mock_event.drift_type = "config_drift"
    mock_event.incident_key = None

    mock_resource = MagicMock()
    mock_resource.name = "web01"
    mock_resource.domain = "cloud"

    mock_session = MagicMock()

    def fake_query(model):
        q = MagicMock()
        if model.__name__ == "DriftEvent":
            q.filter_by.return_value.filter.return_value.all.return_value = [mock_event]
        else:
            q.filter_by.return_value.filter.return_value.all.return_value = []
        return q

    mock_session.query.side_effect = fake_query
    mock_session.get.return_value = mock_resource

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch(
            "infra_brain.agents.notification.send_ops_alert", return_value=True
        ) as mock_send,
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.ops_webhook_url = "https://hooks.example.com/alert"
        result = agent.notify_incidents()

    assert result == 1
    expected_key = f"drift:{resource_id}:instance_type"
    mock_send.assert_called_once_with(
        "drift_incident",
        ["[Infra Drift] cloud/web01: config_drift on 'instance_type'"],
        agent_name="NotificationAgent",
        dedup_key=expected_key,
    )
    assert mock_event.incident_key == expected_key


def test_notify_incidents_correlates_open_compliance_violation():
    mock_violation = MagicMock()
    mock_violation.id = uuid.uuid4()
    mock_violation.rule = "no-open-ssh"
    mock_violation.host = "host-1"
    mock_violation.detail = "port 22 open to 0.0.0.0/0"
    mock_violation.incident_key = None

    mock_session = MagicMock()

    def fake_query(model):
        q = MagicMock()
        if model.__name__ == "DriftEvent":
            q.filter_by.return_value.filter.return_value.all.return_value = []
        else:
            q.filter_by.return_value.filter.return_value.all.return_value = [mock_violation]
        return q

    mock_session.query.side_effect = fake_query

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch(
            "infra_brain.agents.notification.send_ops_alert", return_value=True
        ) as mock_send,
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.ops_webhook_url = "https://hooks.example.com/alert"
        result = agent.notify_incidents()

    assert result == 1
    expected_key = "compliance:no-open-ssh:host-1"
    mock_send.assert_called_once_with(
        "compliance_incident",
        ["[Compliance] no-open-ssh on host-1: port 22 open to 0.0.0.0/0"],
        agent_name="NotificationAgent",
        dedup_key=expected_key,
    )
    assert mock_violation.incident_key == expected_key


def test_notify_incidents_does_not_persist_key_when_delivery_fails():
    resource_id = uuid.uuid4()
    mock_event = MagicMock()
    mock_event.id = uuid.uuid4()
    mock_event.resource_id = resource_id
    mock_event.field = "cpu"
    mock_event.drift_type = "config_drift"
    mock_event.incident_key = None

    mock_resource = MagicMock()
    mock_resource.name = "host1"
    mock_resource.domain = "linux"

    mock_session = MagicMock()

    def fake_query(model):
        q = MagicMock()
        if model.__name__ == "DriftEvent":
            q.filter_by.return_value.filter.return_value.all.return_value = [mock_event]
        else:
            q.filter_by.return_value.filter.return_value.all.return_value = []
        return q

    mock_session.query.side_effect = fake_query
    mock_session.get.return_value = mock_resource

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch("infra_brain.agents.notification.send_ops_alert", return_value=False),
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.ops_webhook_url = "https://hooks.example.com/alert"
        result = agent.notify_incidents()

    assert result == 0
    assert mock_event.incident_key is None


def test_notify_incidents_swallows_exceptions_and_continues():
    """A raise from send_ops_alert for one row must not abort the whole pass."""
    resource_id_1 = uuid.uuid4()
    resource_id_2 = uuid.uuid4()

    mock_event1 = MagicMock()
    mock_event1.id = uuid.uuid4()
    mock_event1.resource_id = resource_id_1
    mock_event1.field = "cpu"
    mock_event1.drift_type = "config_drift"
    mock_event1.incident_key = None

    mock_event2 = MagicMock()
    mock_event2.id = uuid.uuid4()
    mock_event2.resource_id = resource_id_2
    mock_event2.field = "mem"
    mock_event2.drift_type = "config_drift"
    mock_event2.incident_key = None

    mock_resource1 = MagicMock(name="r1", domain="linux")
    mock_resource1.name = "host1"
    mock_resource1.domain = "linux"
    mock_resource2 = MagicMock(name="r2", domain="linux")
    mock_resource2.name = "host2"
    mock_resource2.domain = "linux"

    def mock_session_get(model, rid):
        return mock_resource1 if rid == resource_id_1 else mock_resource2

    mock_session = MagicMock()

    def fake_query(model):
        q = MagicMock()
        if model.__name__ == "DriftEvent":
            q.filter_by.return_value.filter.return_value.all.return_value = [
                mock_event1,
                mock_event2,
            ]
        else:
            q.filter_by.return_value.filter.return_value.all.return_value = []
        return q

    mock_session.query.side_effect = fake_query
    mock_session.get.side_effect = mock_session_get

    def send_side_effect(category, messages, agent_name=None, dedup_key=None):
        if "cpu" in dedup_key:
            raise RuntimeError("webhook boom")
        return True

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch(
            "infra_brain.agents.notification.send_ops_alert", side_effect=send_side_effect
        ),
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.ops_webhook_url = "https://hooks.example.com/alert"
        result = agent.notify_incidents()

    assert result == 1
    assert mock_event1.incident_key is None
    assert mock_event2.incident_key == f"drift:{resource_id_2}:mem"


# ---------------------------------------------------------------------------
# publish_event fan-out (GitLab #112) — subscriber notification alongside the
# legacy single ops_webhook_url destination
# ---------------------------------------------------------------------------


def test_notify_ops_alerts_also_fans_out_via_publish_event():
    agent = NotificationAgent.__new__(NotificationAgent)
    agent.settings = MagicMock()
    agent.settings.notification_max_per_sweep = 1000

    with (
        patch("infra_brain.agents.notification.send_ops_alert", return_value=True),
        patch("infra_brain.agents.notification.publish_event") as mock_publish,
    ):
        result = agent.notify_ops_alerts("collection_health", ["linux: stale"])

    assert result is True
    mock_publish.assert_called_once_with(
        "collection_health", ["linux: stale"], agent_name="NotificationAgent"
    )


def test_notify_ops_alerts_publish_event_failure_does_not_change_return_value():
    """A subscriber-fan-out failure must never change notify_ops_alerts' return
    value — the legacy ops_webhook_url delivery result is authoritative."""
    agent = NotificationAgent.__new__(NotificationAgent)
    agent.settings = MagicMock()
    agent.settings.notification_max_per_sweep = 1000

    with (
        patch("infra_brain.agents.notification.send_ops_alert", return_value=True),
        patch(
            "infra_brain.agents.notification.publish_event",
            side_effect=RuntimeError("subscriber webhook down"),
        ),
    ):
        result = agent.notify_ops_alerts("collection_health", ["linux: stale"])

    assert result is True


def test_notify_incidents_fans_out_drift_event_via_publish_event():
    resource_id = uuid.uuid4()
    mock_event = MagicMock()
    mock_event.id = uuid.uuid4()
    mock_event.resource_id = resource_id
    mock_event.field = "instance_type"
    mock_event.drift_type = "config_drift"
    mock_event.incident_key = None

    mock_resource = MagicMock()
    mock_resource.name = "web01"
    mock_resource.domain = "vsphere"

    mock_session = MagicMock()

    def fake_query(model):
        q = MagicMock()
        if model.__name__ == "DriftEvent":
            q.filter_by.return_value.filter.return_value.all.return_value = [mock_event]
        else:
            q.filter_by.return_value.filter.return_value.all.return_value = []
        return q

    mock_session.query.side_effect = fake_query
    mock_session.get.return_value = mock_resource

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch("infra_brain.agents.notification.send_ops_alert", return_value=True),
        patch("infra_brain.agents.notification.publish_event") as mock_publish,
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.ops_webhook_url = "https://hooks.example.com/alert"
        agent.notify_incidents()

    expected_key = f"drift:{resource_id}:instance_type"
    mock_publish.assert_called_once_with(
        "drift.incident",
        ["[Infra Drift] vsphere/web01: config_drift on 'instance_type'"],
        domain="vsphere",
        dedup_key=expected_key,
        agent_name="NotificationAgent",
    )


def test_notify_incidents_drift_event_publish_event_failure_does_not_block_correlation():
    resource_id = uuid.uuid4()
    mock_event = MagicMock()
    mock_event.id = uuid.uuid4()
    mock_event.resource_id = resource_id
    mock_event.field = "cpu"
    mock_event.drift_type = "config_drift"
    mock_event.incident_key = None

    mock_resource = MagicMock()
    mock_resource.name = "host1"
    mock_resource.domain = "linux"

    mock_session = MagicMock()

    def fake_query(model):
        q = MagicMock()
        if model.__name__ == "DriftEvent":
            q.filter_by.return_value.filter.return_value.all.return_value = [mock_event]
        else:
            q.filter_by.return_value.filter.return_value.all.return_value = []
        return q

    mock_session.query.side_effect = fake_query
    mock_session.get.return_value = mock_resource

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch("infra_brain.agents.notification.send_ops_alert", return_value=True),
        patch(
            "infra_brain.agents.notification.publish_event",
            side_effect=RuntimeError("subscriber webhook down"),
        ),
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.ops_webhook_url = "https://hooks.example.com/alert"
        result = agent.notify_incidents()

    assert result == 1
    assert mock_event.incident_key == f"drift:{resource_id}:cpu"


def test_notify_incidents_fans_out_compliance_violation_via_publish_event():
    resource_id = uuid.uuid4()
    mock_violation = MagicMock()
    mock_violation.id = uuid.uuid4()
    mock_violation.rule = "no-open-ssh"
    mock_violation.host = "host-1"
    mock_violation.detail = "port 22 open to 0.0.0.0/0"
    mock_violation.resource_id = resource_id
    mock_violation.incident_key = None

    mock_resource = MagicMock()
    mock_resource.domain = "cloud"

    mock_session = MagicMock()

    def fake_query(model):
        q = MagicMock()
        if model.__name__ == "DriftEvent":
            q.filter_by.return_value.filter.return_value.all.return_value = []
        else:
            q.filter_by.return_value.filter.return_value.all.return_value = [mock_violation]
        return q

    mock_session.query.side_effect = fake_query
    mock_session.get.return_value = mock_resource

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch("infra_brain.agents.notification.send_ops_alert", return_value=True),
        patch("infra_brain.agents.notification.publish_event") as mock_publish,
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.ops_webhook_url = "https://hooks.example.com/alert"
        agent.notify_incidents()

    expected_key = "compliance:no-open-ssh:host-1"
    mock_publish.assert_called_once_with(
        "compliance.incident",
        ["[Compliance] no-open-ssh on host-1: port 22 open to 0.0.0.0/0"],
        domain="cloud",
        dedup_key=expected_key,
        agent_name="NotificationAgent",
    )


def test_notify_ops_alerts_swallows_unexpected_exceptions():
    """notify_ops_alerts must never raise — a delivery-path bug must not crash the scheduler."""
    agent = NotificationAgent.__new__(NotificationAgent)
    agent.settings = MagicMock()
    agent.settings.notification_max_per_sweep = 1000

    with patch(
        "infra_brain.agents.notification.send_ops_alert",
        side_effect=RuntimeError("boom"),
    ):
        result = agent.notify_ops_alerts("scheduler_deadman", ["scheduler: heartbeat stale"])

    assert result is False


# ---------------------------------------------------------------------------
# TRK-277 / GitLab #157 — sweep must complete + record an audit trail when a
# sink is unconfigured, instead of a hard early return that skips everything
# ---------------------------------------------------------------------------


def test_notify_all_jira_unset_still_processes_sweep_and_records_skip():
    """The old bug: JIRA_URL unset caused a hard `return` before the session
    was even opened, silently skipping drift ticketing, compliance
    ticketing, AND all status bookkeeping in one shot. Now: no tickets are
    created (can't be, Jira isn't configured) and events/violations stay
    unacknowledged, but the sweep completes and records what it skipped via
    an AuditLog row a health check can query."""
    drift_id = uuid.uuid4()
    resource_id = uuid.uuid4()

    mock_event = MagicMock()
    mock_event.id = drift_id
    mock_event.resource_id = resource_id
    mock_event.drift_type = "config_drift"
    mock_event.field = "instance_type"
    mock_event.old_value = {"v": "t3.small"}
    mock_event.new_value = {"v": "t3.medium"}
    mock_event.jira_key = None
    mock_event.status = "open"

    violation_id = uuid.uuid4()
    mock_violation = MagicMock()
    mock_violation.id = violation_id
    mock_violation.rule = "no-open-ssh-to-internet"
    mock_violation.severity = "high"
    mock_violation.host = "web01"
    mock_violation.detail = "port 22 open to 0.0.0.0/0"
    mock_violation.resource_id = None
    mock_violation.status = "open"

    mock_resource = MagicMock()
    mock_resource.name = "web01"
    mock_resource.domain = "cloud"

    mock_session = MagicMock()
    mock_session.query.side_effect = _query_side_effect(
        drift_events=[mock_event], violations=[mock_violation]
    )
    mock_session.get.return_value = mock_resource

    with (
        patch("infra_brain.agents.notification.get_session") as mock_ctx,
        patch("infra_brain.agents.notification.create_jira_ticket") as mock_jira,
        patch("infra_brain.agents.notification.create_compliance_jira_ticket") as mock_compliance,
        patch("infra_brain.agents.notification.upsert_confluence_page") as mock_conf,
    ):
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.jira_url = ""
        agent.settings.confluence_url = ""
        agent.callbacks = []
        agent.notify_all()  # must not raise; must not hard-return before doing anything

    # No tickets created and nothing acknowledged — Jira genuinely isn't configured.
    mock_jira.assert_not_called()
    mock_compliance.assert_not_called()
    mock_conf.assert_not_called()
    assert mock_event.status == "open"
    assert mock_violation.status == "open"

    # But the sweep must have recorded what it skipped instead of going silent.
    added_audit_rows = [
        call.args[0]
        for call in mock_session.add.call_args_list
        if isinstance(call.args[0], AuditLog)
    ]
    assert len(added_audit_rows) >= 2, "expected a skip row for both the drift and compliance sinks"
    tools_recorded = {row.tool for row in added_audit_rows}
    assert "notify_all:jira_drift" in tools_recorded
    assert "notify_all:jira_compliance" in tools_recorded
    for row in added_audit_rows:
        assert row.allowed is False
        assert row.agent == "NotificationAgent"
        assert "not configured" in row.denial_reason


def test_notify_incidents_ops_webhook_unset_still_records_skip():
    """The ops_webhook_url gate in notify_incidents returns 0 without
    touching the DB — that's still safe (see the docstring: running
    correlation with a no-op webhook would falsely mark rows as
    correlated), but it must now leave an audit trail distinguishing
    "webhook off" from "webhook on, nothing to correlate this cycle"."""
    mock_session = MagicMock()

    with patch("infra_brain.agents.notification.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.ops_webhook_url = ""
        result = agent.notify_incidents()  # must not raise

    assert result == 0
    mock_session.query.assert_not_called()

    added_audit_rows = [
        call.args[0]
        for call in mock_session.add.call_args_list
        if isinstance(call.args[0], AuditLog)
    ]
    assert len(added_audit_rows) == 1
    assert added_audit_rows[0].tool == "notify_incidents:ops_webhook"
    assert added_audit_rows[0].allowed is False
    assert "not configured" in added_audit_rows[0].denial_reason
    mock_session.commit.assert_called()


def test_notify_ops_alerts_webhook_unset_still_records_skip_and_returns_true():
    """notify_ops_alerts must preserve its "unconfigured == clean no-op ==
    True" contract for the caller, while still leaving an audit-trail row
    behind distinguishing a skipped delivery from a delivered one."""
    mock_session = MagicMock()

    with patch("infra_brain.agents.notification.get_session") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        agent = NotificationAgent.__new__(NotificationAgent)
        agent.settings = MagicMock()
        agent.settings.notification_max_per_sweep = 1000
        agent.settings.ops_webhook_url = ""
        result = agent.notify_ops_alerts("collection_health", ["linux: stale"])

    assert result is True

    added_audit_rows = [
        call.args[0]
        for call in mock_session.add.call_args_list
        if isinstance(call.args[0], AuditLog)
    ]
    assert len(added_audit_rows) == 1
    assert added_audit_rows[0].tool == "notify_ops_alerts"
    assert added_audit_rows[0].allowed is False
    assert "not configured" in added_audit_rows[0].denial_reason
