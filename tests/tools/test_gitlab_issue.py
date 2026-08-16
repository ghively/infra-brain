from unittest.mock import patch, MagicMock

import httpx
import pytest

from infra_brain.tools.gitlab_issue import create_gitlab_issue, comment_on_gitlab_issue


def _mock_resp(data, status=200):
    r = MagicMock()
    r.json.return_value = data
    r.status_code = status
    r.raise_for_status = MagicMock()
    return r


def _mock_error_resp(status):
    """A non-2xx response whose raise_for_status() actually raises, like httpx does."""
    r = MagicMock()
    r.status_code = status
    r.json.return_value = []

    def _raise():
        raise httpx.HTTPStatusError("error", request=MagicMock(), response=r)

    r.raise_for_status = MagicMock(side_effect=_raise)
    return r


def test_create_gitlab_issue_returns_new_issue():
    """Happy path: no matching open issue exists — POST creates a new one."""
    mock_session = MagicMock()
    with (
        patch("infra_brain.tools.gitlab_issue.httpx.post") as mock_post,
        patch("infra_brain.tools.gitlab_issue.httpx.get") as mock_get,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        # No open issues match
        mock_get.return_value = _mock_resp([], status=200)
        mock_post.return_value = _mock_resp(
            {
                "iid": 42,
                "title": "Drift detected on host-01",
                "web_url": "https://gitlab.example.com/issues/42",
            }
        )

        result = create_gitlab_issue(
            project_id=5,
            title="Drift detected on host-01",
            description="infra-brain found unmanaged drift.",
            labels=["drift", "auto-filed"],
        )

    assert result["web_url"] == "https://gitlab.example.com/issues/42"
    assert "_already_existed" not in result
    mock_post.assert_called_once()
    posted_json = mock_post.call_args.kwargs["json"]
    assert posted_json["title"] == "Drift detected on host-01"
    assert posted_json["labels"] == ["drift", "auto-filed"]


def test_create_gitlab_issue_is_idempotent_on_title_match():
    """An existing open issue with the same title is returned, no POST is made."""
    mock_session = MagicMock()
    with (
        patch("infra_brain.tools.gitlab_issue.httpx.post") as mock_post,
        patch("infra_brain.tools.gitlab_issue.httpx.get") as mock_get,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = _mock_resp(
            [
                {
                    "iid": 7,
                    "title": "Drift detected on host-01",
                    "web_url": "https://gitlab.example.com/issues/7",
                }
            ],
            status=200,
        )

        result = create_gitlab_issue(
            project_id=5,
            title="Drift detected on host-01",
        )

    assert result["_already_existed"] is True
    assert result["web_url"] == "https://gitlab.example.com/issues/7"
    mock_post.assert_not_called()


def test_create_gitlab_issue_empty_open_issue_list():
    """Edge case: GET returns an empty list (no open issues at all) — proceeds to create."""
    mock_session = MagicMock()
    with (
        patch("infra_brain.tools.gitlab_issue.httpx.post") as mock_post,
        patch("infra_brain.tools.gitlab_issue.httpx.get") as mock_get,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = _mock_resp([], status=200)
        mock_post.return_value = _mock_resp(
            {"iid": 1, "title": "New issue", "web_url": "https://gitlab.example.com/issues/1"}
        )

        result = create_gitlab_issue(project_id=5, title="New issue")

    assert result["web_url"] == "https://gitlab.example.com/issues/1"
    mock_post.assert_called_once()


def test_create_gitlab_issue_raises_when_existing_issue_check_fails():
    """The idempotency GET returning a non-200 (transient 5xx, rate limit, auth
    hiccup) must raise, not silently fall through and POST a duplicate issue —
    GitLab's issues API has no server-side duplicate protection."""
    mock_session = MagicMock()
    with (
        patch("infra_brain.tools.gitlab_issue.httpx.post") as mock_post,
        patch("infra_brain.tools.gitlab_issue.httpx.get") as mock_get,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = _mock_error_resp(503)

        with pytest.raises(httpx.HTTPStatusError):
            create_gitlab_issue(project_id=5, title="Drift detected on host-01")

    mock_post.assert_not_called()


def test_create_gitlab_issue_blocked_by_write_gate_sends_nothing():
    """A DLP-flagged payload (Luhn-valid PAN) must raise PermissionError and
    make no HTTP calls at all — deny-fails-closed, same contract as
    create_inventory_mr's gate."""
    mock_session = MagicMock()
    with (
        patch("infra_brain.tools.gitlab_issue.httpx.post") as mock_post,
        patch("infra_brain.tools.gitlab_issue.httpx.get") as mock_get,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(PermissionError):
            create_gitlab_issue(
                project_id=5,
                title="Card on file",
                description="4111111111111111",
            )

    mock_get.assert_not_called()
    mock_post.assert_not_called()


def test_comment_on_gitlab_issue_returns_created_note():
    """Happy path: POST a comment/note on an existing issue."""
    mock_session = MagicMock()
    with (
        patch("infra_brain.tools.gitlab_issue.httpx.post") as mock_post,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        mock_post.return_value = _mock_resp(
            {"id": 101, "body": "Investigating now."}
        )

        result = comment_on_gitlab_issue(project_id=5, issue_iid=42, body="Investigating now.")

    assert result["body"] == "Investigating now."
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0].endswith("/issues/42/notes")
    assert kwargs["json"] == {"body": "Investigating now."}
