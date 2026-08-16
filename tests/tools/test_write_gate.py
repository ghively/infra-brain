"""Item 1.2 tests — F-004.3: pre-write DLP + audit gate on every external write."""

from unittest.mock import MagicMock, patch

import pytest

from infra_brain.callbacks.write_gate import gate_external_write

# Luhn-valid test PAN (the standard Visa test number — never a real card).
_PAN = "4111 1111 1111 1111"


def _stub_gate_db():
    """Patch get_session in write_gate; returns the MagicMock session for asserts."""
    session = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    p = patch("infra_brain.callbacks.write_gate.get_session", return_value=ctx)
    return p, session


def test_pan_payload_denied_and_audited():
    """Fails today: write_gate module does not exist."""
    p, session = _stub_gate_db()
    with p:
        with pytest.raises(PermissionError, match="write-gate"):
            gate_external_write("gitlab", "create_inventory_mr", f"card {_PAN}", "t")
    row = session.add.call_args.args[0]
    assert row.verdict == "deny"
    assert row.tool == "gitlab:create_inventory_mr"


def test_clean_payload_allowed_and_audited():
    p, session = _stub_gate_db()
    with p:
        gate_external_write("jira", "create_jira_ticket", "no card data here", "t")
    row = session.add.call_args.args[0]
    assert row.verdict == "allow"
    assert row.status == "ok"


def _make_luhn_valid(prefix: str) -> str:
    """Append the check digit that makes *prefix* Luhn-valid (mirrors test_dlp)."""
    for d in "0123456789":
        candidate = prefix + d
        digits = [int(c) for c in candidate]
        total = 0
        for i, n in enumerate(reversed(digits)):
            if i % 2 == 1:
                n *= 2
                if n > 9:
                    n -= 9
            total += n
        if total % 10 == 0:
            return candidate
    raise AssertionError("unreachable")


@pytest.mark.parametrize(
    "prefix,label",
    [
        ("000000000000000", "rapid7-agent-id"),  # 16 digits, prefix "00"
        ("580000000000000000", "x509-cert-serial"),  # 19 digits, prefix "58"
        ("501800000000000", "maestro-shaped-serial"),  # 16 digits, BIN 5018
    ],
)
def test_luhn_valid_non_card_identifier_does_not_block_write(prefix, label):
    """Regression: the gate must use the corroborated is_probable_pan() check,
    not bare Luhn. Cert serials / Rapid7 IDs / epoch-ns timestamps are Luhn-valid
    by chance (~1 in 10) and routinely appear in outbound MR/ticket/page payloads
    — blocking them would break sanctioned writes (see dlp._matches_card_network).
    """
    identifier = _make_luhn_valid(prefix)
    p, session = _stub_gate_db()
    with p:
        gate_external_write("gitlab", "create_inventory_mr", f"{label}: {identifier}", "t")
    row = session.add.call_args.args[0]
    assert row.verdict == "allow"
    assert row.status == "ok"
    assert row.error is None


def test_create_inventory_mr_refused_before_any_post():
    """ROADMAP 1.2 acceptance: a DLP-triggering MR payload is refused before any
    httpx.post. Fails today (no gate — httpx.post would be reached)."""
    from infra_brain.tools import gitlab_mr

    p, _ = _stub_gate_db()
    with (
        p,
        patch("infra_brain.tools.gitlab_mr.httpx") as mock_httpx,
    ):
        with pytest.raises(PermissionError, match="write-gate"):
            gitlab_mr.create_inventory_mr(
                project_id=1,
                branch_name="b",
                file_path="f.yml",
                new_content=f"hosts:\n  # {_PAN}\n",
                commit_message="m",
                mr_title="t",
                mr_description="d",
            )
        mock_httpx.post.assert_not_called()
        mock_httpx.put.assert_not_called()


def test_create_jira_ticket_refused_before_any_post():
    from infra_brain.tools import jira

    p, _ = _stub_gate_db()
    with (
        p,
        patch("infra_brain.tools.jira.ticket_exists", return_value=None),
        patch("infra_brain.tools.jira.httpx") as mock_httpx,
    ):
        with pytest.raises(PermissionError, match="write-gate"):
            jira.create_jira_ticket("summary", f"desc {_PAN}", drift_event_id=MagicMock())
        mock_httpx.post.assert_not_called()


def test_upsert_confluence_page_refused_before_any_http():
    from infra_brain.tools import confluence

    p, _ = _stub_gate_db()
    with (
        p,
        patch("infra_brain.tools.confluence.httpx") as mock_httpx,
        patch("infra_brain.tools.confluence.get_session"),
    ):
        with pytest.raises(PermissionError, match="write-gate"):
            confluence.upsert_confluence_page("digest", "title", f"<p>{_PAN}</p>")
        mock_httpx.get.assert_not_called()
        mock_httpx.post.assert_not_called()
        mock_httpx.put.assert_not_called()


def test_post_digest_logs_gate_denial_instead_of_pass():
    """digest.py must LOG a gate denial (not `pass`) and still return the markdown."""
    from infra_brain import digest as digest_mod

    with (
        patch.object(digest_mod, "build_digest_markdown", return_value="# md"),
        patch(
            "infra_brain.tools.confluence.upsert_confluence_page",
            side_effect=PermissionError("[write-gate] PAN detected"),
        ),
        patch.object(digest_mod.logger, "warning") as mock_warn,
    ):
        assert digest_mod.post_digest() == "# md"
        assert mock_warn.called
        assert "BLOCKED" in mock_warn.call_args.args[0]
