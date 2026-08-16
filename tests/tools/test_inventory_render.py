"""Module tests for infra_brain.tools.inventory_render (MR-E TEST-4).

``ruamel.yaml`` is an optional extra (pyproject.toml's "inventory" group) not
installed in the default dev venv, so ``add_hosts_to_inventory`` falls
through to the PyYAML path in this environment — the same gap the 4 existing
smoke assertions in tests/agents/test_inventory_reconcile.py exercise
implicitly. This file adds: (1) direct, thorough coverage of the PyYAML
fallback (``_add_with_pyyaml``) including edge cases the smoke tests don't
reach, and (2) coverage of the ruamel path (``_add_with_ruamel``) via a
lightweight fake module injected into ``sys.modules`` — so both branches of
``add_hosts_to_inventory``'s try/except are actually exercised without
requiring the optional dependency to be installed.
"""

import sys
import types

import pytest
import yaml

from infra_brain.tools.inventory_render import (
    _add_with_pyyaml,
    add_hosts_to_inventory,
)

SAMPLE_INV = """
all:
  children:
    web:
      hosts:
        web-01:
        web-02:
"""


# ---------------------------------------------------------------------------
# add_hosts_to_inventory — dispatch + PyYAML fallback (ruamel not installed
# in this environment, so this is the path actually exercised end-to-end)
# ---------------------------------------------------------------------------


def test_add_hosts_to_inventory_adds_new_host_to_existing_group():
    out = add_hosts_to_inventory(SAMPLE_INV, "web", ["web-03"])
    data = yaml.safe_load(out)
    assert "web-03" in data["all"]["children"]["web"]["hosts"]


def test_add_hosts_to_inventory_never_removes_existing_hosts():
    out = add_hosts_to_inventory(SAMPLE_INV, "web", ["web-03"])
    data = yaml.safe_load(out)
    hosts = data["all"]["children"]["web"]["hosts"]
    assert "web-01" in hosts
    assert "web-02" in hosts


def test_add_hosts_to_inventory_creates_new_group_when_absent():
    out = add_hosts_to_inventory(SAMPLE_INV, "discovered", ["new-01"])
    data = yaml.safe_load(out)
    assert "new-01" in data["all"]["children"]["discovered"]["hosts"]
    # Existing group untouched.
    assert "web-01" in data["all"]["children"]["web"]["hosts"]


def test_add_hosts_to_inventory_skips_already_present_host():
    out = add_hosts_to_inventory(SAMPLE_INV, "web", ["web-01"])
    data = yaml.safe_load(out)
    # Still exactly one web-01 entry (dict key — duplication would just be
    # idempotent anyway, but this proves no error/mutation of the value).
    assert data["all"]["children"]["web"]["hosts"]["web-01"] is None


def test_add_hosts_to_inventory_empty_host_list_is_a_noop():
    assert add_hosts_to_inventory(SAMPLE_INV, "web", []) == SAMPLE_INV


def test_add_hosts_to_inventory_filters_falsy_host_entries():
    out = add_hosts_to_inventory(SAMPLE_INV, "web", ["", None, "web-04"])
    data = yaml.safe_load(out)
    assert "web-04" in data["all"]["children"]["web"]["hosts"]
    assert None not in data["all"]["children"]["web"]["hosts"]


def test_add_hosts_to_inventory_all_falsy_hosts_is_a_noop():
    assert add_hosts_to_inventory(SAMPLE_INV, "web", ["", None]) == SAMPLE_INV


def test_add_hosts_to_inventory_adds_multiple_hosts_at_once():
    out = add_hosts_to_inventory(SAMPLE_INV, "web", ["web-03", "web-04"])
    data = yaml.safe_load(out)
    hosts = data["all"]["children"]["web"]["hosts"]
    assert {"web-01", "web-02", "web-03", "web-04"} <= set(hosts)


# ---------------------------------------------------------------------------
# _add_with_pyyaml — direct unit coverage of edge cases
# ---------------------------------------------------------------------------


