"""Tests for the single sanctioned host_purpose_map.yml MR helper.

Mocking mirrors tests/tools/test_gitlab_mr.py: the real create_inventory_mr runs
with httpx.{get,post,put} patched, so the sanctioned write path (and its
write-gate call) actually executes. The write-gate is stubbed at
``infra_brain.tools.gitlab_mr.gate_external_write`` so we can (a) assert it is
invoked and (b) simulate a fail-closed deny — no live GitLab calls anywhere.
"""

import base64

import yaml
import pytest
from unittest.mock import MagicMock, patch

from infra_brain.tools.host_purpose_map_mr import open_host_purpose_map_mr


# Existing file already has ONE other host, to prove it is preserved untouched.
_EXISTING_YAML = """\
# host_purpose_map.yml
host_purpose_map:
  OTHERHOST01:
    purpose: "Untouched neighbour"
    vlan: "20"
    subnet: "10.0.20.0/24"
"""


def _mock_resp(data, status=200):
    r = MagicMock()
    r.json.return_value = data
    r.status_code = status
    r.raise_for_status = MagicMock()
    return r


def _file_get_payload(text):
    return {
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
    }


def _fake_gitlab_get_factory(file_text, open_mrs):
    """gitlab_get stub used by the HELPER (file read + open-MR probe)."""

    def _fake(path):
        if "/repository/files/" in path:
            if file_text is None:
                raise _http_404()
            return _file_get_payload(file_text)
        if "/merge_requests" in path:
            return open_mrs
        return {}

    return _fake


def _http_404():
    import httpx

    resp = MagicMock()
    resp.status_code = 404
    return httpx.HTTPStatusError("404", request=MagicMock(), response=resp)


def _find_post_json(mock_post, key):
    """Return the json body of the first post call whose body contains *key*."""
    for call in mock_post.call_args_list:
        body = call.kwargs.get("json") or {}
        if key in body:
            return body
    return None


def test_human_persistence_mutates_one_host_and_calls_sanctioned_path():
    """proposed=False: write-gate invoked, only target host mutated, others kept,
    create_inventory_mr called with expected project/branch/title, returns mr_url."""
    helper_session = MagicMock()
    with (
        patch("infra_brain.tools.host_purpose_map_mr.gitlab_get") as mock_hget,
        patch("infra_brain.tools.host_purpose_map_mr.get_session") as mock_hsess,
        patch("infra_brain.tools.gitlab_mr.gate_external_write") as mock_gate,
        patch("infra_brain.tools.gitlab_mr.httpx.post") as mock_post,
        patch("infra_brain.tools.gitlab_mr.httpx.get") as mock_get,
        patch("infra_brain.tools.gitlab_mr.httpx.put"),
    ):
        mock_hsess.return_value.__enter__ = lambda s: helper_session
        mock_hsess.return_value.__exit__ = MagicMock(return_value=False)
        # Helper reads: file exists (with OTHERHOST01), no open MR yet -> created.
        mock_hget.side_effect = _fake_gitlab_get_factory(_EXISTING_YAML, [])
        # create_inventory_mr file-check on the branch: 404 (new branch), then no open MR.
        mock_get.side_effect = [_mock_resp({}, status=404), _mock_resp([], status=200)]
        mock_post.side_effect = [
            _mock_resp({"name": "infra-brain/host-purpose/prod-lnx-ftb-01"}),  # branch create
            _mock_resp({"file_path": "host_purpose_map.yml"}),  # file create
            _mock_resp({"web_url": "https://gitlab.example.com/mr/321"}),  # MR create
        ]

        result = open_host_purpose_map_mr(
            "prod-lnx-ftb-01",
            purpose="Fleet Transmission Backup",
            vlan="11",
            subnet="198.51.100.16/24",
            actor="alice@corp",
            proposed=False,
        )

    # write-gate was invoked through the sanctioned create_inventory_mr path.
    assert mock_gate.called
    assert mock_gate.call_args.kwargs["system"] == "gitlab"

    # The committed file content mutates exactly the target host; other preserved.
    file_body = _find_post_json(mock_post, "content")
    assert file_body is not None
    committed = yaml.safe_load(file_body["content"])["host_purpose_map"]
    assert committed["OTHERHOST01"] == {
        "purpose": "Untouched neighbour",
        "vlan": "20",
        "subnet": "10.0.20.0/24",
    }
    assert committed["prod-lnx-ftb-01"] == {
        "purpose": "Fleet Transmission Backup",
        "vlan": "11",
        "subnet": "198.51.100.16/24",
    }
    assert set(committed) == {"OTHERHOST01", "prod-lnx-ftb-01"}

    # Sanctioned path called with expected project id / branch / title.
    branch_body = _find_post_json(mock_post, "branch")
    assert branch_body["branch"] == "infra-brain/host-purpose/prod-lnx-ftb-01"
    mr_body = _find_post_json(mock_post, "title")
    assert mr_body["title"] == (
        "chore(host-purpose): persist prod-lnx-ftb-01 purpose/VLAN (via alice@corp)"
    )
    project_urls = [c.args[0] for c in mock_post.call_args_list if c.args]
    assert any("/projects/6/" in u for u in project_urls)

    assert result["mr_url"] == "https://gitlab.example.com/mr/321"
    assert result["branch"] == "infra-brain/host-purpose/prod-lnx-ftb-01"
    assert result["action"] == "created"
    assert result["file_path"] == "host_purpose_map.yml"


