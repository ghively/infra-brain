from unittest.mock import patch
from infra_brain.callbacks.registry import build_callbacks
from infra_brain.callbacks.audit import AuditCallbackHandler
from infra_brain.callbacks.readonly import ReadOnlyToolValidator
from infra_brain.callbacks.dlp import DLPCallbackHandler
from infra_brain.callbacks.observation import ObservationCallbackHandler


def test_build_callbacks_returns_all_handlers():
    with patch("infra_brain.callbacks.registry.get_settings") as mock:
        mock.return_value.scan_readonly_enforce = True
        mock.return_value.dlp_fail_closed = True
        mock.return_value.langfuse_enabled = False
        cbs = build_callbacks("TestAgent", "linux")
    types = [type(cb) for cb in cbs]
    assert AuditCallbackHandler in types
    assert ReadOnlyToolValidator in types
    assert DLPCallbackHandler in types
    assert ObservationCallbackHandler in types


def test_notification_agent_gets_whitelisted_post():
    with patch("infra_brain.callbacks.registry.get_settings") as mock:
        mock.return_value.scan_readonly_enforce = True
        mock.return_value.dlp_fail_closed = True
        mock.return_value.langfuse_enabled = False
        mock.return_value.jira_url = "https://jira.example.com"
        mock.return_value.confluence_url = "https://confluence.example.com"
        cbs = build_callbacks(
            "NotificationAgent",
            "notify",
            whitelisted_post=["https://jira.example.com", "https://confluence.example.com"],
        )
    rv = next(cb for cb in cbs if isinstance(cb, ReadOnlyToolValidator))
    assert "https://jira.example.com" in rv.whitelisted_post
