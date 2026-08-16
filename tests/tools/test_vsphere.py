"""Tests for vSphere tools — Task 15. All tests must pass WITHOUT pyVmomi installed."""

from unittest.mock import patch, MagicMock
import pytest as _pytest_vs
from langchain_core.tools import ToolException as _ToolException_vs


def _unconfigured_settings():
    s = MagicMock()
    s.vsphere_host = ""
    s.vsphere_hosts = ""
    s.vsphere_user = ""
    s.vsphere_password = ""
    s.vsphere_ssl_verify = True
    s.vsphere_connect_timeout = 30
    return s


def _configured_settings(host="vcenter.example.com"):
    s = MagicMock()
    s.vsphere_host = host
    s.vsphere_hosts = ""
    s.vsphere_user = "admin"
    s.vsphere_password = "secret"
    s.vsphere_ssl_verify = False
    s.vsphere_connect_timeout = 30
    return s


def test_vsphere_vms_returns_empty_when_unconfigured():
    from infra_brain.tools.vsphere import vsphere_vms_tool

    with patch("infra_brain.tools.vsphere.get_settings", return_value=_unconfigured_settings()):
        result = vsphere_vms_tool.invoke({})
    assert result == []


def test_vsphere_hosts_returns_empty_when_unconfigured():
    from infra_brain.tools.vsphere import vsphere_hosts_tool

    with patch("infra_brain.tools.vsphere.get_settings", return_value=_unconfigured_settings()):
        result = vsphere_hosts_tool.invoke({})
    assert result == []


def test_vsphere_datastores_returns_empty_when_unconfigured():
    from infra_brain.tools.vsphere import vsphere_datastores_tool

    with patch("infra_brain.tools.vsphere.get_settings", return_value=_unconfigured_settings()):
        result = vsphere_datastores_tool.invoke({})
    assert result == []


def test_vsphere_vms_returns_empty_when_pyvmomi_missing(monkeypatch):
    """When pyVmomi is not installed AND vsphere_host is set, returns [] with warning."""
    import infra_brain.tools.vsphere as vsphere_mod

    original = vsphere_mod._PYVMOMI_AVAILABLE
    try:
        vsphere_mod._PYVMOMI_AVAILABLE = False
        with patch("infra_brain.tools.vsphere.get_settings", return_value=_configured_settings()):
            result = vsphere_mod.vsphere_vms_tool.invoke({})
        assert result == []
    finally:
        vsphere_mod._PYVMOMI_AVAILABLE = original


def test_vsphere_hosts_returns_empty_when_pyvmomi_missing(monkeypatch):
    import infra_brain.tools.vsphere as vsphere_mod

    original = vsphere_mod._PYVMOMI_AVAILABLE
    try:
        vsphere_mod._PYVMOMI_AVAILABLE = False
        with patch("infra_brain.tools.vsphere.get_settings", return_value=_configured_settings()):
            result = vsphere_mod.vsphere_hosts_tool.invoke({})
        assert result == []
    finally:
        vsphere_mod._PYVMOMI_AVAILABLE = original


def test_vsphere_datastores_returns_empty_when_pyvmomi_missing():
    import infra_brain.tools.vsphere as vsphere_mod

    original = vsphere_mod._PYVMOMI_AVAILABLE
    try:
        vsphere_mod._PYVMOMI_AVAILABLE = False
        with patch("infra_brain.tools.vsphere.get_settings", return_value=_configured_settings()):
            result = vsphere_mod.vsphere_datastores_tool.invoke({})
        assert result == []
    finally:
        vsphere_mod._PYVMOMI_AVAILABLE = original


def test_vsphere_module_imports_without_pyvmomi():
    """The module must be importable even when pyVmomi is not installed."""
    import infra_brain.tools.vsphere as vsphere_mod

    assert hasattr(vsphere_mod, "vsphere_vms_tool")
    assert hasattr(vsphere_mod, "vsphere_hosts_tool")
    assert hasattr(vsphere_mod, "vsphere_datastores_tool")
    assert hasattr(vsphere_mod, "_PYVMOMI_AVAILABLE")


# ---------------------------------------------------------------------------
# TRK-024 — collection failure raises ToolException (was silently [])
# ---------------------------------------------------------------------------
def test_vsphere_vms_raises_toolexception_on_collection_error():
    """TRK-024: a real collection failure now surfaces as ToolException instead of
    silently degrading to []. Consistent with the other tool modules; ToolNode still
    catches it downstream (handle_tool_errors=True) so the chat loop does not abort."""
    import infra_brain.tools.vsphere as vsphere_mod

    original = vsphere_mod._PYVMOMI_AVAILABLE
    try:
        vsphere_mod._PYVMOMI_AVAILABLE = True
        with (
            patch(
                "infra_brain.tools.vsphere.get_settings",
                return_value=_configured_settings(),
            ),
            patch(
                "infra_brain.tools.vsphere._connect",
                side_effect=RuntimeError("vcenter down"),
            ),
        ):
            with _pytest_vs.raises(_ToolException_vs, match="collection failed"):
                vsphere_mod.vsphere_vms_tool.invoke({})
    finally:
        vsphere_mod._PYVMOMI_AVAILABLE = original


