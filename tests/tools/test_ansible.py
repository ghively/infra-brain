import base64
import json
import re
from types import SimpleNamespace
import pytest
from unittest.mock import patch, MagicMock
from langchain_core.tools import ToolException
from infra_brain.tools import ansible as ansible_mod
from infra_brain.tools.ansible import (
    run_ansible_facts,
    ansible_inventory_tool,
    ansible_ping_tool,
    run_ansible_getent,
    run_ansible_slurp,
    run_ansible_listen_ports,
    run_ansible_crontab,
    _parse_user_crontab,
    _parse_system_crontab,
    _parse_ss_listen_ports,
)


def test_run_ansible_tree_reads_real_per_host_files_and_ignores_bad_ones(tmp_path):
    """_run_ansible_tree itself, with no mocking below subprocess.run: proves the
    --tree + per-host-file-read approach actually parses what a real `ansible`
    invocation writes, unlike the old json.loads(stdout) approach it replaced
    (which cannot work — see the docstring on _run_ansible_tree)."""

    def _fake_run(cmd, **kwargs):
        # cmd ends with ["--tree", <tmpdir>] appended by _run_ansible_tree.
        tree_dir = cmd[-1]
        with open(f"{tree_dir}/hostA", "w") as f:
            json.dump({"ansible_facts": {"ansible_distribution": "Ubuntu"}}, f)
        with open(f"{tree_dir}/hostB", "w") as f:
            f.write("not valid json")  # simulates a module crash mid-write; must not blow up
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("infra_brain.tools.ansible.subprocess.run", side_effect=_fake_run):
        rc, stderr, raw = ansible_mod._run_ansible_tree(
            ["ansible", "all", "-m", "setup", "-i", "/inv"], timeout=60, env={}
        )

    assert rc == 0
    assert raw == {"hostA": {"ansible_facts": {"ansible_distribution": "Ubuntu"}}}
    assert "hostB" not in raw


def test_run_ansible_crontab_merges_root_and_system_paths():
    root_text = "*/5 * * * * /usr/local/bin/backup.sh\n"
    system_text = "17 * * * * root run-parts /etc/cron.hourly\n"

    def _fake_tree(cmd, timeout, env):
        joined = " ".join(cmd)
        if "src=/var/spool/cron/crontabs/root" in joined:
            return 0, "", {"web01": {"content": base64.b64encode(root_text.encode()).decode()}}
        if "src=/var/spool/cron/root" in joined:
            return 2, "not found", {}  # RHEL path absent on this (Debian) host
        if "src=/etc/crontab" in joined:
            return 0, "", {"web01": {"content": base64.b64encode(system_text.encode()).decode()}}
        return 1, "unexpected", {}

    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch("infra_brain.tools.ansible._run_ansible_tree", side_effect=_fake_tree),
    ):
        result = run_ansible_crontab(target="web01", inventory="/etc/ansible/hosts")

    rows = result["web01"]["ansible_facts"]["ansible_crontab"]
    assert {
        "owner": "root",
        "schedule": "*/5 * * * *",
        "command": "/usr/local/bin/backup.sh",
    } in rows
    assert {
        "owner": "root",
        "schedule": "17 * * * *",
        "command": "run-parts /etc/cron.hourly",
    } in rows


def _facts_settings(**over):
    # F-017: ping-scope disabled here so this test exercises the single setup
    # call directly; the ping-scoping path has dedicated tests in
    # test_ansible_timeout.py.
    base = dict(
        ansible_inventory_path="/etc/ansible/hosts",
        ansible_facts_timeout_seconds=270,
        ansible_ping_timeout_seconds=60,
        ansible_ping_scope_enabled=False,
        ansible_forks=30,
        ansible_connect_timeout_seconds=8,
        ansible_ssh_args="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_run_ansible_facts_calls_readonly_command():
    fake_raw = {"web01": {"ansible_facts": {"ansible_distribution": "Ubuntu"}}}

    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch(
            "infra_brain.tools.ansible._run_ansible_tree", return_value=(0, "", fake_raw)
        ) as mock_tree,
    ):
        result = run_ansible_facts(target="web01", inventory="/etc/ansible/hosts")

    cmd = mock_tree.call_args[0][0]
    assert "ansible" in cmd[0]
    assert "-m" in cmd
    assert "setup" in cmd
    assert "ansible-playbook" not in " ".join(cmd)
    assert result["web01"]["ansible_facts"]["ansible_distribution"] == "Ubuntu"


