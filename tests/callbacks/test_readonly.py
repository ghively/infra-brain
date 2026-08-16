import uuid
import pytest
from unittest.mock import patch, MagicMock
from infra_brain.callbacks.readonly import ReadOnlyToolValidator


def test_get_request_allowed():
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        h = ReadOnlyToolValidator(agent_name="TestAgent")
        # Should not raise
        h.on_tool_start(
            {"name": "HttpTool"},
            '{"method": "GET", "url": "https://gitlab.example.com/api/v4/projects"}',
            run_id=uuid.uuid4(),
        )


def test_post_request_denied_when_not_whitelisted():
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        h = ReadOnlyToolValidator(agent_name="TestAgent")
        with pytest.raises(PermissionError, match="read-only"):
            h.on_tool_start(
                {"name": "HttpTool"},
                '{"method": "POST", "url": "https://gitlab.example.com/api/v4/projects"}',
                run_id=uuid.uuid4(),
            )


def test_post_allowed_for_whitelisted_url():
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        h = ReadOnlyToolValidator(
            agent_name="TestAgent", whitelisted_post=["https://jira.example.com"]
        )
        # Should not raise
        h.on_tool_start(
            {"name": "JiraTool"},
            '{"method": "POST", "url": "https://jira.example.com/rest/api/3/issue"}',
            run_id=uuid.uuid4(),
        )


def test_ansible_non_check_denied():
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        h = ReadOnlyToolValidator(agent_name="TestAgent")
        with pytest.raises(PermissionError, match="read-only"):
            h.on_tool_start(
                {"name": "AnsibleTool"},
                '{"command": "ansible-playbook site.yml"}',
                run_id=uuid.uuid4(),
            )


def test_ansible_check_allowed():
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        h = ReadOnlyToolValidator(agent_name="TestAgent")
        h.on_tool_start(
            {"name": "AnsibleTool"},
            '{"command": "ansible all -m setup -i inventory/"}',
            run_id=uuid.uuid4(),
        )


def test_ansible_playbook_check_allowed():
    """ansible-playbook --check is a dry-run and must be allowed."""
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        h = ReadOnlyToolValidator(agent_name="TestAgent")
        # Should NOT raise — ansible-playbook with --check is a dry-run
        h.on_tool_start(
            {"name": "ansible"},
            '{"command": "ansible-playbook site.yml --check"}',
            run_id=uuid.uuid4(),
        )


def test_sql_mutation_blocked():
    """SQL INSERT/UPDATE/DELETE must be blocked for sql_db_query tool."""
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        handler = ReadOnlyToolValidator(agent_name="IntegrationAgent")
        with pytest.raises(PermissionError, match="Mutating SQL"):
            handler.on_tool_start(
                serialized={"name": "sql_db_query"},
                input_str="INSERT INTO resources (name) VALUES ('x')",
                run_id=uuid.uuid4(),
            )


def test_sql_select_allowed():
    """SQL SELECT must be allowed."""
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        handler = ReadOnlyToolValidator(agent_name="IntegrationAgent")
        # Should not raise
        handler.on_tool_start(
            serialized={"name": "sql_db_query"},
            input_str="SELECT * FROM resources WHERE domain = 'linux'",
            run_id=uuid.uuid4(),
        )


def test_warn_only_when_not_enforcing():
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        mock_settings.return_value.scan_readonly_enforce = False
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        h = ReadOnlyToolValidator(agent_name="TestAgent")
        # Should not raise — only warns
        h.on_tool_start(
            {"name": "HttpTool"},
            '{"method": "DELETE", "url": "https://gitlab.example.com/api/v4/projects/1"}',
            run_id=uuid.uuid4(),
        )


def test_sql_json_wrapped_insert_blocked():
    """INSERT inside a JSON-wrapped input string must be caught (bypass fix)."""
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        import json as _json

        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        handler = ReadOnlyToolValidator(agent_name="IntegrationAgent")
        with pytest.raises(PermissionError, match="Mutating SQL"):
            handler.on_tool_start(
                serialized={"name": "sql_db_query"},
                input_str=_json.dumps({"query": "INSERT INTO resources VALUES (1)"}),
                run_id=uuid.uuid4(),
            )


