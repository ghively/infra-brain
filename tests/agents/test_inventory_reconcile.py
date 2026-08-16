"""Tests for InventoryReconcileAgent + host-match / YAML-render helpers."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import InventoryReconcileEvent, Resource
from infra_brain.tools.hostmatch import (
    inventory_host_index,
    is_host_in_inventory,
    normalize_host,
)
from infra_brain.tools.inventory_render import add_hosts_to_inventory

from tests.support.pg import make_engine

SAMPLE_INV = """\
all:
  children:
    web:
      hosts:
        web-01:
        web-02:
"""


# ── host matching ────────────────────────────────────────────────────────────


def test_normalize_host():
    assert normalize_host("Web-01.corp.example.com.") == "web-01"
    assert normalize_host("DB02") == "db02"
    assert normalize_host("") == ""
    assert normalize_host(None) == ""


def test_inventory_index_and_membership():
    idx = inventory_host_index(["web-01.corp.com", "db-02"])
    # FQDN, its short form, and the plain short entry all match
    assert is_host_in_inventory("web-01", idx)
    assert is_host_in_inventory("web-01.corp.com", idx)
    assert is_host_in_inventory("DB-02.anything.net", idx)
    assert not is_host_in_inventory("web-99", idx)
    assert not is_host_in_inventory("", idx)


# ── YAML render (add-only) ───────────────────────────────────────────────────


def test_render_adds_to_existing_group_preserving_others():
    out = add_hosts_to_inventory(SAMPLE_INV, "web", ["web-03"])
    assert "web-03" in out
    assert "web-01" in out and "web-02" in out  # add-only: existing untouched


def test_render_creates_missing_group():
    out = add_hosts_to_inventory(SAMPLE_INV, "discovered", ["new-01"])
    assert "discovered" in out and "new-01" in out
    assert "web-01" in out  # still present


def test_render_idempotent_existing_host():
    out = add_hosts_to_inventory(SAMPLE_INV, "web", ["web-01"])
    assert out.count("web-01") == 1  # not duplicated


def test_render_empty_hosts_noop():
    assert add_hosts_to_inventory(SAMPLE_INV, "web", []) == SAMPLE_INV


# ── agent ────────────────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_session(engine):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.agents.inventory_reconcile.get_session", _get_session):
        yield engine


def _seed_resource(engine, name, domain="linux"):
    with Session(engine) as s:
        s.add(Resource(domain=domain, type=f"{domain}_host", name=name, source="test"))
        s.commit()


def _make_agent(**overrides):
    from infra_brain.agents.inventory_reconcile import InventoryReconcileAgent

    defaults = {
        "inventory_source_project_id": 123,
        "inventory_branch": "main",
        "inventory_file_path": "inventories/hosts.yml",
        "inventory_default_group": "discovered",
        "inventory_group_map": "",
        "inventory_match_domains": "linux,windows",
        "inventory_host_types": (
            "octopus:octopus_machine,vsphere:vsphere_vm|vsphere_host,vuln:r7_asset,"
            "cloud:ec2_instance"
        ),
        # MR-J item 3: default off, matching every other optional
        # GitLab-sourced collector's "unset id = idle" convention.
        "host_purpose_map_project_id": 0,
        "host_purpose_map_branch": "main",
        "host_purpose_map_file_path": "playbooks/system_inventory.yml",
    }
    defaults.update(overrides)
    settings = SimpleNamespace(**defaults)
    agent = InventoryReconcileAgent.__new__(InventoryReconcileAgent)
    agent.settings = settings
    # __new__ bypasses BaseAgent.__init__, so self.callbacks is never set; set it here.
    agent.callbacks = []
    return agent


def _patch_gitlab(content=SAMPLE_INV):
    gl_file = MagicMock()
    gl_file.invoke.return_value = content
    return patch("infra_brain.agents.inventory_reconcile.gitlab_file_tool", gl_file)


def test_inventory_reconcile_domain():
    from infra_brain.agents.inventory_reconcile import InventoryReconcileAgent

    assert InventoryReconcileAgent.domain == "inventory_reconcile"


def test_proposes_missing_host_via_mr(patched_session, monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_MR_ENABLED", "true")  # MR creation is gated off by default
    engine = patched_session
    _seed_resource(engine, "web-03", "linux")  # absent from SAMPLE_INV

    mr_cls = MagicMock()
    mr_cls.return_value.propose_inventory_fix.return_value = "http://gitlab/mr/1"

    with _patch_gitlab(), patch("infra_brain.agents.inventory_reconcile.InventoryMRAgent", mr_cls):
        _make_agent().collect()

    pf = mr_cls.return_value.propose_inventory_fix
    assert pf.called, "MR should be proposed for a missing host"
    content = pf.call_args.kwargs["corrected_content"]
    assert "web-03" in content
    assert "web-01" in content and "web-02" in content  # add-only

    with Session(engine) as s:
        events = s.query(InventoryReconcileEvent).all()
    assert len(events) == 1
    assert events[0].host == "web-03"
    assert events[0].target_group == "discovered"
    assert events[0].status == "proposed"
    assert events[0].mr_url == "http://gitlab/mr/1"


def test_no_mr_when_all_present(patched_session):
    engine = patched_session
    _seed_resource(engine, "web-01", "linux")  # already in inventory

    mr_cls = MagicMock()
    with _patch_gitlab(), patch("infra_brain.agents.inventory_reconcile.InventoryMRAgent", mr_cls):
        _make_agent().collect()

    assert not mr_cls.return_value.propose_inventory_fix.called


def test_idempotent_skips_already_proposed(patched_session):
    engine = patched_session
    _seed_resource(engine, "web-03", "linux")
    with Session(engine) as s:
        s.add(InventoryReconcileEvent(host="web-03", domain="linux", target_group="discovered"))
        s.commit()

    mr_cls = MagicMock()
    with _patch_gitlab(), patch("infra_brain.agents.inventory_reconcile.InventoryMRAgent", mr_cls):
        _make_agent().collect()

    assert not mr_cls.return_value.propose_inventory_fix.called


def test_idle_when_project_unset(patched_session):
    mr_cls = MagicMock()
    with _patch_gitlab(), patch("infra_brain.agents.inventory_reconcile.InventoryMRAgent", mr_cls):
        result = _make_agent(inventory_source_project_id=0).collect()
    assert result == []
    assert not mr_cls.return_value.propose_inventory_fix.called


# ---------------------------------------------------------------------------
# H-6: an MR-creation failure (or MR disabled entirely) must NEVER write a
# status="proposed" event with mr_url=None — that poisons
# _filter_already_proposed() into excluding the host forever, even though no
# proposal was ever actually made.
# ---------------------------------------------------------------------------


def test_mr_creation_failure_raises_and_writes_no_event(patched_session, monkeypatch):
    """H-6: MR creation raising must surface as a real failure (like every
    other GitLab call in this agent), and must NOT leave behind a
    status="proposed"/mr_url=None row — that row would poison
    _filter_already_proposed() into never proposing the host again."""
    monkeypatch.setenv("INFRA_BRAIN_MR_ENABLED", "true")
    engine = patched_session
    _seed_resource(engine, "web-03", "linux")

    mr_cls = MagicMock()
    mr_cls.return_value.propose_inventory_fix.side_effect = RuntimeError("gitlab MR API 500")

    with (
        _patch_gitlab(),
        patch("infra_brain.agents.inventory_reconcile.InventoryMRAgent", mr_cls),
        pytest.raises(RuntimeError, match="gitlab MR API 500"),
    ):
        _make_agent().collect()

    with Session(engine) as s:
        events = s.query(InventoryReconcileEvent).all()
    assert events == [], (
        "an MR-creation failure must not write any InventoryReconcileEvent row — "
        f"got {[(e.host, e.status, e.mr_url) for e in events]}"
    )


def test_mr_disabled_writes_no_proposed_event(patched_session):
    """H-6: with MR creation disabled entirely (INFRA_BRAIN_MR_ENABLED unset,
    the default), a missing host must not be recorded as status="proposed"
    with mr_url=None — nothing was actually proposed, so nothing should claim
    to have been."""
    engine = patched_session
    _seed_resource(engine, "web-03", "linux")

    mr_cls = MagicMock()
    with _patch_gitlab(), patch("infra_brain.agents.inventory_reconcile.InventoryMRAgent", mr_cls):
        result = _make_agent().collect()

    assert result == []
    assert not mr_cls.return_value.propose_inventory_fix.called
    with Session(engine) as s:
        events = s.query(InventoryReconcileEvent).all()
    assert events == [], (
        "MR-disabled must not write a status=\"proposed\" event with mr_url=None — "
        f"got {[(e.host, e.status, e.mr_url) for e in events]}"
    )


# ---------------------------------------------------------------------------
# AA-R-13 (F-007): a real GitLab failure must propagate (status="failed"),
# not be swallowed into a bare [] return indistinguishable from "nothing to
# reconcile" (status="completed", 0 resources).
# ---------------------------------------------------------------------------


def test_gitlab_file_read_failure_propagates(patched_session):
    gl_file = MagicMock()
    gl_file.invoke.side_effect = RuntimeError("gitlab 500")
    with (
        patch("infra_brain.agents.inventory_reconcile.gitlab_file_tool", gl_file),
        pytest.raises(RuntimeError, match="gitlab 500"),
    ):
        _make_agent().collect()


def test_tree_walk_failure_propagates(patched_session):
    tree_tool = MagicMock()
    tree_tool.invoke.side_effect = RuntimeError("gitlab tree 500")
    with (
        patch("infra_brain.agents.inventory_reconcile.gitlab_repository_tree_tool", tree_tool),
        pytest.raises(RuntimeError, match="gitlab tree 500"),
    ):
        _make_agent(inventory_file_path="").collect()


def test_group_map_routes_host(patched_session, monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_MR_ENABLED", "true")  # MR creation is gated off by default
    engine = patched_session
    _seed_resource(engine, "win-09", "windows")

    mr_cls = MagicMock()
    mr_cls.return_value.propose_inventory_fix.return_value = "http://mr"
    with _patch_gitlab(), patch("infra_brain.agents.inventory_reconcile.InventoryMRAgent", mr_cls):
        _make_agent(inventory_group_map="windows:winhosts").collect()

    content = mr_cls.return_value.propose_inventory_fix.call_args.kwargs["corrected_content"]
    assert "winhosts" in content and "win-09" in content


def test_all_three_tool_invokes_fire_on_tool_start(monkeypatch, patched_session):
    """F-004.2 acceptance: all 3 tool invokes carry config parameter.

    Fails today: the 3 invokes pass no config, so no callback handler
    ever sees them. With config passed, the callbacks can observe.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import infra_brain.agents.inventory_reconcile as mod

    # Mock tools that track invocation
    gitlab_file_tool = MagicMock()
    gitlab_file_tool.invoke = MagicMock(return_value="all:\n  hosts: {}\n")
    gitlab_file_tool.name = "gitlab_file_tool"

    parse_ansible_inventory_content_tool = MagicMock()
    parse_ansible_inventory_content_tool.invoke = MagicMock(return_value={"hosts": {}})
    parse_ansible_inventory_content_tool.name = "parse_ansible_inventory_content_tool"

    gitlab_repository_tree_tool = MagicMock()
    gitlab_repository_tree_tool.invoke = MagicMock(
        return_value=[{"type": "blob", "path": "inventories/hosts.yml", "name": "hosts.yml"}]
    )
    gitlab_repository_tree_tool.name = "gitlab_repository_tree_tool"

    monkeypatch.setattr(mod, "gitlab_file_tool", gitlab_file_tool)
    monkeypatch.setattr(
        mod, "parse_ansible_inventory_content_tool", parse_ansible_inventory_content_tool
    )
    monkeypatch.setattr(mod, "gitlab_repository_tree_tool", gitlab_repository_tree_tool)

    agent = mod.InventoryReconcileAgent()
    agent.settings = SimpleNamespace(
        inventory_source_project_id=1,
        inventory_file_path="",  # empty -> forces _discover_inventory_path (tree tool)
        inventory_branch="main",
        inventory_match_domains="",  # empty -> no DB query, no MR attempt
        inventory_group_map="",
        inventory_default_group="all",
        # MR-J item 3: default off (separate GitLab source from the above).
        host_purpose_map_project_id=0,
        host_purpose_map_branch="main",
        host_purpose_map_file_path="playbooks/system_inventory.yml",
    )

    agent.collect()

    # Verify all three tools were invoked WITH config parameter
    assert gitlab_repository_tree_tool.invoke.called, "tree tool was not invoked"
    assert gitlab_file_tool.invoke.called, "file tool was not invoked"
    assert parse_ansible_inventory_content_tool.invoke.called, "parse tool was not invoked"

    # Verify each call included config parameter
    for mock, name in [
        (gitlab_repository_tree_tool, "tree"),
        (gitlab_file_tool, "file"),
        (parse_ansible_inventory_content_tool, "parse"),
    ]:
        call_args = mock.invoke.call_args
        assert call_args is not None, f"{name} tool invoke was not called"
        # Check that 'config' was passed as a keyword argument
        assert "config" in call_args.kwargs, (
            f"{name} tool invoke did not receive config parameter; "
            f"this means callbacks are not being passed to the tool"
        )