def test_run_ansible_facts_rejects_playbook_command():
    with pytest.raises(ValueError, match="read-only"):
        run_ansible_facts(target="all; ansible-playbook evil.yml", inventory="/inv")


def test_ansible_inventory_tool_returns_parsed_json():
    inventory_data = {
        "_meta": {"hostvars": {"host1": {"ansible_host": "198.51.100.17"}}},
        "all": {"children": ["linux", "windows"]},
        "linux": {"hosts": ["host1"]},
    }
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(inventory_data)
    mock_result.stderr = ""
    with patch("infra_brain.tools.ansible.subprocess.run", return_value=mock_result):
        result = ansible_inventory_tool.invoke({"inventory": "/etc/ansible/hosts"})
    assert "linux" in result
    assert "host1" in result["linux"]["hosts"]


def test_ansible_inventory_tool_rejects_unsafe_path():
    with pytest.raises(ToolException, match="unsafe characters"):
        ansible_inventory_tool.invoke({"inventory": "/etc/ansible/../../etc/passwd"})


def test_ansible_ping_tool_returns_results():
    with patch(
        "infra_brain.tools.ansible._run_ansible_tree",
        return_value=(0, "", {"host1": {"ping": "pong"}}),
    ):
        result = ansible_ping_tool.invoke({"target": "linux", "inventory": "/etc/ansible/hosts"})
    assert "host1" in result


def test_ansible_ping_tool_rejects_unsafe_target():
    with pytest.raises(ToolException, match="unsafe characters"):
        ansible_ping_tool.invoke({"target": "host; rm -rf /", "inventory": "/etc/ansible/hosts"})


def test_ansible_ping_tool_uses_ansible_env_helper():
    """GitLab finding: ansible_ping_tool must build its subprocess env via
    _ansible_env() like every sibling ansible function (run_ansible_facts,
    _reachable_hosts, run_ansible_getent/slurp/listen_ports/command,
    run_windows_ansible_facts) instead of os.environ.copy() directly — the
    helper is what supplies ANSIBLE_HOST_KEY_CHECKING=False (no known_hosts in
    a fresh container) and ANSIBLE_SSH_ARGS (legacy host-key algorithms for
    old fleet hosts); bypassing it means every ping against a real host fails
    host-key verification/algorithm negotiation even though facts-gathering
    works fine.
    """
    sentinel_env = {"ANSIBLE_HOST_KEY_CHECKING": "False", "MARKER": "from-ansible-env"}
    captured = {}

    def _fake_tree(cmd, timeout, env):
        captured["env"] = env
        return 0, "", {"host1": {"ping": "pong"}}

    with (
        patch("infra_brain.tools.ansible._ansible_env", return_value=sentinel_env) as mock_env,
        patch("infra_brain.tools.ansible._run_ansible_tree", side_effect=_fake_tree),
    ):
        ansible_ping_tool.invoke({"target": "linux", "inventory": "/etc/ansible/hosts"})

    mock_env.assert_called_once_with()
    assert captured["env"] == sentinel_env


def test_ansible_inventory_tool_timeout_raises():
    import subprocess

    with patch(
        "infra_brain.tools.ansible.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ansible-inventory", timeout=60),
    ):
        with pytest.raises(subprocess.TimeoutExpired):
            ansible_inventory_tool.invoke({"inventory": "/etc/ansible/hosts"})


# ---------------------------------------------------------------------------
# MR-J (INV-1): getent / slurp / listen_ports_facts enrichment
# ---------------------------------------------------------------------------


