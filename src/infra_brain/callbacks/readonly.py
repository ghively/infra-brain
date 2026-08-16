import json
import logging
import re
import uuid
from langchain_core.callbacks import BaseCallbackHandler
from infra_brain.config import get_settings
from infra_brain.db.models import AuditLog
from infra_brain.db.session import get_session

log = logging.getLogger(__name__)

#  MR-J (INV-1): three additional dedicated, narrow-parameter fact-gathering
#  modules are allow-listed alongside setup/ping/gather_facts so the
#  LinuxAgent enrichment gather (getent/listen_ports/crontab-via-slurp — see
#  agents/linux.py, tools/ansible.py) can run through the same boundary gate
#  as every other Ansible call. None of the three can execute an arbitrary
#  command or accept a caller-supplied shell string:
#    - ansible.builtin.getent   — reads NSS databases (passwd/group); no exec.
#    - ansible.builtin.slurp    — reads one file's bytes; no exec. The calling
#      code in tools/ansible.py additionally restricts ``src`` to a hardcoded
#      allow-list of crontab paths, so this does not become a general
#      arbitrary-file-read primitive.
#    - community.general.listen_ports_facts — parses the kernel's listening
#      socket table; no exec, no free-form args.
#  The DENY list below (command/shell/raw/script, -e/--extra-vars, shell
#  metacharacters, --become) is UNCHANGED — these three additions do not
#  touch it and remain subject to it.
ALLOWED_ANSIBLE_PATTERNS = re.compile(
    r"ansible(-playbook)?\s+.*(-m\s+(setup|ping|gather_facts|"
    r"ansible\.builtin\.getent|ansible\.builtin\.slurp|"
    r"community\.general\.listen_ports_facts)|--list-hosts|--check|--syntax-check)"
)
_ANSIBLE_EXTRA_VARS = re.compile(r"(^|\s)(-e|--extra-vars)(\s|=|$)")

# Ansible DENY list — checked FIRST; if matched, always deny regardless of allow-pattern.
_ANSIBLE_DENY = re.compile(
    r"(^|\s)(-e\b|-e\S|--extra-vars)"  # extra-vars incl -eFOO (no space)
    r"|(^|\s)-m\s+(command|shell|raw|script|ansible\.builtin\.(command|shell|raw|script))\b"
    r"|--become\b|(^|\s)-K\b|--ask-become-pass\b|--ask-pass\b"
    r"|[;&|`]|\$\(",  # shell chaining / substitution
    re.IGNORECASE,
)

# --- TRK-031 review remediation: posture command-tool gate ------------------
#
# The four Linux posture tools (agents/linux.py, tools/ansible.py) legitimately
# invoke ``ansible.builtin.command`` — which ``_ANSIBLE_DENY`` above forbids in
# general — with a FIXED, non-caller-supplied read-only command. The generic
# name-based reconstruction in on_tool_start cannot recognize that: their names
# carry no getent/slurp/listen_ports keyword, so without this they would fall
# into the fail-open ``else: setup`` branch and be waved through as a FAKE
# ``-m setup`` (zero enforcement). They are instead handled by a DEDICATED,
# fail-CLOSED branch that reconstructs the REAL ``-m ansible.builtin.command``
# invocation and requires the command to be one of these exact strings.
#
# ``_POSTURE_READONLY_COMMANDS`` is an INDEPENDENT second copy of the command
# constants in tools/ansible.py — deliberately NOT imported — so the gate
# enforces read-only-ness on its OWN authority over what the LLM may ASK for.
# HONEST LIMIT (corrected at the rev12 safety review): the tool input carries
# no command string, so this gate validates its own copy, never the argv
# tools/ansible.py actually builds — a tampered constant in that module is
# OUT of this gate's reach. The execution-time backstop for that case lives
# in tools/ansible.py itself: ``_BECOME_ALLOWED_CMDS`` membership is asserted
# at the point ``-b`` is appended, so a tampered constant cannot silently run
# privileged. The two lists must be extended together, deliberately.
#
# TRK-282: tools/ansible.py's iptables/exportfs/net-conf-list calls now run
# with ansible's ``-b``/``--become`` (root is required to read those three —
# see the privilege audit next to _FIREWALL_IPTABLES_CMD in tools/ansible.py).
# This gate is deliberately UNCHANGED by that: it validates the input JSON's
# target/inventory/command against this fixed allow-list, never the real
# subprocess argv tools/ansible.py builds, so it never sees (and does not need
# to see) the ``-b`` flag — become only changes the PRIVILEGE the
# already-allow-listed, still-fixed command runs at, not WHICH command can
# run. The blanket ``--become\b`` deny in ``_ANSIBLE_DENY`` above is likewise
# untouched: it still blocks any attempt to smuggle become into an arbitrary
# reconstructed ansible command string outside this dedicated fail-closed
# branch.
_POSTURE_CERT_CMD = (
    "find /etc/ssl/certs /etc/pki/tls/certs /etc/pki/ca-trust/source/anchors "
    "-type f ( -name *.pem -o -name *.crt ) "
    "-exec openssl x509 -noout -subject -issuer -startdate -enddate -fingerprint -in {} ;"
)
_POSTURE_READONLY_COMMANDS = frozenset(
    {
        "apt list --upgradable",
        "dnf --quiet check-updates",
        _POSTURE_CERT_CMD,
        "iptables -S",
        "exportfs -v",
        "net conf list",
    }
)
# Each posture command-tool NAME -> the exact command(s) it is known to run.
# Used to reconstruct the real invocation when the tool (as normal) passes only
# target/inventory and no explicit command.
_POSTURE_COMMAND_TOOLS = {
    "ansible_pending_updates_tool": ("apt list --upgradable", "dnf --quiet check-updates"),
    "ansible_certificates_tool": (_POSTURE_CERT_CMD,),
    "ansible_firewall_rules_tool": ("iptables -S",),
    "ansible_shares_tool": ("exportfs -v", "net conf list"),
}
# Shell metacharacters / traversal in the caller-supplied target/inventory —
# gate-side defense-in-depth (the tool also validates these via its own regex).
_POSTURE_ARG_DENY = re.compile(r"[;&|`$()<>]|\.\.")

