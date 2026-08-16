"""
QueryAgent — answers natural-language questions about infrastructure via read-only SQL.

Uses plain SQLAlchemy for SQL execution (no langchain_community dependency).
The LLM generates SELECT queries; sanitize_sql_query() + the read-only DB role
provide defence-in-depth against mutations.

collect() is a no-op for scope != "health": QueryAgent is on-demand only. It
also runs a weekly DB-connectivity health-check sweep when dispatched with
scope="health" — see scheduler.py SchedulerService.start()'s
``add_job_scoped("query", "health", ...)`` call (NOT the _DEFAULT_SCHEDULES
dict; a plain add_job() there would dispatch scope="all", which this collect()
treats as a no-op — AA-C-3/AA-D-23).
query(question) is the main entry point.
"""

import json
import logging
import re

import sqlglot
from langchain_core.tools import ToolException, tool
from sqlalchemy import JSON, text
from sqlglot import expressions as sqlexp

from infra_brain.agents.llm_base import LLMAgent
from infra_brain.config import get_settings
from infra_brain.db.models import Base
from infra_brain.db.session import get_readonly_engine
from infra_brain.etl.base import scrub_dsn
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)

# Tables exposed to the SQL agent — only read-only, domain-relevant tables.
# This is a prompt-scope allowlist only (it feeds _build_schema_block() below,
# which the SQL system prompt tells the LLM is the full schema) — it has no
# mechanical enforcement counterpart in sanitize_sql_query()/
# _ast_readonly_guard(), which never check table names. Audit finding
# (query-nl-schema-staleness): the newer governance/graph tables were never
# added here, so query_nl refused any question touching them even though
# they're registered on the same Base.metadata and _build_schema_block()
# would happily describe them. Before adding another table here, verify it
# introduces no jsonb column-name collision with an existing allowed table
# (see _JSONB_COLUMN_NAMES below — it is flattened, not table-qualified).
_ALLOWED_TABLES = [
    "resources",
    "snapshots",
    "drift_events",
    "r7_assets",
    "r7_vulnerabilities",
    "octopus_projects",
    "octopus_machines",
    "linux_hosts",
    "windows_patch_state",
    "host_identities",
    "vuln_queue",
    "compliance_violations",
    "graph_nodes",
    "graph_edges",
    "root_cause_notes",
    "governance_events",
]

_SQL_DENY_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|REPLACE|TRUNCATE|GRANT|REVOKE|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)
_MULTI_STMT_RE = re.compile(r";\s*\S")

# GitLab #165: query() is reached from the `query_nl` MCP tool, which is
# INTERACTIVE — a client is blocked on it. Two knobs are deliberately tighter
# here than the batch-collector defaults they override, and ONLY here:
#
#   * max_iters 4 (vs reason()'s default 8) — an NL->SQL answer that hasn't
#     converged in four model turns is not going to; the extra four turns only
#     buy latency the caller pays for.
#   * attempts 2 (vs REASON_MAX_ATTEMPTS=3) — one connection-blip retry is
#     worth the wait interactively; two is not. The global default is
#     unchanged, so every batch collector keeps all three attempts.
_QUERY_MAX_ITERS = 4
_QUERY_REASON_ATTEMPTS = 2


def _missing_llm_credential(settings) -> str | None:
    """Return a human-readable reason string when the configured LLM provider has
    no usable credential, else ``None``.

    GitLab #165 fail-fast preflight. Without this, an unconfigured provider is
    only discovered deep inside reason() — after the ReAct graph is built, the
    checkpointer is opened, and (for some providers) a request is actually
    attempted and retried. The user waits, and then gets a provider SDK's own
    error text, which rarely says "you have not configured a model."

    Deliberately checks only what is knowable LOCALLY and cheaply — this must
    never make a network call. It cannot tell a wrong key from a right one; it
    only catches "nothing is configured at all", which is the failure mode that
    was wasting an entire interactive round-trip.
    """
    provider = (getattr(settings, "llm_provider", "") or "anthropic").lower()

    if provider == "anthropic":
        if not (getattr(settings, "anthropic_api_key", "") or "").strip():
            return "anthropic_api_key is empty"
        return None

    if provider == "openai":
        # An OpenAI-COMPATIBLE local gateway (Ollama, vLLM, LiteLLM) authenticates
        # by network reachability and needs no key — llm.py substitutes a
        # placeholder key in exactly that case. So a base_url alone is a complete
        # configuration here; requiring a key too would reject a valid setup.
        has_key = bool((getattr(settings, "openai_api_key", "") or "").strip())
        has_base_url = bool((getattr(settings, "openai_base_url", "") or "").strip())
        if not (has_key or has_base_url):
            return "neither openai_api_key nor openai_base_url is set"
        return None

    if provider == "bedrock":
        # Bedrock auth is IAM (instance role / profile / env chain), which cannot
        # be validated without a call. Region is the one required local setting,
        # so that is all this checks — a Bedrock deployment with a region set is
        # NOT reported as misconfigured here.
        if not (getattr(settings, "bedrock_region", "") or "").strip():
            return "bedrock_region is empty"
        return None

    # Unknown provider: llm.py raises ValueError on it anyway. Say so up front.
    return f"unknown llm_provider {provider!r}"