def test_run_ansible_getent_passwd_calls_dedicated_module():
    fake_raw = {
        "web01": {
            "ansible_facts": {
                "getent_passwd": {"deploy": ["x", "1001", "1001", "", "/home/deploy", "/bin/bash"]}
            }
        }
    }
    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch(
            "infra_brain.tools.ansible._run_ansible_tree", return_value=(0, "", fake_raw)
        ) as mock_tree,
    ):
        result = run_ansible_getent(
            target="web01", inventory="/etc/ansible/hosts", database="passwd"
        )

    cmd = mock_tree.call_args[0][0]
    assert "ansible.builtin.getent" in cmd
    assert "database=passwd" in cmd
    assert result["web01"]["ansible_facts"]["getent_passwd"]["deploy"][5] == "/bin/bash"


def test_run_ansible_getent_rejects_unsupported_database():
    with pytest.raises(ValueError, match="unsupported getent database"):
        run_ansible_getent(target="web01", inventory="/etc/ansible/hosts", database="shadow")


def test_run_ansible_slurp_rejects_path_outside_allowlist():
    with pytest.raises(ValueError, match="not in the allow-list"):
        run_ansible_slurp(target="web01", inventory="/etc/ansible/hosts", path="/etc/shadow")


def test_run_ansible_slurp_decodes_base64_content():
    raw_text = "0 3 * * * /usr/local/bin/backup.sh\n"
    fake_raw = {
        "web01": {
            "content": base64.b64encode(raw_text.encode()).decode(),
            "encoding": "base64",
        }
    }
    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch(
            "infra_brain.tools.ansible._run_ansible_tree", return_value=(0, "", fake_raw)
        ) as mock_tree,
    ):
        result = run_ansible_slurp(
            target="web01", inventory="/etc/ansible/hosts", path="/var/spool/cron/crontabs/root"
        )

    cmd = mock_tree.call_args[0][0]
    assert "ansible.builtin.slurp" in cmd
    assert "src=/var/spool/cron/crontabs/root" in cmd
    assert result["web01"]["text"] == raw_text


def test_run_ansible_slurp_missing_file_returns_empty_not_raise():
    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch(
            "infra_brain.tools.ansible._run_ansible_tree",
            return_value=(2, "file not found", {}),
        ),
    ):
        result = run_ansible_slurp(
            target="web01", inventory="/etc/ansible/hosts", path="/etc/crontab"
        )
    assert result == {}


def test_run_ansible_listen_ports_calls_dedicated_module():
    fake_raw = {
        "web01": {
            "ansible_facts": {
                "tcp_listen": [{"address": "0.0.0.0", "port": 22, "name": "sshd", "pid": 123}],
                "udp_listen": [],
            }
        }
    }
    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch(
            "infra_brain.tools.ansible._run_ansible_tree", return_value=(0, "", fake_raw)
        ) as mock_tree,
    ):
        result = run_ansible_listen_ports(target="web01", inventory="/etc/ansible/hosts")

    cmd = mock_tree.call_args[0][0]
    assert "community.general.listen_ports_facts" in cmd
    normalized = result["web01"]["ansible_facts"]["ansible_listen_ports"]
    assert normalized == [{"port": 22, "protocol": "tcp", "name": "sshd", "state": "LISTEN"}]


def test_parse_user_crontab_skips_comments_and_blank_lines():
    text = "# comment\n\n*/5 * * * * /usr/bin/rotate.sh --quiet\n"
    rows = _parse_user_crontab(text, owner="root")
    assert rows == [
        {"owner": "root", "schedule": "*/5 * * * *", "command": "/usr/bin/rotate.sh --quiet"}
    ]


def test_parse_system_crontab_extracts_owner_column():
    text = "SHELL=/bin/sh\n17 *\t* * *\troot cd / && run-parts /etc/cron.hourly\n"
    rows = _parse_system_crontab(text)
    assert rows == [
        {"owner": "root", "schedule": "17 * * * *", "command": "cd / && run-parts /etc/cron.hourly"}
    ]