def test_agent_proposal_uses_propose_branch_and_writes_no_db():
    """proposed=True: propose-branch/title, and the helper never touches the
    HostPurposeMap table (no DB write of that model in this path)."""
    helper_session = MagicMock()
    with (
        patch("infra_brain.tools.host_purpose_map_mr.gitlab_get") as mock_hget,
        patch("infra_brain.tools.host_purpose_map_mr.get_session") as mock_hsess,
        patch("infra_brain.tools.gitlab_mr.gate_external_write"),
        patch("infra_brain.tools.gitlab_mr.httpx.post") as mock_post,
        patch("infra_brain.tools.gitlab_mr.httpx.get") as mock_get,
        patch("infra_brain.tools.gitlab_mr.httpx.put"),
    ):
        mock_hsess.return_value.__enter__ = lambda s: helper_session
        mock_hsess.return_value.__exit__ = MagicMock(return_value=False)
        # File missing (404) -> start from empty map; no open MR -> created.
        mock_hget.side_effect = _fake_gitlab_get_factory(None, [])
        mock_get.side_effect = [_mock_resp({}, status=404), _mock_resp([], status=200)]
        mock_post.side_effect = [
            _mock_resp({"name": "infra-brain/host-purpose-propose/web01"}),
            _mock_resp({"file_path": "host_purpose_map.yml"}),
            _mock_resp({"web_url": "https://gitlab.example.com/mr/999"}),
        ]

        result = open_host_purpose_map_mr(
            "WEB01",
            purpose="Guessed web frontend",
            vlan=None,
            subnet=None,
            actor="drift-agent",
            proposed=True,
        )

    assert result["branch"] == "infra-brain/host-purpose-propose/web01"
    mr_body = _find_post_json(mock_post, "title")
    assert mr_body["title"] == "propose(host-purpose): WEB01 purpose/VLAN"
    assert "No database write" in mr_body["description"] or "No database write" in (
        _find_post_json(mock_post, "description") or {}
    ).get("description", "")

    # Only host committed is the new one (started from empty file).
    file_body = _find_post_json(mock_post, "content")
    committed = yaml.safe_load(file_body["content"])["host_purpose_map"]
    assert set(committed) == {"WEB01"}
    assert committed["WEB01"]["purpose"] == "Guessed web frontend"

    # The ONLY DB session opened is the audit AgentActionLog write — no
    # HostPurposeMap object is ever added.
    added = [c.args[0] for c in helper_session.add.call_args_list]
    assert all(type(obj).__name__ != "HostPurposeMap" for obj in added)
    assert any(type(obj).__name__ == "AgentActionLog" for obj in added)
    assert result["mr_url"] == "https://gitlab.example.com/mr/999"


