"""Tests for the Teams outgoing-webhook synchronous reply builder (GitLab #111)."""

from unittest.mock import MagicMock, patch

import pytest

from infra_brain.tools.teams import build_teams_reply


def test_build_teams_reply_shapes_message():
    mock_session = MagicMock()

    with patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx:
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        reply = build_teams_reply("db01 has 2 open drift events")

    assert reply == {"type": "message", "text": "db01 has 2 open drift events"}


def test_build_teams_reply_blocked_by_write_gate_on_pan():
    mock_session = MagicMock()

    with patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx:
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(PermissionError):
            build_teams_reply("card 4111111111111111 leaked")
