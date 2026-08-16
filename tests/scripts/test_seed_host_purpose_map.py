"""Hermetic tests for scripts/seed_host_purpose_map.py.

Uses an inline inventory fixture (no dependency on /tmp) covering the six
representative host-line shapes called out in the seed spec:

    (a) IP + full pipe comment          -> purpose/vlan/subnet all populated
    (b) FQDN + no comment               -> purpose/vlan/subnet all blank
    (c) NO-IP host, pipe comment "NO IP"-> purpose+vlan populated, subnet blank
    (d) FQDN + freeform (non-pipe)      -> purpose/vlan blank, subnet blank
    (e) 10.x IP                         -> subnet /24 derived from 10.x
    (f) VLAN with 4 digits (VLAN_2035)  -> vlan normalized to "2035"
"""

from scripts.seed_host_purpose_map import parse_inventory_text, render_yaml

# Six host lines wrapped in a realistic all -> children -> group -> hosts tree
# so structural keys (children/hosts) and a *_facility group header are present
# and must be excluded from the parsed rows.
FIXTURE = """\
all:
  children:

    sitea_ubuntu:
      hosts:
        HOSTA01: {ansible_host: 198.51.100.21}  # Fleet Transmission File Backup | Ubuntu 22.04 | VLAN_11
        HOSTB02: {ansible_host: hostb02.example.internal}
        HOSTC03:  # Fusion RabbitMQ - Fusion       | Ubuntu 22.04 | VLAN_115 | NO IP
        HOSTD04: {ansible_host: hostd04.example.internal}  # NOT UPDATED 4/19 - verify
        HOSTE05: {ansible_host: 10.90.81.224}  # Camelot HAProxy Server         | CentOS 7     | VLAN_101

    siteb_facility:
      hosts:
        HOSTF06: {ansible_host: 10.121.33.30}  # Squid Proxy / Mail Relay       | CentOS 7     | VLAN_2035
"""

EXPECTED = [
    {
        "hostname": "HOSTA01",
        "purpose": "Fleet Transmission File Backup",
        "vlan": "11",
        "subnet": "198.51.100.0/24",
    },
    {
        "hostname": "HOSTB02",
        "purpose": "",
        "vlan": "",
        "subnet": "",
    },
    {
        "hostname": "HOSTC03",
        "purpose": "Fusion RabbitMQ - Fusion",
        "vlan": "115",
        "subnet": "",
    },
    {
        "hostname": "HOSTD04",
        "purpose": "",
        "vlan": "",
        "subnet": "",
    },
    {
        "hostname": "HOSTE05",
        "purpose": "Camelot HAProxy Server",
        "vlan": "101",
        "subnet": "10.90.81.0/24",
    },
    {
        "hostname": "HOSTF06",
        "purpose": "Squid Proxy / Mail Relay",
        "vlan": "2035",
        "subnet": "10.121.33.0/24",
    },
]


def test_parses_exact_rows_in_order():
    rows = parse_inventory_text(FIXTURE)
    assert rows == EXPECTED


def test_structural_and_group_keys_excluded():
    rows = parse_inventory_text(FIXTURE)
    names = {r["hostname"] for r in rows}
    # Group headers, hosts:/children: keys, and *_facility never appear.
    assert names == {"HOSTA01", "HOSTB02", "HOSTC03", "HOSTD04", "HOSTE05", "HOSTF06"}
    assert "hosts" not in names
    assert "children" not in names
    assert "sitea_ubuntu" not in names
    assert "siteb_facility" not in names


def test_purpose_is_text_before_first_pipe():
    rows = {r["hostname"]: r for r in parse_inventory_text(FIXTURE)}
    # Only the segment before the FIRST pipe, stripped of surrounding space.
    assert rows["HOSTA01"]["purpose"] == "Fleet Transmission File Backup"
    assert rows["HOSTF06"]["purpose"] == "Squid Proxy / Mail Relay"


def test_vlan_normalization():
    rows = {r["hostname"]: r for r in parse_inventory_text(FIXTURE)}
    assert rows["HOSTA01"]["vlan"] == "11"
    assert rows["HOSTF06"]["vlan"] == "2035"  # 4-digit VLAN normalized to digits


def test_subnet_slash24_for_ipv4():
    rows = {r["hostname"]: r for r in parse_inventory_text(FIXTURE)}
    assert rows["HOSTA01"]["subnet"] == "198.51.100.0/24"
    assert rows["HOSTE05"]["subnet"] == "10.90.81.0/24"  # 10.x derivation


def test_subnet_blank_for_fqdn_and_no_ip():
    rows = {r["hostname"]: r for r in parse_inventory_text(FIXTURE)}
    assert rows["HOSTB02"]["subnet"] == ""  # FQDN ansible_host
    assert rows["HOSTC03"]["subnet"] == ""  # NO IP host


def test_blank_purpose_and_vlan_for_no_comment_and_freeform():
    rows = {r["hostname"]: r for r in parse_inventory_text(FIXTURE)}
    # (b) no comment
    assert rows["HOSTB02"]["purpose"] == ""
    assert rows["HOSTB02"]["vlan"] == ""
    # (d) freeform non-pipe comment
    assert rows["HOSTD04"]["purpose"] == ""
    assert rows["HOSTD04"]["vlan"] == ""


def test_no_ip_host_is_emitted():
    # Explicit-blank / NO-IP hosts must not be dropped.
    rows = parse_inventory_text(FIXTURE)
    assert any(r["hostname"] == "HOSTC03" for r in rows)


def test_render_yaml_shape_is_nested_dict_block_style():
    rows = parse_inventory_text(FIXTURE)
    out = render_yaml(rows)
    assert out.startswith("# host_purpose_map.yml")
    assert "\nhost_purpose_map:\n" in out
    assert "  HOSTA01:\n" in out
    assert '    purpose: "Fleet Transmission File Backup"\n' in out
    assert '    vlan: "11"\n' in out
    assert '    subnet: "198.51.100.0/24"\n' in out
    # Blank scalars are emitted as explicit empty double-quoted strings.
    assert '    purpose: ""\n' in out


def test_render_yaml_roundtrips_through_infra_brain_parser():
    # The rendered file must be consumable by the real parser as a top-level
    # host_purpose_map mapping of nested dicts.
    from infra_brain.tools.host_purpose_map import parse_host_purpose_map

    out = render_yaml(parse_inventory_text(FIXTURE))
    parsed = parse_host_purpose_map(out, source="test")
    by_host = {r["hostname"]: r for r in parsed}
    assert by_host["HOSTA01"]["purpose"] == "Fleet Transmission File Backup"
    assert by_host["HOSTA01"]["vlan"] == "11"
    assert by_host["HOSTA01"]["subnet"] == "198.51.100.0/24"
    assert set(by_host) == {
        "HOSTA01",
        "HOSTB02",
        "HOSTC03",
        "HOSTD04",
        "HOSTE05",
        "HOSTF06",
    }