# ── host-shaped Resource.type filtering (GitLab issue #120) ────────────────
#
# _discovered_hosts() must not treat every Resource row in a matched domain as
# a host: Octopus/vSphere multiplex several types under one domain, and
# Rapid7's real domain string is "vuln" (not "rapid7").


def _add_resource(session, domain, type_, name):
    session.add(Resource(domain=domain, type=type_, name=name, source="test"))


def test_discovered_hosts_filters_octopus_to_machines_only(patched_session):
    engine = patched_session
    with Session(engine) as s:
        _add_resource(s, "octopus", "octopus_environment", "Production")
        _add_resource(s, "octopus", "octopus_server", "octopus-server")
        _add_resource(s, "octopus", "octopus_project", "my-project")
        _add_resource(s, "octopus", "octopus_machine", "octo-host-01")
        s.commit()

    agent = _make_agent()
    result = agent._discovered_hosts(["octopus"])
    assert result == [("octo-host-01", "octopus")]


def test_discovered_hosts_filters_vsphere_to_vm_and_host_only(patched_session):
    engine = patched_session
    with Session(engine) as s:
        _add_resource(s, "vsphere", "vsphere_vcenter", "vcenter-01")
        _add_resource(s, "vsphere", "vsphere_permission", "perm-01")
        _add_resource(s, "vsphere", "vsphere_license", "lic-01")
        _add_resource(s, "vsphere", "vsphere_alarm", "alarm-01")
        _add_resource(s, "vsphere", "vsphere_vm", "vm-01")
        _add_resource(s, "vsphere", "vsphere_host", "esxi-01")
        s.commit()

    agent = _make_agent()
    result = agent._discovered_hosts(["vsphere"])
    assert sorted(result) == sorted([("vm-01", "vsphere"), ("esxi-01", "vsphere")])


