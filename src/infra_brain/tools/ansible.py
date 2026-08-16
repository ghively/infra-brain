import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime

from langchain_core.tools import ToolException, tool

from infra_brain.config import get_settings

logger = logging.getLogger(__name__)

_SAFE_TARGET = re.compile(r"^[\w,\-\.\:\/]+$")
# Windows paths use backslashes; allow them in inventory paths without loosening
# the guard against traversal sequences (the ".." check is separate).
_SAFE_PATH = re.compile(r"^[\w,\-\.\:\/\\]+$")

# GitLab #143: prefix marking a RuntimeError message as "inventory parsed to
# zero hosts" — a distinct, diagnosable condition from "hosts exist but are
# unreachable" (real connectivity/SSH failure). Callers (LinuxAgent.collect)
# check for this prefix to convert the failure into CollectorSkipped instead
# of a generic status="failed", mirroring WindowsAgent's CollectorSkipped
# self-skip gate for unconfigured credentials.
ZERO_HOST_INVENTORY_MARKER = "[inventory-zero-hosts]"

# GitLab #160: prefix marking a RuntimeError message as "the inventory
# matched hosts but every one of them was unreachable" — a real
# connectivity/SSH/credential outage, distinct from ZERO_HOST_INVENTORY_MARKER
# above (which means the target pattern matched nothing at all, before any
# connection was even attempted). Callers (LinuxAgent.collect) check for this
# prefix to relabel the failure distinctly instead of the generic "no
# reachable hosts" text, which previously read identically to any other
# ansible_facts_tool failure in collection_runs.error_message.
ALL_HOSTS_UNREACHABLE_MARKER = "[all-hosts-unreachable]"

# Prefix marking a RuntimeError message as "there is no inventory to read at
# all" — ANSIBLE_INVENTORY_PATH unset, or set to a path that does not exist on
# this host (e.g. the inventory volume was never mounted into the container).
# This is the same class as ZERO_HOST_INVENTORY_MARKER above — an absent
# dependency, not a runtime fault — and belongs in the same CollectorSkipped
# bucket. Without the marker it surfaced as a bare RuntimeError and LinuxAgent
# recorded status="failed", so a collector that was simply never wired up read
# exactly like a live SSH outage and re-failed on every scheduled run with no
# action that could ever clear it.
INVENTORY_UNAVAILABLE_MARKER = "[inventory-unavailable]"

# TRK-282: prefix marking a RuntimeError message as "a privileged posture
# command (run with --become) was denied sudo by the target host", a
# DISTINCT condition from that command genuinely producing zero rows (no
# firewall rules / no exports). Without this marker, a non-root ansible
# service account that lacks passwordless sudo makes run_ansible_firewall_rules
# / run_ansible_shares silently return {} for that host — indistinguishable
# from "this host really has no firewall rules configured" — which is exactly
# the latent regression TRK-282 flagged ahead of TRK-099 (fleet SSH)
# provisioning a non-root account. Callers (LinuxAgent._merge_enrichment_facts
# via _safe_invoke) treat any raised exception as "could not look" and skip
# the destructive delete-then-reinsert for that category (H-5), so promoting
# this from silent-empty to a raised, marked error both prevents wiping
# previously-collected rows and surfaces a diagnosable
# collection_runs.error_message instead of a quiet zero-row "success".
BECOME_DENIED_MARKER = "[become-denied]"

# Signatures sudo/ansible print when privilege escalation is attempted but the
# service account has no (passwordless) sudo rights for the target host. Kept
# permissive (several real-world sudo/PAM phrasings) rather than a single
# exact string, since the goal is "reliably catch the denial", not parse a
# fixed format. Matched against the module result's msg/stderr fields only —
# never against stdout, so a legitimate iptables/exportfs/net output that
# happens to contain one of these words cannot be misclassified (none of the
# real command outputs use this phrasing).
_BECOME_DENIED_RE = re.compile(
    r"missing sudo password"
    r"|sudo:.*password is required"
    r"|a password is required"
    r"|is not in the sudoers file"
    r"|sorry, (user|you) .*(is|are) not allowed"
    r"|no tty present"
    r"|incorrect password attempts",
    re.IGNORECASE,
)


def _become_denied_hosts(raw: dict) -> list[str]:
    """Return the hostnames whose per-host ansible result shows sudo/become
    was attempted and denied (see ``_BECOME_DENIED_RE``/``BECOME_DENIED_MARKER``
    above). Only inspects the module's own error-shaped fields — ``msg``
    (the become plugin's failure message when the module never ran),
    ``module_stderr``/``stderr`` (become-wrapper output) — never ``stdout``,
    which is the actual command's real (trusted) output.

    OPERATIONAL CONSTRAINT (rev12 safety review, MEDIUM-2 — deliberate): a
    single denied host aborts the WHOLE fleet gather. That direction is
    fail-safe on purpose — the reconcile scope for these probes is
    probe-granular, so returning partial data with denied hosts dropped would
    let the destructive delete-then-reinsert pass wipe the denied hosts' rows
    (the exact C-1 failure ReconcileScope exists to prevent). The cost is that
    one lagging host stalls firewall/shares collection fleet-wide during the
    TRK-099 rollout: provision the sudoers drop-in on ALL fleet hosts before
    expecting these probes live (runbook §1). Per-host scope granularity for
    these two probes is the eventual fix if the stall ever bites in practice.

    COVERAGE LIMIT: detection reads per-host tree files; a become failure
    ansible classifies such that NO tree file is written for that host is
    invisible here — that host silently degrades exactly as pre-TRK-282.
    """
    denied: list[str] = []
    for host, data in raw.items():
        if not isinstance(data, dict):
            continue
        text = " ".join(str(data.get(field, "")) for field in ("msg", "module_stderr", "stderr"))
        if text.strip() and _BECOME_DENIED_RE.search(text):
            denied.append(host)
    return denied