def test_sql_json_wrapped_select_allowed():
    """SELECT inside a JSON-wrapped input string must be allowed."""
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        import json as _json

        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        handler = ReadOnlyToolValidator(agent_name="IntegrationAgent")
        # Should not raise
        handler.on_tool_start(
            serialized={"name": "sql_db_query"},
            input_str=_json.dumps({"query": "SELECT * FROM resources"}),
            run_id=uuid.uuid4(),
        )


def test_ansible_flag_injection_blocked():
    """ansible-playbook with --syntax-check plus -e extra-vars must be blocked."""
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        h = ReadOnlyToolValidator(agent_name="TestAgent")
        with pytest.raises(PermissionError, match="read-only"):
            h.on_tool_start(
                {"name": "AnsibleTool"},
                '{"command": "ansible-playbook site.yml --syntax-check -e \'become_user=root\'"}',
                run_id=uuid.uuid4(),
            )


@pytest.mark.parametrize(
    "tool_name,expected_module",
    [
        ("ansible_getent_tool", "ansible.builtin.getent"),
        ("ansible_slurp_tool", "ansible.builtin.slurp"),
        ("ansible_listen_ports_tool", "community.general.listen_ports_facts"),
    ],
)
def test_ansible_enrichment_tools_structured_args_allowed(tool_name, expected_module):
    """MR-J (INV-1): the enrichment tools pass structured {target, inventory}
    args (no 'command' key) — same invocation style as ansible_facts_tool /
    ansible_ping_tool. Each must reconstruct to its OWN allow-listed module
    (not silently fall through to 'setup')."""
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as ms,
        patch("infra_brain.callbacks.readonly.get_session") as mss,
    ):
        _sql_enforce_mocks(ms, mss)
        h = ReadOnlyToolValidator(agent_name="TestAgent")
        # Should NOT raise.
        h.on_tool_start(
            {"name": tool_name},
            '{"target": "linux", "inventory": "/etc/ansible/hosts"}',
            run_id=uuid.uuid4(),
        )


def test_ansible_check_with_inventory_still_allowed():
    """ansible -m setup with -i inventory is a legitimate read-only call and must pass."""
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
    ):
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        h = ReadOnlyToolValidator(agent_name="TestAgent")
        # Should NOT raise
        h.on_tool_start(
            {"name": "AnsibleTool"},
            '{"command": "ansible all -m setup -i inventory/"}',
            run_id=uuid.uuid4(),
        )


# ---------------------------------------------------------------------------
# SQL bypass hardening tests
# ---------------------------------------------------------------------------


def _sql_handler():
    """Helper that patches settings/session and returns a ReadOnlyToolValidator."""
    from unittest.mock import patch

    return patch("infra_brain.callbacks.readonly.get_settings"), patch(
        "infra_brain.callbacks.readonly.get_session"
    )


def _make_sql_ctx():
    """Context manager helper — returns (mock_settings_patch, mock_session_patch)."""
    ps = patch("infra_brain.callbacks.readonly.get_settings")
    ss = patch("infra_brain.callbacks.readonly.get_session")
    return ps, ss


def _sql_enforce_mocks(mock_settings, mock_session):
    mock_settings.return_value.scan_readonly_enforce = True
    mock_session.return_value.__enter__ = lambda s: MagicMock()
    mock_session.return_value.__exit__ = MagicMock(return_value=False)


