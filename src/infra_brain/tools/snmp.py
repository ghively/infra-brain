"""
Read-only SNMP collection for network devices (switches, firewalls, load balancers).

Walks the standard SNMPv2 system group (RFC 1213) via a read-only community
string. Gracefully degrades when:
  - pysnmp is not installed (the optional ``net`` extra),
  - no targets / community are configured, or
  - a device is unreachable / SNMP is disabled.

In every degraded case the collector returns nothing real rather than fabricating
data, matching the convention of the vSphere / Rapid7 tools.
"""

import logging

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# RFC 1213 system group — read-only, present on virtually every SNMP agent.
SYSTEM_OIDS: dict[str, str] = {
    "sys_descr": "1.3.6.1.2.1.1.1.0",
    "sys_object_id": "1.3.6.1.2.1.1.2.0",
    "sys_uptime": "1.3.6.1.2.1.1.3.0",
    "sys_contact": "1.3.6.1.2.1.1.4.0",
    "sys_name": "1.3.6.1.2.1.1.5.0",
    "sys_location": "1.3.6.1.2.1.1.6.0",
}


def parse_system_facts(host: str, values: dict[str, str]) -> dict:
    """Map raw SNMP system-group values into a resource dict.

    Pure function — no I/O — so it is fully unit-testable against a captured
    fixture. ``values`` is keyed by the friendly names in ``SYSTEM_OIDS``.
    The device name prefers the SNMP-reported sysName, falling back to ``host``.
    """
    sys_name = (values.get("sys_name") or "").strip()
    return {
        "name": sys_name or host,
        "type": "net_device",
        "data": {
            "host": host,
            "sys_name": sys_name,
            "sys_descr": values.get("sys_descr", ""),
            "sys_object_id": values.get("sys_object_id", ""),
            "sys_uptime": values.get("sys_uptime", ""),
            "sys_contact": values.get("sys_contact", ""),
            "sys_location": values.get("sys_location", ""),
        },
    }


async def _async_get_system(host: str, community: str, port: int, timeout: int, retries: int):
    """Run a single SNMP GET for the system-group OIDs. Returns name->value dict or None."""
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        get_cmd,
    )

    transport = await UdpTransportTarget.create((host, port), timeout=timeout, retries=retries)
    names = list(SYSTEM_OIDS.keys())
    object_types = [ObjectType(ObjectIdentity(SYSTEM_OIDS[n])) for n in names]

    error_indication, error_status, _error_index, var_binds = await get_cmd(
        SnmpEngine(),
        CommunityData(community, mpModel=1),  # mpModel=1 → SNMP v2c
        transport,
        ContextData(),
        *object_types,
    )

    if error_indication:
        logger.info("SNMP %s: no response (%s)", host, error_indication)
        return None
    if error_status:
        logger.info("SNMP %s: error status %s", host, error_status.prettyPrint())
        return None

    values: dict[str, str] = {}
    for name, var_bind in zip(names, var_binds):
        # var_bind is (ObjectType) → unpacks to (oid, value)
        _oid, value = var_bind
        values[name] = value.prettyPrint()
    return values


def query_system_facts(
    host: str,
    community: str,
    port: int = 161,
    timeout: int = 2,
    retries: int = 1,
) -> dict | None:
    """Query one device's SNMP system group. Returns name->value dict, or None.

    Returns None (never raises, never fabricates) on missing pysnmp, unreachable
    host, or any SNMP error — callers simply skip the device.
    """
    import asyncio

    try:
        return asyncio.run(_async_get_system(host, community, port, timeout, retries))
    except ImportError:
        logger.warning("pysnmp not installed — skipping SNMP collection (install the 'net' extra)")
        return None
    except Exception as exc:  # noqa: BLE001 — any transport/SNMP failure degrades to skip
        logger.warning("SNMP query to %s failed: %s", host, exc)
        return None


@tool
def net_device_facts_tool(
    host: str,
    community: str,
    port: int = 161,
    timeout: int = 2,
    retries: int = 1,
) -> dict | None:
    """Query SNMP system-group facts from a network device and return a resource dict (read-only).

    Returns {name, type, data} on success, or None if the device is unreachable.
    """
    values = query_system_facts(host, community, port=port, timeout=timeout, retries=retries)
    if values is None:
        return None
    return parse_system_facts(host, values)
