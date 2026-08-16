"""Tests for infra_brain.tools.eol's HTTP-calling tool, ``eol_cycles_tool``.

``normalize_os_string``/``normalize_fingerprint`` (the OS-string mapper in
this same module) already have thorough coverage via
``tests/agents/test_eol.py`` (imported directly from ``infra_brain.tools.eol``)
— this file fills the one remaining gap (MR-E TEST-4): the actual HTTP tool
that hits endoflife.date, which the agent tests only ever mock out.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from infra_brain.tools.eol import eol_cycles_tool


def _fake_client(json_body, status_ok=True):
    """A MagicMock standing in for `with ReadOnlyClient(...) as client:`.

    ``client.get(url)`` returns a fake ``httpx.Response``-shaped mock with
    ``.json()`` / ``.raise_for_status()`` configured — mirrors the real
    ``ReadOnlyClient.get()`` -> ``httpx.Response`` contract.
    """
    resp = MagicMock()
    resp.json.return_value = json_body
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock(status_code=404)
        )
    client = MagicMock()
    client.get.return_value = resp
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    return ctx


def test_eol_cycles_tool_returns_parsed_json():
    cycles = [{"cycle": "9", "eol": "2027-05-31", "latest": "9.4", "lts": True}]
    with patch("infra_brain.tools.eol.ReadOnlyClient", return_value=_fake_client(cycles)):
        result = eol_cycles_tool.invoke({"product": "rhel"})
    assert result == cycles


def test_eol_cycles_tool_builds_lowercase_dashed_slug():
    with patch("infra_brain.tools.eol.ReadOnlyClient", return_value=_fake_client([])) as mock_cls:
        eol_cycles_tool.invoke({"product": "Windows Server"})
    # The client instance's .get() must be called with the slugified URL.
    fake_client_instance = mock_cls.return_value.__enter__.return_value
    called_url = fake_client_instance.get.call_args[0][0]
    assert called_url.endswith("/windows-server.json")


def test_eol_cycles_tool_slugifies_multi_word_product_with_mixed_case():
    with patch("infra_brain.tools.eol.ReadOnlyClient", return_value=_fake_client([])) as mock_cls:
        eol_cycles_tool.invoke({"product": "Rocky Linux"})
    fake_client_instance = mock_cls.return_value.__enter__.return_value
    called_url = fake_client_instance.get.call_args[0][0]
    assert called_url.endswith("/rocky-linux.json")


def test_eol_cycles_tool_calls_raise_for_status():
    ctx = _fake_client([])
    with patch("infra_brain.tools.eol.ReadOnlyClient", return_value=ctx):
        eol_cycles_tool.invoke({"product": "centos"})
    fake_client_instance = ctx.__enter__.return_value
    fake_client_instance.get.return_value.raise_for_status.assert_called_once()


def test_eol_cycles_tool_propagates_http_errors():
    """A 404 (unknown product) must raise, not silently return []  — the
    caller (EOLAgent) needs to distinguish "no data" from "product typo"."""
    with patch(
        "infra_brain.tools.eol.ReadOnlyClient",
        return_value=_fake_client([], status_ok=False),
    ), pytest.raises(httpx.HTTPStatusError):
        eol_cycles_tool.invoke({"product": "not-a-real-product"})


def test_eol_cycles_tool_uses_15s_timeout():
    with patch("infra_brain.tools.eol.ReadOnlyClient", return_value=_fake_client([])) as mock_cls:
        eol_cycles_tool.invoke({"product": "ubuntu"})
    _, kwargs = mock_cls.call_args
    assert kwargs.get("timeout") == 15


def test_eol_cycles_tool_rewrites_renamed_vmware_esxi_slug():
    """TRK-257 regression: endoflife.date renamed the ``vmware-esxi`` product
    slug to ``esxi`` and 301-redirects the old URL. The read-only client does
    not follow redirects (tools/http_readonly.py — deliberate structural
    constraint), so the stale slug silently returned no data every run. The
    tool must request the current ``esxi.json`` slug directly, so no redirect
    is ever hit, while callers keep passing the stable internal identifier
    ``vmware-esxi`` (used throughout agents/eol.py's _OS_RULES/cache keys)."""
    with patch("infra_brain.tools.eol.ReadOnlyClient", return_value=_fake_client([])) as mock_cls:
        eol_cycles_tool.invoke({"product": "vmware-esxi"})
    fake_client_instance = mock_cls.return_value.__enter__.return_value
    called_url = fake_client_instance.get.call_args[0][0]
    assert called_url.endswith("/esxi.json")
    assert "vmware-esxi" not in called_url


def test_eol_cycles_tool_slug_alias_is_case_insensitive():
    with patch("infra_brain.tools.eol.ReadOnlyClient", return_value=_fake_client([])) as mock_cls:
        eol_cycles_tool.invoke({"product": "VMware ESXi"})
    fake_client_instance = mock_cls.return_value.__enter__.return_value
    called_url = fake_client_instance.get.call_args[0][0]
    assert called_url.endswith("/esxi.json")
