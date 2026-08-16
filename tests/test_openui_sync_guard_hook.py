"""Tests for .claude/hooks/openui-sync-guard.py (P7.2, 2026-08-01).

This PostToolUse hook used to fire only on dashboard_api.py, but that file
is now a re-export shim only -- every real route handler lives in
api/routers/*.py, which the hook never matched. Locks down the fix: the
reminder now fires for api/routers/ edits too.
"""

import io
import json
import runpy
import sys
from contextlib import redirect_stderr
from pathlib import Path

HOOK_PATH = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "openui-sync-guard.py"


def _run_hook(payload: dict) -> str:
    stdin = io.StringIO(json.dumps(payload))
    stderr = io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = stdin
    try:
        with redirect_stderr(stderr):
            try:
                runpy.run_path(str(HOOK_PATH), run_name="__main__")
            except SystemExit:
                pass
    finally:
        sys.stdin = old_stdin
    return stderr.getvalue()


def test_fires_on_router_file_edit():
    out = _run_hook({
        "tool_name": "Edit",
        "tool_input": {"file_path": "src/infra_brain/api/routers/hosts.py"},
    })
    assert "OpenUI sync reminder" in out


def test_fires_on_dashboard_api_edit():
    out = _run_hook({
        "tool_name": "Write",
        "tool_input": {"file_path": "src/infra_brain/dashboard_api.py"},
    })
    assert "OpenUI sync reminder" in out


def test_silent_on_unrelated_file():
    out = _run_hook({
        "tool_name": "Edit",
        "tool_input": {"file_path": "src/infra_brain/scheduler.py"},
    })
    assert out == ""


def test_silent_on_non_edit_tool():
    out = _run_hook({
        "tool_name": "Read",
        "tool_input": {"file_path": "src/infra_brain/api/routers/hosts.py"},
    })
    assert out == ""


def test_handles_windows_path_separators():
    out = _run_hook({
        "tool_name": "Edit",
        "tool_input": {"file_path": "C:\\repo\\src\\infra_brain\\api\\routers\\hosts.py"},
    })
    assert "OpenUI sync reminder" in out