def test_discovered_hosts_vuln_domain_returns_r7_assets(patched_session):
    engine = patched_session
    with Session(engine) as s:
        _add_resource(s, "vuln", "r7_asset", "scanned-host-01")
        s.commit()

    agent = _make_agent()
    result = agent._discovered_hosts(["vuln"])
    assert result == [("scanned-host-01", "vuln")]


def test_discovered_hosts_unfiltered_domain_keeps_old_behavior(patched_session):
    """linux/windows aren't in inventory_host_types -> no type filter applied."""
    engine = patched_session
    _seed_resource(engine, "web-03", "linux")

    agent = _make_agent()
    result = agent._discovered_hosts(["linux"])
    assert result == [("web-03", "linux")]


# ── GitLab #141: Octopus variable/credential/team names must never become
# host gap-candidates, even under a misconfigured inventory_host_types ─────
#
# 100% of a 200-row sample of pending gap proposals were Octopus variable
# keys (e.g. "redisconnectionstring", "privatepgpkey" — one PAN/DUKPT-related
# name among them), team names ("EMV Team"), and permission-scope strings
# ("Everyone") rather than real hosts. octopus.py's collector never writes a
# generic Resource row for these entity types (they live in detail-only
# tables with no Resource FK — see db/models/octopus.py), but the type
# filter is the boundary that must hold even if that ever changes, or if the
# deployed inventory_host_types setting is stale/misconfigured (the
# production incident: an env value pinned before GitLab #120 added the
# "octopus:octopus_machine" entry).