def _ansible_env(extra: dict | None = None) -> dict:
    """Base subprocess env for every ansible run.

    Centralizes two connection concerns that were missing when the collector
    runs inside a fresh container:

    * ``ANSIBLE_HOST_KEY_CHECKING=False`` — the container has no known_hosts,
      so the first connection to any host would otherwise fail on an unknown
      host key in non-interactive mode.
    * ``ANSIBLE_SSH_ARGS`` — many fleet hosts are old servers that only offer
      legacy host-key/kex algorithms (``ssh-rsa``/``ssh-dss``); modern OpenSSH
      rejects them ("no matching host key type"). settings.ansible_ssh_args
      re-enables the legacy algorithms so negotiation succeeds. This is a
      read-only connectivity concern; auth still requires a valid key.

    Callers' explicit env (``extra``) and any pre-set ANSIBLE_* in os.environ
    win via setdefault, so an operator can override per-deployment.
    """
    settings = get_settings()
    env = {**os.environ, "ANSIBLE_CACHE_PLUGIN": "memory", **(extra or {})}
    env.setdefault("ANSIBLE_HOST_KEY_CHECKING", "False")
    if settings.ansible_ssh_args:
        env.setdefault("ANSIBLE_SSH_ARGS", settings.ansible_ssh_args)
    return env


