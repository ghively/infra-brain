"""CRL/OCSP responder reachability probe — read-only (GitLab issue #94).

A single GET-only health check against a tracked CertificateAuthority row's
``crl_url``/``ocsp_url``. Routed through ``@tool`` (like every other external
call in this codebase) rather than called as a bare function, so it flows
through the LLM/agent ``callbacks`` chain — ``AuditCallbackHandler`` records
the call, ``ReadOnlyToolValidator`` enforces GET-only, and
``DLPCallbackHandler`` sees the response — instead of being invisible to all
three, which a direct ``readonly_get()`` call from the agent would be.
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from langchain_core.tools import tool

from infra_brain.tools.http_readonly import ReadOnlyHTTPError, readonly_get

logger = logging.getLogger(__name__)


def _is_ssrf_safe_target(url: str) -> bool:
    """Reject targets that resolve to a private/loopback/link-local/reserved
    IP. ``crl_url``/``ocsp_url`` are populated via a manual-seed path (per the
    module docstring), so an operator typo or a malicious seed pointing this
    field at an internal address would otherwise turn a routine health probe
    into an SSRF primitive able to reach cloud metadata endpoints
    (169.254.169.254) or internal-only services from the server's own network
    position — mirrors ``tools/webhook_publish.py``'s ``_is_ssrf_safe_target``
    (same rationale, same accepted DNS-rebinding TOCTOU gap: this resolves the
    hostname once and checks that address, not the address httpx eventually
    connects to).
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


@tool
def crl_ocsp_probe_tool(url: str) -> str:
    """GET-only reachability probe against a CRL/OCSP responder URL.

    Returns ``"reachable"`` (2xx/3xx), ``"unreachable"`` (any other status,
    network error, or a target that fails the SSRF host check), never
    raises for those cases. A read-only-boundary denial
    (``ReadOnlyHTTPError``, a ``PermissionError`` subclass) is deliberately
    NOT caught here — the callback chain's own denial must propagate and
    fail the run rather than being silently recorded as "unreachable".
    """
    if not _is_ssrf_safe_target(url):
        logger.warning(
            "pki: refusing responder probe to %r — resolves to a private/"
            "loopback/reserved address",
            url,
        )
        return "unreachable"
    try:
        resp = readonly_get(url)
    except ReadOnlyHTTPError:
        # A read-only-boundary denial must fail the run, not be recorded as
        # ordinary "unreachable" row data.
        raise
    except Exception as exc:  # network/HTTP failure -> unreachable, not a crash
        logger.info("pki: responder probe failed for %s: %s", url, exc)
        return "unreachable"
    return "reachable" if resp.status_code < 400 else "unreachable"