def test_discovered_hosts_octopus_credential_and_team_names_excluded(patched_session):
    """A defense-in-depth regression guard: even non-machine Octopus
    Resource rows carrying credential/team-shaped names (which the current
    collector never actually creates) are excluded by the type filter —
    only octopus_machine rows become gap candidates."""
    engine = patched_session
    with Session(engine) as s:
        # Simulated variable-key / credential-adjacent / team / permission-scope
        # rows, as if a future or historical bug wrote them with domain=octopus.
        _add_resource(s, "octopus", "octopus_variable", "redisconnectionstring")
        _add_resource(s, "octopus", "octopus_variable", "privatepgpkey")
        _add_resource(s, "octopus", "octopus_team", "EMV Team")
        _add_resource(s, "octopus", "octopus_permission", "Everyone")
        _add_resource(s, "octopus", "octopus_account", "Octopus Administrators")
        # Genuine machine/target entity — must still surface as a gap candidate.
        _add_resource(s, "octopus", "octopus_machine", "octo-host-02")
        s.commit()

    agent = _make_agent()
    result = agent._discovered_hosts(["octopus"])
    assert result == [("octo-host-02", "octopus")]


def test_discovered_hosts_octopus_filtered_even_with_stale_host_types_config(
    patched_session,
):
    """Mandatory floor: if inventory_host_types is misconfigured/stale and
    omits the octopus entry entirely, octopus must still be restricted to
    octopus_machine rather than silently falling back to "no filter"."""
    engine = patched_session
    with Session(engine) as s:
        _add_resource(s, "octopus", "octopus_variable", "tokensigningkey")
        _add_resource(s, "octopus", "octopus_team", "Octopus Administrators")
        _add_resource(s, "octopus", "octopus_machine", "octo-host-03")
        s.commit()

    # Stale config: no "octopus:" entry at all (as if pinned before #120).
    agent = _make_agent(
        inventory_host_types="vsphere:vsphere_vm|vsphere_host,vuln:r7_asset,cloud:ec2_instance"
    )
    result = agent._discovered_hosts(["octopus"])
    assert result == [("octo-host-03", "octopus")]


