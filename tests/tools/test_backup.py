"""Tests for the backup-system job-status polling tool (GitLab issue #96)."""

from unittest.mock import MagicMock, patch

from infra_brain.tools import backup as backup_module
from infra_brain.tools.backup import backup_jobs_tool


def _resp(data):
    r = MagicMock()
    r.json.return_value = data
    r.raise_for_status = MagicMock()
    return r


def _mock_settings(backup_api_key=""):
    s = MagicMock()
    s.backup_api_key = backup_api_key
    s.api_timeout_seconds = 30
    return s


def test_module_has_no_dead_unconfigured_helper():
    """``_unconfigured`` was vestigial (never called anywhere in the repo) —
    the tool does its own inline ``base_url``/``backend`` check instead. It
    must not be reintroduced without also wiring it into ``backup_jobs_tool``
    (or another caller), or it becomes orphaned dead code again."""
    assert not hasattr(backup_module, "_unconfigured")


def test_backup_jobs_tool_returns_empty_when_base_url_missing():
    result = backup_jobs_tool.invoke({"base_url": "", "backend": "veeam"})
    assert result == []


def test_backup_jobs_tool_returns_empty_when_backend_missing():
    result = backup_jobs_tool.invoke({"base_url": "http://backup.example.com", "backend": ""})
    assert result == []


def test_backup_jobs_tool_normalizes_veeam_jobs():
    with patch("infra_brain.tools.backup.get_settings", return_value=_mock_settings()):
        with patch("infra_brain.tools.backup.ReadOnlyClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = _resp(
                [
                    {
                        "name": "nightly-veeam-job",
                        "objectName": "vm-01",
                        "lastSuccessTime": "2026-08-01T00:00:00Z",
                        "lastSureBackupTime": None,
                        "retentionDays": 30,
                        "lastResult": "Success",
                    }
                ]
            )
            result = backup_jobs_tool.invoke(
                {"base_url": "http://veeam.example.com", "backend": "veeam"}
            )

    assert result == [
        {
            "job_name": "nightly-veeam-job",
            "resource_name": "vm-01",
            "last_success_at": "2026-08-01T00:00:00Z",
            "last_restore_test_at": None,
            "retention_days": 30,
            "status": "Success",
        }
    ]
