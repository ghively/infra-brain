"""Tests for WindowsAgent WinRM credential guard and secure injection (F-021).

Verifies:
- collect() raises CollectorSkipped when ansible_win_password is empty.
- run_windows_ansible_facts() never places the password in argv.
- run_windows_ansible_facts() places the password in subprocess env.
- The Jinja2 lookup template (not the raw value) appears in argv.
- Input safety validation rejects unsafe targets/inventories.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from infra_brain.etl.base import CollectorSkipped
from infra_brain.tools.ansible import run_windows_ansible_facts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Use a forward-slash path that passes the _SAFE_PATH regex on all platforms.
_FAKE_INV = "/fake/inventory"
_FAKE_WIN_FACTS = json.dumps(
    {
        "winhost01": {
            "ansible_facts": {
                "ansible_os_name": "Windows Server 2022 Standard",
                "ansible_os_version": "10.0.20348",
                "ansible_architecture": "64-bit",
                "ansible_hostname": "WINHOST01",
                "ansible_domain": "corp.example.com",
                "ansible_services": {},
                "ansible_pending_updates": [],
            }
        }
    }
)

_FAKE_PASSWORD = "s3cr3tW1nRM!"


def _make_settings(win_password: str = _FAKE_PASSWORD, win_user: str = "Administrator"):
    s = MagicMock()
    s.ansible_win_password = win_password
    s.ansible_win_user = win_user
    s.ansible_inventory_path = _FAKE_INV
    return s


def _make_agent(win_password: str = _FAKE_PASSWORD):
    from infra_brain.agents.windows import WindowsAgent

    agent = WindowsAgent.__new__(WindowsAgent)
    agent.settings = _make_settings(win_password=win_password)
    agent.callbacks = []
    return agent


# ---------------------------------------------------------------------------
# CollectorSkipped guard
# ---------------------------------------------------------------------------


class TestWindowsCollectorSkippedGuard:
    def test_collect_raises_skipped_when_password_empty(self):
        """collect() must raise CollectorSkipped when ansible_win_password is ''."""
        agent = _make_agent(win_password="")
        with pytest.raises(CollectorSkipped, match="ansible_win_password not configured"):
            agent.collect()

    def test_collect_raises_skipped_when_password_whitespace(self):
        """Whitespace-only password is treated as unconfigured (stripped before check)."""
        agent = _make_agent(win_password="   ")
        with pytest.raises(CollectorSkipped):
            agent.collect()


# ---------------------------------------------------------------------------
# argv safety: password MUST NOT appear in argv
# ---------------------------------------------------------------------------


class TestRunWindowsAnsibleFactsArgvSafety:
    """F-021: password must be injected via subprocess env, never argv."""

    def _run_captured(self):
        """Call run_windows_ansible_facts with mocked subprocess; return captured call."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = _FAKE_WIN_FACTS
        fake_result.stderr = ""

        captured: dict = {}

        def _fake_run(cmd, **kw):
            captured["cmd"] = cmd
            captured["env"] = kw.get("env", {})
            return fake_result

        with (
            patch("infra_brain.tools.ansible.subprocess.run", side_effect=_fake_run),
            patch("infra_brain.tools.ansible.os.path.exists", return_value=True),
        ):
            run_windows_ansible_facts(
                target="windows",
                inventory=_FAKE_INV,
                win_user="Administrator",
                extra_env={"ANSIBLE_WIN_PASSWORD": _FAKE_PASSWORD},
            )
        return captured

    def test_password_not_in_argv(self):
        """The WinRM password must not appear anywhere in the command argv."""
        cap = self._run_captured()
        for arg in cap["cmd"]:
            assert _FAKE_PASSWORD not in arg, (
                f"password found in argv arg: {arg!r} — this is a security defect"
            )

    def test_password_in_env(self):
        """The WinRM password must be present in the subprocess env dict."""
        cap = self._run_captured()
        assert cap["env"].get("ANSIBLE_WIN_PASSWORD") == _FAKE_PASSWORD

    def test_jinja2_lookup_in_argv(self):
        """The Jinja2 lookup template (not the raw password) must appear in argv."""
        cap = self._run_captured()
        argv_str = " ".join(cap["cmd"])
        assert "lookup('env','ANSIBLE_WIN_PASSWORD')" in argv_str

    def test_win_setup_module_used(self):
        """The -m win_setup module must be specified (not -m setup)."""
        cap = self._run_captured()
        assert "-m" in cap["cmd"]
        m_idx = cap["cmd"].index("-m")
        assert cap["cmd"][m_idx + 1] == "win_setup"

    def test_win_user_in_argv_as_extra_var(self):
        """ansible_user= extra var must be passed in argv (username, not secret)."""
        cap = self._run_captured()
        argv_str = " ".join(cap["cmd"])
        assert "ansible_user=Administrator" in argv_str

    def test_ansible_cache_plugin_memory_in_env(self):
        """ANSIBLE_CACHE_PLUGIN=memory must be set to prevent disk/redis caching."""
        cap = self._run_captured()
        assert cap["env"].get("ANSIBLE_CACHE_PLUGIN") == "memory"