def test_discovered_hosts_octopus_filtered_with_empty_host_types_config(patched_session):
    """Same mandatory-floor guarantee when inventory_host_types is blank."""
    engine = patched_session
    with Session(engine) as s:
        _add_resource(s, "octopus", "octopus_variable", "privatepgpkey")
        _add_resource(s, "octopus", "octopus_machine", "octo-host-04")
        s.commit()

    agent = _make_agent(inventory_host_types="")
    result = agent._discovered_hosts(["octopus"])
    assert result == [("octo-host-04", "octopus")]


# ── host purpose/VLAN map sync (MR-J item 3) ────────────────────────────────


_PURPOSE_MAP_YAML = """\
- hosts: all
  vars:
    host_purpose_map:
      web-01: "Primary web frontend"
      db-01:
        purpose: "Primary database"
        vlan: "VLAN20-App"
        subnet: "10.90.12.0/24"
"""


def test_sync_host_purpose_map_idle_when_unset(patched_session):
    """host_purpose_map_project_id=0 (default) -> no GitLab call, no rows."""
    with _patch_gitlab() as gl_file:
        agent = _make_agent(inventory_source_project_id=0)
        agent.collect()
    assert not gl_file.invoke.called


def test_sync_host_purpose_map_upserts_rows(patched_session):
    from infra_brain.db.models import HostPurposeMap

    engine = patched_session
    gl_file = MagicMock()
    gl_file.invoke.return_value = _PURPOSE_MAP_YAML

    with patch("infra_brain.agents.inventory_reconcile.gitlab_file_tool", gl_file):
        agent = _make_agent(inventory_source_project_id=0, host_purpose_map_project_id=5)
        agent.collect()

    with Session(engine) as s:
        rows = {r.hostname: r for r in s.query(HostPurposeMap).all()}
    assert rows["web-01"].purpose == "Primary web frontend"
    assert rows["web-01"].vlan is None
    assert rows["db-01"].vlan == "VLAN20-App"
    assert rows["db-01"].subnet == "10.90.12.0/24"
    assert rows["db-01"].source == "5:playbooks/system_inventory.yml"


