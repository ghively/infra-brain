"""Tests for the sandboxed read-only script runner."""

import pytest
from unittest.mock import patch, MagicMock
from infra_brain.tools.script_runner import run_readonly_script_tool, _scan_for_mutations


def test_disabled_by_default():
    with patch("infra_brain.tools.script_runner.get_settings") as gs:
        gs.return_value.scripts_enabled = False
        out = run_readonly_script_tool.invoke({"language": "bash", "script": "ls"})
    assert "disabled" in out["error"]


@pytest.mark.parametrize(
    "script",
    [
        "rm -rf /data",
        "rmdir /s /q C:\\tmp",
        "del somefile.txt",
        "Remove-Item C:\\x -Recurse",
        "Set-Content -Path f -Value x",
        "Add-Content -Path f -Value x",
        "Out-File -FilePath log.txt",
        "New-Item -ItemType File x.txt",
        "Clear-Content -Path f",
        "import os; os.remove('/etc/passwd')",
        "import os; os.unlink('/etc/shadow')",
        "import os; os.rmdir('/tmp/x')",
        "import shutil; shutil.rmtree('/var/x')",
        "open('/etc/passwd', 'w').write('x')",
        "open('/etc/passwd', 'a')",
        "open('/etc/passwd', 'x')",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sdb",
        "fdisk /dev/sda",
        "INSERT INTO users VALUES (1,'x')",
        "UPDATE users SET name='x'",
        "DELETE FROM users WHERE 1=1",
        "DROP TABLE users",
        "TRUNCATE TABLE logs",
        "ALTER TABLE users ADD col INT",
        "CREATE TABLE hacked (id INT)",
        "echo x > /etc/passwd",
        "chmod 777 /etc/shadow",
        "chown root /tmp/x",
        "systemctl start sshd",
        "systemctl stop nginx",
        "systemctl restart apache2",
        "systemctl enable cron",
        "systemctl disable firewalld",
        "reg add HKLM\\Software\\x",
        "reg delete HKLM\\Software\\x",
        "Stop-Service -Name sshd",
        "Start-Service -Name nginx",
        "Restart-Computer -Force",
        "shutdown /r /t 0",
        "psql -c 'DROP TABLE users'",
        # argv/list-style invocations — the keyword is followed by a quote/comma,
        # never whitespace, so a \s-only pattern would miss these entirely.
        "import subprocess; subprocess.run(['rm', '-rf', '/'])",
        "import subprocess; subprocess.run(['del', 'somefile.txt'])",
        "import subprocess; subprocess.run(['systemctl', 'stop', 'sshd'])",
        "import subprocess; subprocess.run(['systemctl', 'start', 'sshd'])",
        "import subprocess; subprocess.run(['dd', 'if=/dev/zero', 'of=/dev/sda'])",
        "import subprocess; subprocess.run(['reg', 'add', 'HKLM\\\\Software\\\\x'])",
        "import subprocess; subprocess.run(['reg', 'delete', 'HKLM\\\\Software\\\\x'])",
        "import subprocess; subprocess.run(['format', 'C:'])",
        "cur.execute(' '.join(['INSERT', 'INTO', 'users', 'VALUES', '(1)']))",
    ],
)
def test_mutation_scan_blocks_dangerous(script):
    with pytest.raises(PermissionError):
        _scan_for_mutations(script)


def test_mutation_scan_allows_benign_format_call():
    """str.format(...) must not be flagged — only the disk 'format' command construct
    (bare token not immediately followed by '(') should trip the denylist."""
    _scan_for_mutations("msg = '{}-{}'.format(host, status)")


def test_readonly_script_runs_when_enabled():
    fake = MagicMock(returncode=0, stdout="host01\n", stderr="")
    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch("infra_brain.tools.script_runner.subprocess.run", return_value=fake),
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        out = run_readonly_script_tool.invoke({"language": "bash", "script": "hostname"})
    assert out["stdout"] == "host01\n"
    assert out["returncode"] == 0