def _run_ansible_tree(cmd: list[str], timeout: float, env: dict) -> tuple[int, str, dict]:
    """Run an ad-hoc ``ansible`` command with ``--tree``, returning
    ``(returncode, stderr, per_host_dict)``.

    The ad-hoc ``ansible`` CLI's stdout is a human-readable report
    (``hostname | SUCCESS => {...}``), never valid JSON on its own — this is
    true even for a single host, not just for a multi-host target. Piping
    stdout straight into ``json.loads()`` (the previous approach here) always
    raised ``JSONDecodeError`` against a real Ansible install; it only ever
    "worked" against mocked subprocess output in tests. ``--tree <dir>``
    writes one JSON file per host — the module's real per-host result, with
    no wrapping text — which is the ad-hoc CLI's actual supported way to get
    structured output (``ansible-playbook`` has real callback plugins for
    this; the ad-hoc CLI does not).

    A per-host failure does not raise here — the caller decides whether an
    empty ``per_host`` (nothing written at all) is fatal. A host that failed
    its module call but has a tree file (e.g. a non-zero-exit shell command
    that still ran) is included; the caller's parsers already tolerate a
    missing key.
    """
    tmpdir = tempfile.mkdtemp(prefix="ansible-tree-")
    try:
        result = subprocess.run(
            [*cmd, "--tree", tmpdir],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        per_host: dict = {}
        for name in os.listdir(tmpdir):
            path = os.path.join(tmpdir, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path) as f:
                    per_host[name] = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
        return result.returncode, result.stderr, per_host
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _reachable_hosts(target: str, inv: str, env: dict) -> tuple[list[str], list[str]]:
    """Return ``(reachable, unreachable)`` hostnames within *target* per
    ``ansible -m ping``.

    Uses ``-o`` (one line per host): a success line looks like
    ``hostname | SUCCESS => {...}``. Unreachable/failed hosts are logged and
    excluded so one dead host cannot stall the whole facts gather (F-017).

    GitLab #143: the caller distinguishes two failure shapes from this pair —
    ``reachable=[] and unreachable=[]`` means the target pattern matched ZERO
    hosts in the inventory (nothing was even attempted; likely a
    misconfigured ``ANSIBLE_INVENTORY_PATH``), versus ``reachable=[] and
    unreachable=[...]`` which means hosts exist but a real connectivity/SSH
    problem prevented reaching any of them. Ansible prints no per-host lines
    at all in the former case (host pattern match happens before any
    connection attempt), so both lists being empty is diagnostic, not just
    "no reachable hosts" like the latter.
    """
    settings = get_settings()
    # --forks: parallelize across the fleet; -T: bound each host's SSH connect
    # so unreachable hosts fail fast instead of stalling a fork until the
    # subprocess-level ansible_ping_timeout_seconds kills the whole ping (F-017
    # follow-up: 175-host fleet timed out at 60s on the default 5 forks).
    cmd = [
        "ansible",
        target,
        "-m",
        "ping",
        "-o",
        "-i",
        inv,
        "--forks",
        str(settings.ansible_forks),
        "-T",
        str(settings.ansible_connect_timeout_seconds),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=settings.ansible_ping_timeout_seconds,
        env=env,
    )
    reachable: list[str] = []
    unreachable: list[str] = []
    for line in (result.stdout or "").splitlines():
        if "|" not in line:
            continue
        host = line.split("|", 1)[0].strip()
        if not host:
            continue
        if "SUCCESS" in line:
            reachable.append(host)
        else:
            unreachable.append(host)
    if unreachable:
        logger.warning(
            "ansible ping: %d unreachable host(s) excluded from setup: %s",
            len(unreachable),
            ",".join(sorted(unreachable)),
        )
    return reachable, unreachable


def run_ansible_facts(target: str, inventory: str) -> dict:
    """Runs ansible -m setup (read-only fact gathering) against target.

    F-017: the setup timeout is settings.ansible_facts_timeout_seconds (was a
    hardcoded 60s that killed every fleet-wide gather), and the target is
    first scoped to hosts that answer ping (unless
    ansible_ping_scope_enabled=False).
    """
    if not _SAFE_TARGET.match(target) or ".." in target:
        raise ValueError(f"[read-only] target contains unsafe characters: {target}")
    if inventory and (not _SAFE_PATH.match(inventory) or ".." in inventory):
        raise ValueError(f"[read-only] inventory path contains unsafe characters: {inventory}")
    if "playbook" in target.lower() or "playbook" in inventory.lower():
        raise ValueError("[read-only] playbook commands are not permitted")

    settings = get_settings()
    inv = inventory or settings.ansible_inventory_path
    if not inv:
        raise RuntimeError(
            f"{INVENTORY_UNAVAILABLE_MARKER} ANSIBLE_INVENTORY_PATH is not configured; "
            "set it to the mounted inventory path"
        )
    if not os.path.exists(inv):
        raise RuntimeError(
            f"{INVENTORY_UNAVAILABLE_MARKER} ansible inventory path does not exist: {inv}"
        )
    # Force in-memory fact caching so ambient fact_caching config cannot write to disk/Redis.
    env = _ansible_env()

    scoped_target = target
    if settings.ansible_ping_scope_enabled:
        reachable, unreachable = _reachable_hosts(target, inv, env)
        if not reachable and not unreachable:
            # GitLab #143: the target pattern matched ZERO hosts in the
            # inventory — Ansible never even attempted a connection (no
            # per-host ping lines at all), which is a config problem
            # (ANSIBLE_INVENTORY_PATH likely points above the real inventory
            # file, e.g. at a repo root instead of
            # inventory/production/hosts), not a connectivity outage. Fail
            # fast with a diagnosable message instead of the generic
            # "no reachable hosts" text below, which reads identically to a
            # genuine SSH/network failure.
            raise RuntimeError(
                f"{ZERO_HOST_INVENTORY_MARKER} Ansible inventory at '{inv}' parsed 0 hosts "
                f"for target {target!r} - path likely points above the inventory file "
                f"(expected e.g. inventory/production/hosts, not a directory with no "
                f"host-defining file at its root); no connection attempted"
            )
        if not reachable:
            # GitLab #160: hosts DID match the inventory pattern and the ping
            # preflight ran against all of them, but none answered — a real
            # SSH/network/credential outage, not a config problem. Include the
            # unreachable count and a truncated sample of hostnames so
            # collection_runs.error_message clearly distinguishes this from
            # the ZERO_HOST_INVENTORY_MARKER case above (0 hosts matched,
            # nothing was even attempted) — the two previously read
            # identically as a generic failure.
            sample = ",".join(sorted(unreachable)[:10])
            raise RuntimeError(
                f"{ALL_HOSTS_UNREACHABLE_MARKER} ansible ping: all {len(unreachable)} "
                f"host(s) unreachable in target {target!r} (inventory {inv}); "
                f"sample: {sample}"
            )
        scoped_target = ",".join(sorted(reachable))

    cmd = ["ansible", scoped_target, "-m", "setup", "-i", inv]
    rc, stderr, raw = _run_ansible_tree(cmd, settings.ansible_facts_timeout_seconds, env)
    if not raw and rc != 0:
        raise RuntimeError(f"ansible -m setup failed: {stderr}")
    return raw


@tool
def ansible_facts_tool(target: str, inventory: str = "") -> dict:
    """Gather Ansible facts from a host or group (read-only, -m setup only)."""
    try:
        return run_ansible_facts(target, inventory)
    except (ValueError, RuntimeError) as exc:
        raise ToolException(str(exc)) from exc


# ---------------------------------------------------------------------------
# MR-J (INV-1): Linux enrichment gather.
#
# docs/audit/FLEET_INVENTORY_AND_VSPHERE_AUDIT_2026-07-06.md found that
# LinuxAgent's detail writers (agents/linux.py) are "wired and ready" for
# ``ansible_getent_passwd`` / ``ansible_getent_group`` / ``ansible_listen_ports``
# / ``ansible_crontab`` but plain ``ansible -m setup`` never produces those
# keys, so LinuxUser/LinuxCron/LinuxPort stay empty forever. Each function
# below runs exactly ONE dedicated, narrow-parameter, non-command-execution
# Ansible module (allow-listed in callbacks/readonly.py alongside setup/ping):
#   - ansible.builtin.getent                  (passwd/group enumeration)
#   - ansible.builtin.slurp                   (single-file read, path
#     restricted to a hardcoded crontab-location allow-list — never
#     caller-supplied, so this cannot become a general file-read primitive)
#   - community.general.listen_ports_facts    (listening socket table)
# None of the three accepts a shell string or an arbitrary command, unlike
# ``-m command``/``-m shell`` which remain hard-denied.
# ---------------------------------------------------------------------------

_GETENT_DATABASES = ("passwd", "group")
# Fixed, non-caller-supplied slurp targets — root's crontab (Debian/RHEL differ
# on where the crontab spool file lives, so both are tried) and the system
# crontab. cron.d is intentionally NOT covered (would need ansible.builtin.find
# to enumerate the directory first); see MR-J report for the triage note.
_CRONTAB_SLURP_PATHS = (
    "/var/spool/cron/crontabs/root",  # Debian/Ubuntu
    "/var/spool/cron/root",  # RHEL/CentOS
    "/etc/crontab",  # system crontab (6-field: min hour dom mon dow user cmd)
)
_SYSTEM_CRONTAB_PATH = "/etc/crontab"


def _validate_target_and_inventory(target: str, inventory: str) -> str:
    """Shared safety validation + inventory resolution for the enrichment calls."""
    if not _SAFE_TARGET.match(target) or ".." in target:
        raise ValueError(f"[read-only] target contains unsafe characters: {target}")
    if inventory and (not _SAFE_PATH.match(inventory) or ".." in inventory):
        raise ValueError(f"[read-only] inventory path contains unsafe characters: {inventory}")
    settings = get_settings()
    inv = inventory or settings.ansible_inventory_path
    if not inv:
        raise RuntimeError(
            f"{INVENTORY_UNAVAILABLE_MARKER} ANSIBLE_INVENTORY_PATH is not configured; "
            "set it to the mounted inventory path"
        )
    if not os.path.exists(inv):
        raise RuntimeError(
            f"{INVENTORY_UNAVAILABLE_MARKER} ansible inventory path does not exist: {inv}"
        )
    return inv


def run_ansible_getent(target: str, inventory: str, database: str) -> dict:
    """Run ``ansible.builtin.getent`` (read-only NSS lookup, no shell).

    ``database`` is restricted to ``passwd``/``group`` — the only two the
    LinuxUser/LinuxCron writers consume. Returns the per-host
    ``{hostname: {"ansible_facts": {"getent_<database>": {...}}}}`` shape the
    real Ansible module produces.
    """
    if database not in _GETENT_DATABASES:
        raise ValueError(f"[read-only] unsupported getent database: {database!r}")
    inv = _validate_target_and_inventory(target, inventory)
    settings = get_settings()
    env = _ansible_env()
    cmd = [
        "ansible",
        target,
        "-m",
        "ansible.builtin.getent",
        "-a",
        f"database={database}",
        "-i",
        inv,
    ]
    rc, stderr, raw = _run_ansible_tree(cmd, settings.ansible_facts_timeout_seconds, env)
    if not raw and rc != 0:
        raise RuntimeError(f"ansible -m ansible.builtin.getent failed: {stderr}")
    return raw


def run_ansible_slurp(target: str, inventory: str, path: str) -> dict:
    """Run ``ansible.builtin.slurp`` (single read-only file read, no shell).

    ``path`` MUST be one of ``_CRONTAB_SLURP_PATHS`` — never caller-supplied —
    so this cannot be turned into an arbitrary-file-read primitive. Returns
    the per-host dict with ``content`` base64-decoded into ``text`` for
    convenience; a host with no such file simply fails that one ansible call
    (non-zero rc), which callers treat as "nothing there", not fatal.
    """
    if path not in _CRONTAB_SLURP_PATHS:
        raise ValueError(f"[read-only] slurp path not in the allow-list: {path!r}")
    inv = _validate_target_and_inventory(target, inventory)
    settings = get_settings()
    env = _ansible_env()
    cmd = [
        "ansible",
        target,
        "-m",
        "ansible.builtin.slurp",
        "-a",
        f"src={path}",
        "-i",
        inv,
    ]
    rc, stderr, raw = _run_ansible_tree(cmd, settings.ansible_facts_timeout_seconds, env)
    if not raw:
        # Missing file (no crontab for root, no /etc/crontab on some distros)
        # is the overwhelmingly common case — not a real error.
        logger.info("ansible slurp %s: no host produced a file (likely absent): %s", path, stderr)
        return {}
    for host_data in raw.values():
        content = host_data.get("content")
        if content:
            try:
                host_data["text"] = base64.b64decode(content).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - malformed base64 must not abort the gather
                host_data["text"] = ""
    return raw


def run_ansible_listen_ports(target: str, inventory: str) -> dict:
    """Run ``community.general.listen_ports_facts`` (read-only, no shell).

    This is the exact module the fleet-ansible playbook itself already uses
    (see docs/audit/FLEET_INVENTORY_AND_VSPHERE_AUDIT_2026-07-06.md), so the
    collection is confirmed available in the target environment. The module's
    own return shape (``ansible_facts.tcp_listen`` / ``udp_listen``, each a
    list of ``{address, port, name, pid, protocol}``) is normalized here into
    the flat ``ansible_listen_ports`` list LinuxAgent's writer already expects
    (matching the ``{port, protocol, name}`` shape it reads).
    """
    inv = _validate_target_and_inventory(target, inventory)
    settings = get_settings()
    env = _ansible_env()
    cmd = [
        "ansible",
        target,
        "-m",
        "community.general.listen_ports_facts",
        "-i",
        inv,
    ]
    rc, stderr, raw = _run_ansible_tree(cmd, settings.ansible_facts_timeout_seconds, env)
    if not raw and rc != 0:
        raise RuntimeError(f"ansible -m community.general.listen_ports_facts failed: {stderr}")
    for host_data in raw.values():
        facts = host_data.get("ansible_facts") or {}
        normalized: list[dict] = []
        for proto in ("tcp_listen", "udp_listen"):
            for entry in facts.get(proto, []) or []:
                normalized.append(
                    {
                        "port": entry.get("port"),
                        "protocol": "udp" if proto == "udp_listen" else "tcp",
                        "name": entry.get("name"),
                        "state": "LISTEN",
                    }
                )
        if normalized:
            facts["ansible_listen_ports"] = normalized
            host_data["ansible_facts"] = facts
    return raw


def _parse_user_crontab(text: str, owner: str) -> list[dict]:
    """Parse a per-user crontab (``min hour dom mon dow command``) into rows."""
    rows: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        rows.append({"owner": owner, "schedule": " ".join(parts[:5]), "command": parts[5]})
    return rows


def _parse_system_crontab(text: str) -> list[dict]:
    """Parse ``/etc/crontab`` (``min hour dom mon dow user command``) into rows."""
    rows: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if (
            not line
            or line.startswith("#")
            or line.startswith(("SHELL=", "PATH=", "MAILTO=", "HOME="))
        ):
            continue
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        rows.append({"owner": parts[5], "schedule": " ".join(parts[:5]), "command": parts[6]})
    return rows


_SS_LOCAL_ADDR_RE = re.compile(r"^\S+:(\d+|\*)$")
_SS_PROCESS_RE = re.compile(r'\(\("([^"]+)"')


def _parse_ss_listen_ports(stdout: str) -> list[dict]:
    """Fallback parser for raw ``ss -tlnp``-style text (kept for the case a
    caller has that text already); the primary INV-1 path uses the dedicated
    ``community.general.listen_ports_facts`` module instead, which returns
    structured JSON and needs no text parsing.
    """
    rows: list[dict] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("state", "netid")):
            continue
        fields = line.split()
        local = next((f for f in fields if _SS_LOCAL_ADDR_RE.match(f)), None)
        if not local:
            continue
        port_str = local.rsplit(":", 1)[-1]
        if port_str == "*":
            continue
        try:
            port = int(port_str)
        except ValueError:
            continue
        proc_match = _SS_PROCESS_RE.search(line)
        rows.append(
            {
                "port": port,
                "protocol": "tcp",
                "name": proc_match.group(1) if proc_match else None,
                "state": "LISTEN",
            }
        )
    return rows