def test_sync_host_purpose_map_is_idempotent_upsert(patched_session):
    from infra_brain.db.models import HostPurposeMap

    engine = patched_session
    gl_file = MagicMock()
    gl_file.invoke.return_value = _PURPOSE_MAP_YAML

    with patch("infra_brain.agents.inventory_reconcile.gitlab_file_tool", gl_file):
        agent = _make_agent(inventory_source_project_id=0, host_purpose_map_project_id=5)
        agent.collect()
        agent.collect()

    with Session(engine) as s:
        assert s.query(HostPurposeMap).count() == 2


def test_sync_host_purpose_map_read_failure_raises(patched_session):
    """A configured-but-unreachable source is a real failure (AA-R-13), not a
    silently-swallowed no-op."""
    gl_file = MagicMock()
    gl_file.invoke.side_effect = RuntimeError("gitlab unreachable")

    with patch("infra_brain.agents.inventory_reconcile.gitlab_file_tool", gl_file):
        agent = _make_agent(inventory_source_project_id=0, host_purpose_map_project_id=5)
        with pytest.raises(RuntimeError, match="gitlab unreachable"):
            agent.collect()


def test_host_purpose_map_failure_does_not_block_main_inventory_reconcile(
    patched_session, monkeypatch
):
    """A host_purpose_map sync failure must not abort the independently-gated
    main inventory reconcile below it in collect() — regression guard: the
    module's own docstring claims the two are 'independently gated', but
    collect() used to run _sync_host_purpose_map() unguarded before the main
    reconcile, so a purpose-map-source outage (e.g. a 404) aborted the whole
    run before the main inventory reconcile ever executed. The failure must
    still surface (BaseAgent.run() records status='failed' per AA-R-13), but
    only after the main path has had its chance to run.
    """
    monkeypatch.setenv("INFRA_BRAIN_MR_ENABLED", "true")
    engine = patched_session
    _seed_resource(engine, "web-03", "linux")  # absent from SAMPLE_INV

    def gl_invoke(args, config=None):
        if args["project_id"] == 5:  # host_purpose_map source (see _make_agent default)
            raise RuntimeError("404 Not Found")
        return SAMPLE_INV  # main inventory source (project_id=123)

    gl_file = MagicMock()
    gl_file.invoke.side_effect = gl_invoke

    mr_cls = MagicMock()
    mr_cls.return_value.propose_inventory_fix.return_value = "http://gitlab/mr/1"

    with (
        patch("infra_brain.agents.inventory_reconcile.gitlab_file_tool", gl_file),
        patch("infra_brain.agents.inventory_reconcile.InventoryMRAgent", mr_cls),
    ):
        agent = _make_agent(host_purpose_map_project_id=5)
        with pytest.raises(RuntimeError, match="404 Not Found"):
            agent.collect()

    # The main reconcile still ran and proposed the missing host despite the
    # purpose-map failure — this is the part that used to never happen.
    pf = mr_cls.return_value.propose_inventory_fix
    assert pf.called, "main inventory reconcile must still run when only host_purpose_map fails"
    with Session(engine) as s:
        events = s.query(InventoryReconcileEvent).all()
    assert len(events) == 1
    assert events[0].host == "web-03"


