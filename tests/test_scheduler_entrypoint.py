"""F-002 — the scheduler entry point must configure INFO stdout logging.

Would fail before the fix: run_scheduler_forever() never called
logging.basicConfig, so the container emitted zero log lines.
"""

import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

import infra_brain.scheduler as sched


def test_run_scheduler_forever_configures_stdout_logging(monkeypatch):
    monkeypatch.setattr("infra_brain.secrets.load_secrets_into_env", lambda: None)
    monkeypatch.setattr("infra_brain.observability.init_tracing", lambda: None)
    svc = MagicMock()
    monkeypatch.setattr(sched, "start_scheduler", lambda: svc)

    def _boom(_seconds):
        raise SystemExit

    monkeypatch.setattr("time.sleep", _boom)

    sched.run_scheduler_forever()

    root = logging.getLogger()
    assert root.level == logging.INFO
    handlers = [
        h for h in root.handlers if isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
    ]
    assert handlers, "no stdout StreamHandler on the root logger"
    svc.stop.assert_called_once()


def test_console_script_target_exists():
    # pyproject [project.scripts] infra-brain-scheduler points here.
    assert callable(sched.run_scheduler_forever)


def test_sigterm_handler_raises_system_exit_to_trigger_drain():
    """F19: the SIGTERM handler must raise SystemExit so run_scheduler_forever's
    ``except SystemExit`` drain path (svc.stop() with wait) runs. Invoke the
    handler directly — do NOT send a real signal.
    """
    import signal

    captured = {}

    def _fake_signal(signum, handler):
        captured["signum"] = signum
        captured["handler"] = handler

    with patch("signal.signal", _fake_signal):
        sched._install_sigterm_handler()

    assert captured["signum"] == signal.SIGTERM
    # Calling the installed handler must raise SystemExit (drains via the loop's
    # existing except SystemExit branch), not terminate silently.
    with pytest.raises(SystemExit):
        captured["handler"](signal.SIGTERM, None)


def test_sigterm_handler_registration_survives_platform_failure():
    """Windows/non-main-thread: signal.signal() may raise — must not propagate."""

    def _boom(signum, handler):
        raise ValueError("signal only works in main thread")

    with patch("signal.signal", _boom):
        # Must not raise.
        sched._install_sigterm_handler()