@tool
def ansible_getent_tool(target: str, inventory: str = "", database: str = "passwd") -> dict:
    """Enumerate NSS passwd/group entries via ``ansible.builtin.getent`` (read-only)."""
    try:
        return run_ansible_getent(target, inventory, database)
    except (ValueError, RuntimeError) as exc:
        raise ToolException(str(exc)) from exc


@tool
def ansible_slurp_tool(target: str, inventory: str = "", path: str = _SYSTEM_CRONTAB_PATH) -> dict:
    """Read one crontab-location file via ``ansible.builtin.slurp`` (read-only)."""
    try:
        return run_ansible_slurp(target, inventory, path)
    except (ValueError, RuntimeError) as exc:
        raise ToolException(str(exc)) from exc


def run_ansible_crontab(target: str, inventory: str) -> dict:
    """Gather root + system crontabs via repeated ``ansible.builtin.slurp`` calls.

    Tries every path in ``_CRONTAB_SLURP_PATHS`` (root's crontab under both the
    Debian and RHEL spool layouts, plus ``/etc/crontab``) and merges whatever
    is found into the ``ansible_crontab`` shape LinuxCron's writer expects. A
    host missing one or more of these files (the common case — most hosts
    don't have a root crontab) simply contributes fewer rows; this never
    raises on a missing file (see ``run_ansible_slurp``).
    """
    merged: dict[str, dict] = {}
    for path in _CRONTAB_SLURP_PATHS:
        slurped = run_ansible_slurp(target, inventory, path)
        for hostname, host_data in slurped.items():
            text = host_data.get("text", "")
            if not text:
                continue
            rows = (
                _parse_system_crontab(text)
                if path == _SYSTEM_CRONTAB_PATH
                else _parse_user_crontab(text, owner="root")
            )
            if not rows:
                continue
            facts = merged.setdefault(hostname, {"ansible_facts": {}})["ansible_facts"]
            facts.setdefault("ansible_crontab", []).extend(rows)
    return merged


