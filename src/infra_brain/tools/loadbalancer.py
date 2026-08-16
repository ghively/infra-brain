"""Load balancer / reverse proxy / CDN read-only tools (GitLab issue #100).

Four vendors, all naturally GET-only:
  - F5 BIG-IP        — iControl REST (``/mgmt/tm/ltm/...``)
  - nginx Plus       — the nginx Plus API (``/api/{version}/http/upstreams``)
  - HAProxy          — the stats page CSV export (``;csv``)
  - Cloudflare       — the Cloudflare API (``api.cloudflare.com``)

Every call goes through ``infra_brain.tools.http_readonly.readonly_get`` (a
GET/HEAD-only ``httpx.Client``) — the structural read-only layer (R2 layer 1).
Each tool returns a list of resource dicts: ``{"name", "type", "data"}``,
mirroring the ``aws.py`` / ``octopus_tool.py`` shape so ``LoadBalancerAgent``
can feed them straight into ``ETLConnector.collect()``.
"""

from __future__ import annotations

import csv
import io
import logging

from langchain_core.tools import tool

from infra_brain.config import get_settings
from infra_brain.tools.http_readonly import readonly_get, tool_http_errors

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# F5 BIG-IP (iControl REST)
# ---------------------------------------------------------------------------


@tool
@tool_http_errors
def f5_ltm_pools_tool(host: str, ssl_verify: bool = True) -> list:
    """List F5 LTM pools and their members via iControl REST (read-only).

    Credentials are read from settings inside this function, never taken as a
    tool argument — tool-call arguments are persisted verbatim into
    ``AuditLog``/``AgentActionLog`` (and shipped to the Langfuse trace store
    when tracing is on), so an admin username/password must never appear there.
    """
    s = get_settings()
    resp = readonly_get(
        f"https://{host}/mgmt/tm/ltm/pool",
        params={"expandSubcollections": "true"},
        auth=(s.f5_username, s.f5_password),
        verify=ssl_verify,
    )
    resp.raise_for_status()
    items = []
    for pool in resp.json().get("items", []):
        members = [
            {
                "address": m.get("address", ""),
                "port": int(str(m.get("name", "")).rsplit(":", 1)[-1] or 0),
                "state": m.get("state", ""),
                "session": m.get("session", ""),
            }
            for m in pool.get("membersReference", {}).get("items", [])
        ]
        items.append(
            {
                "name": pool.get("name", ""),
                "type": "lb_pool",
                "data": {
                    "vendor": "f5",
                    "instance": host,
                    "monitor_type": pool.get("monitor", ""),
                    "members": members,
                },
            }
        )
    return items


@tool
@tool_http_errors
def f5_ltm_virtual_servers_tool(host: str, ssl_verify: bool = True) -> list:
    """List F5 LTM virtual servers (listeners) via iControl REST (read-only).

    Credentials are read from settings inside this function — see
    ``f5_ltm_pools_tool`` docstring for why they are never a tool argument.
    """
    s = get_settings()
    resp = readonly_get(
        f"https://{host}/mgmt/tm/ltm/virtual",
        auth=(s.f5_username, s.f5_password),
        verify=ssl_verify,
    )
    resp.raise_for_status()
    items = []
    for vs in resp.json().get("items", []):
        destination = vs.get("destination", "")  # "/Common/10.0.0.1:443"
        address, _, port = destination.rpartition(":")
        client_ssl = any(
            p.get("name") == "clientssl" for p in vs.get("profilesReference", {}).get("items", [])
        )
        items.append(
            {
                "name": vs.get("name", ""),
                "type": "lb_virtual_server",
                "data": {
                    "vendor": "f5",
                    "instance": host,
                    "pool": (vs.get("pool") or "").split("/")[-1],
                    "address": address.split("/")[-1],
                    "port": int(port) if port.isdigit() else None,
                    "protocol": vs.get("ipProtocol", ""),
                    "tls_enabled": client_ssl,
                },
            }
        )
    return items


# ---------------------------------------------------------------------------
# nginx Plus API
# ---------------------------------------------------------------------------


@tool
@tool_http_errors
def nginx_plus_upstreams_tool(api_url: str) -> list:
    """List nginx Plus upstreams (pools) and their servers (read-only).

    ``api_url`` is the full base, e.g. ``http://host/api/8``.
    """
    resp = readonly_get(f"{api_url}/http/upstreams")
    resp.raise_for_status()
    items = []
    for name, upstream in resp.json().items():
        members = [
            {
                "address": srv.get("server", "").rsplit(":", 1)[0],
                "port": int(srv.get("server", "0:0").rsplit(":", 1)[-1] or 0),
                "state": srv.get("state", ""),
                "weight": srv.get("weight"),
            }
            for srv in upstream.get("peers", [])
        ]
        items.append(
            {
                "name": name,
                "type": "lb_pool",
                "data": {
                    "vendor": "nginx",
                    "instance": api_url,
                    "monitor_type": "http" if upstream.get("healthchecks") else None,
                    "members": members,
                },
            }
        )
    return items


# ---------------------------------------------------------------------------
# HAProxy (stats page CSV export)
# ---------------------------------------------------------------------------


