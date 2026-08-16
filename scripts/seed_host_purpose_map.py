#!/usr/bin/env python
r"""One-time seed parser: Ansible inventory -> curated host_purpose_map.yml.

Reads one or more Ansible inventory YAML files (the fleet-ansible facility
splits, e.g. ``01-tn.yml`` and ``02-mnf.yml``) and emits a curated
``host_purpose_map.yml`` in the top-level-mapping shape that infra-brain's
``infra_brain.tools.host_purpose_map.parse_host_purpose_map`` consumes.

The per-host purpose / OS / VLAN metadata lives in the inline ``# comment``
on each host line, which YAML parsers discard. Extraction is therefore
line/regex based. Each host becomes a nested dict::

    host_purpose_map:
      prod-lnx-ftb-01:
        purpose: "Fleet Transmission File Backup"
        vlan: "11"
        subnet: "198.51.100.0/24"

Parsing rules (see the seed task spec):

1. A host line is a key under a ``hosts:`` block. We match indented lines of
   the form ``HOSTNAME:`` optionally followed by ``{ansible_host: <ip|fqdn>}``
   and/or a trailing ``# comment``. A line is only treated as a host when it
   has an ``ansible_host`` OR its comment contains ``VLAN_`` (this captures the
   NO-IP hosts and excludes group headers, which have neither). Structural keys
   ``hosts``/``children`` and any ``*_facility`` name are excluded.
2. purpose = text before the FIRST ``|`` in the comment, stripped; else "".
3. vlan = digits from ``VLAN_(\d+)`` in the comment; else "".
4. subnet = first three octets + ``.0/24`` when ansible_host is a dotted IPv4;
   else "" (FQDN or missing).
5. Every host is emitted, including explicit-blank ones.
"""

from __future__ import annotations

import argparse
import re
import sys

# Indented ``HOSTNAME:`` optionally followed by ``{... ansible_host: X ...}``
# and/or a trailing ``# comment``. Require >=6 leading spaces (host lines sit
# under ``<group>: -> hosts:``), which structurally excludes top-level and
# group keys at shallower indentation.
_HOST_LINE = re.compile(
    r"^\s{6,}(?P<name>[A-Za-z0-9][\w.-]*):\s*"
    r"(?:\{[^}]*ansible_host:\s*(?P<ah>[^,}\s]+)[^}]*\})?\s*"
    r"(?:#\s*(?P<comment>.*))?$"
)
_VLAN = re.compile(r"VLAN_(\d+)")
_IPV4 = re.compile(r"^\d+\.\d+\.\d+\.\d+$")

# Structural / non-host keys that the host-line regex could otherwise match.
_STRUCTURAL = {"hosts", "children"}


def _parse_line(line: str) -> dict | None:
    """Return a host row dict for *line*, or None if it is not a host line."""
    match = _HOST_LINE.match(line)
    if not match:
        return None

    name = match.group("name")
    ansible_host = match.group("ah")
    comment = (match.group("comment") or "").strip()

    # Exclude structural keys and facility group headers.
    if name in _STRUCTURAL or name.endswith("_facility"):
        return None

    # A host only qualifies with an ansible_host OR a VLAN_ marker in its
    # comment. Group headers have neither.
    has_vlan_marker = "VLAN_" in comment
    if not ansible_host and not has_vlan_marker:
        return None

    # purpose: text before the first pipe, else empty.
    if "|" in comment:
        purpose = comment.split("|", 1)[0].strip()
    else:
        purpose = ""

    # vlan: digits from VLAN_<n>, else empty.
    vlan_match = _VLAN.search(comment)
    vlan = vlan_match.group(1) if vlan_match else ""

    # subnet: /24 derived from a dotted IPv4 ansible_host, else empty.
    if ansible_host and _IPV4.match(ansible_host):
        octets = ansible_host.split(".")
        subnet = f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"
    else:
        subnet = ""

    return {"hostname": name, "purpose": purpose, "vlan": vlan, "subnet": subnet}


def parse_inventory_text(text: str) -> list[dict]:
    """Parse inventory *text* into ordered host rows (file order preserved)."""
    rows: list[dict] = []
    for line in text.splitlines():
        row = _parse_line(line)
        if row is not None:
            rows.append(row)
    return rows


def parse_inventory_files(paths: list[str]) -> list[dict]:
    """Parse each inventory file in *paths* in order, concatenating rows.

    Source host ordering is preserved: files are read in the given order and
    hosts within each file in file order.
    """
    rows: list[dict] = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            rows.extend(parse_inventory_text(handle.read()))
    return rows


_HEADER = """\
# host_purpose_map.yml
#
# Curated host -> purpose / VLAN / subnet map for infra-brain.
#
# infra-brain's inventory_reconcile agent reads this file (located via
# HOST_PURPOSE_MAP_PROJECT_ID + host_purpose_map_file_path) through
# infra_brain.tools.host_purpose_map.parse_host_purpose_map, which consumes
# the top-level `host_purpose_map:` mapping below.
#
# Seeded ONCE from the playbooks/fleet-ansible inventory host comments
# (purpose | OS | VLAN). Going forward it is maintained by humans (UI edits)
# and by agent-proposed GitLab MRs -- NOT regenerated from inventory.
#
# Blank purpose/vlan/subnet are intentional placeholders for humans to fill
# in (hosts whose inventory comment had no pipe-purpose, no VLAN_ marker, or
# no dotted-IPv4 ansible_host).
"""


def _quote(value: str) -> str:
    r"""Double-quote a scalar, escaping backslashes and double quotes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_yaml(rows: list[dict]) -> str:
    """Render *rows* as the curated host_purpose_map.yml text (block style)."""
    lines = [_HEADER, "host_purpose_map:"]
    for row in rows:
        lines.append(f"  {row['hostname']}:")
        lines.append(f"    purpose: {_quote(row['purpose'])}")
        lines.append(f"    vlan: {_quote(row['vlan'])}")
        lines.append(f"    subnet: {_quote(row['subnet'])}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed a curated host_purpose_map.yml from Ansible inventory files.",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="One or more Ansible inventory YAML files (order preserved).",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to write the generated host_purpose_map.yml.",
    )
    args = parser.parse_args(argv)

    rows = parse_inventory_files(args.inputs)
    output = render_yaml(rows)
    with open(args.output, "w", encoding="utf-8") as handle:
        handle.write(output)

    with_purpose = sum(1 for r in rows if r["purpose"])
    blank = len(rows) - with_purpose
    print(
        f"Wrote {len(rows)} hosts to {args.output} ({with_purpose} with purpose, {blank} blank).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