@tool
def ansible_crontab_slurp_tool(target: str, inventory: str = "") -> dict:
    """Gather root + system crontabs via ``ansible.builtin.slurp`` (read-only, no shell)."""
    try:
        return run_ansible_crontab(target, inventory)
    except (ValueError, RuntimeError) as exc:
        raise ToolException(str(exc)) from exc


@tool
def ansible_listen_ports_tool(target: str, inventory: str = "") -> dict:
    """List listening TCP/UDP ports via ``community.general.listen_ports_facts`` (read-only)."""
    try:
        return run_ansible_listen_ports(target, inventory)
    except (ValueError, RuntimeError) as exc:
        raise ToolException(str(exc)) from exc


# ---------------------------------------------------------------------------
# TRK-031 (W1-c): Linux posture enrichment gather — pending updates,
# certificates, firewall rules, shares.
#
# Unlike the MR-J trio (getent/slurp/listen_ports_facts), there is no dedicated
# read-only Ansible fact module for "packages upgradable", "enumerated certs",
# "live firewall ruleset", or "exported shares", so these run a FIXED,
# non-caller-supplied read-only command via ``ansible.builtin.command`` — the
# command string is a module-level constant per gather, the caller supplies
# ONLY target/inventory (both validated by ``_validate_target_and_inventory``,
# whose regexes reject shell metacharacters). This is read-only BY CONVENTION,
# the same posture CLAUDE.md documents for nmap / ``ip route``: the command
# only reads state, and every invocation is audit-logged by the callback chain.
# The parsers below (independently unit-tested) turn each command's stdout into
# the structured rows agents/linux.py persists.
# ---------------------------------------------------------------------------

# Fixed, non-caller-supplied read-only commands, one per gather.
#
# TRK-282 privilege audit (each classified needs-root / works-unprivileged and
# passed to _run_ansible_command's `become` accordingly):
#   apt list --upgradable   -> works-unprivileged: reads dpkg's status db and
#     the already-fetched apt lists cache, both world-readable; no root needed.
#   dnf --quiet check-updates -> works-unprivileged: reads the rpm db + cached
#     repo metadata; does not require root to just report available updates.
#   openssl cert-enum (find + openssl x509) -> works-unprivileged: every
#     directory searched (/etc/ssl/certs, /etc/pki/tls/certs,
#     /etc/pki/ca-trust/source/anchors) is a public trust store, world-readable
#     by design so any TLS client on the box can use it.
#   iptables -S -> NEEDS ROOT: reading the live ruleset requires CAP_NET_ADMIN
#     (netlink NETLINK_NETFILTER access); a non-root invocation fails with
#     "Permission denied (you must be root)" before printing anything.
#   exportfs -v -> NEEDS ROOT: talks to the kernel's nfsd export table (via
#     rpc.mountd / /proc/fs/nfsd), which is root-only regardless of whether
#     /etc/exports itself is world-readable.
#   net conf list -> NEEDS ROOT: reads Samba's registry-backed config
#     (registry.tdb under /var/lib/samba, normally root:root 0600); a non-root
#     caller gets a permission error opening that database, not empty output.
_PENDING_UPDATES_APT_CMD = "apt list --upgradable"
_PENDING_UPDATES_DNF_CMD = "dnf --quiet check-updates"
# Enumerate the common trust/store dirs and print each cert's identity fields.
_CERT_ENUM_CMD = (
    "find /etc/ssl/certs /etc/pki/tls/certs /etc/pki/ca-trust/source/anchors "
    "-type f ( -name *.pem -o -name *.crt ) "
    "-exec openssl x509 -noout -subject -issuer -startdate -enddate -fingerprint -in {} ;"
)
_FIREWALL_IPTABLES_CMD = "iptables -S"
_SHARES_NFS_CMD = "exportfs -v"
_SHARES_SMB_CMD = "net conf list"

#: The ONLY commands `_run_ansible_command` may run with become=True — an
#: execution-time backstop, not documentation. `callbacks/readonly.py`'s
#: posture gate validates its own copy of these strings, never the argv this
#: module actually hands the subprocess, so before this set existed a tampered
#: module constant (a bad merge, an agent-authored edit) would have run
#: whatever it said — and with become, run it as root. Membership is checked
#: at execution time in `_run_ansible_command`; extending privileged
#: collection means adding the constant HERE and to the gate's allowlist,
#: both, deliberately.
_BECOME_ALLOWED_CMDS = frozenset(
    {
        _FIREWALL_IPTABLES_CMD,
        _SHARES_NFS_CMD,
        _SHARES_SMB_CMD,
    }
)