def test_vsphere_hosts_raises_toolexception_on_collection_error():
    """TRK-024: host collection failure surfaces as ToolException."""
    import infra_brain.tools.vsphere as vsphere_mod

    original = vsphere_mod._PYVMOMI_AVAILABLE
    try:
        vsphere_mod._PYVMOMI_AVAILABLE = True
        with (
            patch(
                "infra_brain.tools.vsphere.get_settings",
                return_value=_configured_settings(),
            ),
            patch(
                "infra_brain.tools.vsphere._connect",
                side_effect=RuntimeError("vcenter down"),
            ),
        ):
            with _pytest_vs.raises(_ToolException_vs, match="collection failed"):
                vsphere_mod.vsphere_hosts_tool.invoke({})
    finally:
        vsphere_mod._PYVMOMI_AVAILABLE = original


# ---------------------------------------------------------------------------
# Multi-vCenter chat tools — the finding: vsphere_vms_tool / vsphere_hosts_tool /
# vsphere_datastores_tool only ever queried the first of several comma-separated
# vsphere_hosts entries, contradicting their "List all..." docstrings.
# ---------------------------------------------------------------------------


def _multi_vcenter_settings():
    s = MagicMock()
    s.vsphere_host = ""
    s.vsphere_hosts = "vc1.example.com, vc2.example.com"
    s.vsphere_user = "admin"
    s.vsphere_password = "secret"
    s.vsphere_ssl_verify = False
    s.vsphere_connect_timeout = 30
    return s


def test_vsphere_vms_queries_every_configured_vcenter():
    """All vCenters in a comma-separated vsphere_hosts list must be scanned and
    their results merged — not just the first one."""
    import infra_brain.tools.vsphere as vsphere_mod

    original = vsphere_mod._PYVMOMI_AVAILABLE
    try:
        vsphere_mod._PYVMOMI_AVAILABLE = True
        mock_si = MagicMock()
        mock_si.RetrieveContent.return_value = MagicMock()

        def fake_collect_vms(content, host):
            return [{"name": f"vm-on-{host}", "type": "vsphere_vm", "data": {}}]

        with (
            patch(
                "infra_brain.tools.vsphere.get_settings",
                return_value=_multi_vcenter_settings(),
            ),
            patch("infra_brain.tools.vsphere._connect", return_value=mock_si) as mock_connect,
            patch("infra_brain.tools.vsphere.Disconnect") as mock_disconnect,
            patch("infra_brain.tools.vsphere.collect_vms", side_effect=fake_collect_vms),
        ):
            result = vsphere_mod.vsphere_vms_tool.invoke({})

        # Both vCenters were connected to and disconnected from.
        connected_hosts = [call.args[0] for call in mock_connect.call_args_list]
        assert connected_hosts == ["vc1.example.com", "vc2.example.com"]
        assert mock_disconnect.call_count == 2
        # Results from both vCenters are present, not just the first.
        names = {item["name"] for item in result}
        assert names == {"vm-on-vc1.example.com", "vm-on-vc2.example.com"}
    finally:
        vsphere_mod._PYVMOMI_AVAILABLE = original


def test_vsphere_hosts_queries_every_configured_vcenter():
    import infra_brain.tools.vsphere as vsphere_mod

    original = vsphere_mod._PYVMOMI_AVAILABLE
    try:
        vsphere_mod._PYVMOMI_AVAILABLE = True
        mock_si = MagicMock()
        mock_si.RetrieveContent.return_value = MagicMock()

        def fake_collect_esxi_hosts(content, host):
            return [{"name": f"esxi-on-{host}", "type": "vsphere_host", "data": {}}]

        with (
            patch(
                "infra_brain.tools.vsphere.get_settings",
                return_value=_multi_vcenter_settings(),
            ),
            patch("infra_brain.tools.vsphere._connect", return_value=mock_si) as mock_connect,
            patch("infra_brain.tools.vsphere.Disconnect"),
            patch(
                "infra_brain.tools.vsphere.collect_esxi_hosts",
                side_effect=fake_collect_esxi_hosts,
            ),
        ):
            result = vsphere_mod.vsphere_hosts_tool.invoke({})

        connected_hosts = [call.args[0] for call in mock_connect.call_args_list]
        assert connected_hosts == ["vc1.example.com", "vc2.example.com"]
        names = {item["name"] for item in result}
        assert names == {"esxi-on-vc1.example.com", "esxi-on-vc2.example.com"}
    finally:
        vsphere_mod._PYVMOMI_AVAILABLE = original