def test_parse_ss_listen_ports_extracts_port_and_process():
    stdout = (
        "State   Recv-Q  Send-Q   Local Address:Port    Peer Address:Port  Process\n"
        'LISTEN  0       128            0.0.0.0:22           0.0.0.0:*      users:(("sshd",pid=734,fd=3))\n'
    )
    rows = _parse_ss_listen_ports(stdout)
    assert rows == [{"port": 22, "protocol": "tcp", "name": "sshd", "state": "LISTEN"}]


# ---------------------------------------------------------------------------
# TRK-031: Linux posture gather tools — pending updates / certs / firewall /
# shares. Parsers are tested directly against representative payloads; the run
# functions are tested with a mocked subprocess to confirm apt+dnf (etc.) merge
# and that a non-zero rc (dnf check-updates returns 100 when updates exist) is
# tolerated rather than dropped.
# ---------------------------------------------------------------------------
from infra_brain.tools.ansible import (  # noqa: E402
    run_ansible_pending_updates,
    run_ansible_firewall_rules,
    run_ansible_shares,
    _parse_apt_upgradable,
    _parse_dnf_check_updates,
    _parse_iptables_rules,
    _parse_openssl_certs,
    _parse_exportfs,
    _parse_smb_net_conf,
)


def test_parse_apt_upgradable_extracts_versions_and_security_flag():
    text = (
        "Listing...\n"
        "nginx/jammy-updates,jammy-security 1.18.0-6ubuntu14.4 amd64 "
        "[upgradable from: 1.18.0-6ubuntu14.3]\n"
        "openssl/jammy-security 3.0.2-0ubuntu1.15 amd64 [upgradable from: 3.0.2-0ubuntu1.10]\n"
        "vim/jammy-updates 2:8.2.3995-1ubuntu2.15 amd64 [upgradable from: 2:8.2.3995-1ubuntu2.11]\n"
    )
    rows = {r["package"]: r for r in _parse_apt_upgradable(text)}
    assert set(rows) == {"nginx", "openssl", "vim"}
    assert rows["nginx"]["available_version"] == "1.18.0-6ubuntu14.4"
    assert rows["nginx"]["current_version"] == "1.18.0-6ubuntu14.3"
    assert rows["nginx"]["security"] is True
    assert rows["openssl"]["security"] is True
    assert rows["vim"]["security"] is False  # only jammy-updates, no security suite
    assert rows["vim"]["manager"] == "apt"


def test_parse_dnf_check_updates_extracts_name_and_security_repo():
    text = (
        "Last metadata expiration check: 0:12:34 ago on Mon 01 Jan 2026.\n"
        "\n"
        "kernel.x86_64                 5.14.0-362.el9          baseos\n"
        "openssl.x86_64                1:3.0.7-25.el9_3        rhel-9-appstream-security\n"
        "Obsoleting Packages\n"
        "oldpkg.noarch                 1.0-1.el9               updates\n"
    )
    rows = {r["package"]: r for r in _parse_dnf_check_updates(text)}
    assert set(rows) == {"kernel", "openssl"}  # Obsoleting section ignored
    assert rows["kernel"]["available_version"] == "5.14.0-362.el9"
    assert rows["kernel"]["current_version"] is None
    assert rows["kernel"]["security"] is False
    assert rows["openssl"]["security"] is True
    assert rows["openssl"]["manager"] == "dnf"


def test_parse_iptables_rules_policy_and_appended_rules():
    text = (
        "-P INPUT ACCEPT\n"
        "-P FORWARD DROP\n"
        "-N DOCKER\n"
        "-A INPUT -i lo -j ACCEPT\n"
        "-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT\n"
        "-A INPUT -p tcp -m tcp --dport 23 -j DROP\n"
    )
    rows = _parse_iptables_rules(text)
    assert all(r["table_name"] == "filter" and r["source"] == "iptables" for r in rows)
    by_text = {r["rule_text"]: r for r in rows}
    assert by_text["-P INPUT ACCEPT"]["chain"] == "INPUT"
    assert by_text["-P INPUT ACCEPT"]["action"] == "ACCEPT"
    assert by_text["-P FORWARD DROP"]["action"] == "DROP"
    assert by_text["-N DOCKER"]["chain"] == "DOCKER"
    assert by_text["-N DOCKER"]["action"] is None
    assert by_text["-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT"]["chain"] == "INPUT"
    assert by_text["-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT"]["action"] == "ACCEPT"
    assert by_text["-A INPUT -p tcp -m tcp --dport 23 -j DROP"]["action"] == "DROP"