def test_unsupported_language_returns_error():
    with patch("infra_brain.tools.script_runner.get_settings") as gs:
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        out = run_readonly_script_tool.invoke({"language": "ruby", "script": "puts 'hi'"})
    assert "unsupported" in out["error"]


def test_timeout_returns_error_dict():
    import subprocess

    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch(
            "infra_brain.tools.script_runner.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="bash", timeout=30),
        ),
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        out = run_readonly_script_tool.invoke({"language": "bash", "script": "hostname"})
    assert "timed out" in out["error"]


def test_missing_interpreter_returns_error_dict():
    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch(
            "infra_brain.tools.script_runner.subprocess.run",
            side_effect=FileNotFoundError("pwsh not found"),
        ),
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        out = run_readonly_script_tool.invoke({"language": "powershell", "script": "Get-Date"})
    assert "interpreter not found" in out["error"]


def test_audit_logs_allowed_script():
    """Audit logger emits an ALLOWED line for permitted scripts."""
    fake = MagicMock(returncode=0, stdout="ok", stderr="")
    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch("infra_brain.tools.script_runner.subprocess.run", return_value=fake),
        patch("infra_brain.tools.script_runner.logger") as mock_log,
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        run_readonly_script_tool.invoke({"language": "python", "script": "print('hello')"})
    # logger.info must have been called with "ALLOWED" in the format string
    called_msgs = [str(call) for call in mock_log.info.call_args_list]
    assert any("ALLOWED" in msg for msg in called_msgs)


def test_audit_logs_denied_script():
    """Audit logger emits a DENIED line for blocked scripts."""
    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch("infra_brain.tools.script_runner.logger") as mock_log,
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        with pytest.raises(PermissionError):
            run_readonly_script_tool.invoke({"language": "bash", "script": "rm -rf /"})
    called_msgs = [str(call) for call in mock_log.warning.call_args_list]
    assert any("DENIED" in msg for msg in called_msgs)


def test_no_shell_true_in_subprocess_call():
    """Verify subprocess.run is called without shell=True."""
    fake = MagicMock(returncode=0, stdout="", stderr="")
    calls = []

    def capture_run(*args, **kwargs):
        calls.append(kwargs)
        return fake

    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch("infra_brain.tools.script_runner.subprocess.run", side_effect=capture_run),
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        run_readonly_script_tool.invoke({"language": "python", "script": "print(1)"})
    assert calls, "subprocess.run was not called"
    assert not calls[0].get("shell", False), "shell=True must never be used"


def test_python_interpreter_cmd_uses_dash_c():
    """Python scripts must be passed as args to -c, not via shell=True."""
    fake = MagicMock(returncode=0, stdout="", stderr="")
    captured_cmd = []

    def capture_run(cmd, **kwargs):
        captured_cmd.append(cmd)
        return fake

    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch("infra_brain.tools.script_runner.subprocess.run", side_effect=capture_run),
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        run_readonly_script_tool.invoke({"language": "python", "script": "print(1)"})
    assert captured_cmd[0][:2] == ["python", "-c"]


# ---------------------------------------------------------------------------
# Task F2 — save_script is called for every run (success + denied + timeout)
# ---------------------------------------------------------------------------


def test_save_script_called_on_successful_run():
    """save_script must be called after a successful script execution."""
    fake = MagicMock(returncode=0, stdout="hello\n", stderr="")
    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch("infra_brain.tools.script_runner.subprocess.run", return_value=fake),
        patch("infra_brain.tools.script_runner.scripts_store") as mock_store,
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        mock_store.save_script.return_value = MagicMock()
        run_readonly_script_tool.invoke({"language": "python", "script": "print('hello')"})
    mock_store.save_script.assert_called_once()
    call_kwargs = mock_store.save_script.call_args
    # Check key fields passed
    args = call_kwargs.args if call_kwargs.args else ()
    # stdout and returncode must be passed
    if args:
        # positional args: name, language, purpose, content, created_by_agent, domain
        pass
    assert mock_store.save_script.called


