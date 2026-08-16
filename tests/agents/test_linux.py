import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.tools import ToolException
from sqlalchemy.orm import Session, sessionmaker

from infra_brain.agents.base import CollectionResult
from infra_brain.agents.linux import LinuxAgent, _iter_mounts, _iter_nics
from infra_brain.db.models import (
    CollectionRun,
    HostCertificate,
    HostFirewallRule,
    HostShare,
    LinuxCron,
    LinuxHost,
    LinuxMount,
    LinuxNic,
    LinuxPendingUpdate,
    LinuxPort,
    LinuxUser,
    Resource,
)
from infra_brain.etl.base import CollectorSkipped
from infra_brain.tools.ansible import (
    ALL_HOSTS_UNREACHABLE_MARKER,
    BECOME_DENIED_MARKER,
    INVENTORY_UNAVAILABLE_MARKER,
    ZERO_HOST_INVENTORY_MARKER,
)


def _session_cm(engine):
    @contextmanager
    def _cm():
        with Session(engine) as s:
            yield s

    return _cm


MOCK_FACTS = {
    "web01": {
        "ansible_facts": {
            "ansible_distribution": "Ubuntu",
            "ansible_distribution_version": "22.04",
            "ansible_kernel": "5.15.0",
            "ansible_architecture": "x86_64",
            "ansible_packages": {"nginx": [{"version": "1.18.0", "arch": "amd64"}]},
            "ansible_services": {"nginx": {"state": "running", "status": "enabled"}},
        }
    }
}


def test_linux_agent_collect_parses_facts():
    agent = LinuxAgent.__new__(LinuxAgent)
    agent.settings = MagicMock()
    agent.settings.ansible_inventory_path = "/etc/ansible/hosts"
    agent.callbacks = []

    with patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool:
        mock_tool.invoke.return_value = MOCK_FACTS
        # H-5: collect() now returns a CollectOutcome so swallowed enrichment-probe
        # failures surface as run errors; the parsed resources are .items.
        items = agent.collect(scope="all").items

    assert len(items) == 1
    assert items[0]["name"] == "web01"
    assert items[0]["type"] == "linux_host"
    assert items[0]["data"]["distro"] == "Ubuntu"
    assert items[0]["data"]["kernel"] == "5.15.0"
    assert "nginx" in [p["name"] for p in items[0]["data"]["packages"]]


def test_linux_agent_collect_returns_empty_on_no_hosts():
    agent = LinuxAgent.__new__(LinuxAgent)
    agent.settings = MagicMock()
    agent.settings.ansible_inventory_path = "/etc/ansible/hosts"
    agent.callbacks = []

    with patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool:
        mock_tool.invoke.return_value = {}
        # H-5: collect() now returns a CollectOutcome so swallowed enrichment-probe
        # failures surface as run errors; the parsed resources are .items.
        items = agent.collect(scope="all").items

    assert items == []


def test_linux_agent_collect_excludes_configured_non_host_aliases():
    facts = dict(MOCK_FACTS)
    facts["cloudflare_tunnel_a"] = {
        "ansible_facts": {
            "ansible_distribution": "Debian",
            "ansible_distribution_version": "13.6",
            "ansible_kernel": "6.8.0-136-generic",
            "ansible_architecture": "x86_64",
            "ansible_packages": {},
            "ansible_services": {},
        }
    }

    agent = LinuxAgent.__new__(LinuxAgent)
    agent.settings = MagicMock()
    agent.settings.ansible_inventory_path = "/etc/ansible/hosts"
    agent.settings.linux_exclude_hosts = "cloudflare_tunnel_a,cloudflare_tunnel_b"
    agent.callbacks = []

    with patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool:
        mock_tool.invoke.return_value = facts
        # H-5: collect() now returns a CollectOutcome so swallowed enrichment-probe
        # failures surface as run errors; the parsed resources are .items.
        items = agent.collect(scope="all").items

    names = [item["name"] for item in items]
    assert "web01" in names
    assert "cloudflare_tunnel_a" not in names


def test_linux_agent_collect_keeps_all_hosts_when_exclude_list_unset():
    agent = LinuxAgent.__new__(LinuxAgent)
    agent.settings = MagicMock()
    agent.settings.ansible_inventory_path = "/etc/ansible/hosts"
    agent.settings.linux_exclude_hosts = ""
    agent.callbacks = []

    with patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool:
        mock_tool.invoke.return_value = MOCK_FACTS
        # H-5: collect() now returns a CollectOutcome so swallowed enrichment-probe
        # failures surface as run errors; the parsed resources are .items.
        items = agent.collect(scope="all").items

    assert [item["name"] for item in items] == ["web01"]


def test_linux_agent_collect_raises_on_ansible_exception():
    """ansible_facts_tool failure propagates — BaseAgent.run() catches it and
    records status='failed', not status='completed'/resources_found=0."""
    agent = LinuxAgent.__new__(LinuxAgent)
    agent.settings = MagicMock()
    agent.settings.ansible_inventory_path = "/etc/ansible/hosts"
    agent.callbacks = []

    with patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool:
        mock_tool.invoke.side_effect = RuntimeError("ansible unreachable")
        with pytest.raises(RuntimeError, match="ansible unreachable"):
            agent.collect(scope="all")


def test_linux_agent_collect_fast_fails_on_zero_parsed_hosts():
    """GitLab #143: a ToolException carrying ZERO_HOST_INVENTORY_MARKER (the
    inventory parsed to 0 hosts for the target — a config problem, not a
    connectivity outage) must be converted to CollectorSkipped, distinct from
    the generic status='failed' path above. The marker prefix itself must not
    leak into the CollectorSkipped reason shown to operators."""
    agent = LinuxAgent.__new__(LinuxAgent)
    agent.settings = MagicMock()
    agent.settings.ansible_inventory_path = "/opt/infra-brain/repos/fleet-ansible"
    agent.callbacks = []

    with patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool:
        mock_tool.invoke.side_effect = ToolException(
            f"{ZERO_HOST_INVENTORY_MARKER} Ansible inventory at "
            "'/opt/infra-brain/repos/fleet-ansible' parsed 0 hosts for target 'all' - "
            "path likely points above the inventory file; no connection attempted"
        )
        with pytest.raises(CollectorSkipped) as exc_info:
            agent.collect(scope="all")

    reason = str(exc_info.value)
    assert ZERO_HOST_INVENTORY_MARKER not in reason
    assert "parsed 0 hosts" in reason


