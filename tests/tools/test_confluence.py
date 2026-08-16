from unittest.mock import patch, MagicMock
from infra_brain.tools.confluence import upsert_confluence_page, build_domain_page_body


def test_build_domain_page_body_contains_resource_names():
    resources = [
        {"name": "web01", "type": "linux_host", "last_seen": "2026-06-18T00:00:00Z"},
        {"name": "web02", "type": "linux_host", "last_seen": "2026-06-18T00:00:00Z"},
    ]
    body = build_domain_page_body("linux", resources)
    assert "web01" in body
    assert "web02" in body
    assert "linux" in body.lower()


def test_build_domain_page_body_escapes_special_xhtml_chars():
    # Confluence storage format is strict XHTML — raw '&'/'<'/'>' in resource
    # fields must not reach the output unescaped, or the PUT/POST to Confluence
    # is rejected as malformed storage value (or corrupts the rendered page).
    resources = [
        {
            "name": "AT&T-switch01",
            "type": "<network>",
            "last_seen": '2026-06-18T00:00:00Z" onmouseover="alert(1)',
        }
    ]
    body = build_domain_page_body("linux", resources)

    assert "AT&T-switch01" not in body
    assert "AT&amp;T-switch01" in body
    assert "<network>" not in body
    assert "&lt;network&gt;" in body

    # No raw unescaped '&' should remain outside of valid entity references.
    import re

    for m in re.finditer(r"&(?!amp;|lt;|gt;|quot;|#39;)", body):
        raise AssertionError(f"unescaped '&' found in body at position {m.start()}: {body[m.start():m.start()+20]!r}")


def test_upsert_creates_page_if_not_exists():
    mock_session = MagicMock()
    mock_session.query.return_value.filter_by.return_value.first.return_value = None

    mock_create_resp = MagicMock()
    mock_create_resp.json.return_value = {"id": "12345"}
    mock_create_resp.raise_for_status = MagicMock()

    with (
        patch("infra_brain.tools.confluence.httpx.get") as mock_get,
        patch("infra_brain.tools.confluence.httpx.post", return_value=mock_create_resp),
        patch("infra_brain.tools.confluence.get_session") as mock_ctx,
        patch("infra_brain.callbacks.write_gate.get_session") as mock_gate_ctx,
    ):
        mock_get.return_value.json.return_value = {"results": []}
        mock_get.return_value.raise_for_status = MagicMock()
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        mock_gate_ctx.return_value.__enter__ = lambda s: mock_session
        mock_gate_ctx.return_value.__exit__ = MagicMock(return_value=False)

        page_id = upsert_confluence_page("linux", "Linux Hosts", "<p>body</p>")

    assert page_id == "12345"
    mock_session.add.assert_called()
