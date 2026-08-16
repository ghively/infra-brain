"""Tests for the WinRM client factory (MR-J item 4).

WindowsAgent previously read ``getattr(self, "_winrm_client", None)`` — an
attribute nothing ever set, so every WinRM-based collector was permanently a
no-op regardless of credentials. This factory is the fix: it must actually
construct a client when pywinrm + credentials are available, and degrade to
None (never raise) otherwise.
"""

from unittest.mock import MagicMock, patch

import pytest

from infra_brain.tools import winrm_client as winrm_client_mod
from infra_brain.tools.winrm_client import get_winrm_client


def test_get_winrm_client_returns_none_without_pywinrm():
    with patch.object(winrm_client_mod, "_PYWINRM_AVAILABLE", False):
        assert get_winrm_client("win01", "Administrator", "s3cret") is None


def test_get_winrm_client_returns_none_on_missing_credentials():
    assert get_winrm_client("win01", "", "s3cret") is None
    assert get_winrm_client("win01", "Administrator", "") is None
    assert get_winrm_client("", "Administrator", "s3cret") is None


def test_get_winrm_client_builds_session_with_expected_endpoint():
    fake_session = MagicMock()
    fake_winrm_module = MagicMock()
    fake_winrm_module.Session.return_value = fake_session

    with (
        patch.object(winrm_client_mod, "_PYWINRM_AVAILABLE", True),
        patch.object(winrm_client_mod, "winrm", fake_winrm_module, create=True),
    ):
        client = get_winrm_client("win01.corp.example.com", "Administrator", "s3cret")

    assert client is fake_session
    fake_winrm_module.Session.assert_called_once_with(
        "http://win01.corp.example.com:5985/wsman",
        auth=("Administrator", "s3cret"),
        transport="ntlm",
        operation_timeout_sec=20,
        read_timeout_sec=30,
    )


def test_get_winrm_client_swallows_session_construction_error():
    fake_winrm_module = MagicMock()
    fake_winrm_module.Session.side_effect = RuntimeError("boom")

    with (
        patch.object(winrm_client_mod, "_PYWINRM_AVAILABLE", True),
        patch.object(winrm_client_mod, "winrm", fake_winrm_module, create=True),
    ):
        assert get_winrm_client("win01", "Administrator", "s3cret") is None


def test_pywinrm_actually_importable_in_this_environment():
    """Sanity check: when the ``winrm`` extra IS installed, the real
    (non-mocked) factory path is exercised at least once. Skipped (not
    failed) when the optional extra isn't present — the real GitLab CI
    `test` job only installs `.[dev]`, matching the project's convention
    for other optional-provider deps (see test_llm_factory.py)."""
    pytest.importorskip("winrm")
    assert winrm_client_mod._PYWINRM_AVAILABLE is True
    client = get_winrm_client("win01.corp.example.com", "Administrator", "s3cret")
    assert client is not None
    assert hasattr(client, "run_ps")