def test_linux_agent_collect_reraises_non_zero_host_tool_exception():
    """A ToolException NOT carrying the zero-host marker (e.g. a real ansible
    -m setup failure) must propagate unchanged, not be swallowed into
    CollectorSkipped."""
    agent = LinuxAgent.__new__(LinuxAgent)
    agent.settings = MagicMock()
    agent.settings.ansible_inventory_path = "/etc/ansible/hosts"
    agent.callbacks = []

    with patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool:
        mock_tool.invoke.side_effect = ToolException("ansible -m setup failed: some real error")
        with pytest.raises(ToolException, match="some real error"):
            agent.collect(scope="all")


def _reachability_settings():
    return SimpleNamespace(
        ansible_inventory_path="/opt/infra-brain/repos/fleet-ansible",
        ansible_facts_timeout_seconds=270,
        ansible_ping_timeout_seconds=60,
        ansible_ping_scope_enabled=True,
        ansible_forks=30,
        ansible_connect_timeout_seconds=8,
        ansible_ssh_args="",
        linux_exclude_hosts="",
    )


@pytest.mark.parametrize(
    "reachable, unreachable, expected",
    [
        ([], [], "skipped"),
        ([], ["h1", "h2"], "failed_unreachable"),
        (["h1"], [], "success"),
    ],
)
def test_linux_agent_collect_over_reachable_hosts_matrix(reachable, unreachable, expected):
    """GitLab #160: a 47s failed run with 0 resources was traced to
    tools.ansible.run_ansible_facts's "no reachable hosts" branch — a real
    SSH/network outage across the fleet, NOT a regression of the GitLab #143
    inventory-path fix (that fix is confirmed live and working; it would
    produce status='skipped' in <1s, not status='failed' at 47s).

    Exercised end-to-end through LinuxAgent.collect ->
    ansible_facts_tool.invoke -> run_ansible_facts, with only
    tools.ansible._reachable_hosts monkeypatched (its ping-preflight
    subprocess call is skipped entirely; the setup subprocess call is mocked
    separately) so the marker propagation is covered all the way up:

    (a) ([], []) — inventory matched ZERO hosts, unchanged self-skip
        (CollectorSkipped, GitLab #143 behavior untouched).
    (b) ([], ["h1", "h2"]) — hosts matched but every one unreachable: a real
        failure whose message explicitly names the unreachable count,
        distinguishable from (a) for the first time (GitLab #160 fix).
    (c) (["h1"], []) — normal successful path, unchanged.
    """
    settings = _reachability_settings()
    agent = LinuxAgent.__new__(LinuxAgent)
    agent.settings = settings
    agent.callbacks = []

    setup_raw = {h: {"ansible_facts": {"ansible_distribution": "Ubuntu"}} for h in reachable}

    def _fake_tree(cmd, timeout, env):
        # Only the "-m setup" gather returns data here; the enrichment probes
        # (getent/listen_ports/slurp) that _merge_enrichment_facts also fires
        # get an empty result, which they already tolerate (best-effort).
        if "setup" in cmd:
            return 0, "", setup_raw
        return 0, "", {}

    with (
        patch("infra_brain.tools.ansible.get_settings", return_value=settings),
        patch("infra_brain.tools.ansible.os.path.exists", return_value=True),
        patch(
            "infra_brain.tools.ansible._reachable_hosts",
            return_value=(reachable, unreachable),
        ),
        patch("infra_brain.tools.ansible._run_ansible_tree", side_effect=_fake_tree),
    ):
        if expected == "skipped":
            with pytest.raises(CollectorSkipped) as exc_info:
                agent.collect(scope="all")
            reason = str(exc_info.value)
            assert ZERO_HOST_INVENTORY_MARKER not in reason
            assert "parsed 0 hosts" in reason
        elif expected == "failed_unreachable":
            with pytest.raises(RuntimeError) as exc_info:
                agent.collect(scope="all")
            message = str(exc_info.value)
            assert ALL_HOSTS_UNREACHABLE_MARKER not in message
            assert f"all {len(unreachable)} host(s) unreachable" in message
            assert "h1" in message and "h2" in message
        else:
            # H-5: collect() now returns a CollectOutcome so swallowed enrichment-probe
            # failures surface as run errors; the parsed resources are .items.
            items = agent.collect(scope="all").items
            assert len(items) == 1
            assert items[0]["name"] == "h1"


# --- linux child detail-table writes (users / crons / ports) ----------------
#
# Enriched-fact shapes: ansible_getent_passwd + ansible_getent_group (users),
# ansible_crontab (crons), ansible_listen_ports (ports). Plain `ansible -m setup`
# does not supply these (collectors are blind today), so the writers are exercised
# here with mocked enriched facts.

ENRICHED_FACTS = {
    "web01": {
        "ansible_facts": {
            "ansible_distribution": "Ubuntu",
            "ansible_kernel": "5.15.0",
            "ansible_architecture": "x86_64",
            "ansible_packages": {"nginx": [{"version": "1.18.0"}]},
            "ansible_services": {"nginx": {"state": "running", "status": "enabled"}},
            "ansible_getent_passwd": {
                "root": ["x", "0", "0", "root", "/root", "/bin/bash"],
                "deploy": ["x", "1000", "1000", "", "/home/deploy", "/bin/bash"],
            },
            "ansible_getent_group": {
                "sudo": ["x", "27", "deploy"],
            },
            "ansible_crontab": [
                {"owner": "deploy", "schedule": "0 3 * * *", "command": "/usr/bin/backup.sh"},
            ],
            "ansible_listen_ports": [
                {"port": 80, "protocol": "tcp", "name": "nginx", "state": "LISTEN"},
                {"port": 443, "protocol": "tcp", "name": "nginx"},
            ],
        }
    }
}


