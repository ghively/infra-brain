"""Read-only authoritative DNS zone/record queries — GitLab issue #95.

Unlike ``tools/http_readonly.py`` (structurally GET-only httpx client), the
DNS protocol has no equivalent "safe verb" split at the transport layer — a
resolver query and a zone transfer (AXFR) both ride the same query/response
framing DDNS updates use. So this module is read-only **by convention**, the
same category CLAUDE.md documents for pyVmomi/k8s-client/nmap: every function
here only ever issues ``dns.resolver.resolve()`` (a lookup) or
``dns.query.xfr()`` (a zone transfer *read*) — never ``dns.update.Update``
(the dnspython DDNS write API), never touched anywhere in this module. The
agent audits every query through ``DnsAgent._audit_dns_query`` (an
AgentActionLog row per call, mirroring ``NetDiscoveryAgent._audit_subprocess``)
so the callback/DLP audit trail still records DNS activity even though it
doesn't ride LangChain's tool-call boundary the way an HTTP-based
``@tool`` does.

dnspython is an optional dependency (see the ``dns`` extra in pyproject.toml)
so importing this module never breaks environments that don't need DNS
collection — ``_DNSPYTHON_AVAILABLE`` mirrors ``tools/vsphere.py``'s
``_PYVMOMI_AVAILABLE`` pattern.
"""

import logging

logger = logging.getLogger(__name__)

try:
    import dns.query
    import dns.rdatatype
    import dns.resolver
    import dns.zone

    _DNSPYTHON_AVAILABLE = True
except ImportError:
    _DNSPYTHON_AVAILABLE = False

# Record types DnsAgent collects. NS is included for delegation records but
# SOA is handled separately (query_soa) since it is a single record with
# named fields, not a natural-key-per-value list like the others.
RECORD_TYPES: tuple[str, ...] = ("A", "AAAA", "CNAME", "MX", "TXT", "SRV", "NS")


def _make_resolver(resolver_host: str, timeout: float):
    resolver = dns.resolver.Resolver()
    if resolver_host:
        resolver.nameservers = [resolver_host]
    resolver.timeout = timeout
    resolver.lifetime = timeout
    return resolver


def query_soa(zone_name: str, resolver_host: str, timeout: float) -> dict | None:
    """Query the zone's SOA record. Returns None if the zone has none/unreachable.

    Raises whatever dnspython raises on a genuine failure (NXDOMAIN, timeout,
    etc.) — the caller decides whether that's a hard error or a soft skip.
    """
    if not _DNSPYTHON_AVAILABLE:
        raise RuntimeError("dnspython not installed")
    resolver = _make_resolver(resolver_host, timeout)
    answer = resolver.resolve(zone_name, "SOA")
    soa = answer[0]
    return {
        "serial": int(soa.serial),
        "primary_ns": str(soa.mname).rstrip("."),
        "admin_email": str(soa.rname).rstrip("."),
        "refresh_seconds": int(soa.refresh),
        "retry_seconds": int(soa.retry),
        "expire_seconds": int(soa.expire),
        "minimum_ttl_seconds": int(soa.minimum),
    }


def query_axfr(zone_name: str, resolver_host: str, timeout: float) -> list[dict]:
    """Attempt a full zone transfer (AXFR). Returns a flat list of record dicts.

    Most authoritative servers REFUSE AXFR from an arbitrary client — this is
    expected and not itself a bug; a REFUSED/timeout here should be treated
    as "deeper record collection unavailable this cycle," not a hard error
    (the caller degrades to SOA-only data, mirroring how EOLAgent/VulnAgent
    degrade to an empty/partial result rather than failing outright when an
    external dependency declines to cooperate).

    Raises on failure — the caller wraps this in its own try/except.
    """
    if not _DNSPYTHON_AVAILABLE:
        raise RuntimeError("dnspython not installed")
    if not resolver_host:
        raise RuntimeError("AXFR requires an explicit dns_resolver_host (authoritative server)")

    xfr = dns.query.xfr(resolver_host, zone_name, timeout=timeout, lifetime=timeout)
    zone = dns.zone.from_xfr(xfr)

    records: list[dict] = []
    origin = str(zone.origin) if zone.origin is not None else zone_name.rstrip(".") + "."
    for name, node in zone.nodes.items():
        rec_name = name.derelativize(zone.origin).to_text().rstrip(".") if zone.origin else str(name)
        for rdataset in node.rdatasets:
            rtype = dns.rdatatype.to_text(rdataset.rdtype)
            if rtype not in RECORD_TYPES:
                continue
            for rdata in rdataset:
                priority = None
                if rtype == "MX":
                    value = str(rdata.exchange).rstrip(".")
                    priority = int(rdata.preference)
                elif rtype == "SRV":
                    value = f"{rdata.target}".rstrip(".")
                    priority = int(rdata.priority)
                elif rtype == "TXT":
                    value = b"".join(rdata.strings).decode("utf-8", errors="replace")
                else:
                    value = str(rdata).rstrip(".")
                records.append(
                    {
                        "name": rec_name or origin.rstrip("."),
                        "type": rtype,
                        "value": value,
                        "ttl": int(rdataset.ttl),
                        "priority": priority,
                    }
                )
    return records


__all__ = ["RECORD_TYPES", "_DNSPYTHON_AVAILABLE", "query_axfr", "query_soa"]
