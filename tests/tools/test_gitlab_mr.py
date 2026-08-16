import httpx
from unittest.mock import patch, MagicMock
from infra_brain.tools.gitlab_mr import create_inventory_mr


def _mock_resp(data, status=200):
    r = MagicMock()
    r.json.return_value = data
    r.status_code = status
    r.raise_for_status = MagicMock()
    return r


def test_create_inventory_mr_returns_mr_url():
    """Happy path: new branch, new file, new MR — returns MR web_url."""
    mock_session = MagicMock()
    with (
        patch("infra_brain.tools.gitlab_mr.httpx.post") as mock_post,
        patch("infra_brain.tools.gitlab_mr.httpx.get") as mock_get,
        patch("infra_brain.tools.gitlab_mr.httpx.put"),
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        # File check on branch returns 404 (file doesn't exist yet)
        check_resp = _mock_resp({}, status=404)
        # MR existence check — no open MRs
        mr_check_resp = _mock_resp([], status=200)
        mock_get.side_effect = [check_resp, mr_check_resp]

        # Branch creation, file creation, MR creation
        mock_post.side_effect = [
            _mock_resp({"name": "fix/inventory-drift"}),
            _mock_resp({"file_path": "inventories/prod.yml"}),
            _mock_resp({"web_url": "https://gitlab.example.com/mr/42"}),
        ]
        result = create_inventory_mr(
            project_id=5,
            branch_name="fix/inventory-drift-2026-06",
            file_path="inventories/production/01-tn.yml",
            new_content="all:\n  hosts:\n    new-host:",
            commit_message="fix(inventory): add new-host detected by infra-brain",
            mr_title="[Infra Brain] Inventory drift: add new-host",
            mr_description="Detected by drift scan. Review and merge to update Ansible inventory.",
        )
    assert "https://" in result
    assert "42" in result


def test_create_inventory_mr_updates_existing_file():
    """When file already exists on branch, PUT is used instead of POST."""
    mock_session = MagicMock()
    with (
        patch("infra_brain.tools.gitlab_mr.httpx.post") as mock_post,
        patch("infra_brain.tools.gitlab_mr.httpx.get") as mock_get,
        patch("infra_brain.tools.gitlab_mr.httpx.put") as mock_put,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        # File check: file already exists (200)
        check_resp = _mock_resp({"file_path": "inv.yml"}, status=200)
        # MR check: no open MRs
        mr_check_resp = _mock_resp([], status=200)
        mock_get.side_effect = [check_resp, mr_check_resp]

        # Branch creation + MR creation (no file POST)
        mock_post.side_effect = [
            _mock_resp({"name": "fix/drift"}),
            _mock_resp({"web_url": "https://gitlab.example.com/mr/99"}),
        ]
        mock_put.return_value = _mock_resp({"file_path": "inv.yml"})

        result = create_inventory_mr(5, "fix/drift", "inv.yml", "content", "msg", "title", "desc")

    mock_put.assert_called_once()
    assert "https://" in result


def test_create_inventory_mr_returns_existing_open_mr():
    """When an open MR for the branch already exists, return its URL (idempotent)."""
    mock_session = MagicMock()
    with (
        patch("infra_brain.tools.gitlab_mr.httpx.post") as mock_post,
        patch("infra_brain.tools.gitlab_mr.httpx.get") as mock_get,
        patch("infra_brain.tools.gitlab_mr.httpx.put") as mock_put,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        # File check: file exists
        check_resp = _mock_resp({"file_path": "inv.yml"}, status=200)
        # MR check: existing open MR found
        mr_check_resp = _mock_resp([{"web_url": "https://gitlab.example.com/mr/77"}], status=200)
        mock_get.side_effect = [check_resp, mr_check_resp]

        mock_post.side_effect = [_mock_resp({"name": "fix/drift"})]
        mock_put.return_value = _mock_resp({"file_path": "inv.yml"})

        result = create_inventory_mr(5, "fix/drift", "inv.yml", "content", "msg", "title", "desc")

    assert result == "https://gitlab.example.com/mr/77"
    # MR creation POST should NOT be called (only branch creation POST)
    assert mock_post.call_count == 1


def test_create_inventory_mr_skips_409_branch_exists():
    """409 on branch creation is tolerated (idempotent)."""
    mock_session = MagicMock()
    with (
        patch("infra_brain.tools.gitlab_mr.httpx.post") as mock_post,
        patch("infra_brain.tools.gitlab_mr.httpx.get") as mock_get,
        patch("infra_brain.tools.gitlab_mr.httpx.put"),
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        # Branch creation raises 409
        branch_409_resp = _mock_resp({}, status=409)
        branch_409_exc = httpx.HTTPStatusError(
            "409 Branch already exists",
            request=MagicMock(),
            response=branch_409_resp,
        )
        # File check: file doesn't exist
        check_resp = _mock_resp({}, status=404)
        # MR check: no open MRs
        mr_check_resp = _mock_resp([], status=200)
        mock_get.side_effect = [check_resp, mr_check_resp]

        # First POST raises 409, second creates file, third creates MR
        branch_post = MagicMock()
        branch_post.raise_for_status.side_effect = branch_409_exc
        mock_post.side_effect = [
            branch_post,
            _mock_resp({"file_path": "inv.yml"}),
            _mock_resp({"web_url": "https://gitlab.example.com/mr/55"}),
        ]

        result = create_inventory_mr(5, "fix/drift", "inv.yml", "content", "msg", "title", "desc")

    assert "https://" in result


def test_create_inventory_mr_is_write_only_to_gitlab():
    """Verify the function returns a GitLab URL (only talks to GitLab)."""
    mock_session = MagicMock()
    with (
        patch("infra_brain.tools.gitlab_mr.httpx.post") as mock_post,
        patch("infra_brain.tools.gitlab_mr.httpx.get") as mock_get,
        patch("infra_brain.tools.gitlab_mr.httpx.put"),
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)
        # File check: file doesn't exist (404), MR check: no open MRs
        mock_get.side_effect = [
            _mock_resp({}, status=404),
            _mock_resp([], status=200),
        ]
        mock_post.side_effect = [
            _mock_resp({"name": "fix/drift"}),
            _mock_resp({"file_path": "f"}),
            _mock_resp({"web_url": "https://gitlab.example.com/mr/1"}),
        ]
        result = create_inventory_mr(5, "fix/drift", "inv.yml", "content", "msg", "title", "desc")
    assert result.startswith("https://")
