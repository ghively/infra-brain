"""Tests for the host purpose/VLAN map parser (MR-J item 3).

The parser accepts two document shapes (see tools/host_purpose_map.py's
module docstring): the CANONICAL top-level ``host_purpose_map:`` mapping used
by the seed file ``host_purpose_map.yml``, and the LEGACY play-level
``vars.host_purpose_map`` form. Per host, the value is a flat purpose string
or a nested ``{purpose, vlan, subnet}`` dict. These tests pin both shapes,
the empty/missing-key and invalid-YAML graceful-degrade contracts, and the
empty-string-preservation choice for nested-dict fields.
"""

from infra_brain.tools.host_purpose_map import parse_host_purpose_map


def test_parse_flat_string_shape():
    content = """
- hosts: all
  vars:
    host_purpose_map:
      web01: "Primary web frontend"
      db01: "Primary database"
"""
    rows = parse_host_purpose_map(content, source="5:playbooks/system_inventory.yml")
    by_host = {r["hostname"]: r for r in rows}
    assert by_host["web01"]["purpose"] == "Primary web frontend"
    assert by_host["web01"]["vlan"] is None
    assert by_host["web01"]["subnet"] is None
    assert by_host["db01"]["source"] == "5:playbooks/system_inventory.yml"


def test_parse_nested_dict_shape():
    content = """
- hosts: all
  vars:
    host_purpose_map:
      db01:
        purpose: "Primary database"
        vlan: "VLAN20-App"
        subnet: "10.90.12.0/24"
"""
    rows = parse_host_purpose_map(content, source="5:playbooks/system_inventory.yml")
    assert len(rows) == 1
    row = rows[0]
    assert row["hostname"] == "db01"
    assert row["purpose"] == "Primary database"
    assert row["vlan"] == "VLAN20-App"
    assert row["subnet"] == "10.90.12.0/24"


def test_parse_mixed_flat_and_nested():
    content = """
- hosts: all
  vars:
    host_purpose_map:
      web01: "Primary web frontend"
      db01:
        purpose: "Primary database"
        vlan: "VLAN20-App"
"""
    rows = parse_host_purpose_map(content, source="x")
    by_host = {r["hostname"]: r for r in rows}
    assert by_host["web01"]["vlan"] is None
    assert by_host["db01"]["vlan"] == "VLAN20-App"


def test_parse_bare_top_level_key():
    content = """
host_purpose_map:
  web01: "Primary web frontend"
"""
    rows = parse_host_purpose_map(content, source="x")
    assert rows == [
        {
            "hostname": "web01",
            "purpose": "Primary web frontend",
            "vlan": None,
            "subnet": None,
            "source": "x",
        }
    ]


def test_parse_missing_key_returns_empty_list():
    content = """
- hosts: all
  vars:
    some_other_var: 1
"""
    assert parse_host_purpose_map(content, source="x") == []


def test_parse_invalid_yaml_returns_empty_list_not_raise():
    assert parse_host_purpose_map("::: not valid yaml :::\n  - [", source="x") == []


def test_parse_empty_content_returns_empty_list():
    assert parse_host_purpose_map("", source="x") == []


# --- Canonical top-level mapping document (the seed file's real shape) ---


def test_parse_top_level_nested_dict_shape():
    """CANONICAL shape, nested-dict per-host value: purpose/vlan/subnet all
    come straight from the dict, and ``source`` is passed through."""
    content = """
host_purpose_map:
  h1:
    purpose: "P"
    vlan: "11"
    subnet: "198.51.100.16/24"
"""
    rows = parse_host_purpose_map(content, source="6:host_purpose_map.yml")
    assert rows == [
        {
            "hostname": "h1",
            "purpose": "P",
            "vlan": "11",
            "subnet": "198.51.100.16/24",
            "source": "6:host_purpose_map.yml",
        }
    ]


def test_parse_top_level_flat_string_shape():
    """CANONICAL shape, flat-string per-host value: purpose is the string,
    vlan/subnet default to None."""
    content = """
host_purpose_map:
  h1: "Primary web"
"""
    rows = parse_host_purpose_map(content, source="6:host_purpose_map.yml")
    assert len(rows) == 1
    row = rows[0]
    assert row["hostname"] == "h1"
    assert row["purpose"] == "Primary web"
    assert row["vlan"] is None
    assert row["subnet"] is None


def test_parse_legacy_play_vars_shape_still_supported():
    """LEGACY play-vars shape must keep parsing after the canonical shape
    became primary."""
    content = """
- hosts: all
  vars:
    host_purpose_map:
      h1: "x"
"""
    rows = parse_host_purpose_map(content, source="s")
    assert rows == [
        {
            "hostname": "h1",
            "purpose": "x",
            "vlan": None,
            "subnet": None,
            "source": "s",
        }
    ]


def test_parse_document_without_map_key_returns_empty_list():
    """A well-formed doc that simply has no ``host_purpose_map`` key ->
    ``[]`` (no crash)."""
    assert parse_host_purpose_map("some_other: {}", source="x") == []


def test_parse_blank_nested_values_preserved_as_empty_string():
    """Empty-string nested values are PRESERVED as "" (Task 1 choice), never
    coerced to None — the graph emitter already skips a blank vlan."""
    content = """
host_purpose_map:
  h1:
    purpose: ""
    vlan: ""
    subnet: ""
"""
    rows = parse_host_purpose_map(content, source="x")
    assert len(rows) == 1
    row = rows[0]
    assert row["purpose"] == ""
    assert row["vlan"] == ""
    assert row["subnet"] == ""