def _run_ansible_command(target: str, inventory: str, argv_cmd: str, become: bool = False) -> dict:
    """Run one FIXED read-only command via ``ansible.builtin.command`` (no shell).

    ``argv_cmd`` is always a module-level constant (never caller-supplied), so
    there is no command-injection surface; ``target``/``inventory`` are
    validated by ``_validate_target_and_inventory``. Returns the per-host
    ``{hostname: {"stdout": ..., "rc": ...}}`` dict Ansible's JSON stdout
    callback produces.

    Non-zero overall rc is TOLERATED and the JSON still parsed: several of these
    read-only queries exit non-zero by design (``dnf check-updates`` → 100 when
    updates exist; ``iptables``/``exportfs``/``net`` → 127 on a host that lacks
    the tool). The per-host ``stdout`` is what matters; a host that produced
    nothing simply contributes no rows.

    TRK-282: ``become=True`` adds ``-b`` (ansible's ad-hoc CLI shorthand for
    ``--become``, defaulting to the ``sudo`` method), elevating ONLY the
    privilege the FIXED, non-caller-supplied ``argv_cmd`` above runs at — this
    cannot become a write primitive, since the command string itself is still
    exactly one of the module-level constants above (also independently
    re-verified against ``_POSTURE_READONLY_COMMANDS`` by
    ``callbacks/readonly.py``'s fail-closed posture gate, which is unaware of
    and unaffected by this flag — it validates the command text, not the
    privilege it runs with). When ``become=True``, every host's result is
    additionally scanned for a become-denied signature (see
    ``_become_denied_hosts``); if any host shows sudo was attempted and
    refused, this raises ``RuntimeError`` prefixed with
    ``BECOME_DENIED_MARKER`` instead of returning the (empty-looking, but
    actually "we were not authorized to look") per-host dict — the whole point
    being that an unauthorized-empty result must never be indistinguishable
    from a genuinely-empty one.
    """
    inv = _validate_target_and_inventory(target, inventory)
    settings = get_settings()
    env = _ansible_env()
    cmd = [
        "ansible",
        target,
        "-m",
        "ansible.builtin.command",
        "-a",
        argv_cmd,
        "-i",
        inv,
    ]
    if become:
        if argv_cmd not in _BECOME_ALLOWED_CMDS:
            raise RuntimeError(
                f"refusing become for {argv_cmd!r}: not in _BECOME_ALLOWED_CMDS "
                "(read-only privileged commands are individually enumerated; "
                "see the comment on that set)"
            )
        cmd.append("-b")
    _rc, _stderr, raw = _run_ansible_tree(cmd, settings.ansible_facts_timeout_seconds, env)
    if become:
        denied_hosts = _become_denied_hosts(raw)
        if denied_hosts:
            sample = ",".join(sorted(denied_hosts)[:10])
            raise RuntimeError(
                f"{BECOME_DENIED_MARKER} sudo/become denied for read-only command "
                f"{argv_cmd!r} on {len(denied_hosts)} host(s) (sample: {sample}); "
                "the ansible service account needs passwordless sudo for this "
                "command (see docs/runbooks/2026-07-20-live-env-enablement.md §1)"
            )
    return raw


def _parse_apt_upgradable(text: str) -> list[dict]:
    """Parse ``apt list --upgradable`` output into pending-update rows.

    Line shape: ``pkg/suite1,suite2 avail_ver arch [upgradable from: cur_ver]``.
    ``security`` is True when any suite token contains ``security``.
    """
    rows: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("Listing") or "/" not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pkg_suite = parts[0]
        package, _, suites = pkg_suite.partition("/")
        available = parts[1]
        current = None
        m = re.search(r"upgradable from:\s*([^\]]+)\]", line)
        if m:
            current = m.group(1).strip()
        rows.append(
            {
                "package": package,
                "current_version": current,
                "available_version": available,
                "security": "security" in suites.lower(),
                "manager": "apt",
            }
        )
    return rows


def _parse_dnf_check_updates(text: str) -> list[dict]:
    """Parse ``dnf check-updates`` output into pending-update rows.

    Line shape: ``name.arch  avail_ver  repo``. ``dnf`` does not report the
    installed version here (→ ``current_version`` None). ``security`` is True
    when the repo token contains ``security``. The trailing ``Obsoleting
    Packages`` section and metadata/header lines are ignored.
    """
    rows: list[dict] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Obsoleting Packages"):
            break
        if stripped.startswith(("Last metadata", "Security:", "Available Upgrades")):
            continue
        parts = stripped.split()
        # A real package line: name.arch  version  repo (exactly 3 columns, and
        # the first token carries the ``.arch`` suffix).
        if len(parts) != 3 or "." not in parts[0]:
            continue
        name_arch, available, repo = parts
        package = name_arch.rsplit(".", 1)[0]
        rows.append(
            {
                "package": package,
                "current_version": None,
                "available_version": available,
                "security": "security" in repo.lower(),
                "manager": "dnf",
            }
        )
    return rows


def _parse_iptables_rules(text: str) -> list[dict]:
    """Parse ``iptables -S`` output into firewall-rule rows (table 'filter').

    ``-P CHAIN TARGET`` (policy), ``-N CHAIN`` (user chain), and
    ``-A CHAIN ... -j TARGET`` (appended rule) are all captured. ``action`` is
    the ``-j`` target (or the policy target for ``-P``), else None.
    """
    rows: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        chain = None
        action = None
        if parts[0] == "-P" and len(parts) >= 3:
            chain, action = parts[1], parts[2]
        elif parts[0] == "-N" and len(parts) >= 2:
            chain = parts[1]
        elif parts[0] == "-A" and len(parts) >= 2:
            chain = parts[1]
            if "-j" in parts:
                j = parts.index("-j")
                if j + 1 < len(parts):
                    action = parts[j + 1]
        else:
            continue
        rows.append(
            {
                "table_name": "filter",
                "chain": chain,
                "rule_text": line,
                "action": action,
                "source": "iptables",
            }
        )
    return rows


def _parse_openssl_date(value: str) -> datetime | None:
    """Parse an openssl ``notBefore``/``notAfter`` date (``Mon  D HH:MM:SS YYYY GMT``)."""
    if not value:
        return None
    cleaned = " ".join(value.replace("GMT", "").split())
    try:
        return datetime.strptime(cleaned, "%b %d %H:%M:%S %Y").replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_openssl_certs(text: str) -> list[dict]:
    """Parse repeated ``openssl x509 -subject -issuer -startdate -enddate
    -fingerprint`` output into certificate rows.

    Each certificate's block begins at a ``subject=`` line; fields accumulate
    until the next ``subject=``. ``store`` is a fixed ``"linux"`` marker (the
    -noout output carries no file path).
    """
    rows: list[dict] = []
    cur: dict | None = None

    def _flush():
        if cur and cur.get("thumbprint"):
            rows.append(cur)

    for line in (text or "").splitlines():
        line = line.rstrip()
        if line.startswith("subject="):
            _flush()
            cur = {
                "store": "linux",
                "subject": line[len("subject=") :].strip(),
                "issuer": None,
                "thumbprint": "",
                "not_before": None,
                "not_after": None,
            }
        elif cur is None:
            continue
        elif line.startswith("issuer="):
            cur["issuer"] = line[len("issuer=") :].strip()
        elif line.startswith("notBefore="):
            cur["not_before"] = _parse_openssl_date(line[len("notBefore=") :].strip())
        elif line.startswith("notAfter="):
            cur["not_after"] = _parse_openssl_date(line[len("notAfter=") :].strip())
        elif "Fingerprint=" in line:
            fp = line.split("Fingerprint=", 1)[1]
            cur["thumbprint"] = fp.replace(":", "").strip()
    _flush()
    return rows