def test_parse_openssl_certs_groups_records_and_parses_dates():
    text = (
        "subject=CN = example.com\n"
        "issuer=C = US, O = Let's Encrypt, CN = R3\n"
        "notBefore=Jan  1 00:00:00 2026 GMT\n"
        "notAfter=Apr  1 12:30:00 2026 GMT\n"
        "SHA1 Fingerprint=AB:CD:EF:12:34\n"
        "subject=CN = internal.local\n"
        "issuer=CN = Internal CA\n"
        "notBefore=Feb 15 00:00:00 2025 GMT\n"
        "notAfter=Feb 15 00:00:00 2027 GMT\n"
        "SHA1 Fingerprint=99:88:77\n"
    )
    rows = _parse_openssl_certs(text)
    assert len(rows) == 2
    first = rows[0]
    assert first["subject"] == "CN = example.com"
    assert first["issuer"] == "C = US, O = Let's Encrypt, CN = R3"
    assert first["thumbprint"] == "ABCDEF1234"
    assert first["store"] == "linux"
    assert first["not_after"].year == 2026 and first["not_after"].month == 4
    assert first["not_before"].year == 2026 and first["not_before"].month == 1
    assert rows[1]["thumbprint"] == "998877"


def test_parse_exportfs_merges_clients_per_path():
    text = (
        "/srv/nfs/data \t10.0.0.0/24(rw,sync,no_root_squash)\n"
        "/srv/nfs/pub  \t<world>(ro,sync)\n"
        "/srv/nfs/data \t198.51.100.16/24(ro,sync)\n"
    )
    rows = {r["name"]: r for r in _parse_exportfs(text)}
    assert set(rows) == {"/srv/nfs/data", "/srv/nfs/pub"}
    assert all(r["share_type"] == "nfs" for r in rows.values())
    data_perms = rows["/srv/nfs/data"]["permissions"]
    assert len(data_perms) == 2
    principals = {p["principal"] for p in data_perms}
    assert principals == {"10.0.0.0/24", "198.51.100.16/24"}
    assert rows["/srv/nfs/pub"]["path"] == "/srv/nfs/pub"


def test_parse_exportfs_continuation_lines_without_repeated_path():
    """GitLab finding: real exportfs -v repeats the path only on the FIRST
    line of a multi-client export; additional clients are on indented
    continuation lines that carry no path at all — e.g.:

        /srv/nfs/data 10.0.0.0/24(rw,sync,no_subtree_check)
                      192.168.1.0/24(ro,sync)

    Stripping the continuation line before matching (the old behavior) drops
    its leading whitespace, the single-token client-spec then fails the
    "path client" regex, and the line is silently skipped — understating who
    has access to the export.
    """
    text = (
        "/srv/nfs/data 10.0.0.0/24(rw,sync,no_subtree_check)\n"
        "              192.168.1.0/24(ro,sync)\n"
    )
    rows = {r["name"]: r for r in _parse_exportfs(text)}
    assert set(rows) == {"/srv/nfs/data"}
    perms = rows["/srv/nfs/data"]["permissions"]
    assert len(perms) == 2
    principals = {p["principal"] for p in perms}
    assert principals == {"10.0.0.0/24", "192.168.1.0/24"}
    accesses = {p["principal"]: p["access"] for p in perms}
    assert accesses["10.0.0.0/24"] == "rw,sync,no_subtree_check"
    assert accesses["192.168.1.0/24"] == "ro,sync"