# ---------------------------------------------------------------------------
# Input safety validation
# ---------------------------------------------------------------------------


class TestRunWindowsAnsibleFactsInputValidation:
    def test_rejects_unsafe_target(self):
        with pytest.raises(ValueError, match="unsafe characters"):
            run_windows_ansible_facts(
                target="../../etc/passwd",
                inventory=_FAKE_INV,
                win_user="Administrator",
                extra_env={"ANSIBLE_WIN_PASSWORD": _FAKE_PASSWORD},
            )

    def test_rejects_target_with_dotdot(self):
        with pytest.raises(ValueError, match="unsafe characters"):
            run_windows_ansible_facts(
                target="windows..hosts",
                inventory=_FAKE_INV,
                win_user="Administrator",
                extra_env={"ANSIBLE_WIN_PASSWORD": _FAKE_PASSWORD},
            )

    def test_rejects_playbook_in_target(self):
        """Target containing 'playbook' is rejected (read-only guard)."""
        # Use a safe inventory path so that check passes and we reach the target check.
        with pytest.raises(ValueError, match="unsafe characters|playbook"):
            run_windows_ansible_facts(
                target="site-playbook",
                inventory=_FAKE_INV,
                win_user="Administrator",
                extra_env={"ANSIBLE_WIN_PASSWORD": _FAKE_PASSWORD},
            )

    def test_rejects_missing_inventory(self):
        """Non-existent inventory path must raise RuntimeError."""
        with pytest.raises(RuntimeError, match="does not exist"):
            run_windows_ansible_facts(
                target="windows",
                inventory="/nonexistent/path/inventory",
                win_user="Administrator",
                extra_env={"ANSIBLE_WIN_PASSWORD": _FAKE_PASSWORD},
            )


# ---------------------------------------------------------------------------
# collect() wiring: result processing
# ---------------------------------------------------------------------------


class TestWindowsCollectProcessing:
    """Verify that collect() passes results through correctly when credentials are set."""

    def test_collect_returns_items(self):
        """collect() maps win_setup facts into the expected item shape."""
        agent = _make_agent()
        fake_facts = json.loads(_FAKE_WIN_FACTS)

        with patch(
            "infra_brain.agents.windows.run_windows_ansible_facts",
            return_value=fake_facts,
        ):
            items = agent.collect(scope="windows")

        assert len(items) == 1
        item = items[0]
        assert item["name"] == "winhost01"
        assert item["type"] == "windows_host"
        assert item["data"]["os_name"] == "Windows Server 2022 Standard"
        assert item["data"]["winrm_status"] == "reachable"

    def test_collect_empty_result_is_not_error(self):
        """An empty win_setup result (no reachable hosts) returns [] without raising."""
        agent = _make_agent()

        with patch(
            "infra_brain.agents.windows.run_windows_ansible_facts",
            return_value={},
        ):
            items = agent.collect(scope="windows")

        assert items == []

    def test_collect_passes_password_in_extra_env_not_args(self):
        """collect() passes win_password via extra_env, NOT as a positional arg."""
        agent = _make_agent()
        captured: dict = {}

        def _fake_tool(target, inventory, win_user, extra_env=None):
            captured["target"] = target
            captured["win_user"] = win_user
            captured["extra_env"] = extra_env or {}
            return {}

        with patch(
            "infra_brain.agents.windows.run_windows_ansible_facts",
            side_effect=_fake_tool,
        ):
            agent.collect(scope="windows")

        assert captured["extra_env"].get("ANSIBLE_WIN_PASSWORD") == _FAKE_PASSWORD
        # Password must NOT appear in target or win_user args either
        assert _FAKE_PASSWORD not in captured["target"]
        assert _FAKE_PASSWORD not in captured["win_user"]