def _jsonb_columns_by_table(table_names: list[str]) -> dict[str, set[str]]:
    """Return ``{table_name: {jsonb_column_names}}`` derived from live ORM metadata.

    Bug (audit 2026-08-06): the NL->SQL system prompt listed column NAMES only,
    with no type info, so the LLM had no way to know ``resources.metadata`` /
    ``r7_assets.details`` / etc. are jsonb — it generated bare ``details ILIKE
    '%caddy%'``, which Postgres rejects with ``operator does not exist: jsonb
    ~~* unknown`` (jsonb supports no pattern-match operator at all; ILIKE needs
    text on both sides). Derived from ``Base.metadata.tables`` (same source as
    ``_build_schema_block``) rather than hand-listed, so it can never drift from
    the real schema.

    ``JSONB`` in this codebase (db/models/_base.py) is ``sqlalchemy.JSON``
    ``.with_variant(postgresql.JSONB(), "postgresql")`` — the column's ``.type``
    stays an instance of the base ``JSON`` class under every dialect, so a plain
    ``isinstance(col.type, JSON)`` check is dialect-agnostic and catches it.
    """
    out: dict[str, set[str]] = {}
    for name in table_names:
        table = Base.metadata.tables.get(name)
        if table is None:
            continue
        jsonb_cols = {col.name for col in table.columns if isinstance(col.type, JSON)}
        if jsonb_cols:
            out[name] = jsonb_cols
    return out


def _build_schema_block(table_names: list[str]) -> str:
    """Render a compact `table(col1, col2, ...)` schema block from live ORM metadata.

    TRK-144 (GitLab #38): the previous prompt listed bare table names with zero
    column info, so the LLM guessed column names from generic conventions (e.g.
    ``resources.hostname``, which doesn't exist — only ``resources.name`` does;
    ``hostname`` is real but lives on r7_assets/linux_hosts/host_identities).
    Building this from ``Base.metadata.tables`` means the prompt can never drift
    from the actual schema — a column rename/add/drop in the ORM is reflected
    automatically, with no hand-maintained list to fall out of sync.

    Any name in ``table_names`` without a corresponding SQLAlchemy Table (e.g. a
    table that exists only as a raw migration, not an ORM model) degrades to a
    bare-name line rather than raising — this function must never crash prompt
    construction over one unmapped table.

    jsonb-typed columns are annotated ``colname:jsonb`` so the LLM has explicit,
    schema-grounded type info to reason about pattern-match casting (see the
    jsonb-cast few-shot example and the auto-cast defense-in-depth in
    ``sanitize_sql_query``/``_autocast_jsonb_pattern_match``).
    """
    jsonb_by_table = _jsonb_columns_by_table(table_names)
    lines = []
    for name in table_names:
        table = Base.metadata.tables.get(name)
        if table is None:
            logger.warning(
                "QueryAgent schema prompt: table %r is in _ALLOWED_TABLES but has no "
                "SQLAlchemy metadata (no ORM model) — falling back to a bare name with "
                "no column list for it.",
                name,
            )
            lines.append(f"- {name} (columns unknown — no ORM model registered)")
            continue
        jsonb_cols = jsonb_by_table.get(name, set())
        columns = ", ".join(
            f"{col.name}:jsonb" if col.name in jsonb_cols else col.name for col in table.columns
        )
        lines.append(f"- {name}({columns})")
    return "\n".join(lines)


