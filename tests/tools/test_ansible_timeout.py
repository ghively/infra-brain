"""F-017: run_ansible_facts uses the configurable timeout, verifies the
inventory path, and scopes setup to ping-reachable hosts.

These fail against the old code: timeout was hardcoded 60, no path check,
no ping scoping.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from infra_brain.tools import ansible as ansible_mod


def _settings(**over):
    base = dict(
        ansible_inventory_path="/opt/infra-brain/repos/fleet-ansible",
        ansible_facts_timeout_seconds=270,
        ansible_ping_timeout_seconds=60,
        ansible_ping_scope_enabled=True,
        ansible_forks=30,
        ansible_connect_timeout_seconds=8,
        ansible_ssh_args="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _proc(stdout="{}", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def test_setup_uses_configured_timeout_and_scopes_to_reachable_hosts():
    ping_calls = []

    def fake_run(cmd, **kwargs):
        # Only the ping preflight (_reachable_hosts) still calls subprocess.run
        # directly — the setup gather goes through _run_ansible_tree, mocked below.
        ping_calls.append((cmd, kwargs))
        return _proc(
            stdout=(
                'hostA | SUCCESS => {"ping": "pong"}\n'
                "hostB | UNREACHABLE! => {}\n"
                'hostC | SUCCESS => {"ping": "pong"}\n'
            )
        )

    tree_calls = []

    def fake_tree(cmd, timeout, env):
        tree_calls.append((cmd, timeout, env))
        return 0, "", {"hostA": {}, "hostC": {}}

    with (
        patch.object(ansible_mod, "get_settings", return_value=_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch.object(ansible_mod.subprocess, "run", side_effect=fake_run),
        patch.object(ansible_mod, "_run_ansible_tree", side_effect=fake_tree),
    ):
        result = ansible_mod.run_ansible_facts("all", "")

    assert result == {"hostA": {}, "hostC": {}}
    setup_cmd, setup_timeout, _setup_env = tree_calls[-1]
    assert setup_timeout == 270
    assert setup_cmd[1] == "hostA,hostC"  # unreachable hostB excluded, sorted

    # The ping preflight must parallelize (--forks) and bound per-host SSH
    # connect time (-T) so a large fleet with unreachable hosts finishes inside
    # ansible_ping_timeout_seconds instead of stalling to a hard timeout.
    ping_cmd, ping_kwargs = ping_calls[0]
    assert ping_cmd[ping_cmd.index("-m") + 1] == "ping"
    assert ping_cmd[ping_cmd.index("--forks") + 1] == "30"
    assert ping_cmd[ping_cmd.index("-T") + 1] == "8"
    assert ping_kwargs["timeout"] == 60


def test_ansible_env_sets_host_key_checking_and_legacy_ssh_args():
    """_ansible_env must disable host-key checking (fresh container has no
    known_hosts) and export legacy ANSIBLE_SSH_ARGS when configured."""
    with patch.object(
        ansible_mod,
        "get_settings",
        return_value=_settings(ansible_ssh_args="-o HostKeyAlgorithms=+ssh-rsa"),
    ):
        env = ansible_mod._ansible_env()
    assert env["ANSIBLE_HOST_KEY_CHECKING"] == "False"
    assert env["ANSIBLE_SSH_ARGS"] == "-o HostKeyAlgorithms=+ssh-rsa"
    assert env["ANSIBLE_CACHE_PLUGIN"] == "memory"


def test_ansible_env_omits_ssh_args_when_empty():
    with patch.object(ansible_mod, "get_settings", return_value=_settings(ansible_ssh_args="")):
        env = ansible_mod._ansible_env()
    assert "ANSIBLE_SSH_ARGS" not in env


def test_missing_inventory_path_raises():
    with (
        patch.object(ansible_mod, "get_settings", return_value=_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=False),
    ):
        with pytest.raises(RuntimeError, match="inventory path does not exist"):
            ansible_mod.run_ansible_facts("all", "")


def test_no_reachable_hosts_raises():
    """GitLab #160: hosts matched the inventory (ping ran against them) but
    none were reachable — the message must carry ALL_HOSTS_UNREACHABLE_MARKER
    plus the unreachable count/sample, distinguishing this real
    connectivity outage from the zero-parsed-hosts config problem below."""
    with (
        patch.object(ansible_mod, "get_settings", return_value=_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch.object(
            ansible_mod.subprocess,
            "run",
            return_value=_proc(stdout="hostA | UNREACHABLE! => {}\n"),
        ),pytest.raises(RuntimeError) as exc_info
    ):
        ansible_mod.run_ansible_facts("all", "")

    message = str(exc_info.value)
    assert ansible_mod.ALL_HOSTS_UNREACHABLE_MARKER in message
    assert "all 1 host(s) unreachable" in message
    assert "hostA" in message


def test_zero_parsed_hosts_raises_marked_error():
    """GitLab #143: when the target pattern matches ZERO hosts in the
    inventory, ansible's ping preflight prints no per-host lines at all
    (neither SUCCESS nor UNREACHABLE) — distinct from the "hosts exist but
    are all unreachable" case above. This must fail fast with a message
    carrying ZERO_HOST_INVENTORY_MARKER (so callers can distinguish a
    misconfigured ANSIBLE_INVENTORY_PATH from a genuine connectivity outage),
    not the generic "no reachable hosts" text.
    """
    with (
        patch.object(ansible_mod, "get_settings", return_value=_settings()),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch.object(ansible_mod.subprocess, "run", return_value=_proc(stdout="")),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            ansible_mod.run_ansible_facts("all", "/opt/infra-brain/repos/fleet-ansible")

    message = str(exc_info.value)
    assert ansible_mod.ZERO_HOST_INVENTORY_MARKER in message
    assert "parsed 0 hosts" in message
    assert "/opt/infra-brain/repos/fleet-ansible" in message
    assert "no reachable hosts" not in message  # not the generic connectivity message


def test_ping_scoping_can_be_disabled():
    run_mock = MagicMock(return_value=_proc(stdout="{}"))
    with (
        patch.object(
            ansible_mod,
            "get_settings",
            return_value=_settings(ansible_ping_scope_enabled=False),
        ),
        patch.object(ansible_mod.os.path, "exists", return_value=True),
        patch.object(ansible_mod.subprocess, "run", run_mock),
    ):
        ansible_mod.run_ansible_facts("all", "")
    assert run_mock.call_count == 1  # no ping call
    cmd = run_mock.call_args[0][0]
    assert cmd[1] == "all"