@pytest.mark.parametrize(
    "sql_input",
    [
        # multi-statement: SELECT then DROP
        "SELECT 1; DROP TABLE resources",
        # leading block comment hides INSERT
        "/* x */ INSERT INTO x VALUES(1)",
        # leading line comment hides UPDATE
        "-- c\nUPDATE x SET y=1",
        # CTE-wrapped DELETE
        "WITH c AS (SELECT 1) DELETE FROM resources WHERE 1=1",
        # Additional verbs that _SQL_MUTATING must now catch
        "MERGE INTO x USING src ON (x.id=src.id) WHEN MATCHED THEN UPDATE SET x.v=src.v",
        "CALL proc()",
        "EXEC sp_helpdb",
        "GRANT SELECT ON resources TO user1",
        "REVOKE INSERT ON resources FROM user1",
        "COPY x FROM '/tmp/data.csv'",
        "RENAME TABLE old TO new",
    ],
)
def test_sql_bypass_denied(sql_input):
    """All exploitable SQL inputs must be denied when passed as raw SQL."""
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as ms,
        patch("infra_brain.callbacks.readonly.get_session") as mss,
    ):
        _sql_enforce_mocks(ms, mss)
        h = ReadOnlyToolValidator(agent_name="TestAgent")
        with pytest.raises(PermissionError, match="read-only"):
            h.on_tool_start(
                {"name": "sql_db_query"},
                sql_input,
                run_id=uuid.uuid4(),
            )


@pytest.mark.parametrize(
    "sql_input",
    [
        "SELECT * FROM resources",
        "SELECT id, name FROM resources WHERE domain='linux'",
    ],
)
def test_sql_plain_select_allowed(sql_input):
    """Plain SELECT statements must always be allowed."""
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as ms,
        patch("infra_brain.callbacks.readonly.get_session") as mss,
    ):
        _sql_enforce_mocks(ms, mss)
        h = ReadOnlyToolValidator(agent_name="TestAgent")
        h.on_tool_start(
            {"name": "sql_db_query"},
            sql_input,
            run_id=uuid.uuid4(),
        )


def test_sql_non_standard_key_insert_blocked():
    """Mutation hidden in a non-standard JSON key 'statement' must be denied."""
    import json as _json

    with (
        patch("infra_brain.callbacks.readonly.get_settings") as ms,
        patch("infra_brain.callbacks.readonly.get_session") as mss,
    ):
        _sql_enforce_mocks(ms, mss)
        h = ReadOnlyToolValidator(agent_name="TestAgent")
        with pytest.raises(PermissionError, match="read-only"):
            h.on_tool_start(
                {"name": "sql_db_query"},
                _json.dumps({"statement": "INSERT INTO x VALUES(1)"}),
                run_id=uuid.uuid4(),
            )


def test_sql_json_select_in_query_allowed():
    """SELECT in JSON 'query' key must be allowed."""
    import json as _json

    with (
        patch("infra_brain.callbacks.readonly.get_settings") as ms,
        patch("infra_brain.callbacks.readonly.get_session") as mss,
    ):
        _sql_enforce_mocks(ms, mss)
        h = ReadOnlyToolValidator(agent_name="TestAgent")
        h.on_tool_start(
            {"name": "sql_db_query"},
            _json.dumps({"query": "SELECT * FROM resources"}),
            run_id=uuid.uuid4(),
        )


@pytest.mark.parametrize(
    "col_name",
    [
        # Word boundary must prevent false positives on column/table names
        "SELECT update_log FROM audit",
        "SELECT create_date, delete_flag FROM records",
        "SELECT * FROM updates",
    ],
)
def test_sql_word_boundary_no_false_positive(col_name):
    """Mutating verbs embedded inside identifiers must NOT be denied (word boundary)."""
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as ms,
        patch("infra_brain.callbacks.readonly.get_session") as mss,
    ):
        _sql_enforce_mocks(ms, mss)
        h = ReadOnlyToolValidator(agent_name="TestAgent")
        # Should NOT raise
        h.on_tool_start(
            {"name": "sql_db_query"},
            col_name,
            run_id=uuid.uuid4(),
        )