def test_add_with_pyyaml_empty_content_builds_fresh_structure():
    out = _add_with_pyyaml("", "web", ["web-01"])
    data = yaml.safe_load(out)
    assert data["all"]["children"]["web"]["hosts"] == {"web-01": None}


def test_add_with_pyyaml_non_dict_content_is_replaced_with_fresh_structure():
    """A YAML doc that parses to a non-dict (e.g. a bare scalar/list) must not
    crash — treated the same as empty content."""
    out = _add_with_pyyaml("- just\n- a\n- list\n", "web", ["web-01"])
    data = yaml.safe_load(out)
    assert data["all"]["children"]["web"]["hosts"] == {"web-01": None}


def test_add_with_pyyaml_preserves_unrelated_top_level_keys():
    content = "vars:\n  some_setting: true\n" + SAMPLE_INV
    out = _add_with_pyyaml(content, "web", ["web-03"])
    data = yaml.safe_load(out)
    assert data["vars"]["some_setting"] is True
    assert "web-03" in data["all"]["children"]["web"]["hosts"]


def test_add_with_pyyaml_preserves_sibling_groups():
    content = SAMPLE_INV.replace(
        "web:\n      hosts:\n        web-01:\n        web-02:\n",
        "web:\n      hosts:\n        web-01:\n        web-02:\n    db:\n      hosts:\n        db-01:\n",
    )
    out = _add_with_pyyaml(content, "web", ["web-03"])
    data = yaml.safe_load(out)
    assert "db-01" in data["all"]["children"]["db"]["hosts"]
    assert "web-03" in data["all"]["children"]["web"]["hosts"]


# ---------------------------------------------------------------------------
# add_hosts_to_inventory — the ruamel path, exercised via a fake module so
# this doesn't require the optional "inventory" extra to be installed.
# ---------------------------------------------------------------------------


class _FakeCommentedMap(dict):
    """Stand-in for ruamel's CommentedMap: a dict is a sufficient fake for
    this module's usage (it only calls .get()/__setitem__/__getitem__)."""


class _FakeYAML:
    """Minimal fake of ruamel.yaml.YAML: round-trips through plain dicts via
    PyYAML underneath, which is enough to prove add_hosts_to_inventory's
    ruamel branch drives _ensure_group_hosts and dump/load correctly."""

    def __init__(self):
        self.preserve_quotes = None

    def load(self, content):
        return yaml.safe_load(content) or {}

    def dump(self, data, stream):
        stream.write(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))


@pytest.fixture
def fake_ruamel(monkeypatch):
    """Inject a fake ruamel.yaml module into sys.modules for the duration of
    a test, then clean it up — lets _add_with_ruamel's `from ruamel.yaml
    import YAML` succeed without the real optional dependency installed."""
    fake_pkg = types.ModuleType("ruamel")
    fake_yaml_mod = types.ModuleType("ruamel.yaml")
    fake_yaml_mod.YAML = _FakeYAML
    fake_pkg.yaml = fake_yaml_mod
    monkeypatch.setitem(sys.modules, "ruamel", fake_pkg)
    monkeypatch.setitem(sys.modules, "ruamel.yaml", fake_yaml_mod)
    yield
    # monkeypatch.setitem handles teardown/restoration automatically.


def test_add_hosts_to_inventory_uses_ruamel_when_available(fake_ruamel):
    out = add_hosts_to_inventory(SAMPLE_INV, "web", ["web-03"])
    data = yaml.safe_load(out)
    assert "web-03" in data["all"]["children"]["web"]["hosts"]
    assert "web-01" in data["all"]["children"]["web"]["hosts"]


def test_add_hosts_to_inventory_ruamel_path_sets_preserve_quotes(fake_ruamel, monkeypatch):
    captured = {}
    real_init = _FakeYAML.__init__

    def _tracking_init(self):
        real_init(self)
        captured["instance"] = self

    monkeypatch.setattr(_FakeYAML, "__init__", _tracking_init)
    add_hosts_to_inventory(SAMPLE_INV, "web", ["web-03"])
    assert captured["instance"].preserve_quotes is True