_SCHEMA_BLOCK = _build_schema_block(_ALLOWED_TABLES)

# All jsonb-typed column names across the allowed tables, used by
# _autocast_jsonb_pattern_match() below. Flattened (not table-qualified) —
# acceptable because in the current schema no allowed table reuses a jsonb
# column's name for a non-jsonb column of the same name; if that ever changes,
# this needs to become alias/table-aware.
_JSONB_COLUMN_NAMES: frozenset[str] = frozenset(
    col for cols in _jsonb_columns_by_table(_ALLOWED_TABLES).values() for col in cols
)

# Few-shot NL->SQL examples. The second example exists specifically to steer the
# model away from the confirmed TRK-144 failure mode: guessing `resources.hostname`
# (a column that exists on r7_assets/linux_hosts/host_identities but NOT on
# resources, which only has `name`).
_FEW_SHOT_EXAMPLES = (
    "Examples:\n"
    'Q: "How many distinct resources are there?"\n'
    "A: SELECT COUNT(DISTINCT id) FROM resources LIMIT 100\n"
    "   (resources' primary key is `id`, not `resource_id` — `resource_id` never"
    " appears as a column ON resources itself. It only exists as a FOREIGN KEY"
    " on CHILD tables that reference resources, e.g. snapshots.resource_id,"
    " drift_events.resource_id, r7_assets.resource_id, vuln_queue.resource_id.)\n\n"
    'Q: "List the names of resources in the cicd domain."\n'
    "A: SELECT name FROM resources WHERE domain = 'cicd' LIMIT 100\n"
    "   (resources has no `hostname` column — hostnames live on r7_assets,"
    " linux_hosts, windows_patch_state, and host_identities instead.)\n\n"
    'Q: "Show hostnames and OS families from Rapid7 asset data."\n'
    "A: SELECT hostname, os_family FROM r7_assets LIMIT 100\n\n"
    'Q: "How many distinct host identities are there?"\n'
    "A: SELECT COUNT(DISTINCT id) FROM host_identities LIMIT 100\n"
    "   (host_identities has NO generic `resource_id` column, unlike most CHILD"
    " tables that reference resources — it stores one column per source"
    " instead: r7_resource_id, vsphere_resource_id, octopus_resource_id,"
    " linux_resource_id, windows_resource_id, net_resource_id,"
    " cloud_resource_id, k8s_resource_id, netdevice_resource_id. Do not guess a"
    " bare `resource_id` column onto this table just because most CHILD tables"
    " have one — and remember `resources` itself never has one either, see the"
    " first example above.)\n\n"
    'Q: "Which hosts are running the caddy service?"\n'
    "A: SELECT id, name FROM resources WHERE metadata::text ILIKE '%caddy%' LIMIT 100\n"
    "   (columns marked `:jsonb` in the schema below are jsonb-typed. Postgres"
    " jsonb has NO pattern-match operator at all — `jsonb ILIKE`/`jsonb LIKE`"
    " raises `operator does not exist: jsonb ~~* unknown`. ALWAYS cast a jsonb"
    " column to text first — `col::text ILIKE '...'` — before ILIKE/LIKE. If"
    " you only need one key's value, extract it as text instead of casting the"
    " whole document — `col->>'key' ILIKE '...'` — which is also already text"
    " and needs no further cast.)\n"
)

_SQL_SYSTEM_PROMPT = (
    "You are a read-only SQL assistant for an infrastructure monitoring database.\n"
    "DO NOT generate DML statements (INSERT, UPDATE, DELETE, ALTER, DROP, TRUNCATE,"
    " GRANT, REVOKE).\n"
    "Only generate SELECT queries. Always include a LIMIT clause.\n"
    "Prefer explicit column lists over SELECT *.\n"
    "Only reference columns that appear in the schema below for a given table —"
    " never guess or assume a column exists by convention.\n"
    "A column listed as `name:jsonb` is jsonb-typed — never compare it directly"
    " with ILIKE/LIKE; cast to text first (`col::text ILIKE '...'`) or extract a"
    " key as text (`col->>'key' ILIKE '...'`).\n"
    "Double-check the query before executing.\n"
    "If uncertain, explain what you would query rather than executing.\n"
    "Schema (table(columns); `col:jsonb` = jsonb-typed, cast before ILIKE/LIKE):\n"
    + _SCHEMA_BLOCK
    + "\n\n"
    + _FEW_SHOT_EXAMPLES
)

