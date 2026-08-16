"""Tests for the SNMP tool.

The parse tests are pure/fast. ``test_query_dead_host_degrades_to_none`` is a
REAL, un-mocked exercise of the pysnmp async code path — it proves the query
layer degrades to None (never raises, never fabricates) when a device does not
answer, and would catch a breaking pysnmp API change.
"""

from infra_brain.tools.snmp import SYSTEM_OIDS, parse_system_facts, query_system_facts


def test_system_oids_cover_rfc1213_system_group():
    # sysName/sysDescr are the load-bearing fields the agent keys on.
    assert SYSTEM_OIDS["sys_name"] == "1.3.6.1.2.1.1.5.0"
    assert SYSTEM_OIDS["sys_descr"] == "1.3.6.1.2.1.1.1.0"
    assert len(SYSTEM_OIDS) == 6


def test_parse_prefers_sysname():
    item = parse_system_facts("10.0.0.5", {"sys_name": "fw-edge-01", "sys_descr": "PAN-OS"})
    assert item["name"] == "fw-edge-01"
    assert item["data"]["host"] == "10.0.0.5"
    assert item["data"]["sys_descr"] == "PAN-OS"


def test_parse_handles_empty_values():
    item = parse_system_facts("10.0.0.6", {})
    assert item["name"] == "10.0.0.6"
    assert item["type"] == "net_device"
    # All system fields present (empty), never missing keys.
    for key in ("sys_descr", "sys_object_id", "sys_uptime", "sys_contact", "sys_location"):
        assert item["data"][key] == ""


def test_query_dead_host_degrades_to_none():
    """No SNMP agent on localhost:161 → query returns None within the timeout."""
    result = query_system_facts("127.0.0.1", "public", port=161, timeout=1, retries=0)
    assert result is None