MUTATING_HTTP_VERBS = {"POST", "PUT", "PATCH", "DELETE"}

# Matches a mutating SQL verb ANYWHERE via word boundaries (not just at string start).
_SQL_MUTATING = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|REPLACE|MERGE|UPSERT|"
    r"GRANT|REVOKE|CALL|EXEC|EXECUTE|COPY|RENAME|REINDEX|VACUUM)\b",
    re.IGNORECASE,
)

# Strip block comments (/* ... */) — use non-greedy, dot-all
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# Strip line comments (-- ...)
_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL block and line comments, replacing with a space."""
    sql = _SQL_BLOCK_COMMENT.sub(" ", sql)
    sql = _SQL_LINE_COMMENT.sub(" ", sql)
    return sql


# --- L-4: sanctioned-write allowlist boundary matching ----------------------
#
# The mutating-verb bypass below used a bare ``url.startswith(prefix)``. The
# prefixes come straight from settings — agents/remediation.py passes
# ``[settings.gitlab_url]`` and agents/notification.py the Jira/Confluence base
# URLs — and those are normally stored WITHOUT a trailing slash. So with
# ``gitlab_url="https://gitlab.example.com"``, a POST to
# ``https://gitlab.example.com.evil.net/...`` shares that prefix and was waved
# straight through the read-only boundary to an attacker-controlled host.
# ``url.startswith("")`` is likewise always True, so a single unset settings
# value degraded the whole allowlist into allow-all.
#
# Matching now requires the prefix to end on a real URL boundary. This is NOT
# a strict subset/superset of the old bare ``url.startswith(prefix)`` — it is
# narrower on the attacker-relevant axis (lookalike hosts / userinfo / bare
# suffixes are now denied) but wider on three harmless axes, because the
# prefix is normalized with ``.rstrip("/")`` before matching where the old
# code compared it raw:
#
#   prefix='https://jira.example.com/'          url='https://jira.example.com'          old=False new=True
#   prefix='https://jira.example.com/'          url='https://jira.example.com?x=1'       old=False new=True
#   prefix='https://gitlab.example.com/api/v4/' url='https://gitlab.example.com/api/v4'  old=False new=True
#
# All three widened cases are the exact same origin AND the exact same path
# prefix as an already-sanctioned write target — the only thing that changed
# is tolerance for a trailing slash on the *configured* prefix, which the
# settings value may or may not carry. None of them admit a new host, path,
# or boundary an attacker could exploit — the STRUCTURAL fix (denying
# lookalike hosts sharing a bare textual prefix) is unrelated to this
# trailing-slash normalization and is unaffected by it. See
# ``tests/callbacks/test_readonly_whitelist_boundary.py`` for tests pinning
# all three shapes above plus the full lookalike-host denial matrix.
# Do not relax the *boundary* check back to a bare startswith.
_URL_BOUNDARY_CHARS = ("/", "?", "#")


def _url_matches_whitelist_prefix(url: str, prefix: str) -> bool:
    """True only if ``url`` is ``prefix`` itself or a descendant of it.

    A trailing slash on the configured prefix is insignificant
    (``https://jira.example.com/`` and ``https://jira.example.com`` behave
    identically). Anything else immediately following the prefix must be a
    path / query / fragment separator — never an additional host label
    (``.evil.net``), userinfo (``@evil.net``), port, or bare suffix.
    An empty/falsy prefix never matches anything.
    """
    if not url or not prefix or not isinstance(url, str) or not isinstance(prefix, str):
        return False
    normalized = prefix.rstrip("/")
    if not normalized:
        return False
    if not url.startswith(normalized):
        return False
    remainder = url[len(normalized) :]
    return remainder == "" or remainder[0] in _URL_BOUNDARY_CHARS


class ReadOnlyToolValidator(BaseCallbackHandler):
    # F-004.1: langchain-core swallows handler exceptions unless raise_error is
    # True. Without this, the PermissionError raised in _deny() was logged as a
    # warning by the CallbackManager and execution CONTINUED — the read-only
    # guarantee did not hold. Never set this back to False.
    raise_error = True

    def __init__(self, agent_name: str, whitelisted_post: list[str] | None = None):
        super().__init__()
        self.agent_name = agent_name
        self.whitelisted_post = whitelisted_post or []

    def _deny(self, tool: str, reason: str, run_id: uuid.UUID):
        with get_session() as session:
            session.add(
                AuditLog(
                    id=uuid.uuid4(),
                    agent=self.agent_name,
                    tool=tool,
                    input_hash="",
                    allowed=False,
                    denial_reason=reason,
                )
            )
            session.commit()
        if get_settings().scan_readonly_enforce:
            raise PermissionError(f"[read-only] {reason}")
        else:
            log.error("[read-only WARN] %s", reason)

    def on_tool_start(self, serialized: dict, input_str: str, *, run_id: uuid.UUID, **kwargs):
        tool_name = serialized.get("name", "")
        try:
            inp = json.loads(input_str)
        except (json.JSONDecodeError, TypeError):
            inp = {}

        # HTTP verb check
        method = inp.get("method", "").upper()
        url = inp.get("url", "")
        if method in MUTATING_HTTP_VERBS:
            # L-4: boundary-anchored, NOT a bare startswith — see
            # _url_matches_whitelist_prefix above for why.
            if any(_url_matches_whitelist_prefix(url, prefix) for prefix in self.whitelisted_post):
                return
            self._deny(tool_name, f"{method} {url} is not read-only", run_id)
            return

        # TRK-031 review remediation: the posture command-tools run
        # ``ansible.builtin.command`` (globally denied) with a FIXED read-only
        # command, so they get a DEDICATED, fail-CLOSED branch here rather than
        # the generic reconstruction below — which would fail OPEN to a fake
        # ``-m setup`` for their names. Every command such a tool is known to
        # run (plus any command explicitly present in the input) must be
        # byte-identical to an entry in ``_POSTURE_READONLY_COMMANDS``; anything
        # else is DENIED. See the ``_POSTURE_*`` declarations above.
        if tool_name in _POSTURE_COMMAND_TOOLS:
            _target = inp.get("target", "") or ""
            _inv = inp.get("inventory", "") or ""
            if _POSTURE_ARG_DENY.search(_target) or _POSTURE_ARG_DENY.search(_inv):
                self._deny(
                    tool_name,
                    f"unsafe characters in posture tool args: "
                    f"target={_target!r} inventory={_inv!r}",
                    run_id,
                )
                return
            candidate_cmds = list(_POSTURE_COMMAND_TOOLS[tool_name])
            presented = inp.get("command")
            if isinstance(presented, str) and presented:
                candidate_cmds.append(presented)
            for _cmd in candidate_cmds:
                if _cmd not in _POSTURE_READONLY_COMMANDS:
                    reconstructed = (
                        f'ansible {_target} -m ansible.builtin.command -a "{_cmd}" -i {_inv}'
                    )
                    self._deny(
                        tool_name,
                        f"posture command tool '{tool_name}' presented a "
                        f"non-allowlisted command: {reconstructed}",
                        run_id,
                    )
                    return
            return

        # Ansible check — deny-list runs FIRST; if matched, always deny.
        # ansible_facts_tool / ansible_ping_tool / the MR-J enrichment tools pass
        # structured args (target, inventory), not a raw command string.
        # Reconstruct one so the deny/allow regexes apply uniformly regardless
        # of invocation style. The MR-J enrichment tools (getent/slurp/
        # listen_ports) each map 1:1 to exactly one allow-listed module via the
        # keyword branches below; ansible_facts_tool and ansible_inventory_tool
        # legitimately fall through to the ``else: setup`` default (see the
        # SECURITY follow-up note there). The posture command-tools do NOT reach
        # here — they are fully handled by the fail-closed branch above.
        command = inp.get("command", "")
        if not command and "ansible" in tool_name.lower():
            _target = inp.get("target", "")
            _inv = inp.get("inventory", "")
            _name = tool_name.lower()
            if "ping" in _name:
                _mod = "ping"
            elif "getent" in _name:
                _mod = "ansible.builtin.getent"
            elif "slurp" in _name:
                _mod = "ansible.builtin.slurp"
            elif "listen_ports" in _name:
                _mod = "community.general.listen_ports_facts"
            else:
                # SECURITY follow-up (TRK-031 review): this default is fail-OPEN
                # — an unrecognized ansible tool name is reconstructed as the
                # allow-listed ``-m setup`` and thus permitted. It is retained
                # (not flipped to deny) because two existing legitimate tools
                # depend on it: ansible_facts_tool (genuinely runs ``-m setup``)
                # and ansible_inventory_tool (runs ``ansible-inventory --list``,
                # which reconstructs to a benign allow-listed string). Flipping
                # to deny-by-default would break both. The posture command-tools
                # that motivated this review are already handled fail-CLOSED
                # above, so they never reach here. A future hardening could
                # replace this with an explicit setup-family allow-set.
                _mod = "setup"
            command = f"ansible {_target} -m {_mod} -i {_inv}"
        if "ansible" in command.lower():
            if _ANSIBLE_DENY.search(command):
                self._deny(
                    tool_name, f"Ansible command not in read-only allowlist: {command}", run_id
                )
            elif not ALLOWED_ANSIBLE_PATTERNS.search(command):
                self._deny(
                    tool_name, f"Ansible command not in read-only allowlist: {command}", run_id
                )

        # SQL tool check: block mutating statements
        if "sql" in tool_name.lower():
            # input_str may be raw SQL or a JSON-wrapped dict — extract query text
            raw_sql = ""
            all_string_values: list[str] = []

            if isinstance(input_str, str):
                try:
                    parsed = json.loads(input_str)
                    if isinstance(parsed, dict):
                        raw_sql = (
                            parsed.get("query") or parsed.get("sql") or parsed.get("input") or ""
                        )
                        # Collect all string values for secondary scan
                        all_string_values = [v for v in parsed.values() if isinstance(v, str)]
                    else:
                        raw_sql = input_str
                except (json.JSONDecodeError, TypeError):
                    raw_sql = input_str
            elif isinstance(input_str, dict):
                raw_sql = (
                    input_str.get("query") or input_str.get("sql") or input_str.get("input") or ""
                )
                all_string_values = [v for v in input_str.values() if isinstance(v, str)]

            # Normalize: strip comments before scanning
            normalized_sql = _strip_sql_comments(raw_sql)

            if _SQL_MUTATING.search(normalized_sql):
                reason = f"Mutating SQL blocked for tool '{tool_name}': {raw_sql[:80]}"
                self._deny(tool_name, reason, run_id)
                return

            # Secondary scan: check ALL string values in a JSON dict for mutating verbs.
            # This catches mutations hidden in non-standard keys (e.g. "statement").
            for val in all_string_values:
                if val == raw_sql:
                    continue  # already scanned above
                normalized_val = _strip_sql_comments(val)
                if _SQL_MUTATING.search(normalized_val):
                    reason = f"Mutating SQL blocked for tool '{tool_name}': {val[:80]}"
                    self._deny(tool_name, reason, run_id)
                    return