def test_sync_host_purpose_map_uses_configured_file_path_and_branch(patched_session):
    gl_file = MagicMock()
    gl_file.invoke.return_value = ""  # no host_purpose_map key -> no-op, not an error

    with patch("infra_brain.agents.inventory_reconcile.gitlab_file_tool", gl_file):
        agent = _make_agent(
            inventory_source_project_id=0,
            host_purpose_map_project_id=5,
            host_purpose_map_file_path="custom/path.yml",
            host_purpose_map_branch="develop",
        )
        agent.collect()

    call_kwargs = gl_file.invoke.call_args.args[0]
    assert call_kwargs["project_id"] == 5
    assert call_kwargs["file_path"] == "custom/path.yml"
    assert call_kwargs["ref"] == "develop"


# ── host-purpose-map deterministic human-vs-repo precedence ─────────────────
#
# Repo source constant reused verbatim from _sync_host_purpose_map:
#   source = f"{pid}:{file_path}"  ->  "5:playbooks/system_inventory.yml" here.
# ui:* rows are human-authored/authoritative; a repo sync must not clobber a
# newer human row unless the file has caught up (then ownership flips to repo).

_REPO_SRC = "5:playbooks/system_inventory.yml"


def _purpose_yaml(host, purpose=None, vlan=None, subnet=None):
    """Build a canonical top-level host_purpose_map YAML for a single host."""
    lines = ["host_purpose_map:", f"  {host}:"]
    if purpose is not None:
        lines.append(f'    purpose: "{purpose}"')
    if vlan is not None:
        lines.append(f'    vlan: "{vlan}"')
    if subnet is not None:
        lines.append(f'    subnet: "{subnet}"')
    return "\n".join(lines) + "\n"


def _seed_purpose_row(engine, **kwargs):
    from infra_brain.db.models import HostPurposeMap

    with Session(engine) as s:
        s.add(HostPurposeMap(**kwargs))
        s.commit()


def _run_purpose_sync(content):
    """Run collect() with only the purpose-map sync active, feeding *content*."""
    gl_file = MagicMock()
    gl_file.invoke.return_value = content
    with patch("infra_brain.agents.inventory_reconcile.gitlab_file_tool", gl_file):
        _make_agent(inventory_source_project_id=0, host_purpose_map_project_id=5).collect()


def test_purpose_sync_repo_newer_updates_repo_row(patched_session):
    """Existing repo-sourced row, file has a newer value -> repo wins (updated)."""
    from infra_brain.db.models import HostPurposeMap

    engine = patched_session
    _seed_purpose_row(engine, hostname="web-01", purpose="X", source=_REPO_SRC)

    _run_purpose_sync(_purpose_yaml("web-01", purpose="Y"))

    with Session(engine) as s:
        row = s.query(HostPurposeMap).filter_by(hostname="web-01").one()
    assert row.purpose == "Y"
    assert row.source == _REPO_SRC


def test_purpose_sync_new_host_inserted_with_repo_source(patched_session):
    """No existing row -> file host inserted with the repo source constant."""
    from infra_brain.db.models import HostPurposeMap

    engine = patched_session
    _run_purpose_sync(_purpose_yaml("new-01", purpose="Fresh"))

    with Session(engine) as s:
        row = s.query(HostPurposeMap).filter_by(hostname="new-01").one()
    assert row.purpose == "Fresh"
    assert row.source == _REPO_SRC