# Result truncation threshold in serialised JSON bytes.
_MAX_RESULT_BYTES = 12_000


# AST deny-set: any of these node types anywhere in the parse tree rejects the
# query. Built via getattr so minor sqlglot renames (AlterTable -> Alter, ...)
# never silently shrink the set to nothing.
_AST_DENY_NAMES = [
    "Insert",
    "Update",
    "Delete",
    "Drop",
    "Create",
    "Merge",
    "Grant",
    "Command",  # raw/verbatim statements sqlglot can't type (COPY, VACUUM, CALL, ...)
    "TruncateTable",
    "Alter",
    "AlterTable",
]
_AST_DENY_NODES = tuple(getattr(sqlexp, name) for name in _AST_DENY_NAMES if hasattr(sqlexp, name))


def _ast_readonly_guard(query: str) -> None:
    """AST statement-type check (item 1.6 / F-004.1) — regex alone is bypassable.

    Rejects: unparseable SQL, multiple statements, any non-SELECT root, SELECT
    INTO (creates a table), SELECT ... FOR UPDATE/SHARE (takes row locks), and
    any mutating node anywhere in the tree (catches CTE-wrapped DML).
    """
    try:
        statements = sqlglot.parse(query, read="postgres", error_level=sqlglot.ErrorLevel.IMMEDIATE)
    except sqlglot.errors.ParseError as exc:
        raise ValueError(f"SQL failed to parse — rejected (read-only guard): {exc}") from exc
    if len(statements) != 1 or statements[0] is None:
        raise ValueError("Exactly one SQL statement is permitted")
    root = statements[0]
    if not isinstance(root, sqlexp.Select):
        raise ValueError("Only SELECT statements are permitted (AST check)")
    if root.args.get("into") is not None:
        raise ValueError("SELECT INTO is not permitted (creates a table)")
    if root.args.get("locks"):
        raise ValueError("SELECT ... FOR UPDATE/SHARE is not permitted (takes locks)")
    for node in root.walk():
        n = node[0] if isinstance(node, tuple) else node
        if isinstance(n, _AST_DENY_NODES):
            raise ValueError(f"Mutating statement node {type(n).__name__} — read-only queries only")


def _autocast_jsonb_pattern_match(query: str) -> str:
    """Defense-in-depth: rewrite bare `<jsonb_col> ILIKE/LIKE ...` into
    `<jsonb_col>::text ILIKE/LIKE ...` before execution.

    Bug (audit 2026-08-06): Postgres jsonb has NO pattern-match operator —
    `... WHERE details ILIKE '%caddy%'` against a jsonb column raises
    ``operator does not exist: jsonb ~~* unknown``. The system prompt now
    tells the LLM to cast (see `_FEW_SHOT_EXAMPLES`), but that is advisory
    only — an LLM is non-deterministic and this failure was reproduced twice
    with differently-shaped generated SQL. This function is the mechanical
    backstop: it fires regardless of whether the model followed the prompt.

    Implementation: parse with sqlglot (same parser/dialect as
    `_ast_readonly_guard`), find every `Like`/`ILike` node (this also matches
    the `~~`/`~~*` operator spellings — sqlglot's postgres dialect normalizes
    those to the same node types), and if its left operand is a bare `Column`
    whose name is a known jsonb column, wrap it in `CAST(col AS TEXT)`.
    Columns already cast (`col::text`) or already extracted as text
    (`col->>'key'`) are left untouched — in both cases the left operand is no
    longer a bare `Column` node, so the `isinstance` check naturally skips
    them (no double-casting, no breaking already-correct queries).

    Best-effort: if the query fails to parse here, it is returned unchanged
    — `_ast_readonly_guard`, called right after this by `sanitize_sql_query`,
    is the authoritative parser and will raise the real, informative error.
    Duplicating that error-handling here would only obscure it.
    """
    if not _JSONB_COLUMN_NAMES:
        return query
    try:
        statements = sqlglot.parse(query, read="postgres", error_level=sqlglot.ErrorLevel.IMMEDIATE)
    except sqlglot.errors.ParseError:
        return query
    if len(statements) != 1 or statements[0] is None:
        return query
    root = statements[0]
    rewritten = False
    for node in root.walk():
        n = node[0] if isinstance(node, tuple) else node
        if not isinstance(n, (sqlexp.Like, sqlexp.ILike)):
            continue
        left = n.this
        if isinstance(left, sqlexp.Column) and left.name in _JSONB_COLUMN_NAMES:
            n.set("this", sqlexp.Cast(this=left.copy(), to=sqlexp.DataType.build("text")))
            rewritten = True
    if not rewritten:
        return query
    logger.info(
        "QueryAgent: auto-cast jsonb column(s) to text before ILIKE/LIKE "
        "(defense-in-depth for uncast pattern-match generated by the LLM)."
    )
    return root.sql(dialect="postgres")