def test_save_script_called_on_denied_script():
    """save_script must be called even when a script is denied (mutation blocked)."""
    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch("infra_brain.tools.script_runner.scripts_store") as mock_store,
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        mock_store.save_script.return_value = MagicMock()
        with pytest.raises(PermissionError):
            run_readonly_script_tool.invoke({"language": "bash", "script": "rm -rf /"})
    mock_store.save_script.assert_called_once()


def test_save_script_called_with_correct_language_on_success():
    """save_script is called with the correct language on success."""
    fake = MagicMock(returncode=0, stdout="", stderr="")
    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch("infra_brain.tools.script_runner.subprocess.run", return_value=fake),
        patch("infra_brain.tools.script_runner.scripts_store") as mock_store,
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        mock_store.save_script.return_value = MagicMock()
        run_readonly_script_tool.invoke({"language": "powershell", "script": "Get-Date"})
    _, kwargs = mock_store.save_script.call_args
    assert kwargs.get("language") == "powershell"


def test_scripts_store_failure_does_not_crash_run():
    """A scripts_store.save_script exception must NOT propagate to the caller."""
    fake = MagicMock(returncode=0, stdout="ok", stderr="")
    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch("infra_brain.tools.script_runner.subprocess.run", return_value=fake),
        patch("infra_brain.tools.script_runner.scripts_store") as mock_store,
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        mock_store.save_script.side_effect = RuntimeError("DB is down")
        result = run_readonly_script_tool.invoke({"language": "bash", "script": "hostname"})
    # Run should still return valid output
    assert result.get("returncode") == 0
    assert result.get("stdout") == "ok"


def test_save_script_called_on_timeout():
    """save_script must be called even when a script times out."""
    import subprocess as sp

    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch(
            "infra_brain.tools.script_runner.subprocess.run",
            side_effect=sp.TimeoutExpired(cmd="bash", timeout=30),
        ),
        patch("infra_brain.tools.script_runner.scripts_store") as mock_store,
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        mock_store.save_script.return_value = MagicMock()
        result = run_readonly_script_tool.invoke({"language": "bash", "script": "sleep 9999"})
    assert "timed out" in result.get("error", "")
    mock_store.save_script.assert_called_once()


# ---------------------------------------------------------------------------
# TRK-024 — unexpected error raises ToolException; read-only denial carve-out
# ---------------------------------------------------------------------------
def test_unexpected_error_raises_toolexception():
    """TRK-024: an unexpected execution error surfaces as ToolException (was a bare
    leaked exception), consistent with the other tool modules. OSError is not a
    FileNotFoundError/TimeoutExpired, so it falls through to the broad handler."""
    from langchain_core.tools import ToolException

    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch(
            "infra_brain.tools.script_runner.subprocess.run",
            side_effect=OSError("exec format error"),
        ),
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        with pytest.raises(ToolException, match="script execution failed"):
            run_readonly_script_tool.invoke({"language": "bash", "script": "hostname"})


def test_mutation_denial_stays_permissionerror_not_toolexception():
    """TRK-024 carve-out: the read-only mutation denial must stay a PermissionError
    (the R2 read-only signal) and never be downgraded to a generic ToolException."""
    from langchain_core.tools import ToolException

    with (
        patch("infra_brain.tools.script_runner.get_settings") as gs,
        patch("infra_brain.tools.script_runner.scripts_store") as mock_store,
    ):
        gs.return_value.scripts_enabled = True
        gs.return_value.script_timeout_seconds = 30
        mock_store.save_script.return_value = MagicMock()
        with pytest.raises(PermissionError) as ei:
            run_readonly_script_tool.invoke({"language": "bash", "script": "rm -rf /data"})
        assert not isinstance(ei.value, ToolException)
