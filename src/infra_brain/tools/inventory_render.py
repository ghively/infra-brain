"""Render helper: add hosts to an Ansible YAML inventory (add-only).

Targets the canonical ``all.children.<group>.hosts`` layout. Prefers
``ruamel.yaml`` round-trip so existing comments/formatting survive the MR diff;
falls back to PyYAML (loses comments) if ruamel is unavailable. Never removes or
modifies existing hosts/groups — only inserts missing host keys.
"""

from __future__ import annotations

from typing import Iterable


def add_hosts_to_inventory(content: str, group: str, hosts: Iterable[str]) -> str:
    """Return inventory `content` with `hosts` added under `all.children.<group>.hosts`.

    Existing entries are untouched; hosts already present are skipped. New host
    keys are added with a null value (Ansible's "no host vars" form).
    """
    hosts = [h for h in hosts if h]
    if not hosts:
        return content
    try:
        return _add_with_ruamel(content, group, hosts)
    except ImportError:
        return _add_with_pyyaml(content, group, hosts)


def _ensure_group_hosts(data: dict, group: str) -> dict:
    """Navigate/create all.children.<group>.hosts on a plain-dict-like tree."""
    if data.get("all") is None:
        data["all"] = {}
    all_ = data["all"]
    if all_.get("children") is None:
        all_["children"] = {}
    children = all_["children"]
    if children.get(group) is None:
        children[group] = {}
    grp = children[group]
    if grp.get("hosts") is None:
        grp["hosts"] = {}
    return grp["hosts"]


def _add_with_ruamel(content: str, group: str, hosts: list[str]) -> str:
    from io import StringIO

    from ruamel.yaml import YAML  # noqa: PLC0415 — optional dep, import on use

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    data = yaml_rt.load(content) or {}
    host_map = _ensure_group_hosts(data, group)
    for host in hosts:
        if host not in host_map:
            host_map[host] = None
    buf = StringIO()
    yaml_rt.dump(data, buf)
    return buf.getvalue()


def _add_with_pyyaml(content: str, group: str, hosts: list[str]) -> str:
    import yaml  # noqa: PLC0415

    data = yaml.safe_load(content) if content.strip() else {}
    if not isinstance(data, dict):
        data = {}
    host_map = _ensure_group_hosts(data, group)
    for host in hosts:
        if host not in host_map:
            host_map[host] = None
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
