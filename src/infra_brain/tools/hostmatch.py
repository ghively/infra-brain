"""Host-name normalization & matching for inventory reconciliation.

Discovered hostnames (from Ansible facts, vSphere, cloud) and inventory entries
rarely match byte-for-byte (FQDN vs short name, trailing dots, case). These
helpers normalize both sides so a host is only flagged "missing" when it is
genuinely absent — deterministic, no fuzzy guessing.
"""

from __future__ import annotations

import re
from typing import Iterable

# KG-6: generic / placeholder names that are NOT stable identities. Merging
# two distinct machines just because both call themselves "localhost" (or were
# seeded with a placeholder) is a false-merge. These normalize to "" so they
# never become a merge key.
_GENERIC_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "unknown",
        "none",
        "null",
        "n/a",
        "na",
        "host",
        "hostname",
        "default",
        "server",
        "vm",
        "template",
        "new virtual machine",
    }
)

# IPv4 dotted-quad (0-255 per octet).
_IPV4_RE = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$")


def normalize_host(name: str) -> str:
    """Return the canonical short, lowercased form of a hostname.

    "Web-01.corp.example.com." → "web-01". Empty/None → "".

    KG-6 hardening (stricter matching to prevent false merges):

    * **Case-insensitive** — always lowercased (unchanged).
    * **IP addresses are NOT truncated.** A dotted-quad like ``10.0.0.42`` used
      to be split on ``.`` and collapsed to ``"10"`` — merging *every* host in
      the 10.x range into one bogus identity. IPv4/IPv6-shaped inputs are now
      returned whole (lowercased), so they only ever match the exact same
      address, never a short-name prefix of an unrelated host.
    * **Generic placeholder names normalize to ""** (``localhost``, ``unknown``,
      ``vm``, ``template`` …) so they are never used as a cross-source merge key.

    TRK-087: the short-name return contract is intentionally preserved (it is a
    dict merge key at 8 host_reconcile call sites + an inventory dedup key, and
    the legitimate short<->FQDN convergence depends on both spellings reducing
    to the same key). Domain-awareness needed to block cross-domain false merges
    is a *pairwise* property and therefore lives in the companion
    :func:`host_domain` / :func:`hosts_domain_conflict` checks, applied at
    edge-emit time — not baked into this single key.
    """
    if not name:
        return ""
    low = name.strip().lower().rstrip(".")
    if not low:
        return ""
    # An IP address is a whole identity — never truncate it on '.'/':'.
    if _IPV4_RE.match(low) or ":" in low:
        return low
    short = low.split(".")[0]
    if short in _GENERIC_HOSTNAMES:
        return ""
    return short


def graph_match_key(name: str) -> str:
    """:func:`normalize_host` plus separator folding — for GRAPH joins only.

    Returns ``normalize_host(name)`` with ``_`` folded to ``-``, so the two
    spellings this homelab genuinely uses for one machine reduce to one key::

        graph_match_key("node-a") == graph_match_key("node_a") == "node-a"

    The services manifest (ground truth from homelab-ansible's
    ``host_vars/*.yml`` ``docker_stacks`` entries) spells hosts with HYPHENS;
    the Ansible inventory that produces ``linux_host`` resources spells the
    same machines with UNDERSCORES. Nothing joined those two spellings, so
    ``Service ─RUNS_ON→ Host`` was inexpressible even once the manifest's
    ``host`` field was carried through (commit 8289227).

    The fold is safe as a *matching* rule: RFC 1123 hostnames cannot contain
    an underscore, so an underscore spelling is always the inventory-sanitised
    form of a hyphen name, never a genuinely different machine. IP-shaped
    inputs are returned whole by ``normalize_host`` and contain no separator
    to fold, so they are unaffected.

    WHY THIS IS NOT FOLDED INTO ``normalize_host`` ITSELF
    -----------------------------------------------------
    ``normalize_host``'s output is PERSISTED as
    ``host_identities.short_hostname`` and is the merge key at 8
    ``host_reconcile`` call sites plus the inventory dedup index. Widening it
    would silently re-key every existing identity row: the next reconcile pass
    would compute ``node-a`` where the stored row says ``node_a``, fail to
    find it, and mint a duplicate identity. Doing that correctly needs a
    backfill migration, which is out of scope for the graph contract work —
    so the fold is confined to the graph engine's declared key matching,
    where it has no persisted-key blast radius. Widening ``normalize_host``
    (with the backfill) remains the right eventual move; it is a separate,
    schema-touching change.

    There is deliberately no second host normaliser here — this composes
    ``normalize_host`` rather than reimplementing any of its rules
    (case-folding, FQDN shortening, IP preservation, placeholder rejection).
    """
    return normalize_host(name).replace("_", "-")


def host_domain(name: str) -> str:
    """Return the lowercased domain suffix of a domain-qualified hostname.

    TRK-087 pairwise domain-compatibility helper. Everything AFTER the first
    DNS label is the domain suffix: ``web01.corp.example.com`` →
    ``corp.example.com``. Returns ``""`` when the name is unqualified (a single
    label), empty/None, or IP-shaped — i.e. there is no domain to compare.

    Uses the same ``strip().lower().rstrip(".")`` normalization and the same
    IP detection (``_IPV4_RE`` / ``":"``) as :func:`normalize_host`, so the two
    stay in lockstep.
    """
    if not name:
        return ""
    low = name.strip().lower().rstrip(".")
    if not low:
        return ""
    # IP addresses have no host/domain split — no domain to compare.
    if _IPV4_RE.match(low) or ":" in low:
        return ""
    first_dot = low.find(".")
    if first_dot == -1:
        return ""  # unqualified single-label name
    return low[first_dot + 1 :]


def hosts_domain_conflict(a: str, b: str) -> bool:
    """True IFF ``a`` and ``b`` are BOTH domain-qualified with DIFFERENT domains.

    TRK-087 pairwise domain-compatibility rule, used at IS_SAME_AS edge-emit
    time to suppress cross-domain false merges. Two hosts that share a first
    DNS label but sit in different domains (``web01.corp.example.com`` vs
    ``web01.dmz.example.org``) are genuinely different machines and must NOT be
    merged.

    Returns False when at least one side is unqualified — that is the
    legitimate short<->FQDN convergence case (one collector reports the bare
    short name, another the FQDN of the same host) — or when both sides share
    the same domain. The rule is inherently non-transitive, which is why it is a
    pairwise check rather than part of the single :func:`normalize_host` key.
    """
    da = host_domain(a)
    db = host_domain(b)
    return bool(da) and bool(db) and da != db


def inventory_host_index(inventory_hosts: Iterable[str]) -> set[str]:
    """Build a lookup set from inventory host entries containing BOTH the full
    lowercased FQDN and its short form, so either spelling matches."""
    index: set[str] = set()
    for host in inventory_hosts or []:
        if not host:
            continue
        low = host.strip().lower().rstrip(".")
        if not low:
            continue
        index.add(low)
        index.add(low.split(".")[0])
    return index


def is_host_in_inventory(host: str, index: set[str]) -> bool:
    """True if `host` (any spelling) is represented in the inventory index."""
    if not host:
        return False
    low = host.strip().lower().rstrip(".")
    return bool(low) and (low in index or low.split(".")[0] in index)
