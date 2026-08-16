"""Tests for the propose_host_purpose_update chat tool (infra_brain.chat.tools).

The tool is PROPOSE-ONLY: it validates that the host exists (a read-only
``_rows`` SELECT), then opens a GitLab MR via the single sanctioned write path
``open_host_purpose_map_mr(..., actor="chat-agent", proposed=True)``. It must
NEVER write the HostPurposeMap DB table itself, and must surface helper errors
as plain strings rather than letting exceptions escape the chat turn.

open_host_purpose_map_mr is patched at the tool-module path
(infra_brain.chat.tools.open_host_purpose_map_mr), where chat/tools.py imports
it — matching the existing chat-tool test convention (patch _rows there too).
"""

from __future__ import annotations

import uuid
from unittest.mock import Mock, patch

from tests.support.pg import make_engine


def test_propose_host_purpose_update_existence_check_is_case_insensitive():
    """GitLab #121 Bug B: the read-only existence check compares
    ``host_purpose_map.hostname`` case-insensitively — a host seeded as
    'WEB01' must still be found when the caller asks about 'web01', instead
    of the case-sensitive '=' silently reporting 'not found'.

    Exercises the real raw SQL (not a mocked ``_rows``) against an in-memory
    sqlite engine so the ``LOWER(hostname) = LOWER(:name)`` fix is actually
    proven, not merely asserted against a stub.
    """
    from sqlalchemy.orm import Session

    from infra_brain.db.models import HostPurposeMap

    engine = make_engine()
    with Session(engine) as s:
        s.add(HostPurposeMap(id=uuid.uuid4(), hostname="WEB01", purpose="Edge", source="seed"))
        s.commit()

    fake = {"mr_url": "https://gl/mr/9", "branch": "hpm-web01", "action": "created"}
    with (
        patch("infra_brain.chat.tools.get_engine", return_value=engine),
        patch("infra_brain.chat.tools.open_host_purpose_map_mr", return_value=fake) as mock_mr,
    ):
        from infra_brain.chat.tools import propose_host_purpose_update

        result = propose_host_purpose_update.invoke({"hostname": "web01", "purpose": "Edge"})

    assert "not found" not in result.lower()
    mock_mr.assert_called_once()


def test_propose_host_purpose_update_proposes_without_db_write():
    """Host exists -> the tool returns the MR url, calls the helper with
    proposed=True/actor='chat-agent', and NEVER opens a write session (no live
    DB write)."""
    fake = {"mr_url": "https://gl/mr/7", "branch": "hpm-web-01", "action": "created"}
    with (
        patch("infra_brain.chat.tools._rows", return_value=[{"ok": 1}]),
        patch("infra_brain.chat.tools.open_host_purpose_map_mr", return_value=fake) as mock_mr,
        patch("infra_brain.chat.tools.get_session") as mock_get_session,
    ):
        from infra_brain.chat.tools import propose_host_purpose_update

        result = propose_host_purpose_update.invoke(
            {"hostname": "web-01", "purpose": "Edge", "vlan": "VLAN10"}
        )

    assert "https://gl/mr/7" in result
    mock_mr.assert_called_once()
    # Propose-only contract: proposed=True, actor tagged as the chat agent.
    assert mock_mr.call_args.kwargs["proposed"] is True
    assert mock_mr.call_args.kwargs["actor"] == "chat-agent"
    # No live DB write: the tool never opens a write session.
    mock_get_session.assert_not_called()


def test_propose_host_purpose_update_host_not_found_opens_no_mr():
    """Host absent (_rows returns []) -> a 'not found' string and the MR helper
    is NEVER called."""
    mock_mr = Mock()
    with (
        patch("infra_brain.chat.tools._rows", return_value=[]),
        patch("infra_brain.chat.tools.open_host_purpose_map_mr", mock_mr),
    ):
        from infra_brain.chat.tools import propose_host_purpose_update

        result = propose_host_purpose_update.invoke({"hostname": "ghost-01"})

    assert "not found" in result.lower()
    mock_mr.assert_not_called()


def test_propose_host_purpose_update_permission_error_surfaced():
    """A write-gate denial (PermissionError) is surfaced as an error string; no
    exception escapes the chat turn."""
    with (
        patch("infra_brain.chat.tools._rows", return_value=[{"ok": 1}]),
        patch(
            "infra_brain.chat.tools.open_host_purpose_map_mr",
            side_effect=PermissionError("write gate denied"),
        ),
    ):
        from infra_brain.chat.tools import propose_host_purpose_update

        result = propose_host_purpose_update.invoke({"hostname": "web-01"})

    assert "gate" in result.lower()
    assert "blocked" in result.lower()
    assert "write gate denied" in result


def test_propose_host_purpose_update_generic_error_surfaced():
    """A generic transport failure is surfaced as a 'Failed to open a proposal
    MR' string; no exception escapes."""
    with (
        patch("infra_brain.chat.tools._rows", return_value=[{"ok": 1}]),
        patch(
            "infra_brain.chat.tools.open_host_purpose_map_mr",
            side_effect=RuntimeError("boom"),
        ),
    ):
        from infra_brain.chat.tools import propose_host_purpose_update

        result = propose_host_purpose_update.invoke({"hostname": "web-01"})

    assert "Failed to open a proposal MR" in result
    assert "boom" in result


def test_propose_host_purpose_update_is_wired_into_chat_tools():
    """The tool is registered in the chat agent's TOOLS list."""
    from infra_brain.chat.agent import TOOLS

    assert any(getattr(t, "name", "") == "propose_host_purpose_update" for t in TOOLS)