def sanitize_sql_query(q: str) -> str:
    """Validate and sanitize a SQL query before execution.

    Layered (item 1.6): fast regex pre-checks (kept as defense in depth), then
    an AST statement-type check via sqlglot. Raises ValueError on any violation.
    Enforces SELECT-only, adds LIMIT if missing.

    Also rewrites bare jsonb-column ILIKE/LIKE comparisons to add an explicit
    `::text` cast (`_autocast_jsonb_pattern_match`) — Postgres has no
    pattern-match operator for jsonb, so an uncast comparison always fails at
    execution time regardless of how well-formed the SQL otherwise is.
    """
    query = str(q or "").strip().rstrip(";")
    if not query:
        raise ValueError("Empty query")
    if not query.upper().lstrip().startswith("SELECT"):
        raise ValueError("Only SELECT statements are permitted")
    if _SQL_DENY_RE.search(query):
        raise ValueError("DML/DDL detected — read-only queries only")
    if _MULTI_STMT_RE.search(query):
        raise ValueError("Multiple statements not allowed")
    # Defense-in-depth: cast jsonb columns before ILIKE/LIKE. Runs before the
    # AST guard so the guard's own parse (and the LIMIT check below) see the
    # final, execution-bound query text.
    query = _autocast_jsonb_pattern_match(query)
    # AST statement-type check — catches what the regexes cannot
    # (e.g. SELECT INTO, comment/structure obfuscation, unparseable SQL).
    _ast_readonly_guard(query)
    # Enforce LIMIT if missing.
    if not re.search(r"\bLIMIT\b", query, re.IGNORECASE):
        query += " LIMIT 100"
    return query


@tool
def execute_sql(query: str) -> str:
    """Execute a read-only SELECT query against the infrastructure database.

    Validates the query against the read-only guard before execution.
    Returns results as a JSON-encoded list of row dicts.
    Only SELECT queries against allowed tables are permitted.

    Args:
        query: A SQL SELECT statement to execute against the infra DB.
    """
    try:
        validated = sanitize_sql_query(query)
    except ValueError as exc:
        raise ToolException(f"SQL rejected by read-only guard: {exc}") from exc
    try:
        engine = get_readonly_engine()
        with engine.connect() as conn:
            result = conn.execute(text(validated))
            rows = [dict(row._mapping) for row in result.fetchall()]
    except Exception as exc:
        raise ToolException(f"SQL execution failed: {exc}") from exc
    serialized = json.dumps(rows, default=str)
    if len(serialized) > _MAX_RESULT_BYTES:
        columns: list[str] = list(rows[0].keys()) if rows else []
        return json.dumps(
            {
                "truncated": True,
                "count": len(rows),
                "columns": columns,
                "hint": "Result too large — add a WHERE or LIMIT clause to narrow the query",
            }
        )
    return serialized


