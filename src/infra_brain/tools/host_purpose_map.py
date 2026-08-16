"""Loader for the curated host-purpose/VLAN map (MR-J item 3).

The map is a hand-maintained record of ~55 hosts' purpose (and, where known,
VLAN/subnet) — "curated knowledge that exists nowhere else" in infra-brain's
data model (docs/audit/FLEET_INVENTORY_AND_VSPHERE_AUDIT_2026-07-06.md Part 1).
This module parses its YAML content once fetched (by
``agents/inventory_reconcile.py``'s ``_sync_host_purpose_map``, via the
existing ``gitlab_file_tool``) into ``HostPurposeMap`` rows.

Two document shapes are supported. We now CONTROL the file format — the seed
file ``host_purpose_map.yml`` at the root of the fleet-inventory repo
(a dedicated fleet-inventory GitLab project) uses the first, which is therefore the CANONICAL/primary
shape:

    1. CANONICAL — a plain top-level ``host_purpose_map:`` mapping document::

           host_purpose_map:
             web01: "Primary web frontend"        # flat: hostname -> purpose string
             db01:
               purpose: "Primary database"         # nested: hostname -> {purpose, vlan, subnet}
               vlan: "VLAN20-App"
               subnet: "10.90.12.0/24"

    2. LEGACY — the same ``host_purpose_map:`` mapping nested under an Ansible
       play's ``vars:`` block (the form it originally lived in inline in the
       fleet-ansible playbook)::

           - hosts: all
             vars:
               host_purpose_map:
                 web01: "Primary web frontend"

``_find_host_purpose_map`` accepts both: it returns the first play-level
``vars.host_purpose_map`` mapping OR a bare top-level ``host_purpose_map``
key it finds, so a file in either shape parses identically.

Per-host value shapes (both document shapes):
    * flat string  -> ``purpose`` is that string; ``vlan``/``subnet`` are None.
    * nested dict   -> ``purpose``/``vlan``/``subnet`` come from the dict's
      matching keys; any missing key yields None. An empty-string value
      (``""``) is PRESERVED as-is (not converted to None) for every field —
      the graph emitter already skips a blank ``vlan``, so keeping "" is
      harmless and lets the source distinguish "explicitly blank" from
      "absent". A flat-string ``""`` is likewise preserved as ``purpose == ""``.

VLAN membership is described elsewhere in the original play as separately
derived from an IP -> subnet map; this parser does NOT attempt that
cross-reference (it needs a second data source — the host's resolved IP —
this loader has no visibility into). A flat-string entry therefore carries no
VLAN/subnet; only the nested-dict shape populates those fields directly.

Degrades to an empty list (not an error) in two cases: (1) no
``host_purpose_map`` mapping is found in either shape (pointing this at the
wrong file/path is a no-op, not a crash), and (2) the content is not valid
YAML — a ``yaml.YAMLError`` is caught and logged at WARNING, then ``[]`` is
returned. The return dict keys are always exactly
``{hostname, purpose, vlan, subnet, source}``.
"""

import logging

import yaml

logger = logging.getLogger(__name__)

# Fallback path used only when ``settings.host_purpose_map_file_path`` is empty.
# Matches the canonical seed file at the root of the fleet-inventory repo
# (a dedicated fleet-inventory GitLab project); config.py's default is the real source of truth.
DEFAULT_FILE_PATH = "host_purpose_map.yml"


def parse_host_purpose_map(content: str, source: str) -> list[dict]:
    """Parse *content* (the host-purpose-map YAML) into HostPurposeMap rows.

    Accepts either the canonical top-level ``host_purpose_map:`` mapping
    document or the legacy play-level ``vars.host_purpose_map`` form (see the
    module docstring). Returns a list of
    ``{hostname, purpose, vlan, subnet, source}`` dicts, or ``[]`` when no
    ``host_purpose_map`` mapping is found in either shape or the content is
    not valid YAML.
    """
    try:
        docs = list(yaml.safe_load_all(content))
    except yaml.YAMLError as exc:
        logger.warning("host_purpose_map: failed to parse YAML from %s: %s", source, exc)
        return []

    host_map = _find_host_purpose_map(docs)
    if not host_map:
        return []

    rows: list[dict] = []
    for hostname, value in host_map.items():
        if not hostname:
            continue
        if isinstance(value, dict):
            rows.append(
                {
                    "hostname": str(hostname),
                    "purpose": value.get("purpose"),
                    "vlan": value.get("vlan"),
                    "subnet": value.get("subnet"),
                    "source": source,
                }
            )
        else:
            rows.append(
                {
                    "hostname": str(hostname),
                    "purpose": str(value) if value is not None else None,
                    "vlan": None,
                    "subnet": None,
                    "source": source,
                }
            )
    return rows


def _find_host_purpose_map(docs: list) -> dict | None:
    """Return the first ``host_purpose_map`` mapping across parsed YAML docs.

    Accepts BOTH supported shapes: a play-level ``vars.host_purpose_map``
    mapping (legacy) and a bare top-level ``host_purpose_map:`` key
    (canonical — the seed file's shape). Returns ``None`` when neither is
    present.
    """
    for doc in docs:
        candidates = doc if isinstance(doc, list) else [doc]
        for play in candidates:
            if not isinstance(play, dict):
                continue
            play_vars = play.get("vars")
            if isinstance(play_vars, dict):
                host_map = play_vars.get("host_purpose_map")
                if isinstance(host_map, dict) and host_map:
                    return host_map
            # Canonical shape: a bare top-level `host_purpose_map:` key (the
            # seed file `host_purpose_map.yml` uses this, no enclosing play).
            host_map = play.get("host_purpose_map")
            if isinstance(host_map, dict) and host_map:
                return host_map
    return None