# ---------------------------------------------------------------------------
# Ansible deny-list hardening tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        # -e with no space (eFOO=bar)
        "ansible all -m setup --syntax-check -eFOO=bar",
        # dangerous module: raw
        "ansible all -m raw --check -a 'cat /etc/shadow'",
        # dangerous module: shell
        "ansible all -m shell --check -a 'id'",
        # dangerous module: command
        "ansible all -m command --check -a 'whoami'",
        # privilege escalation: --become
        "ansible all -m setup --check --become",
        # command chaining: semicolon
        "ansible all -m setup --check; rm -rf /tmp",
        # command chaining: &&
        "ansible all -m setup --check && rm -rf /tmp",
        # command chaining: pipe
        "ansible all -m setup --check | tee /tmp/out",
        # command chaining: $( subshell
        "ansible all -m setup --check -a '$(id)'",
        # -e with space (existing test coverage, keep working)
        "ansible-playbook site.yml --syntax-check -e 'become_user=root'",
    ],
)
def test_ansible_deny_list_blocked(cmd):
    """Commands matching the Ansible deny-list must be denied even if allow-pattern matches."""
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as ms,
        patch("infra_brain.callbacks.readonly.get_session") as mss,
    ):
        _sql_enforce_mocks(ms, mss)
        h = ReadOnlyToolValidator(agent_name="TestAgent")
        with pytest.raises(PermissionError, match="read-only"):
            h.on_tool_start(
                {"name": "AnsibleTool"},
                '{"command": "' + cmd.replace('"', '\\"') + '"}',
                run_id=uuid.uuid4(),
            )


@pytest.mark.parametrize(
    "cmd",
    [
        "ansible all -m setup -i inventory/",
        "ansible-playbook site.yml --check",
        "ansible-playbook site.yml --syntax-check",
        "ansible all -m ping",
        "ansible all -m gather_facts --check",
        "ansible all -m setup --list-hosts",
        # MR-J (INV-1): the three additional dedicated fact-gathering modules.
        "ansible all -m ansible.builtin.getent -i inventory/",
        "ansible all -m ansible.builtin.slurp -i inventory/",
        "ansible all -m community.general.listen_ports_facts -i inventory/",
    ],
)
def test_ansible_legit_readonly_allowed(cmd):
    """Legitimate read-only Ansible commands must be allowed."""
    with (
        patch("infra_brain.callbacks.readonly.get_settings") as ms,
        patch("infra_brain.callbacks.readonly.get_session") as mss,
    ):
        _sql_enforce_mocks(ms, mss)
        h = ReadOnlyToolValidator(agent_name="TestAgent")
        # Should NOT raise
        h.on_tool_start(
            {"name": "AnsibleTool"},
            '{"command": "' + cmd + '"}',
            run_id=uuid.uuid4(),
        )


# --- TRK-031 review: posture command-tool gate (fail-closed) ----------------
#
# The four posture tools run ansible.builtin.command (globally denied) with a
# FIXED read-only command. The gate now recognizes them by name and validates
# every command they are known to run — plus any command presented in the
# input — against its own independent read-only allow-list. Known command ->
# allow; anything else (tampered constant, injected mutating command) -> deny.

import json as _json  # noqa: E402


def _enforcing():
    ctx = patch("infra_brain.callbacks.readonly.get_settings")
    sess = patch("infra_brain.callbacks.readonly.get_session")
    return ctx, sess


_POSTURE_TOOL_NAMES = [
    "ansible_pending_updates_tool",
    "ansible_certificates_tool",
    "ansible_firewall_rules_tool",
    "ansible_shares_tool",
]


@pytest.mark.parametrize("tool_name", _POSTURE_TOOL_NAMES)
def test_posture_tool_known_command_allowed(tool_name):
    """With only target/inventory (the real invocation), the gate reconstructs
    the tool's known read-only command(s) and ALLOWS — no raise."""
    ctx, sess = _enforcing()
    with ctx as mock_settings, sess as mock_session:
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)
        h = ReadOnlyToolValidator(agent_name="LinuxAgent")
        # Must NOT raise.
        h.on_tool_start(
            {"name": tool_name},
            _json.dumps({"target": "all", "inventory": "/etc/ansible/hosts"}),
            run_id=uuid.uuid4(),
        )