class QueryAgent(LLMAgent):
    spec = AgentSpec(
        domain="query",
        tier=Tier.ON_DEMAND,
        schedule=None,
        # max_staleness=None: QueryAgent is the on-demand NL->SQL chat agent, not a
        # data collector. Its only scheduled activity is a bespoke weekly connectivity
        # self-probe (scheduler.py's add_job_scoped("query", "health", ...)), wired
        # outside the declarative _DEFAULT_SCHEDULES machinery. Because it has no
        # declarative schedule it must not be freshness-monitored either -- every other
        # domain honours the invariant "freshness-monitored IFF declaratively
        # scheduled". query was the sole violator (max_staleness set, schedule=None),
        # so check_collection_health() alerted "query: no collection run has ever
        # started" hourly forever. On-demand-only agents (like drift/notification) are
        # not freshness-monitored; query is now treated the same way (TRK-111).
        max_staleness=None,
    )
    llm_role = "sql"

    def collect(self, scope: str = "all") -> list[dict]:
        """QueryAgent runs a lightweight DB connectivity check when scheduled.

        The full NL-to-SQL capability is available on-demand via query().
        The scheduled run verifies DB connectivity and warms the LLM cache.
        """
        if scope == "health":
            # Lightweight health-check: verify DB is reachable and tables exist.
            try:
                engine = get_readonly_engine()
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return [{"name": "db_connectivity", "type": "health_check", "data": {"ok": True}}]
            except Exception as exc:  # noqa: BLE001
                logger.warning("QueryAgent health check failed: %s", exc)
                # SEC-2: a malformed POSTGRES_READONLY_URL/POSTGRES_URL can raise with
                # the DSN (incl. credentials) embedded verbatim in str(exc) — this
                # error string is persisted onto the generic Resource/Snapshot rows
                # via ETLConnector.run(), so scrub before it ever reaches storage.
                return [
                    {
                        "name": "db_connectivity",
                        "type": "health_check",
                        "data": {"ok": False, "error": scrub_dsn(str(exc))},
                    }
                ]
        return []

    def query(self, question: str) -> dict:
        """Answer a natural-language infrastructure question via read-only SQL.

        Early-validates the question to reject obvious DML without calling the LLM.
        Delegates SQL generation and execution to the LLM via reason() + execute_sql tool.

        Returns:
            On success:
                {
                    "question": str,
                    "result": str,   # LLM's final answer text
                    "sql": str,      # always "" — SQL is captured inside execute_sql tool
                }
            On error:
                {
                    "question": str,
                    "error": str,
                    "result": None,
                    "sql": "",
                }
        """
        q = str(question or "").strip()
        if not q:
            return {"question": question, "error": "Empty question", "result": None, "sql": ""}

        # Early validation: reject if the question itself contains obvious DML or multi-statement
        # SQL. Avoids calling the LLM for structurally-invalid inputs. Natural-language questions
        # that happen to mention DML verbs (e.g. "how many rows were deleted?") do not trigger
        # this guard because _MULTI_STMT_RE requires a ';' and _SQL_DENY_RE matches at word
        # boundaries — "were deleted" won't match but "DELETE FROM" will.
        if _SQL_DENY_RE.search(q) or _MULTI_STMT_RE.search(q):
            return {
                "question": question,
                "error": "Query contains DML/DDL statements or multiple SQL statements — not allowed",
                "result": None,
                "sql": "",
            }

        # GitLab #165 fail-fast preflight: if no reasoner model is configured at
        # all, say so immediately and DO NOT invoke the model. Discovering this
        # inside reason() costs the caller a full graph build plus retries and
        # surfaces as an opaque provider SDK error.
        settings = get_settings()
        missing = _missing_llm_credential(settings)
        if missing:
            provider = (getattr(settings, "llm_provider", "") or "anthropic").lower()
            return {
                "question": question,
                "error": (
                    f"no LLM configured for provider {provider} ({missing}) — "
                    "query_nl requires a reasoner model"
                ),
                "result": None,
                "sql": "",
            }

        try:
            result_text = self.reason(
                task=f"{_SQL_SYSTEM_PROMPT}\n\nQuestion: {question}",
                tools=[execute_sql],
                max_iters=_QUERY_MAX_ITERS,
                # #165: bound the TOTAL wall clock. This path bypasses
                # ETLConnector.run() entirely, so this deadline is the only
                # thing standing between a stuck model and an MCP client that
                # hangs until its own transport timeout fires with no error.
                deadline_seconds=getattr(settings, "mcp_tool_timeout_seconds", 60) or None,
                attempts=_QUERY_REASON_ATTEMPTS,
            )
            return {"question": question, "result": result_text, "sql": ""}
        except Exception as exc:
            logger.warning("QueryAgent.query() failed: %s", exc, exc_info=True)
            return {"question": question, "error": str(exc), "result": None, "sql": ""}