@tool
@tool_http_errors
def haproxy_stats_tool(stats_url: str) -> list:
    """Fetch and parse the HAProxy stats page CSV export (read-only).

    Returns one dict per proxy row: FRONTEND rows become ``lb_virtual_server``,
    backend member rows (svname not in {FRONTEND, BACKEND}) become
    ``lb_pool_member``-shaped entries nested under their pool (pxname).

    Credentials are read from settings inside this function — see
    ``f5_ltm_pools_tool`` docstring for why they are never a tool argument.
    """
    s = get_settings()
    auth = (s.haproxy_stats_username, s.haproxy_stats_password) if s.haproxy_stats_username else None
    resp = readonly_get(f"{stats_url.rstrip('?')}?;csv", auth=auth)
    resp.raise_for_status()
    text = resp.text.lstrip("# ")
    reader = csv.DictReader(io.StringIO(text))
    pools: dict[str, dict] = {}
    virtual_servers: list[dict] = []
    for row in reader:
        pxname = row.get("pxname", "")
        svname = row.get("svname", "")
        if not pxname or not svname:
            continue
        if svname == "FRONTEND":
            virtual_servers.append(
                {
                    "name": pxname,
                    "type": "lb_virtual_server",
                    "data": {
                        "vendor": "haproxy",
                        "instance": stats_url,
                        "pool": pxname,
                        "protocol": row.get("mode", ""),
                        "status": row.get("status", ""),
                    },
                }
            )
        elif svname != "BACKEND":
            pool = pools.setdefault(
                pxname,
                {
                    "name": pxname,
                    "type": "lb_pool",
                    "data": {
                        "vendor": "haproxy",
                        "instance": stats_url,
                        "monitor_type": "http",
                        "members": [],
                    },
                },
            )
            addr, _, port = (row.get("addr", "") or "").rpartition(":")
            pool["data"]["members"].append(
                {
                    "address": addr or svname,
                    "port": int(port) if port.isdigit() else 0,
                    "state": row.get("status", ""),
                    "weight": int(row["weight"]) if row.get("weight", "").isdigit() else None,
                }
            )
    return list(pools.values()) + virtual_servers


# ---------------------------------------------------------------------------
# Cloudflare API
# ---------------------------------------------------------------------------


@tool
@tool_http_errors
def cloudflare_zones_tool() -> list:
    """List Cloudflare zones this token can read (read-only).

    Credentials are read from settings inside this function — see
    ``f5_ltm_pools_tool`` docstring for why they are never a tool argument.
    """
    s = get_settings()
    resp = readonly_get(
        "https://api.cloudflare.com/client/v4/zones",
        headers={"Authorization": f"Bearer {s.cloudflare_api_token}"},
    )
    resp.raise_for_status()
    body = resp.json()
    return [
        {
            "name": zone.get("name", ""),
            "type": "lb_instance",
            "data": {"vendor": "cloudflare", "zone_id": zone.get("id", "")},
        }
        for zone in body.get("result", []) or []
    ]


@tool
@tool_http_errors
def cloudflare_load_balancers_tool(zone_id: str) -> list:
    """List Cloudflare Load Balancers (virtual servers) for a zone (read-only).

    Credentials are read from settings inside this function — see
    ``f5_ltm_pools_tool`` docstring for why they are never a tool argument.
    """
    s = get_settings()
    resp = readonly_get(
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/load_balancers",
        headers={"Authorization": f"Bearer {s.cloudflare_api_token}"},
    )
    resp.raise_for_status()
    body = resp.json()
    return [
        {
            "name": lb.get("name", ""),
            "type": "lb_virtual_server",
            "data": {
                "vendor": "cloudflare",
                "instance": zone_id,
                "pool": (lb.get("default_pools") or [None])[0],
                "protocol": "https" if lb.get("proxied", True) else "dns",
                "tls_enabled": bool(lb.get("proxied", True)),
            },
        }
        for lb in body.get("result", []) or []
    ]


@tool
@tool_http_errors
def cloudflare_pools_tool() -> list:
    """List Cloudflare Load Balancer pools (account-scoped, read-only).

    Credentials are read from settings inside this function — see
    ``f5_ltm_pools_tool`` docstring for why they are never a tool argument.
    """
    s = get_settings()
    resp = readonly_get(
        "https://api.cloudflare.com/client/v4/user/load_balancers/pools",
        headers={"Authorization": f"Bearer {s.cloudflare_api_token}"},
    )
    resp.raise_for_status()
    body = resp.json()
    items = []
    for pool in body.get("result", []) or []:
        members = [
            {
                "address": origin.get("address", ""),
                "port": 443,
                "state": "up" if origin.get("enabled", True) else "disabled",
                "weight": origin.get("weight"),
            }
            for origin in pool.get("origins", []) or []
        ]
        items.append(
            {
                "name": pool.get("name", ""),
                "type": "lb_pool",
                "data": {
                    "vendor": "cloudflare",
                    "instance": "cloudflare",
                    "monitor_type": pool.get("monitor", ""),
                    "members": members,
                },
            }
        )
    return items