def _agent_with_raw(raw):
    agent = LinuxAgent.__new__(LinuxAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    agent._last_raw = raw
    return agent


def test_write_linux_details_populates_users_crons_ports(sqlite_engine, session_patcher):
    agent = _agent_with_raw(ENRICHED_FACTS)
    with Session(sqlite_engine) as s:
        s.add(Resource(domain="linux", type="linux_host", name="web01", source="LinuxAgent"))
        s.commit()

    with session_patcher("infra_brain.agents.linux"):
        agent._write_linux_details("all")

    with Session(sqlite_engine) as v:
        host = v.query(LinuxHost).one()

        users = {u.username: u for u in v.query(LinuxUser).all()}
        assert set(users) == {"root", "deploy"}
        assert users["deploy"].shell == "/bin/bash"
        assert users["deploy"].sudo is True  # member of sudo group
        assert users["root"].sudo is False

        crons = v.query(LinuxCron).all()
        assert len(crons) == 1
        assert crons[0].owner == "deploy"
        assert crons[0].schedule == "0 3 * * *"
        assert crons[0].command == "/usr/bin/backup.sh"

        ports = {p.port: p for p in v.query(LinuxPort).all()}
        assert set(ports) == {80, 443}
        assert ports[80].proto == "tcp"
        assert ports[80].process == "nginx"
        assert ports[80].state == "LISTEN"
        assert ports[443].state == "LISTEN"  # default when fact omits it

        # all child rows linked to the single host
        assert all(u.host_id == host.id for u in users.values())


def test_write_linux_details_idempotent_no_dupes(sqlite_engine, session_patcher):
    agent = _agent_with_raw(ENRICHED_FACTS)
    with Session(sqlite_engine) as s:
        s.add(Resource(domain="linux", type="linux_host", name="web01", source="LinuxAgent"))
        s.commit()

    with session_patcher("infra_brain.agents.linux"):
        agent._write_linux_details("all")
        agent._write_linux_details("all")

    with Session(sqlite_engine) as v:
        assert v.query(LinuxHost).count() == 1
        assert v.query(LinuxUser).count() == 2
        assert v.query(LinuxCron).count() == 1
        assert v.query(LinuxPort).count() == 2


def test_write_linux_details_updates_existing_host_facts(sqlite_engine, session_patcher):
    """S-16: distro/kernel/arch must be refreshed on every scan, not frozen at
    the values captured on the first insert (an OS upgrade previously never
    showed up on subsequent collections)."""
    agent = _agent_with_raw(ENRICHED_FACTS)
    with Session(sqlite_engine) as s:
        s.add(Resource(domain="linux", type="linux_host", name="web01", source="LinuxAgent"))
        s.commit()

    with session_patcher("infra_brain.agents.linux"):
        agent._write_linux_details("all")

    with Session(sqlite_engine) as v:
        host = v.query(LinuxHost).one()
        assert host.distro == "Ubuntu"
        assert host.kernel == "5.15.0"
        assert host.arch == "x86_64"

    upgraded = {
        "web01": {
            "ansible_facts": {
                **ENRICHED_FACTS["web01"]["ansible_facts"],
                "ansible_distribution": "Ubuntu",
                "ansible_kernel": "5.19.0",  # kernel upgraded
                "ansible_architecture": "aarch64",  # migrated to a new arch
            }
        }
    }
    agent2 = _agent_with_raw(upgraded)
    with session_patcher("infra_brain.agents.linux"):
        agent2._write_linux_details("all")

    with Session(sqlite_engine) as v:
        assert v.query(LinuxHost).count() == 1  # still one row, updated in place
        host = v.query(LinuxHost).one()
        assert host.kernel == "5.19.0"
        assert host.arch == "aarch64"


def test_write_linux_details_plain_setup_facts_leave_child_tables_empty(
    sqlite_engine, session_patcher
):
    """Blind collectors (plain setup, no enriched keys) → child tables stay empty, no error."""
    agent = _agent_with_raw(MOCK_FACTS)
    with Session(sqlite_engine) as s:
        s.add(Resource(domain="linux", type="linux_host", name="web01", source="LinuxAgent"))
        s.commit()

    with session_patcher("infra_brain.agents.linux"):
        agent._write_linux_details("all")

    with Session(sqlite_engine) as v:
        assert v.query(LinuxHost).count() == 1
        assert v.query(LinuxUser).count() == 0
        assert v.query(LinuxCron).count() == 0
        assert v.query(LinuxPort).count() == 0


def test_write_details_surfaces_linux_failure(sqlite_engine):
    agent = _agent_with_raw(ENRICHED_FACTS)
    result = CollectionResult(
        run_id=uuid.uuid4(), domain="linux", resources_found=0, drift_count=0, status="completed"
    )

    def _boom():
        raise RuntimeError("linux detail write exploded")

    with patch("infra_brain.etl.base.get_session", _session_cm(sqlite_engine)):
        agent._write_details(result, _boom)

    assert result.status == "failed"
    assert any("linux detail write exploded" in e for e in result.errors)


# --- long-tail: mounts / NICs (already-gathered setup facts) ----------------


_MOUNTS_NICS_FACTS = {
    "web01": {
        "ansible_facts": {
            "ansible_distribution": "Ubuntu",
            "ansible_kernel": "5.15.0",
            "ansible_architecture": "x86_64",
            "ansible_packages": {},
            "ansible_services": {},
            "ansible_mounts": [
                {
                    "mount": "/",
                    "device": "/dev/sda1",
                    "fstype": "ext4",
                    "size_total": 107374182400,  # 100 GiB
                    "size_available": 53687091200,  # 50 GiB
                },
                {"mount": "/boot", "device": "/dev/sda2", "fstype": "ext4"},  # no size fields
            ],
            "ansible_interfaces": ["eth0", "lo"],
            "ansible_eth0": {
                "macaddress": "aa:bb:cc:dd:ee:ff",
                "ipv4": {"address": "10.0.0.5", "netmask": "255.255.255.0"},
                "ipv6": [{"address": "fe80::1", "scope": "link"}],
                "speed": 1000,
            },
            "ansible_lo": {"macaddress": "00:00:00:00:00:00", "ipv4": {}},
        }
    }
}


def test_iter_mounts_converts_bytes_to_gb_and_skips_missing_sizes():
    facts = _MOUNTS_NICS_FACTS["web01"]["ansible_facts"]
    rows = list(_iter_mounts(facts))
    by_mount = {r["mount"]: r for r in rows}
    assert by_mount["/"]["size_total_gb"] == 100.0
    assert by_mount["/"]["size_available_gb"] == 50.0
    assert by_mount["/boot"]["size_total_gb"] is None
    assert by_mount["/boot"]["size_available_gb"] is None


def test_iter_nics_resolves_per_interface_dict_and_skips_missing():
    facts = _MOUNTS_NICS_FACTS["web01"]["ansible_facts"]
    rows = {r["name"]: r for r in _iter_nics(facts)}
    assert rows["eth0"]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert rows["eth0"]["ipv4"] == "10.0.0.5"
    assert rows["eth0"]["ipv6"] == "fe80::1"
    assert rows["eth0"]["speed_mbps"] == 1000
    assert rows["lo"]["ipv4"] is None  # empty ipv4 dict -> None, not KeyError


def test_iter_nics_skips_interface_with_no_matching_fact_key():
    """An interface listed in ansible_interfaces but with no corresponding
    ansible_<name> dict (can happen for virtual/bonded interfaces the
    collector didn't fully enumerate) is skipped, not a crash."""
    facts = {"ansible_interfaces": ["eth0", "bond0"]}  # no ansible_eth0/ansible_bond0 at all
    assert list(_iter_nics(facts)) == []


def test_write_linux_details_populates_mounts_and_nics(sqlite_engine, session_patcher):
    agent = _agent_with_raw(_MOUNTS_NICS_FACTS)
    with Session(sqlite_engine) as s:
        s.add(Resource(domain="linux", type="linux_host", name="web01", source="LinuxAgent"))
        s.commit()

    with session_patcher("infra_brain.agents.linux"):
        agent._write_linux_details("all")

    with Session(sqlite_engine) as v:
        mounts = {m.mount: m for m in v.query(LinuxMount).all()}
        assert set(mounts) == {"/", "/boot"}
        assert mounts["/"].size_total_gb == 100.0
        assert mounts["/"].fstype == "ext4"

        nics = {n.name: n for n in v.query(LinuxNic).all()}
        assert set(nics) == {"eth0", "lo"}
        assert nics["eth0"].mac == "aa:bb:cc:dd:ee:ff"
        assert nics["eth0"].speed_mbps == 1000


def test_write_linux_details_mounts_and_nics_idempotent(sqlite_engine, session_patcher):
    agent = _agent_with_raw(_MOUNTS_NICS_FACTS)
    with Session(sqlite_engine) as s:
        s.add(Resource(domain="linux", type="linux_host", name="web01", source="LinuxAgent"))
        s.commit()

    with session_patcher("infra_brain.agents.linux"):
        agent._write_linux_details("all")
        agent._write_linux_details("all")

    with Session(sqlite_engine) as v:
        assert v.query(LinuxMount).count() == 2
        assert v.query(LinuxNic).count() == 2


# --- ansible failure propagation tests (Commit 2 regression) ----------------


def test_linux_run_marks_failed_on_ansible_tool_error(engine):
    """ansible_facts_tool raising RuntimeError propagates through collect() so
    BaseAgent.run() records status='failed' with a populated error_message.
    Previously the exception was swallowed and the run was marked completed/0."""
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    with (
        patch("infra_brain.etl.base.get_session", _session),
        patch("infra_brain.agents.linux.get_session", _session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool,
    ):
        mock_tool.invoke.side_effect = RuntimeError("ansible -m setup failed: unreachable")
        agent = LinuxAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "failed", "Run should be failed when ansible errors"
    assert result.resources_found == 0
    assert any("unreachable" in e for e in result.errors)

    with Session(engine) as verify:
        run = verify.get(CollectionRun, result.run_id)
        assert run.status == "failed"
        assert run.error_message is not None
        assert "unreachable" in run.error_message


def test_linux_run_marks_skipped_on_zero_parsed_hosts(engine):
    """GitLab #143: a zero-parsed-host inventory must record status='skipped'
    on CollectionRun (via CollectorSkipped), not status='failed' — so a
    misconfigured ANSIBLE_INVENTORY_PATH is visually distinguishable from a
    genuine fleet-connectivity outage in collection_runs."""
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    with (
        patch("infra_brain.etl.base.get_session", _session),
        patch("infra_brain.agents.linux.get_session", _session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool,
    ):
        mock_tool.invoke.side_effect = ToolException(
            f"{ZERO_HOST_INVENTORY_MARKER} Ansible inventory at "
            "'/opt/infra-brain/repos/fleet-ansible' parsed 0 hosts for target 'all' - "
            "path likely points above the inventory file; no connection attempted"
        )
        agent = LinuxAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "skipped"
    assert result.resources_found == 0
    assert not any(ZERO_HOST_INVENTORY_MARKER in e for e in result.errors)

    with Session(engine) as verify:
        run = verify.get(CollectionRun, result.run_id)
        assert run.status == "skipped"
        assert run.error_message is not None
        assert "parsed 0 hosts" in run.error_message
        assert ZERO_HOST_INVENTORY_MARKER not in run.error_message


def test_linux_run_sets_drift_count_for_new_port(engine):
    """AA-C-2: LinuxAgent.run() must report a nonzero drift_count when the
    detail-write phase emits a new_listening_port DriftEvent, and that event
    must carry collection_run_id so it is actually counted.

    The enrichment probes are mocked to succeed-with-nothing so this stays a
    clean-run test. Before H-5 they were left unmocked and every one of them
    raised (unconfigured inventory) — silently, because the swallow reported
    nothing; the run is now correctly "partial" in that state, which is the
    behaviour ``test_failed_listen_ports_probe_does_not_permanently_disarm_
    port_drift`` covers on purpose.
    """
    from infra_brain.db.models import DriftEvent, LinuxPort

    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    with Session(engine) as s:
        resource = Resource(
            domain="linux", type="linux_host", name="drift-host-01", source="LinuxAgent"
        )
        s.add(resource)
        s.commit()
        host = LinuxHost(resource_id=resource.id, distro="Ubuntu", kernel="5.15.0", arch="x86_64")
        s.add(host)
        s.commit()
        s.add(LinuxPort(host_id=host.id, port=22, proto="tcp", process="sshd", state="LISTEN"))
        s.commit()

    facts_with_new_port = {
        "drift-host-01": {
            "ansible_facts": {
                "ansible_distribution": "Ubuntu",
                "ansible_distribution_version": "22.04",
                "ansible_kernel": "5.15.0",
                "ansible_architecture": "x86_64",
                "ansible_packages": {},
                "ansible_services": {},
                "ansible_listen_ports": [
                    {"port": 22, "protocol": "tcp", "name": "sshd", "state": "LISTEN"},
                    {"port": 9999, "protocol": "tcp", "name": "evil", "state": "LISTEN"},
                ],
            }
        }
    }

    with (
        patch("infra_brain.etl.base.get_session", _session),
        patch("infra_brain.agents.linux.get_session", _session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool,
        patch.multiple(
            "infra_brain.agents.linux",
            **{attr: _h5_probe() for attr in _H5_PROBE_ATTRS},
        ),
    ):
        mock_tool.invoke.return_value = facts_with_new_port
        agent = LinuxAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "completed"
    assert result.drift_count == 1, "new_listening_port drift must be counted, not stuck at 0"

    with Session(engine) as v:
        event = v.query(DriftEvent).filter_by(drift_type="new_listening_port").one()
        assert event.collection_run_id == result.run_id


def test_linux_run_completed_on_empty_inventory(engine):
    """ansible_facts_tool returning {} (empty inventory) is NOT an error —
    run should be status='completed' with resources_found=0."""
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    with (
        patch("infra_brain.etl.base.get_session", _session),
        patch("infra_brain.agents.linux.get_session", _session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool,
    ):
        mock_tool.invoke.return_value = {}
        agent = LinuxAgent()
        result = agent.run(trigger_type="scheduled", scope="all")

    assert result.status == "completed"
    assert result.resources_found == 0
    assert result.errors == []


# --- TRK-031: Linux posture writers (pending updates / certs / shares / fw) --
#
# Enriched-fact keys the posture gather tools supply:
#   pending_updates    -> linux_pending_updates (host_id)
#   host_certificates  -> host_certificates     (resource_id)
#   host_shares        -> host_shares           (resource_id)
#   firewall_rules     -> host_firewall_rules   (resource_id)


_POSTURE_FACTS = {
    "web01": {
        "ansible_facts": {
            "ansible_distribution": "Ubuntu",
            "ansible_kernel": "5.15.0",
            "ansible_architecture": "x86_64",
            "ansible_packages": {},
            "ansible_services": {},
            "pending_updates": [
                {
                    "package": "openssl",
                    "current_version": "3.0.2-0ubuntu1.10",
                    "available_version": "3.0.2-0ubuntu1.15",
                    "security": True,
                    "manager": "apt",
                },
                {
                    "package": "vim",
                    "current_version": "2:8.2-1",
                    "available_version": "2:8.2-2",
                    "security": False,
                    "manager": "apt",
                },
            ],
            "host_certificates": [
                {
                    "store": "linux",
                    "subject": "CN = web01.local",
                    "issuer": "CN = Internal CA",
                    "thumbprint": "ABCDEF1234",
                    "not_before": datetime(2025, 1, 1, tzinfo=UTC),
                    "not_after": datetime(2035, 1, 1, tzinfo=UTC),
                },
            ],
            "host_shares": [
                {
                    "share_type": "nfs",
                    "name": "/srv/nfs/data",
                    "path": "/srv/nfs/data",
                    "permissions": [{"principal": "10.0.0.0/24", "access": "rw,sync"}],
                },
            ],
            "firewall_rules": [
                {
                    "table_name": "filter",
                    "chain": "INPUT",
                    "rule_text": "-A INPUT -p tcp --dport 22 -j ACCEPT",
                    "action": "ACCEPT",
                    "source": "iptables",
                },
                {
                    "table_name": "filter",
                    "chain": "INPUT",
                    "rule_text": "-P INPUT DROP",
                    "action": "DROP",
                    "source": "iptables",
                },
            ],
        }
    }
}


def test_write_linux_details_populates_posture_tables(sqlite_engine, session_patcher):
    agent = _agent_with_raw(_POSTURE_FACTS)
    with Session(sqlite_engine) as s:
        s.add(Resource(domain="linux", type="linux_host", name="web01", source="LinuxAgent"))
        s.commit()

    with session_patcher("infra_brain.agents.linux"):
        agent._write_linux_details("all")

    with Session(sqlite_engine) as v:
        updates = {u.package: u for u in v.query(LinuxPendingUpdate).all()}
        assert set(updates) == {"openssl", "vim"}
        assert updates["openssl"].available_version == "3.0.2-0ubuntu1.15"
        assert updates["openssl"].security is True
        assert updates["vim"].security is False
        # linked to the linux host row
        host = v.query(LinuxHost).one()
        assert all(u.host_id == host.id for u in updates.values())

        certs = v.query(HostCertificate).all()
        assert len(certs) == 1
        assert certs[0].thumbprint == "ABCDEF1234"
        assert certs[0].is_expired is False
        assert certs[0].days_until_expiry is not None and certs[0].days_until_expiry > 0

        shares = v.query(HostShare).all()
        assert len(shares) == 1
        assert shares[0].share_type == "nfs"
        assert shares[0].name == "/srv/nfs/data"

        rules = {r.rule_text: r for r in v.query(HostFirewallRule).all()}
        assert set(rules) == {"-A INPUT -p tcp --dport 22 -j ACCEPT", "-P INPUT DROP"}
        assert rules["-P INPUT DROP"].action == "DROP"
        assert rules["-A INPUT -p tcp --dport 22 -j ACCEPT"].source == "iptables"


def test_write_linux_details_posture_idempotent_no_dupes(sqlite_engine, session_patcher):
    agent = _agent_with_raw(_POSTURE_FACTS)
    with Session(sqlite_engine) as s:
        s.add(Resource(domain="linux", type="linux_host", name="web01", source="LinuxAgent"))
        s.commit()

    with session_patcher("infra_brain.agents.linux"):
        agent._write_linux_details("all")
        agent._write_linux_details("all")

    with Session(sqlite_engine) as v:
        assert v.query(LinuxPendingUpdate).count() == 2
        assert v.query(HostCertificate).count() == 1
        assert v.query(HostShare).count() == 1
        assert v.query(HostFirewallRule).count() == 2


def test_write_linux_details_plain_facts_leave_posture_tables_empty(sqlite_engine, session_patcher):
    """Blind collectors (no posture keys) -> posture tables stay empty, no error."""
    agent = _agent_with_raw(MOCK_FACTS)
    with Session(sqlite_engine) as s:
        s.add(Resource(domain="linux", type="linux_host", name="web01", source="LinuxAgent"))
        s.commit()

    with session_patcher("infra_brain.agents.linux"):
        agent._write_linux_details("all")

    with Session(sqlite_engine) as v:
        assert v.query(LinuxPendingUpdate).count() == 0
        assert v.query(HostCertificate).count() == 0
        assert v.query(HostShare).count() == 0
        assert v.query(HostFirewallRule).count() == 0


def test_merge_enrichment_facts_pulls_posture_probes(monkeypatch):
    """_merge_enrichment_facts invokes the four posture tools and merges their
    fact keys into raw[host]['ansible_facts']."""
    agent = LinuxAgent.__new__(LinuxAgent)
    agent.settings = MagicMock()
    agent.callbacks = []

    raw = {"web01": {"ansible_facts": {}}}

    def _mk(key, rows):
        m = MagicMock()
        m.invoke.return_value = {"web01": {"ansible_facts": {key: rows}}}
        return m

    import infra_brain.agents.linux as linux_mod

    # neutralize the MR-J probes so only the posture merges are asserted
    for name in (
        "ansible_getent_tool",
        "ansible_listen_ports_tool",
        "ansible_crontab_slurp_tool",
    ):
        stub = MagicMock()
        stub.invoke.return_value = {}
        monkeypatch.setattr(linux_mod, name, stub)

    monkeypatch.setattr(
        linux_mod, "ansible_pending_updates_tool", _mk("pending_updates", [{"package": "openssl"}])
    )
    monkeypatch.setattr(
        linux_mod, "ansible_certificates_tool", _mk("host_certificates", [{"thumbprint": "AB"}])
    )
    monkeypatch.setattr(
        linux_mod,
        "ansible_firewall_rules_tool",
        _mk("firewall_rules", [{"rule_text": "-P INPUT DROP"}]),
    )
    monkeypatch.setattr(linux_mod, "ansible_shares_tool", _mk("host_shares", [{"name": "/srv/x"}]))

    agent._merge_enrichment_facts(raw, scope="all", inventory="/etc/ansible/hosts")

    facts = raw["web01"]["ansible_facts"]
    assert facts["pending_updates"][0]["package"] == "openssl"
    assert facts["host_certificates"][0]["thumbprint"] == "AB"
    assert facts["firewall_rules"][0]["rule_text"] == "-P INPUT DROP"
    assert facts["host_shares"][0]["name"] == "/srv/x"


def test_linux_agent_collect_self_skips_when_inventory_path_missing():
    """A missing/unset inventory path is an absent dependency, not a failure.

    tools/ansible.py raises a bare RuntimeError when ANSIBLE_INVENTORY_PATH is
    unset or names a path that does not exist on this host (the usual cause:
    the inventory volume was never mounted into the container). That escaped
    uncaught and BaseAgent recorded status="failed" — reading identically to a
    live SSH outage and re-failing on every scheduled run, with nothing short
    of mounting the volume able to clear it. It now takes the same
    CollectorSkipped path as the zero-host case, and the marker prefix must not
    leak into the reason operators see.
    """
    agent = LinuxAgent.__new__(LinuxAgent)
    agent.settings = MagicMock()
    agent.settings.ansible_inventory_path = "/opt/inventory"
    agent.callbacks = []

    # ToolException, NOT RuntimeError. tools/ansible.py raises RuntimeError, but
    # @tool wraps it before it reaches collect(). The first version of this test
    # mocked RuntimeError, so it passed against a handler that never fired in
    # production — the marker leaked into the error text and linux stayed
    # "failed" for 153 escalating runs. Verified against the live tool.
    with patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool:
        mock_tool.invoke.side_effect = ToolException(
            f"{INVENTORY_UNAVAILABLE_MARKER} ansible inventory path does not exist: /opt/inventory"
        )
        with pytest.raises(CollectorSkipped) as exc_info:
            agent.collect(scope="all")

    reason = str(exc_info.value)
    assert INVENTORY_UNAVAILABLE_MARKER not in reason
    assert "/opt/inventory" in reason


def test_linux_agent_collect_self_skips_on_unwrapped_runtime_error():
    """The defensive RuntimeError twin still works for a direct (unwrapped) call."""
    agent = LinuxAgent.__new__(LinuxAgent)
    agent.settings = MagicMock()
    agent.settings.ansible_inventory_path = "/opt/inventory"
    agent.callbacks = []

    with patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool:
        mock_tool.invoke.side_effect = RuntimeError(
            f"{INVENTORY_UNAVAILABLE_MARKER} ansible inventory path does not exist: /opt/inventory"
        )
        with pytest.raises(CollectorSkipped):
            agent.collect(scope="all")


def test_linux_agent_collect_reraises_unmarked_runtime_error():
    """A RuntimeError without the marker is a real fault and must still fail."""
    agent = LinuxAgent.__new__(LinuxAgent)
    agent.settings = MagicMock()
    agent.settings.ansible_inventory_path = "/opt/inventory"
    agent.callbacks = []

    with patch("infra_brain.agents.linux.ansible_facts_tool") as mock_tool:
        mock_tool.invoke.side_effect = RuntimeError("ansible-playbook segfaulted")
        with pytest.raises(RuntimeError, match="segfaulted"):
            agent.collect(scope="all")


# --- H-5: a failed enrichment probe must not wipe detail rows --------------
#
# ``_merge_enrichment_facts`` swallows every probe failure into ``{}``. The
# merge loop only writes a fact key when the probe supplied one, so a failed
# probe leaves that key ABSENT — and the detail writers' unconditional
# delete-then-reinsert reads ``facts.get(key, []) or []``, which is identical
# for "absent" and "present but empty". A single SSH blip therefore deleted
# every stored port/user/cron/posture row and reinserted nothing, while the run
# still reported ``completed`` with no errors.
#
# The port table is the one that hurts most: ``_check_port_drift`` compares
# against the rows it snapshotted just before the delete, so once the table is
# emptied ``prev_ports`` is empty on every subsequent run, the
# ``if prev_ports:`` guard short-circuits, and new-listening-port drift NEVER
# FIRES AGAIN — the detector silently disarms itself permanently.

_H5_HOST = "disarm-host-01"

_H5_PROBE_ATTRS = (
    "ansible_getent_tool",
    "ansible_crontab_slurp_tool",
    "ansible_listen_ports_tool",
    "ansible_pending_updates_tool",
    "ansible_certificates_tool",
    "ansible_firewall_rules_tool",
    "ansible_shares_tool",
)


def _h5_setup_facts():
    """Plain ``-m setup`` facts — deliberately carries NO enrichment keys, so
    every enrichment fact can only ever arrive from its probe."""
    return {
        _H5_HOST: {
            "ansible_facts": {
                "ansible_distribution": "Ubuntu",
                "ansible_distribution_version": "22.04",
                "ansible_kernel": "5.15.0",
                "ansible_architecture": "x86_64",
                "ansible_packages": {},
                "ansible_services": {},
            }
        }
    }


def _h5_probe(payload=None):
    """A probe that SUCCEEDS, returning *payload* (default ``{}`` — the real
    tools omit a host entirely when it has no rows, see
    tools/ansible.py::_merge_host_fact)."""
    probe = MagicMock()
    probe.invoke.return_value = {} if payload is None else payload
    return probe


def _h5_failing_probe():
    """The REAL failure shape. Every ansible enrichment tool is @tool-decorated
    and re-raises its RuntimeError as ``ToolException`` (tools/ansible.py) — the
    exact mismatch that made an earlier fix here mock RuntimeError, ship green,
    and keep failing in production for 153 more runs."""
    probe = MagicMock()
    probe.invoke.side_effect = ToolException(
        "ansible -m community.general.listen_ports_facts failed: ssh: connect timed out"
    )
    return probe


def _h5_facts_payload(key, rows):
    return {_H5_HOST: {"ansible_facts": {key: rows}}}


@contextmanager
def _h5_run_env(engine, **probe_overrides):
    """Run a real ``LinuxAgent().run()`` against *engine* with every enrichment
    probe mocked; named overrides replace the default report-nothing probe."""
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    probes = {attr: _h5_probe() for attr in _H5_PROBE_ATTRS}
    probes.update(probe_overrides)

    with (
        patch("infra_brain.etl.base.get_session", _session),
        patch("infra_brain.agents.linux.get_session", _session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.agents.linux.ansible_facts_tool") as facts_tool,
        patch.multiple("infra_brain.agents.linux", **probes),
    ):
        facts_tool.invoke.return_value = _h5_setup_facts()
        yield


def test_failed_listen_ports_probe_does_not_permanently_disarm_port_drift(sqlite_engine):
    """H-5 regression: one failed listen_ports probe must not delete the stored
    port rows, and port-drift detection must STILL FIRE on the next healthy run.

    On the unfixed code the middle run wipes linux_ports, so the third run's
    ``prev_ports`` snapshot is empty and the new port 9999 is never reported —
    for that host, forever.
    """
    from infra_brain.db.models import DriftEvent

    engine = sqlite_engine

    def _ports(*ports):
        return _h5_probe(
            _h5_facts_payload(
                "ansible_listen_ports",
                [{"port": p, "protocol": "tcp", "name": "svc", "state": "LISTEN"} for p in ports],
            )
        )

    # --- run 1: healthy probe establishes the baseline -----------------------
    with _h5_run_env(engine, ansible_listen_ports_tool=_ports(22)):
        r1 = LinuxAgent().run(trigger_type="scheduled", scope="all")
    assert r1.status == "completed"
    with Session(engine) as v:
        assert {p.port for p in v.query(LinuxPort).all()} == {22}

    # --- run 2: the probe FAILS (SSH blip) -----------------------------------
    with _h5_run_env(engine, ansible_listen_ports_tool=_h5_failing_probe()):
        r2 = LinuxAgent().run(trigger_type="scheduled", scope="all")

    with Session(engine) as v:
        survived = {p.port for p in v.query(LinuxPort).all()}
    assert survived == {22}, (
        "a failed listen_ports probe means 'could not look', not 'no ports' — "
        "the stored rows must survive"
    )

    # The swallowed probe failure must be visible on the run, not reported green.
    assert r2.status == "partial"
    assert any("listen_ports" in e for e in r2.errors), r2.errors
    with Session(engine) as v:
        assert v.get(CollectionRun, r2.run_id).status == "partial"

    # --- run 3: probe healthy again, a new port appears ----------------------
    with _h5_run_env(engine, ansible_listen_ports_tool=_ports(22, 9999)):
        r3 = LinuxAgent().run(trigger_type="scheduled", scope="all")

    assert r3.status == "completed"
    assert r3.drift_count == 1, (
        "port-drift detection must survive an intervening failed probe — "
        "a wiped linux_ports table disarms it permanently"
    )
    with Session(engine) as v:
        events = v.query(DriftEvent).filter_by(drift_type="new_listening_port").all()
        assert [e.new_value["port"] for e in events] == [9999]
        assert {p.port for p in v.query(LinuxPort).all()} == {22, 9999}


def test_failed_enrichment_probes_preserve_user_cron_and_posture_rows(sqlite_engine):
    """H-5, non-port half: the same swallow wipes users/crons/updates/shares.

    Also pins the flip side of the distinction — a probe that SUCCEEDS while
    reporting nothing is a genuine measurement of "no rows", and must still
    prune. Skipping the delete is scoped to "could not look", not to "empty".
    """
    engine = sqlite_engine

    healthy = {
        "ansible_getent_tool": _h5_probe(
            {
                _H5_HOST: {
                    "ansible_facts": {
                        # the getent module's own raw fact keys, which
                        # _merge_enrichment_facts renames to ansible_getent_*
                        "getent_passwd": {
                            "deploy": ["x", "1000", "1000", "", "/home/deploy", "/bin/bash"]
                        },
                        "getent_group": {"sudo": ["x", "27", "deploy"]},
                    }
                }
            }
        ),
        "ansible_crontab_slurp_tool": _h5_probe(
            _h5_facts_payload(
                "ansible_crontab",
                [{"owner": "deploy", "schedule": "0 3 * * *", "command": "/usr/bin/backup.sh"}],
            )
        ),
        "ansible_pending_updates_tool": _h5_probe(
            _h5_facts_payload(
                "pending_updates",
                [{"package": "openssl", "available_version": "3.0.2", "manager": "apt"}],
            )
        ),
        "ansible_shares_tool": _h5_probe(
            _h5_facts_payload("host_shares", [{"name": "/srv/exports", "share_type": "nfs"}])
        ),
    }
    failing = {attr: _h5_failing_probe() for attr in healthy}

    # --- phase 1: healthy probes populate the tables -------------------------
    with _h5_run_env(engine, **healthy):
        LinuxAgent().run(trigger_type="scheduled", scope="all")
    with Session(engine) as v:
        assert v.query(LinuxUser).count() == 1
        assert v.query(LinuxCron).count() == 1
        assert v.query(LinuxPendingUpdate).count() == 1
        assert v.query(HostShare).count() == 1

    # --- phase 2: probes fail -> nothing may be deleted ----------------------
    with _h5_run_env(engine, **failing):
        r2 = LinuxAgent().run(trigger_type="scheduled", scope="all")
    with Session(engine) as v:
        assert v.query(LinuxUser).count() == 1, "failed getent probe must not delete users"
        assert v.query(LinuxCron).count() == 1, "failed crontab probe must not delete crons"
        assert v.query(LinuxPendingUpdate).count() == 1, (
            "failed pending_updates probe must not delete pending-update rows"
        )
        assert v.query(HostShare).count() == 1, "failed shares probe must not delete shares"
    assert r2.status == "partial"
    assert len(r2.errors) >= 4, r2.errors

    # --- phase 3: probes succeed reporting nothing -> rows ARE pruned --------
    with _h5_run_env(engine):
        r3 = LinuxAgent().run(trigger_type="scheduled", scope="all")
    assert r3.status == "completed"
    with Session(engine) as v:
        assert v.query(LinuxUser).count() == 0
        assert v.query(LinuxCron).count() == 0
        assert v.query(LinuxPendingUpdate).count() == 0
        assert v.query(HostShare).count() == 0


# --- TRK-282: sudo-denied become failure must be a distinguishable error, ---
# not a swallowed empty-success, for the two privileged posture gathers
# (firewall_rules / host_shares). ansible_firewall_rules_tool/ansible_shares_tool
# are @tool-wrapped, so a RuntimeError from tools/ansible.py's become-denied
# check (BECOME_DENIED_MARKER) reaches LinuxAgent as a ToolException, exactly
# like _h5_failing_probe()'s generic SSH-timeout shape above — this test pins
# the become-specific variant end to end: the marker text must survive into
# CollectOutcome/CollectionRun.errors (so a human triaging
# collection_runs.error_message can tell "sudo was refused" apart from "no
# firewall rules configured"), and existing rows must not be deleted.
def _h5_become_denied_probe(cmd_label: str):
    probe = MagicMock()
    probe.invoke.side_effect = ToolException(
        f"{BECOME_DENIED_MARKER} sudo/become denied for read-only command "
        f"{cmd_label!r} on 1 host(s) (sample: {_H5_HOST}); the ansible service "
        "account needs passwordless sudo for this command"
    )
    return probe


def test_become_denied_posture_probe_is_distinguishable_and_preserves_rows(sqlite_engine):
    engine = sqlite_engine

    healthy = {
        "ansible_firewall_rules_tool": _h5_probe(
            _h5_facts_payload("firewall_rules", [{"rule_text": "-P INPUT DROP"}])
        ),
        "ansible_shares_tool": _h5_probe(
            _h5_facts_payload("host_shares", [{"name": "/srv/exports", "share_type": "nfs"}])
        ),
    }

    # --- phase 1: healthy (become succeeds) probes populate the tables -------
    with _h5_run_env(engine, **healthy):
        r1 = LinuxAgent().run(trigger_type="scheduled", scope="all")
    assert r1.status == "completed"
    with Session(engine) as v:
        assert v.query(HostFirewallRule).count() == 1
        assert v.query(HostShare).count() == 1

    # --- phase 2: sudo is denied (e.g. TRK-099 fleet SSH lands a non-root ----
    # service account with no sudo rights) -> distinguishable error, no delete.
    denied = {
        "ansible_firewall_rules_tool": _h5_become_denied_probe("iptables -S"),
        "ansible_shares_tool": _h5_become_denied_probe("exportfs -v"),
    }
    with _h5_run_env(engine, **denied):
        r2 = LinuxAgent().run(trigger_type="scheduled", scope="all")

    with Session(engine) as v:
        assert v.query(HostFirewallRule).count() == 1, (
            "sudo-denied probe must not delete existing firewall rules"
        )
        assert v.query(HostShare).count() == 1, "sudo-denied probe must not delete existing shares"
    assert r2.status == "partial"
    assert any(BECOME_DENIED_MARKER in e for e in r2.errors), (
        f"become-denied failure must be distinguishable in errors, got: {r2.errors}"
    )
    with Session(engine) as v:
        run_row = v.get(CollectionRun, r2.run_id)
        assert run_row.status == "partial"
        assert BECOME_DENIED_MARKER in (run_row.error_message or ""), (
            "collection_runs.error_message must carry the distinguishable marker, "
            "not read as a generic/empty failure"
        )