def test_parse_smb_net_conf_skips_global_and_extracts_shares():
    text = (
        "[global]\n"
        "\tworkgroup = WORKGROUP\n"
        "\tserver string = Samba\n"
        "[data]\n"
        "\tpath = /srv/samba/data\n"
        "\tread only = no\n"
        "\tvalid users = alice, bob\n"
        "[public]\n"
        "\tpath = /srv/samba/public\n"
        "\tguest ok = yes\n"
    )
    rows = {r["name"]: r for r in _parse_smb_net_conf(text)}
    assert set(rows) == {"data", "public"}  # [global] skipped
    assert all(r["share_type"] == "smb" for r in rows.values())
    assert rows["data"]["path"] == "/srv/samba/data"
    principals = {p["principal"] for p in rows["data"]["permissions"]}
    assert {"alice", "bob"} <= principals


def test_run_ansible_pending_updates_merges_apt_and_dnf_tolerating_rc():
    apt_text = "Listing...\nnginx/jammy-security 1.1 amd64 [upgradable from: 1.0]\n"
    dnf_text = "kernel.x86_64  5.14.0  baseos\n"

    def _fake_tree(cmd, timeout, env):
        joined = " ".join(cmd)
        if "apt list --upgradable" in joined:
            return 0, "", {"web01": {"stdout": apt_text, "rc": 0}}
        if "check-updates" in joined:
            # dnf check-updates returns rc=100 when updates exist — must NOT drop
            return 100, "", {"rhel01": {"stdout": dnf_text, "rc": 100}}
        return 1, "", {}

    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch("infra_brain.tools.ansible._run_ansible_tree", side_effect=_fake_tree),
    ):
        result = run_ansible_pending_updates(target="all", inventory="/etc/ansible/hosts")

    apt_rows = result["web01"]["ansible_facts"]["pending_updates"]
    assert apt_rows[0]["package"] == "nginx" and apt_rows[0]["manager"] == "apt"
    dnf_rows = result["rhel01"]["ansible_facts"]["pending_updates"]
    assert dnf_rows[0]["package"] == "kernel" and dnf_rows[0]["manager"] == "dnf"


def test_run_ansible_firewall_rules_normalizes_per_host():
    fake_raw = {
        "web01": {"stdout": "-P INPUT DROP\n-A INPUT -p tcp --dport 22 -j ACCEPT\n", "rc": 0}
    }

    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch("infra_brain.tools.ansible._run_ansible_tree", return_value=(0, "", fake_raw)),
    ):
        result = run_ansible_firewall_rules(target="web01", inventory="/etc/ansible/hosts")

    rules = result["web01"]["ansible_facts"]["firewall_rules"]
    assert {r["action"] for r in rules} == {"DROP", "ACCEPT"}


def test_run_ansible_shares_merges_nfs_and_smb():
    def _fake_tree(cmd, timeout, env):
        joined = " ".join(cmd)
        if "exportfs" in joined:
            return 0, "", {"nfs01": {"stdout": "/srv/nfs/data \t10.0.0.0/24(rw)\n", "rc": 0}}
        if "net" in joined and "conf" in joined:
            return 0, "", {"smb01": {"stdout": "[data]\n\tpath = /srv/samba/data\n", "rc": 0}}
        return 0, "", {}

    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch("infra_brain.tools.ansible._run_ansible_tree", side_effect=_fake_tree),
    ):
        result = run_ansible_shares(target="all", inventory="/etc/ansible/hosts")

    nfs = result["nfs01"]["ansible_facts"]["host_shares"]
    assert nfs[0]["share_type"] == "nfs"
    smb = result["smb01"]["ansible_facts"]["host_shares"]
    assert smb[0]["share_type"] == "smb"


# --- TRK-031 review: adversarial inputs to the posture command tools --------
#
# Mirror the existing *_rejects_unsafe_target / *_rejects_...path guards: each
# posture run-function must reject an unsafe target and a traversal inventory
# path (both enforced by the shared _validate_target_and_inventory).
from infra_brain.tools.ansible import run_ansible_certificates  # noqa: E402