def _parse_exportfs(text: str) -> list[dict]:
    """Parse ``exportfs -v`` output into NFS share rows (one per exported path).

    Line shape: ``/path <client>(opts)``. When an export is shared with more
    than one client/network, real ``exportfs -v`` prints the path only on the
    first line and indents the remaining client entries on continuation lines
    that carry no path at all (e.g. ``/srv/nfs/data 10.0.0.0/24(rw)`` followed
    by a line that is just ``              192.168.1.0/24(ro)``). Those
    continuation lines are detected by their *original* leading whitespace
    (checked before stripping) and attributed to the most recently seen path.
    All client lines for the same path are merged into a single row's
    ``permissions`` list.
    """
    by_path: dict[str, dict] = {}
    last_path: str | None = None
    for raw_line in (text or "").splitlines():
        if not raw_line.strip():
            continue
        if raw_line[:1].isspace() and last_path is not None:
            # Continuation line: another client/network for the same export
            # path as the previous line (the path is not repeated here).
            path = last_path
            client_spec = raw_line.strip()
        else:
            line = raw_line.strip()
            m = re.match(r"^(\S+)\s+(.+)$", line)
            if not m:
                continue
            path, client_spec = m.group(1), m.group(2).strip()
        cm = re.match(r"^(.*?)\((.*)\)$", client_spec)
        if cm:
            principal, access = cm.group(1).strip(), cm.group(2).strip()
        else:
            principal, access = client_spec, ""
        row = by_path.setdefault(
            path,
            {
                "share_type": "nfs",
                "name": path,
                "path": path,
                "permissions": [],
            },
        )
        row["permissions"].append({"principal": principal, "access": access})
        last_path = path
    return list(by_path.values())


def _parse_smb_net_conf(text: str) -> list[dict]:
    """Parse ``net conf list`` (INI) output into SMB share rows.

    The ``[global]`` section is skipped; every other ``[section]`` is a share.
    ``valid users`` (comma-separated) become the permission principals, else a
    single ``everyone`` principal; ``access`` reflects the read-only setting.
    """
    sections: dict[str, dict] = {}
    order: list[str] = []
    current: str | None = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        sm = re.match(r"^\[(.+)\]$", stripped)
        if sm:
            current = sm.group(1).strip()
            if current.lower() != "global":
                sections.setdefault(current, {})
                order.append(current)
            continue
        if current is None or current.lower() == "global":
            continue
        if "=" in stripped:
            key, _, val = stripped.partition("=")
            sections[current][key.strip().lower()] = val.strip()

    rows: list[dict] = []
    for name in order:
        cfg = sections[name]
        read_only = cfg.get("read only", "no").strip().lower() in ("yes", "true")
        access = "ro" if read_only else "rw"
        users = [u.strip() for u in cfg.get("valid users", "").split(",") if u.strip()]
        principals = users or ["everyone"]
        rows.append(
            {
                "share_type": "smb",
                "name": name,
                "path": cfg.get("path"),
                "permissions": [{"principal": p, "access": access} for p in principals],
            }
        )
    return rows


def _merge_host_fact(dest: dict, host: str, key: str, rows: list) -> None:
    """Append *rows* under ``dest[host]['ansible_facts'][key]`` (best-effort)."""
    if not rows:
        return
    facts = dest.setdefault(host, {"ansible_facts": {}})["ansible_facts"]
    facts.setdefault(key, []).extend(rows)


def run_ansible_pending_updates(target: str, inventory: str) -> dict:
    """Gather pending package updates (apt + dnf) into the ``pending_updates`` key.

    Runs both ``apt list --upgradable`` and ``dnf check-updates``; a host only
    yields rows for the manager it actually has. Read-only by convention.
    """
    result: dict[str, dict] = {}
    apt_raw = _run_ansible_command(target, inventory, _PENDING_UPDATES_APT_CMD)
    for host, host_data in apt_raw.items():
        _merge_host_fact(
            result, host, "pending_updates", _parse_apt_upgradable(host_data.get("stdout", ""))
        )
    dnf_raw = _run_ansible_command(target, inventory, _PENDING_UPDATES_DNF_CMD)
    for host, host_data in dnf_raw.items():
        _merge_host_fact(
            result, host, "pending_updates", _parse_dnf_check_updates(host_data.get("stdout", ""))
        )
    return result


def run_ansible_certificates(target: str, inventory: str) -> dict:
    """Gather host certificates into the ``host_certificates`` key (read-only)."""
    result: dict[str, dict] = {}
    raw = _run_ansible_command(target, inventory, _CERT_ENUM_CMD)
    for host, host_data in raw.items():
        _merge_host_fact(
            result, host, "host_certificates", _parse_openssl_certs(host_data.get("stdout", ""))
        )
    return result


def run_ansible_firewall_rules(target: str, inventory: str) -> dict:
    """Gather firewall rules (iptables) into the ``firewall_rules`` key (read-only).

    TRK-282: ``become=True`` — reading the live ``iptables`` ruleset needs
    CAP_NET_ADMIN (root); see the privilege audit above _FIREWALL_IPTABLES_CMD.
    Still read-only: the command run under elevated privilege is the same
    fixed ``iptables -S`` string, which only lists rules, never mutates them.
    """
    result: dict[str, dict] = {}
    raw = _run_ansible_command(target, inventory, _FIREWALL_IPTABLES_CMD, become=True)
    for host, host_data in raw.items():
        _merge_host_fact(
            result, host, "firewall_rules", _parse_iptables_rules(host_data.get("stdout", ""))
        )
    return result


