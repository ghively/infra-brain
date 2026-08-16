"""Tests for QueryAgent — natural-language SQL query agent.

F-01 CRITICAL (audit 2026-07-06): query.py previously imported AgentExecutor /
create_tool_calling_agent (removed in langchain 1.3.10) and langchain_community
(not installed). Those imports have been replaced with the create_agent +
execute_sql @tool pattern already used in llm_base.py. All tests here must
import directly — no ImportError skip guards.
"""

import json
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from infra_brain.agents.query import (
    QueryAgent,
    _ALLOWED_TABLES,
    _JSONB_COLUMN_NAMES,
    _SQL_SYSTEM_PROMPT,
    _autocast_jsonb_pattern_match,
    _build_schema_block,
    _jsonb_columns_by_table,
    execute_sql,
    sanitize_sql_query,
)
from infra_brain.db.models import Resource

from tests.support.pg import make_engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent() -> QueryAgent:
    """Build a QueryAgent without a real DB or LLM (unit-test mode)."""
    agent = QueryAgent.__new__(QueryAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    agent._llm = None
    return agent


def _fake_settings(**overrides):
    """A settings stand-in for query()'s GitLab #165 credential preflight.

    Defaults to a fully-configured anthropic provider so a test that cares about
    the reason() delegation path gets PAST the preflight. Uses SimpleNamespace,
    not MagicMock, because MagicMock auto-creates every attribute — a preflight
    bug (reading the wrong field) would silently pass against a MagicMock.
    """
    values = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "sk-ant-test",
        "openai_api_key": "",
        "openai_base_url": "",
        "bedrock_region": "us-east-1",
        "mcp_tool_timeout_seconds": 60,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _llm_configured(**overrides):
    """Patch query.get_settings so the credential preflight sees a real provider."""
    return patch(
        "infra_brain.agents.query.get_settings",
        return_value=_fake_settings(**overrides),
    )


def _make_sqlite_engine():
    engine = make_engine()
    return engine


# ---------------------------------------------------------------------------
# Domain / lifecycle
# ---------------------------------------------------------------------------


def test_query_agent_domain():
    agent = _make_agent()
    assert agent.domain == "query"


def test_collect_returns_empty_list_for_default_scope():
    """collect('all') is a no-op — the health check only fires for scope='health'."""
    agent = _make_agent()
    assert agent.collect(scope="all") == []


def test_collect_health_scope_returns_result():
    """collect('health') performs a DB connectivity check and returns a result item."""
    agent = _make_agent()
    engine = _make_sqlite_engine()
    with patch("infra_brain.agents.query.get_readonly_engine", return_value=engine):
        items = agent.collect(scope="health")
    assert len(items) == 1
    assert items[0]["name"] == "db_connectivity"
    assert items[0]["data"]["ok"] is True


def test_collect_health_scope_scrubs_dsn_from_connection_failure(caplog):
    """SEC-2: a connection failure whose message embeds a DSN (e.g. a malformed
    POSTGRES_READONLY_URL raising sqlalchemy.exc.ArgumentError with the full
    string, credentials included) must not persist the raw credentials into the
    stored health-check item — that data flows into Resource.metadata_/Snapshot
    via ETLConnector.run()."""
    agent = _make_agent()

    class _BoomEngine:
        def connect(self):
            raise RuntimeError(
                "Could not parse SQLAlchemy URL from string "
                "'postgresql://svc_user:s3cr3t-pw@db.internal:5432/infra'"
            )

    with patch("infra_brain.agents.query.get_readonly_engine", return_value=_BoomEngine()):
        items = agent.collect(scope="health")

    assert len(items) == 1
    error = items[0]["data"]["error"]
    assert "s3cr3t-pw" not in error
    assert "svc_user" not in error
    assert "[REDACTED]@" in error
    # Host/port/db stay visible — useful for triage, not sensitive on their own.
    assert "db.internal:5432/infra" in error


# ---------------------------------------------------------------------------
# Early DML / multi-statement validation in query()
# ---------------------------------------------------------------------------


def test_query_returns_error_dict_on_dml():
    """query() must refuse obvious DML before reaching the LLM."""
    agent = _make_agent()
    result = agent.query("DELETE FROM resources")
    assert isinstance(result, dict)
    assert "error" in result
    assert result["result"] is None
    assert result["sql"] == ""


def test_query_returns_error_dict_on_multi_statement():
    """query() must refuse multi-statement queries (;-separated)."""
    agent = _make_agent()
    result = agent.query("SELECT 1; DROP TABLE resources")
    assert isinstance(result, dict)
    assert "error" in result
    assert result["result"] is None


def test_query_empty_question():
    agent = _make_agent()
    result = agent.query("")
    assert "error" in result
    assert result["result"] is None


# ---------------------------------------------------------------------------
# reason() delegation path
# ---------------------------------------------------------------------------


def test_query_delegates_to_reason_and_returns_result():
    """query() calls self.reason() and wraps the text as 'result'."""
    agent = _make_agent()
    with (
        _llm_configured(),
        patch.object(agent, "reason", return_value="Found 3 linux hosts") as mock_reason,
    ):
        result = agent.query("How many linux hosts are there?")
    assert result["result"] == "Found 3 linux hosts"
    assert result["question"] == "How many linux hosts are there?"
    assert result["sql"] == ""
    mock_reason.assert_called_once()


def test_query_returns_error_dict_when_reason_raises():
    """query() catches exceptions from reason() and returns an error dict."""
    agent = _make_agent()
    with (
        _llm_configured(),
        patch.object(agent, "reason", side_effect=RuntimeError("LLM unavailable")),
    ):
        result = agent.query("How many hosts do we have?")
    assert "error" in result
    assert "LLM unavailable" in result["error"]
    assert result["result"] is None


# ---------------------------------------------------------------------------
# Item 1.6 (Wave 1) — AST statement-type guard on sanitize_sql_query
# ---------------------------------------------------------------------------


def test_select_into_rejected():
    """SELECT INTO creates a table yet passes the regex sanitizer (starts with
    SELECT, no deny verb). The AST guard rejects it."""
    with pytest.raises(ValueError):
        sanitize_sql_query("SELECT * INTO evil_copy FROM resources")


def test_cte_wrapped_dml_rejected():
    """CTE-wrapped DML must stay rejected (both via SELECT-prefix check and AST walk)."""
    with pytest.raises(ValueError):
        sanitize_sql_query("WITH d AS (DELETE FROM resources RETURNING id) SELECT * FROM d")


def test_select_for_update_rejected():
    """Row-locking SELECTs are rejected (AST locks check)."""
    with pytest.raises(ValueError):
        sanitize_sql_query("SELECT * FROM resources FOR UPDATE")


def test_plain_select_still_passes_and_gets_limit():
    """No over-blocking: a plain SELECT passes and gains LIMIT 100."""
    out = sanitize_sql_query("SELECT name FROM resources WHERE domain = 'cicd'")
    assert out.startswith("SELECT name FROM resources")
    assert out.endswith("LIMIT 100")


# ---------------------------------------------------------------------------
# execute_sql @tool
# ---------------------------------------------------------------------------


def test_execute_sql_tool_rejects_dml():
    """execute_sql tool raises ToolException for DML — never reaches the DB."""
    from langchain_core.tools import ToolException

    with pytest.raises(ToolException, match="read-only guard"):
        execute_sql.invoke({"query": "DELETE FROM resources"})


def test_execute_sql_tool_runs_select():
    """execute_sql tool returns JSON rows from a real SQLite in-memory engine."""
    engine = _make_sqlite_engine()
    with Session(engine) as s:
        s.add(
            Resource(
                id=uuid.uuid4(),
                domain="cicd",
                type="pipeline",
                name="test-pipe",
                source="gitlab",
                zone="corpor",
                last_seen=datetime.now(timezone.utc),
            )
        )
        s.commit()

    with patch("infra_brain.agents.query.get_readonly_engine", return_value=engine):
        raw = execute_sql.invoke({"query": "SELECT name FROM resources LIMIT 10"})

    rows = json.loads(raw)
    assert isinstance(rows, list)
    assert any(r["name"] == "test-pipe" for r in rows)


# ---------------------------------------------------------------------------
# TRK-144 / GitLab #38 — schema-grounded SQL system prompt
# ---------------------------------------------------------------------------


def test_schema_block_is_metadata_driven_for_resources():
    """_build_schema_block() must render real ORM columns for `resources`, not a
    hand-maintained guess. This is the regression test for TRK-144: the model
    previously guessed `resources.hostname` (a column that exists on OTHER
    tables, not `resources`), because the prompt carried zero column info."""
    block = _build_schema_block(_ALLOWED_TABLES)

    # Find the resources(...) line specifically — other tables (r7_assets,
    # linux_hosts, host_identities) legitimately DO have a hostname/short_hostname
    # column, so this must not be a whole-block substring check.
    resources_lines = [
        line for line in block.splitlines() if line.strip().startswith("- resources(")
    ]
    assert len(resources_lines) == 1, block
    resources_line = resources_lines[0]

    for real_col in ("name", "domain", "type", "source", "zone"):
        assert real_col in resources_line, (
            f"expected real column {real_col!r} in {resources_line!r}"
        )
    assert "hostname" not in resources_line, (
        f"resources has no hostname column — schema block must not claim it does: {resources_line!r}"
    )


def test_build_schema_block_handles_missing_orm_table_gracefully():
    """A table name with no backing SQLAlchemy model (e.g. a raw/migration-only
    table) must degrade to a bare-name fallback line, never raise."""
    block = _build_schema_block(["resources", "this_table_does_not_exist_anywhere"])
    assert "- resources(" in block
    assert "this_table_does_not_exist_anywhere" in block
    assert "columns unknown" in block


def test_allowed_tables_includes_graph_and_governance_tables():
    """Audit finding (query-nl-schema-staleness): query_nl refused any
    question touching graph_nodes/graph_edges/root_cause_notes/
    governance_events, stating they were "not part of the schema available"
    — they're registered on the same Base.metadata as every other allowed
    table, the omission was purely this hardcoded list never being extended.
    """
    for table in ("graph_nodes", "graph_edges", "root_cause_notes", "governance_events"):
        assert table in _ALLOWED_TABLES
    block = _build_schema_block(_ALLOWED_TABLES)
    for table in ("graph_nodes", "graph_edges", "root_cause_notes", "governance_events"):
        assert f"- {table}(" in block
        assert "columns unknown" not in block.split(f"- {table}(")[1].split("\n")[0]


def test_allowed_tables_vuln_queue_name_matches_real_model():
    """Standalone bug fix (GitLab #48 / design doc Part 2): _ALLOWED_TABLES
    previously listed "vuln_queue_items", but VulnQueueItem's real
    __tablename__ is "vuln_queue" (db/models/rapid7.py). The wrong name meant
    _build_schema_block() silently degraded to a "columns unknown" fallback
    line instead of ever rendering the real columns — this entry never
    actually worked. Assert the fixed name is present and NOT the old wrong one."""
    assert "vuln_queue" in _ALLOWED_TABLES
    assert "vuln_queue_items" not in _ALLOWED_TABLES


def test_schema_block_renders_real_vuln_queue_columns():
    """_build_schema_block() must now resolve vuln_queue to its real ORM model
    and list its actual columns, not degrade to the "columns unknown" fallback
    that "vuln_queue_items" (the old, wrong name) always hit."""
    block = _build_schema_block(_ALLOWED_TABLES)

    vuln_queue_lines = [
        line for line in block.splitlines() if line.strip().startswith("- vuln_queue(")
    ]
    assert len(vuln_queue_lines) == 1, block
    vuln_queue_line = vuln_queue_lines[0]

    for real_col in ("id", "resource_id", "cve_id", "kb_id", "severity", "sla_due", "status"):
        assert real_col in vuln_queue_line, (
            f"expected real column {real_col!r} in {vuln_queue_line!r}"
        )
    assert "columns unknown" not in vuln_queue_line, (
        f"vuln_queue has a real ORM model — must not fall back: {vuln_queue_line!r}"
    )


def test_system_prompt_contains_schema_and_few_shot_examples():
    """The assembled _SQL_SYSTEM_PROMPT must carry the dynamic schema block and
    at least one few-shot example that correctly uses resources.name (not
    resources.hostname) as a directly-relevant counter-example."""
    assert "resources(" in _SQL_SYSTEM_PROMPT
    assert "SELECT name FROM resources" in _SQL_SYSTEM_PROMPT
    assert "Examples:" in _SQL_SYSTEM_PROMPT


def test_query_passes_schema_grounded_prompt_to_reason():
    """query() must hand the schema-grounded system prompt (not the old bare
    table-name list) into reason()'s task string."""
    agent = _make_agent()
    with _llm_configured(), patch.object(agent, "reason", return_value="ok") as mock_reason:
        agent.query("How many resources are there?")
    task_arg = mock_reason.call_args.kwargs.get("task") or mock_reason.call_args.args[0]
    assert "resources(" in task_arg
    assert "SELECT name FROM resources" in task_arg


def test_execute_sql_tool_truncates_large_result():
    """execute_sql returns a truncation object when the result exceeds the byte cap."""
    import infra_brain.agents.query as query_mod

    engine = _make_sqlite_engine()
    # Patch _MAX_RESULT_BYTES to 1 to force truncation on any result.
    with patch.object(query_mod, "_MAX_RESULT_BYTES", 1):
        with Session(engine) as s:
            s.add(
                Resource(
                    id=uuid.uuid4(),
                    domain="cicd",
                    type="pipeline",
                    name="pipe-trunc",
                    source="gitlab",
                    zone="corpor",
                    last_seen=datetime.now(timezone.utc),
                )
            )
            s.commit()
        with patch("infra_brain.agents.query.get_readonly_engine", return_value=engine):
            raw = execute_sql.invoke({"query": "SELECT name FROM resources LIMIT 10"})
    obj = json.loads(raw)
    assert obj.get("truncated") is True
    assert "count" in obj


# ---------------------------------------------------------------------------
# GitLab #165 — query_nl must not hang, and must not call the model when no
# reasoner is configured.
#
# query() -> reason() bypasses ETLConnector.run() entirely, so before this fix
# NOTHING bounded it: max_iters * llm_request_timeout_seconds * retries, a
# ~2880s worst case. Every MCP client's own transport timeout fired first, with
# zero error surfaced to anyone — the silent hang of #165.
# ---------------------------------------------------------------------------


class _SleepyToolModel(GenericFakeChatModel):
    """Fake chat model whose every turn blocks for *delay* seconds.

    Stands in for a wedged/glacial LLM endpoint. Counts invocations so a test
    can assert the model was never reached at all.
    """

    delay: float = 5.0
    calls: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(messages)
        time.sleep(self.delay)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_query_returns_error_within_deadline_instead_of_hanging():
    """A model that blocks 5s under a 1s deadline must return an error in ~1s.

    This is the #165 regression guard: the assertion that matters is the
    ELAPSED time. A version of query() without deadline_seconds still returns
    an error dict eventually — it just takes 5s to do it, which is exactly the
    hang being fixed. So assert on the clock, not only on the payload.
    """
    calls: list = []
    model = _SleepyToolModel(
        messages=iter([AIMessage(content="too late")]),
        delay=5.0,
        calls=calls,
    )
    agent = _make_agent()
    agent.llm = model
    # _make_agent() hands out a MagicMock settings, which reason()'s per-run
    # token-ceiling check cannot compare against an int. These two new tests are
    # the only ones that run the REAL reason(), so they need a real settings
    # object (ceiling absent => disabled).
    agent.settings = _fake_settings()

    started = time.monotonic()
    with (
        _llm_configured(mcp_tool_timeout_seconds=1),
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")),
    ):
        result = agent.query("How many linux hosts are there?")
    elapsed = time.monotonic() - started

    assert result["result"] is None
    assert "error" in result
    assert "deadline" in result["error"].lower()
    # Comfortably under the model's own 5s block, and above the 1s budget.
    assert 0.5 < elapsed < 4.0, f"query() took {elapsed:.2f}s; deadline was 1s"


def test_query_fails_fast_when_no_llm_configured_without_invoking_model():
    """anthropic provider + empty key => explicit config error, model never called."""
    calls: list = []
    model = _SleepyToolModel(
        messages=iter([AIMessage(content="should never be produced")]),
        delay=5.0,
        calls=calls,
    )
    agent = _make_agent()
    agent.llm = model

    started = time.monotonic()
    with _llm_configured(llm_provider="anthropic", anthropic_api_key=""):
        result = agent.query("How many linux hosts are there?")
    elapsed = time.monotonic() - started

    assert result["result"] is None
    assert "no LLM configured" in result["error"]
    assert "anthropic" in result["error"]
    # The whole point of a PREFLIGHT: zero model invocations.
    assert len(calls) == 0, f"model was invoked {len(calls)} time(s) despite no credential"
    # And it costs nothing — no graph build, no retry, no 5s sleep.
    assert elapsed < 1.0, f"preflight took {elapsed:.2f}s; it must not touch the model"


def test_query_openai_provider_accepts_base_url_without_api_key():
    """A keyless OpenAI-compatible local gateway is a VALID config, not an error.

    llm.py substitutes a placeholder key for exactly this case, so demanding
    openai_api_key here would reject a working Ollama/vLLM deployment.
    """
    agent = _make_agent()
    with (
        _llm_configured(
            llm_provider="openai",
            anthropic_api_key="",
            openai_api_key="",
            openai_base_url="http://ollama.internal:11434/v1",
        ),
        patch.object(agent, "reason", return_value="42 hosts") as mock_reason,
    ):
        result = agent.query("How many linux hosts are there?")
    assert result["result"] == "42 hosts"
    mock_reason.assert_called_once()


# ---------------------------------------------------------------------------
# Audit 2026-08-06 — Bug 1: jsonb column ILIKE with no ::text cast
#
# Reproduced live twice: "Which hosts are running the caddy service?" produced
# `... WHERE details ILIKE '%caddy%'` / `... WHERE metadata ILIKE ...` against
# a jsonb-typed column, raising `operator does not exist: jsonb ~~* unknown`
# — Postgres jsonb has no pattern-match operator at all. sanitize_sql_query()
# now auto-casts via _autocast_jsonb_pattern_match() as a mechanical backstop
# to the (also fixed) prompt guidance, since the LLM is non-deterministic.
# ---------------------------------------------------------------------------


def test_jsonb_columns_detected_for_resources_metadata():
    """resources.metadata is jsonb — _jsonb_columns_by_table() must find it via
    isinstance(col.type, JSON), not a hand-maintained list."""
    jsonb_by_table = _jsonb_columns_by_table(_ALLOWED_TABLES)
    assert "metadata" in jsonb_by_table.get("resources", set())
    assert "metadata" in _JSONB_COLUMN_NAMES


def test_jsonb_columns_detected_for_r7_assets_details():
    """r7_assets.details is jsonb — same TRK-144-style guarantee, this time for
    types instead of names: derived from the ORM, not guessed."""
    jsonb_by_table = _jsonb_columns_by_table(_ALLOWED_TABLES)
    assert "details" in jsonb_by_table.get("r7_assets", set())


def test_schema_block_annotates_jsonb_columns():
    """The schema block fed to the LLM must mark jsonb columns explicitly so
    the model has the type information it needs to cast before ILIKE."""
    block = _build_schema_block(_ALLOWED_TABLES)
    resources_line = next(
        line for line in block.splitlines() if line.strip().startswith("- resources(")
    )
    assert "metadata:jsonb" in resources_line
    # A non-jsonb column must NOT carry the marker.
    assert "name:jsonb" not in resources_line


def test_system_prompt_instructs_jsonb_cast_before_pattern_match():
    """The assembled system prompt must explicitly tell the model to cast
    jsonb columns before ILIKE/LIKE (root-cause fix, not just the backstop)."""
    assert "jsonb" in _SQL_SYSTEM_PROMPT
    assert "ILIKE" in _SQL_SYSTEM_PROMPT
    assert "::text" in _SQL_SYSTEM_PROMPT


def test_autocast_rewrites_bare_jsonb_ilike_reproducing_bug1():
    """Reproduces the exact confirmed failure: a bare ILIKE against a jsonb
    column (as the LLM generated it, uncast) must come out of sanitization
    already cast to text — this is what makes the query actually executable
    against real Postgres instead of raising `operator does not exist: jsonb
    ~~* unknown`."""
    out = sanitize_sql_query("SELECT id, name FROM resources WHERE metadata ILIKE '%caddy%'")
    assert "metadata" in out
    assert "ILIKE" in out
    # Must be cast to text (either sqlglot's canonical CAST(... AS TEXT) form,
    # or the ::text shorthand) before ILIKE — never a bare jsonb comparison.
    assert "CAST(metadata AS TEXT)" in out or "metadata::text" in out


def test_autocast_rewrites_bare_jsonb_like_and_details_column():
    """Second reproduction shape from the audit: `details LIKE` (not ILIKE) on
    a different jsonb column (r7_assets.details) must also be cast."""
    out = sanitize_sql_query("SELECT hostname FROM r7_assets WHERE details LIKE '%caddy%' LIMIT 50")
    assert "CAST(details AS TEXT)" in out or "details::text" in out


def test_autocast_does_not_double_cast_already_correct_query():
    """A query that already casts correctly must pass through unchanged in
    substance — no double-casting, no corruption of a working query."""
    out = sanitize_sql_query(
        "SELECT id FROM resources WHERE metadata::text ILIKE '%caddy%' LIMIT 100"
    )
    assert "metadata::text ILIKE '%caddy%'" in out
    assert "CAST(CAST(" not in out


def test_autocast_leaves_jsonb_key_extraction_untouched():
    """`col->>'key' ILIKE ...` already extracts a text value — casting it too
    would be a no-op at best; assert the rewrite correctly recognizes this
    shape (left operand is not a bare Column) and leaves it alone."""
    out = sanitize_sql_query(
        "SELECT id FROM resources WHERE metadata->>'service' ILIKE '%caddy%' LIMIT 100"
    )
    assert "CAST(" not in out
    assert "->>" in out


def test_autocast_does_not_touch_non_jsonb_column_ilike():
    """A plain text column compared with ILIKE must be left exactly alone —
    the rewrite must not fire for every ILIKE, only jsonb-typed ones."""
    out = sanitize_sql_query("SELECT id FROM resources WHERE name ILIKE '%caddy%' LIMIT 100")
    assert "CAST" not in out
    assert "name ILIKE '%caddy%'" in out


def test_autocast_helper_no_ops_on_unparseable_input():
    """_autocast_jsonb_pattern_match must never raise — it degrades to a
    no-op on unparseable input and lets the AST guard downstream in
    sanitize_sql_query raise the real, informative parse error."""
    garbage = "SELECT ((( FROM WHERE"
    assert _autocast_jsonb_pattern_match(garbage) == garbage
    with pytest.raises(ValueError):
        sanitize_sql_query(garbage)


# ---------------------------------------------------------------------------
# Audit 2026-08-06 — Bug 2: hallucinated resources.resource_id
#
# Reproduced live twice: a datastore/host-utilization question produced SQL
# selecting `resource_id` FROM `resources`, which raises
# psycopg2.errors.UndefinedColumn — resources' primary key is `id`;
# `resource_id` only exists as a FOREIGN KEY on CHILD tables (snapshots,
# drift_events, r7_assets, vuln_queue, ...). The dynamic schema block already
# rendered the real columns correctly (TRK-144), but the accompanying
# few-shot prose asserted host_identities lacked resource_id "unlike every
# other table above" — which read as implying `resources` itself (the first
# table listed) DOES have one. That is the root cause this section guards.
# ---------------------------------------------------------------------------


def test_resources_schema_line_has_id_not_resource_id():
    """Root fact check: resources' primary key is `id`; it has no
    `resource_id` column of its own (that only exists on child tables)."""
    block = _build_schema_block(_ALLOWED_TABLES)
    resources_line = next(
        line for line in block.splitlines() if line.strip().startswith("- resources(")
    )
    assert "- resources(id," in resources_line
    # "resource_id" must not appear as a column name on the resources() line
    # itself (a substring match on "id" elsewhere in the line is fine — this
    # specifically checks the token "resource_id" is absent).
    cols = resources_line.split("(", 1)[1].rstrip(")").split(", ")
    assert "resource_id" not in cols


def test_few_shot_examples_do_not_imply_resources_has_resource_id():
    """The misleading generalization ('unlike every other table above') that
    could be read as claiming `resources` has a `resource_id` column must not
    appear verbatim in the prompt — it must be scoped to CHILD/referencing
    tables only, and the prompt must say outright that resources' PK is
    `id`."""
    assert "unlike every other table above" not in _SQL_SYSTEM_PROMPT
    assert "resources' primary key is `id`" in _SQL_SYSTEM_PROMPT


def test_query_passes_deadline_and_tightened_limits_to_reason():
    """The interactive path must bound wall clock AND trim turns/retries."""
    agent = _make_agent()
    with (
        _llm_configured(mcp_tool_timeout_seconds=30),
        patch.object(agent, "reason", return_value="ok") as mock_reason,
    ):
        agent.query("How many resources are there?")
    import infra_brain.agents.query as query_mod

    kwargs = mock_reason.call_args.kwargs
    assert kwargs["deadline_seconds"] == 30
    assert kwargs["max_iters"] == query_mod._QUERY_MAX_ITERS
    assert kwargs["attempts"] == query_mod._QUERY_REASON_ATTEMPTS
    # Tighter than the global defaults, which other callers still rely on.
    assert kwargs["max_iters"] < 8
    assert kwargs["attempts"] < 3