_POSTURE_RUN_FUNCS = [
    run_ansible_pending_updates,
    run_ansible_certificates,
    run_ansible_firewall_rules,
    run_ansible_shares,
]

_UNSAFE_TARGET = "host; rm -rf /"
_TRAVERSAL_INV = "../../etc/passwd"


@pytest.mark.parametrize("run_fn", _POSTURE_RUN_FUNCS)
def test_posture_run_rejects_unsafe_target(run_fn):
    with patch.object(ansible_mod, "get_settings", return_value=_facts_settings()):
        with pytest.raises(ValueError, match="unsafe characters"):
            run_fn(target=_UNSAFE_TARGET, inventory="/etc/ansible/hosts")


@pytest.mark.parametrize("run_fn", _POSTURE_RUN_FUNCS)
def test_posture_run_rejects_traversal_inventory_path(run_fn):
    with patch.object(ansible_mod, "get_settings", return_value=_facts_settings()):
        with pytest.raises(ValueError, match="unsafe characters"):
            run_fn(target="all", inventory=_TRAVERSAL_INV)


@pytest.mark.parametrize(
    "tool",
    [
        "ansible_pending_updates_tool",
        "ansible_certificates_tool",
        "ansible_firewall_rules_tool",
        "ansible_shares_tool",
    ],
)
def test_posture_tool_wraps_validation_error_as_toolexception(tool):
    tool_obj = getattr(ansible_mod, tool)
    with patch.object(ansible_mod, "get_settings", return_value=_facts_settings()):
        with pytest.raises(ToolException):
            tool_obj.invoke({"target": _UNSAFE_TARGET, "inventory": "/etc/ansible/hosts"})


# ---------------------------------------------------------------------------
# TRK-282: privileged posture commands (iptables -S / exportfs -v /
# net conf list) need root; a non-root ansible service account would
# otherwise silently return empty results indistinguishable from "this host
# genuinely has no firewall rules/shares". These tests pin: (1) exactly the
# three privileged calls carry -b/--become, the two unprivileged ones don't;
# (2) a sudo-denied stderr/msg signature raises a [become-denied]-marked
# error rather than returning an empty-looking success.
# ---------------------------------------------------------------------------
from infra_brain.tools.ansible import BECOME_DENIED_MARKER  # noqa: E402


def test_run_ansible_firewall_rules_uses_become():
    fake_raw = {"web01": {"stdout": "-P INPUT DROP\n", "rc": 0}}

    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch(
            "infra_brain.tools.ansible._run_ansible_tree", return_value=(0, "", fake_raw)
        ) as mock_tree,
    ):
        run_ansible_firewall_rules(target="web01", inventory="/etc/ansible/hosts")

    cmd = mock_tree.call_args[0][0]
    assert "-b" in cmd, "iptables -S needs root: the call must carry --become"


def test_run_ansible_shares_uses_become_for_both_nfs_and_smb():
    calls = []

    def _fake_tree(cmd, timeout, env):
        calls.append(list(cmd))
        if "exportfs" in " ".join(cmd):
            return 0, "", {"nfs01": {"stdout": "/srv/nfs/data \t10.0.0.0/24(rw)\n", "rc": 0}}
        return 0, "", {"smb01": {"stdout": "[data]\n\tpath = /srv/samba/data\n", "rc": 0}}

    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch("infra_brain.tools.ansible._run_ansible_tree", side_effect=_fake_tree),
    ):
        run_ansible_shares(target="all", inventory="/etc/ansible/hosts")

    assert len(calls) == 2, "expected one exportfs call and one net conf list call"
    for cmd in calls:
        assert "-b" in cmd, f"NFS/SMB share gathers need root: {cmd} must carry --become"