def run_ansible_shares(target: str, inventory: str) -> dict:
    """Gather NFS (exportfs) + SMB (net conf) shares into the ``host_shares`` key.

    TRK-282: both sub-gathers use ``become=True`` — ``exportfs -v`` needs root
    to query the kernel's nfsd export table, and ``net conf list`` needs root
    to read Samba's registry-backed config; see the privilege audit above
    _SHARES_NFS_CMD/_SHARES_SMB_CMD. Still read-only: both commands only list
    existing exports/shares, never create or remove one.
    """
    result: dict[str, dict] = {}
    nfs_raw = _run_ansible_command(target, inventory, _SHARES_NFS_CMD, become=True)
    for host, host_data in nfs_raw.items():
        _merge_host_fact(result, host, "host_shares", _parse_exportfs(host_data.get("stdout", "")))
    smb_raw = _run_ansible_command(target, inventory, _SHARES_SMB_CMD, become=True)
    for host, host_data in smb_raw.items():
        _merge_host_fact(
            result, host, "host_shares", _parse_smb_net_conf(host_data.get("stdout", ""))
        )
    return result


@tool
def ansible_pending_updates_tool(target: str, inventory: str = "") -> dict:
    """List pending package updates (apt/dnf) for Linux hosts (read-only)."""
    try:
        return run_ansible_pending_updates(target, inventory)
    except (ValueError, RuntimeError) as exc:
        raise ToolException(str(exc)) from exc


@tool
def ansible_certificates_tool(target: str, inventory: str = "") -> dict:
    """Enumerate host certificates from the trust stores (read-only)."""
    try:
        return run_ansible_certificates(target, inventory)
    except (ValueError, RuntimeError) as exc:
        raise ToolException(str(exc)) from exc


@tool
def ansible_firewall_rules_tool(target: str, inventory: str = "") -> dict:
    """List the live firewall ruleset (iptables) for Linux hosts (read-only)."""
    try:
        return run_ansible_firewall_rules(target, inventory)
    except (ValueError, RuntimeError) as exc:
        raise ToolException(str(exc)) from exc


@tool
def ansible_shares_tool(target: str, inventory: str = "") -> dict:
    """List NFS/SMB shares exported by Linux hosts (read-only)."""
    try:
        return run_ansible_shares(target, inventory)
    except (ValueError, RuntimeError) as exc:
        raise ToolException(str(exc)) from exc


def run_windows_ansible_facts(
    target: str,
    inventory: str,
    win_user: str,
    extra_env: dict | None = None,
) -> dict:
    """Run ``ansible -m win_setup`` against Windows hosts via WinRM.

    Security contract (F-021): the WinRM password is NEVER placed in argv.
    The caller must supply ``ANSIBLE_WIN_PASSWORD`` inside *extra_env*; this
    function forwards it to subprocess only via the ``env`` kwarg.  The
    command line carries the Jinja2 lookup template
    ``ansible_password={{ lookup('env','ANSIBLE_WIN_PASSWORD') }}`` so
    Ansible reads the value from the controller's environment at run-time —
    not from the process argument vector that appears in audit logs.
    """
    if not _SAFE_TARGET.match(target) or ".." in target:
        raise ValueError(f"[read-only] target contains unsafe characters: {target}")
    if inventory and (not _SAFE_PATH.match(inventory) or ".." in inventory):
        raise ValueError(f"[read-only] inventory path contains unsafe characters: {inventory}")
    if "playbook" in target.lower() or "playbook" in inventory.lower():
        raise ValueError("[read-only] playbook commands are not permitted")

    settings = get_settings()
    inv = inventory or settings.ansible_inventory_path
    if not inv:
        raise RuntimeError(
            f"{INVENTORY_UNAVAILABLE_MARKER} ANSIBLE_INVENTORY_PATH is not configured; "
            "set it to the mounted inventory path"
        )
    if not os.path.exists(inv):
        raise RuntimeError(
            f"{INVENTORY_UNAVAILABLE_MARKER} ansible inventory path does not exist: {inv}"
        )

    # Password lives only in the subprocess env dict — not in argv.
    env = _ansible_env(extra_env)

    # Jinja2 lookup reads ANSIBLE_WIN_PASSWORD from the controller env at run
    # time.  The literal password string never appears in this list.
    cmd = [
        "ansible",
        target,
        "-m",
        "win_setup",
        "-i",
        inv,
        "-e",
        f"ansible_user={win_user}",
        "-e",
        "ansible_password={{ lookup('env','ANSIBLE_WIN_PASSWORD') }}",
    ]
    rc, stderr, raw = _run_ansible_tree(cmd, settings.ansible_facts_timeout_seconds, env)
    if not raw and rc != 0:
        raise RuntimeError(f"ansible -m win_setup failed: {stderr}")
    return raw


@tool
def ansible_inventory_tool(inventory: str = "") -> dict:
    """
    List all groups and hosts in the Ansible inventory (read-only).
    Runs: ansible-inventory --list -i <inventory>
    Returns parsed JSON with full group/host structure.
    """
    inv = inventory or get_settings().ansible_inventory_path
    if inv and (not _SAFE_PATH.match(inv) or ".." in inv):
        raise ToolException(f"[read-only] inventory path contains unsafe characters: {inv}")
    cmd = ["ansible-inventory", "--list", "-i", inv]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise ToolException(f"ansible-inventory --list failed: {result.stderr}")
    return json.loads(result.stdout)


@tool
def ansible_ping_tool(target: str, inventory: str = "") -> dict:
    """
    Test connectivity to an Ansible host or group using -m ping (read-only).
    Returns dict of hostname -> {ping: pong} or error.
    """
    if not _SAFE_TARGET.match(target):
        raise ToolException(f"[read-only] target contains unsafe characters: {target}")
    inv = inventory or get_settings().ansible_inventory_path
    if inv and (not _SAFE_PATH.match(inv) or ".." in inv):
        raise ToolException(f"[read-only] inventory path contains unsafe characters: {inv}")
    cmd = ["ansible", target, "-m", "ping", "-i", inv]
    rc, stderr, raw = _run_ansible_tree(cmd, 120, _ansible_env())
    if not raw and rc != 0:
        return {"error": stderr}
    return raw