def test_purpose_sync_human_newer_not_clobbered(patched_session):
    """ui:* row differs from file -> human is authoritative; row left untouched."""
    from infra_brain.db.models import HostPurposeMap

    engine = patched_session
    _seed_purpose_row(engine, hostname="web-01", purpose="X", source="ui:alice")

    _run_purpose_sync(_purpose_yaml("web-01", purpose="Y"))  # Y != X

    with Session(engine) as s:
        row = s.query(HostPurposeMap).filter_by(hostname="web-01").one()
    assert row.purpose == "X"  # not clobbered
    assert row.source == "ui:alice"  # ownership retained


def test_purpose_sync_merged_match_flips_back_to_repo(patched_session):
    """ui:* row equals file (human's persistence MR merged) -> flip to repo."""
    from infra_brain.db.models import HostPurposeMap

    engine = patched_session
    _seed_purpose_row(engine, hostname="web-01", purpose="X", source="ui:alice")

    _run_purpose_sync(_purpose_yaml("web-01", purpose="X"))  # file caught up

    with Session(engine) as s:
        row = s.query(HostPurposeMap).filter_by(hostname="web-01").one()
    assert row.purpose == "X"
    assert row.source == _REPO_SRC  # ownership flipped back to repo


def test_purpose_sync_blank_normalization_counts_as_match(patched_session):
    """_norm(None) == _norm("") -> a blank ui:* row vs a None-purpose file entry
    is a match, so ownership flips back to repo (proves blank normalization)."""
    from infra_brain.db.models import HostPurposeMap

    engine = patched_session
    _seed_purpose_row(engine, hostname="web-01", purpose="", vlan="", subnet="", source="ui:bob")

    # Nested dict with vlan/subnet blank and NO purpose key -> purpose parses None.
    _run_purpose_sync(_purpose_yaml("web-01", vlan="", subnet=""))

    with Session(engine) as s:
        row = s.query(HostPurposeMap).filter_by(hostname="web-01").one()
    assert row.source == _REPO_SRC  # treated as match -> flipped back to repo


def test_sync_host_purpose_map_absent_file_is_not_a_failure(patched_session):
    """A 404 on the map file means "nothing to sync", not a broken run.

    Same situation as the empty-map case (`if not rows: return 0`) — there is
    no purpose map to apply. It used to propagate: collect() computes the main
    inventory reconciliation FIRST and re-raises afterwards, so the whole
    result was computed and then thrown away, escalating 159x in a row.
    Verified against the live repo: host_purpose_map.yml is absent from all 327
    paths in project 113, so the 404 is accurate and permanent until the file
    is created.
    """
    from langchain_core.tools import ToolException

    not_found = ToolException("Client error '404 Not Found' for url '.../host_purpose_map.yml'")
    not_found.status_code = 404

    gl_file = MagicMock()
    gl_file.invoke.side_effect = not_found

    with patch("infra_brain.agents.inventory_reconcile.gitlab_file_tool", gl_file):
        agent = _make_agent(inventory_source_project_id=0, host_purpose_map_project_id=5)
        # No raise — and the main reconciliation's result survives.
        assert agent.collect() == []
        assert agent._sync_host_purpose_map() == 0


def test_sync_host_purpose_map_non_404_http_error_still_raises(patched_session):
    """Only 404 is benign. A 500 (or auth failure) is still a real failure."""
    from langchain_core.tools import ToolException

    server_error = ToolException("Server error '500 Internal Server Error' for url '...'")
    server_error.status_code = 500

    gl_file = MagicMock()
    gl_file.invoke.side_effect = server_error

    with patch("infra_brain.agents.inventory_reconcile.gitlab_file_tool", gl_file):
        agent = _make_agent(inventory_source_project_id=0, host_purpose_map_project_id=5)
        with pytest.raises(RuntimeError, match="failed to read"):
            agent.collect()