@pytest.mark.parametrize(
    "run_fn,cmd_fragment",
    [
        (run_ansible_pending_updates, "apt list --upgradable"),
        (run_ansible_certificates, "openssl"),
    ],
)
def test_unprivileged_posture_commands_do_not_use_become(run_fn, cmd_fragment):
    """apt/dnf/cert-enum read only world-readable state — no become needed.
    Not blanket: only the calls that actually need root (iptables/exportfs/
    net conf list, covered above) should carry -b."""
    calls = []

    def _fake_tree(cmd, timeout, env):
        calls.append(list(cmd))
        return 0, "", {}

    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch("infra_brain.tools.ansible._run_ansible_tree", side_effect=_fake_tree),
    ):
        run_fn(target="web01", inventory="/etc/ansible/hosts")

    assert calls, "expected at least one ansible invocation"
    for cmd in calls:
        assert "-b" not in cmd, f"{cmd_fragment} is unprivileged and must not carry --become"


def test_run_ansible_firewall_rules_raises_distinguishable_error_on_become_denied():
    """Sudo denied must surface as a [become-denied] error, NOT an empty-looking
    success — the whole point of TRK-282 (an unauthorized empty must not read
    identically to a genuinely-empty firewall ruleset)."""
    fake_raw = {
        "web01": {
            "msg": "Missing sudo password",
            "failed": True,
            "unreachable": False,
        }
    }

    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch("infra_brain.tools.ansible._run_ansible_tree", return_value=(1, "", fake_raw)),
    ):
        with pytest.raises(RuntimeError, match=re.escape(BECOME_DENIED_MARKER)):
            run_ansible_firewall_rules(target="web01", inventory="/etc/ansible/hosts")


def test_run_ansible_shares_raises_distinguishable_error_on_become_denied():
    def _fake_tree(cmd, timeout, env):
        if "exportfs" in " ".join(cmd):
            return (
                1,
                "",
                {
                    "nfs01": {
                        "msg": "",
                        "module_stderr": "sudo: a password is required\n",
                        "failed": True,
                    }
                },
            )
        return 0, "", {}

    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch("infra_brain.tools.ansible._run_ansible_tree", side_effect=_fake_tree),
    ):
        with pytest.raises(RuntimeError, match=re.escape(BECOME_DENIED_MARKER)):
            run_ansible_shares(target="all", inventory="/etc/ansible/hosts")


def test_become_denied_pattern_does_not_false_positive_on_real_command_not_found():
    """rc=127 (command missing on the host, e.g. no `net` binary installed) is a
    normal, TOLERATED outcome (see _run_ansible_command's docstring) and must
    NOT be misclassified as a become denial — only an actual sudo/PAM denial
    signature in msg/stderr should raise."""
    fake_raw = {"web01": {"stdout": "", "stderr": "bash: net: command not found", "rc": 127}}

    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch("infra_brain.tools.ansible._run_ansible_tree", return_value=(127, "", fake_raw)),
    ):
        # Must not raise: this is a genuine "no such command", not a become denial.
        result = run_ansible_firewall_rules(target="web01", inventory="/etc/ansible/hosts")
    assert result == {}


def test_become_refused_for_a_command_not_in_the_allowlist():
    """rev12 safety review MEDIUM-1: the execution-time backstop.

    The posture gate in callbacks/readonly.py validates its own copy of the
    command strings, never the argv this module builds — so a tampered module
    constant is out of the gate's reach. This pins the in-module backstop: a
    become=True call whose argv_cmd is not one of the individually enumerated
    read-only privileged commands must refuse BEFORE any subprocess runs.
    """
    import pytest as _pytest

    from infra_brain.tools import ansible as mod

    called = []
    with (
        patch.object(ansible_mod, "get_settings", return_value=_facts_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch(
            "infra_brain.tools.ansible._run_ansible_tree",
            side_effect=lambda *a, **k: called.append(1) or (0, "", {}),
        ),
    ):
        with _pytest.raises(RuntimeError, match="_BECOME_ALLOWED_CMDS"):
            mod._run_ansible_command("all", "/etc/ansible/hosts", "iptables -F", become=True)
    assert not called, "the refusal must fire before any subprocess invocation"