def test_vsphere_datastores_queries_every_configured_vcenter():
    import infra_brain.tools.vsphere as vsphere_mod

    original = vsphere_mod._PYVMOMI_AVAILABLE
    try:
        vsphere_mod._PYVMOMI_AVAILABLE = True
        mock_si = MagicMock()
        mock_si.RetrieveContent.return_value = MagicMock()

        def fake_collect_datastores(content, host):
            return [{"name": f"ds-on-{host}", "type": "vsphere_datastore", "data": {}}]

        with (
            patch(
                "infra_brain.tools.vsphere.get_settings",
                return_value=_multi_vcenter_settings(),
            ),
            patch("infra_brain.tools.vsphere._connect", return_value=mock_si) as mock_connect,
            patch("infra_brain.tools.vsphere.Disconnect"),
            patch(
                "infra_brain.tools.vsphere.collect_datastores",
                side_effect=fake_collect_datastores,
            ),
        ):
            result = vsphere_mod.vsphere_datastores_tool.invoke({})

        connected_hosts = [call.args[0] for call in mock_connect.call_args_list]
        assert connected_hosts == ["vc1.example.com", "vc2.example.com"]
        names = {item["name"] for item in result}
        assert names == {"ds-on-vc1.example.com", "ds-on-vc2.example.com"}
    finally:
        vsphere_mod._PYVMOMI_AVAILABLE = original


def test_vsphere_vms_one_bad_vcenter_does_not_blank_the_others():
    """A single unreachable vCenter in a multi-vCenter list must not wipe out
    results from the vCenters that did respond."""
    import infra_brain.tools.vsphere as vsphere_mod

    original = vsphere_mod._PYVMOMI_AVAILABLE
    try:
        vsphere_mod._PYVMOMI_AVAILABLE = True
        mock_si = MagicMock()
        mock_si.RetrieveContent.return_value = MagicMock()

        def fake_connect(host, *args, **kwargs):
            if host == "vc1.example.com":
                raise RuntimeError("vc1 down")
            return mock_si

        with (
            patch(
                "infra_brain.tools.vsphere.get_settings",
                return_value=_multi_vcenter_settings(),
            ),
            patch("infra_brain.tools.vsphere._connect", side_effect=fake_connect),
            patch("infra_brain.tools.vsphere.Disconnect"),
            patch(
                "infra_brain.tools.vsphere.collect_vms",
                return_value=[{"name": "vm-on-vc2", "type": "vsphere_vm", "data": {}}],
            ),
        ):
            result = vsphere_mod.vsphere_vms_tool.invoke({})

        assert [item["name"] for item in result] == ["vm-on-vc2"]
    finally:
        vsphere_mod._PYVMOMI_AVAILABLE = original


# ---------------------------------------------------------------------------
# perf-history percent scaling — the finding: collect_vm_perf_history averaged
# raw vSphere 'percent'-unit counter values (hundredths of a percent) without
# dividing by 100, so *_pct_avg fields were stored ~100x too large.
# ---------------------------------------------------------------------------


def _make_obj_content(props_dict: dict):
    """Build a mock ObjectContent with propSet from a flat dict."""
    obj = MagicMock()
    prop_list = []
    for name, val in props_dict.items():
        p = MagicMock()
        p.name = name
        p.val = val
        prop_list.append(p)
    obj.propSet = prop_list
    return obj


def test_collect_vm_perf_history_scales_percent_counters_to_0_100():
    import infra_brain.tools.vsphere as vsphere_mod

    original = vsphere_mod._PYVMOMI_AVAILABLE
    try:
        vsphere_mod._PYVMOMI_AVAILABLE = True

        vm_obj = _make_obj_content(
            {"name": "vm-prod", "runtime.powerState": "poweredOn", "config.template": False}
        )
        sentinel_mor = object()
        vm_obj.obj = sentinel_mor

        cpu_series = MagicMock()
        cpu_series.id.counterId = 6
        # Raw vSphere values are hundredths of a percent: 4500 == 45.00%.
        cpu_series.value = [4000, 5000, 4500]

        entity_result = MagicMock()
        entity_result.entity = sentinel_mor
        entity_result.value = [cpu_series]

        counter_cpu = MagicMock()
        counter_cpu.groupInfo.key = "cpu"
        counter_cpu.nameInfo.key = "usage"
        counter_cpu.rollupType = "average"
        counter_cpu.key = 6

        perf_mgr = MagicMock()
        perf_mgr.perfCounter = [counter_cpu]
        perf_mgr.QueryPerf.return_value = [entity_result]

        content = MagicMock()
        content.perfManager = perf_mgr

        with (
            patch("infra_brain.tools.vsphere.vim", MagicMock()),
            patch(
                "infra_brain.tools.vsphere._collect_properties", return_value=[vm_obj]
            ),
        ):
            result = vsphere_mod.collect_vm_perf_history(content, "vc1.example.com")

        assert "vm-prod" in result
        # (4000 + 5000 + 4500) / 3 = 4500.0 hundredths-of-a-percent == 45.0%.
        assert result["vm-prod"]["perf_cpu_usage_pct_avg"] == 45.0
    finally:
        vsphere_mod._PYVMOMI_AVAILABLE = original