@pytest.mark.parametrize("tool_name", _POSTURE_TOOL_NAMES)
@pytest.mark.parametrize(
    "bad_command",
    ["rm -rf /", "apt-get install evil", "iptables -F", "dnf install backdoor"],
)
def test_posture_tool_tampered_command_denied(tool_name, bad_command):
    """A command NOT byte-identical to the gate's read-only allow-list is
    DENIED (fail-closed), even for a recognized posture tool name."""
    ctx, sess = _enforcing()
    with ctx as mock_settings, sess as mock_session:
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)
        h = ReadOnlyToolValidator(agent_name="LinuxAgent")
        with pytest.raises(PermissionError, match="non-allowlisted command"):
            h.on_tool_start(
                {"name": tool_name},
                _json.dumps(
                    {
                        "target": "all",
                        "inventory": "/etc/ansible/hosts",
                        "command": bad_command,
                    }
                ),
                run_id=uuid.uuid4(),
            )


def test_posture_tool_unsafe_target_denied():
    """Shell metacharacters in the caller-supplied target are denied by the
    gate's defense-in-depth arg check."""
    ctx, sess = _enforcing()
    with ctx as mock_settings, sess as mock_session:
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)
        h = ReadOnlyToolValidator(agent_name="LinuxAgent")
        with pytest.raises(PermissionError, match="unsafe characters"):
            h.on_tool_start(
                {"name": "ansible_firewall_rules_tool"},
                _json.dumps({"target": "host; rm -rf /", "inventory": "/etc/ansible/hosts"}),
                run_id=uuid.uuid4(),
            )


def test_posture_tool_ansible_command_module_not_faked_as_setup():
    """Regression guard for the review finding: a posture tool presenting the
    raw ansible.builtin.command module string is NOT waved through — the gate
    denies it because that string is not in the read-only command allow-list."""
    ctx, sess = _enforcing()
    with ctx as mock_settings, sess as mock_session:
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)
        h = ReadOnlyToolValidator(agent_name="LinuxAgent")
        with pytest.raises(PermissionError, match="non-allowlisted command"):
            h.on_tool_start(
                {"name": "ansible_shares_tool"},
                _json.dumps(
                    {
                        "target": "all",
                        "inventory": "/etc/ansible/hosts",
                        "command": "cat /etc/shadow",
                    }
                ),
                run_id=uuid.uuid4(),
            )


def test_ansible_facts_tool_still_allowed_via_setup_fallback():
    """Decision #3 regression guard: ansible_facts_tool legitimately relies on
    the fail-open setup fallback and must still be allowed (not flipped to
    deny)."""
    ctx, sess = _enforcing()
    with ctx as mock_settings, sess as mock_session:
        mock_settings.return_value.scan_readonly_enforce = True
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)
        h = ReadOnlyToolValidator(agent_name="LinuxAgent")
        # Must NOT raise.
        h.on_tool_start(
            {"name": "ansible_facts_tool"},
            _json.dumps({"target": "all", "inventory": "/etc/ansible/hosts"}),
            run_id=uuid.uuid4(),
        )


def test_permissive_mode_denial_logs_at_error_not_warning(caplog):
    """#85: scan_readonly_enforce=False must not downgrade a real violation to
    WARNING — the write still proceeds (permissive), but the log must be loud.

    Invoked via the public on_tool_start() entry point (a POST request),
    matching this file's existing test_warn_only_when_not_enforcing()
    convention — that existing test only asserts "does not raise"; this one
    adds the log-level assertion that was missing."""
    import logging

    from infra_brain.callbacks.readonly import ReadOnlyToolValidator

    with (
        patch("infra_brain.callbacks.readonly.get_settings") as mock_settings,
        patch("infra_brain.callbacks.readonly.get_session") as mock_session,
        caplog.at_level(logging.WARNING, logger="infra_brain.callbacks.readonly"),
    ):
        mock_settings.return_value.scan_readonly_enforce = False
        mock_session.return_value.__enter__ = lambda s: MagicMock()
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        h = ReadOnlyToolValidator(agent_name="TestAgent")
        h.on_tool_start(
            {"name": "HttpTool"},
            '{"method": "POST", "url": "https://gitlab.example.com/api/v4/projects"}',
            run_id=uuid.uuid4(),
        )

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert error_records, (
        f"expected the permissive-mode denial to log at ERROR; got levels {[r.levelname for r in caplog.records]}"
    )
    assert not warning_records, "permissive-mode denial must not log at WARNING (#85)"