def test_idempotent_refresh_reports_updated():
    """Second run for the same host: an open MR already exists on the branch, so
    the helper reports action='updated' (and the file is refreshed via PUT)."""
    helper_session = MagicMock()
    with (
        patch("infra_brain.tools.host_purpose_map_mr.gitlab_get") as mock_hget,
        patch("infra_brain.tools.host_purpose_map_mr.get_session") as mock_hsess,
        patch("infra_brain.tools.gitlab_mr.gate_external_write"),
        patch("infra_brain.tools.gitlab_mr.httpx.post") as mock_post,
        patch("infra_brain.tools.gitlab_mr.httpx.get") as mock_get,
        patch("infra_brain.tools.gitlab_mr.httpx.put") as mock_put,
    ):
        mock_hsess.return_value.__enter__ = lambda s: helper_session
        mock_hsess.return_value.__exit__ = MagicMock(return_value=False)
        # Helper probe: an open MR already exists for the branch -> "updated".
        existing_mr = [{"web_url": "https://gitlab.example.com/mr/77"}]
        mock_hget.side_effect = _fake_gitlab_get_factory(_EXISTING_YAML, existing_mr)
        # create_inventory_mr: file exists on branch (PUT), open MR found -> reuse URL.
        mock_get.side_effect = [
            _mock_resp({"file_path": "host_purpose_map.yml"}, status=200),
            _mock_resp(existing_mr, status=200),
        ]
        mock_post.side_effect = [_mock_resp({"name": "infra-brain/host-purpose/prod-lnx-ftb-01"})]
        mock_put.return_value = _mock_resp({"file_path": "host_purpose_map.yml"})

        result = open_host_purpose_map_mr(
            "prod-lnx-ftb-01",
            purpose="Updated purpose",
            vlan="11",
            subnet="198.51.100.16/24",
            actor="alice@corp",
            proposed=False,
        )

    assert result["action"] == "updated"
    assert result["mr_url"] == "https://gitlab.example.com/mr/77"
    mock_put.assert_called_once()  # file refreshed on the existing branch


def test_write_gate_denial_propagates():
    """A fail-closed write-gate deny (PermissionError) is NOT swallowed —
    it propagates out of the helper unchanged."""
    helper_session = MagicMock()
    with (
        patch("infra_brain.tools.host_purpose_map_mr.gitlab_get") as mock_hget,
        patch("infra_brain.tools.host_purpose_map_mr.get_session") as mock_hsess,
        patch("infra_brain.tools.gitlab_mr.gate_external_write") as mock_gate,
        patch("infra_brain.tools.gitlab_mr.httpx.post") as mock_post,
    ):
        mock_hsess.return_value.__enter__ = lambda s: helper_session
        mock_hsess.return_value.__exit__ = MagicMock(return_value=False)
        mock_hget.side_effect = _fake_gitlab_get_factory(_EXISTING_YAML, [])
        mock_gate.side_effect = PermissionError(
            "[write-gate] PAN detected in outbound write payload"
        )

        with pytest.raises(PermissionError, match="write-gate"):
            open_host_purpose_map_mr(
                "prod-lnx-ftb-01",
                purpose="x",
                vlan="11",
                subnet="198.51.100.16/24",
                actor="alice@corp",
                proposed=False,
            )

    # Gate blocked BEFORE any GitLab write POST left the process.
    mock_post.assert_not_called()
    # The deny was still audited (verdict recorded), not silently dropped.
    audited = [c.args[0] for c in helper_session.add.call_args_list]
    assert any(getattr(obj, "verdict", None) == "deny" for obj in audited)
