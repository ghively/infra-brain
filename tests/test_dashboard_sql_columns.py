"""Guards against the class of bug where a raw SQL query references a column or
table that does not exist in the ORM schema.

This is the June-2026 audit safety net, repointed after the Streamlit UI was
retired. The dashboard data API (``dashboard_api.py``) is built almost entirely
on the SQLAlchemy ORM (type-safe, no column-name strings to drift), so the
remaining raw-SQL surface is the chat agent's read-only query tools
(``infra_brain/chat/tools.py``) plus any inline ``text(...)`` SQL in the API.
This validator parses every such SQL string and asserts each column reference
resolves to a real column on the table it is queried from — no DB required.
"""

from __future__ import annotations

import os
import pathlib
import re
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text

import infra_brain.chat as _chat_pkg
import infra_brain.dashboard_api as _api_mod
from infra_brain.db.models import Base

# Roots scanned for raw SQL: the chat tools package, the dashboard API module,
# and api/routers/ (P7.3, 2026-08-01 -- this validator previously missed raw
# SQL in the router modules entirely; hosts.py's _RESOURCE_OWNERSHIP_UPSERT/
# _HOST_PURPOSE_UPSERT and fleet.py's raw INSERT..ON CONFLICT statements went
# unvalidated since dashboard_api.py is now a re-export shim with no SQL of
# its own).
SCAN_ROOT = pathlib.Path(_chat_pkg.__file__).parent.parent  # src/infra_brain
SQL_FILES = [
    pathlib.Path(_chat_pkg.__file__).parent,  # infra_brain/chat/ (dir)
    pathlib.Path(_api_mod.__file__),  # infra_brain/dashboard_api.py (file)
    SCAN_ROOT / "api" / "routers",  # infra_brain/api/routers/ (dir)
]

COLUMNS_BY_TABLE: dict[str, set[str]] = {
    name: {c.name for c in table.columns} for name, table in Base.metadata.tables.items()
}
ALL_TABLES = set(COLUMNS_BY_TABLE)

# Lowercase identifiers that are not columns. SQL keywords/functions are written
# UPPERCASE throughout the code, so the only lowercase non-column tokens we
# expect are these literals.
_NON_COLUMN_TOKENS = {"true", "false", "null"}


_UPDATE_SET_RE = re.compile(r"UPDATE\s+[a-z_][a-z0-9_]*.*?\bSET\b", re.IGNORECASE | re.DOTALL)
# The fragment must START (after whitespace) with a real SQL keyword, not
# merely CONTAIN one -- api/routers/'s docstrings are prose-heavy (P7.3) and
# things like "Read-only (SELECT only)." trip a bare substring check that
# happened to never fire in the narrower chat/-only scope this file used to
# have. UPDATE alone isn't enough either -- "Update title or is_public..." is
# a genuine one-line docstring in ui.py that starts with the word "Update";
# requiring an eventual SET (via _UPDATE_SET_RE, checked separately below)
# is what actually distinguishes real SQL from an imperative-mood docstring.
_SQL_START_RE = re.compile(r"^\s*(SELECT|INSERT\s+INTO|UPDATE)\b", re.IGNORECASE)


def _is_sql_of_interest(fragment: str) -> bool:
    """SELECT (existing coverage) or INSERT INTO / UPDATE ... SET (P7.3 —
    api/routers/'s upsert statements, previously invisible to this file's
    SELECT-only string filter)."""
    m = _SQL_START_RE.match(fragment)
    if not m:
        return False
    if m.group(1).upper() == "UPDATE":
        return _UPDATE_SET_RE.match(fragment.lstrip()) is not None
    return True


def _iter_sql() -> list[tuple[str, str]]:
    """Return (relative_path, sql) for every SQL string in the scanned roots."""
    files: list[pathlib.Path] = []
    for root in SQL_FILES:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
        else:
            files.append(root)
    out: list[tuple[str, str]] = []
    for py in files:
        src = py.read_text(encoding="utf-8")
        rel = str(py.relative_to(SCAN_ROOT))
        for m in re.finditer(r'"""(.*?)"""', src, re.DOTALL):
            if _is_sql_of_interest(m.group(1)):
                out.append((rel, m.group(1)))
        for m in re.finditer(r'"((?:[^"\n]|\\")*SELECT(?:[^"\n]|\\")*)"', src):
            out.append((rel, m.group(1)))
    return out


def _balanced_paren_spans(s: str) -> list[tuple[int, int]]:
    """Yield (start, end) index spans of top-level parenthesised groups."""
    spans, depth, start = [], 0, -1
    for i, ch in enumerate(s):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append((start, i + 1))
    return spans


def _scope_tables(fragment: str) -> tuple[dict[str, str], set[str]]:
    """Parse FROM/JOIN clauses -> (alias->table map, set of tables in scope)."""
    alias_map: dict[str, str] = {}
    tables: set[str] = set()
    for tbl, alias in re.findall(
        r"(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)(\s+[a-z][a-z0-9_]*)?", fragment
    ):
        tables.add(tbl)
        alias = alias.strip()
        if alias:
            alias_map[alias] = tbl
    return alias_map, tables


def _validate(sql: str, errors: list[str]) -> None:
    """Recursively validate a SQL fragment, descending into subqueries first."""
    work = re.sub(r"'[^']*'", " ", sql)  # strip string literals

    for start, end in reversed(_balanced_paren_spans(work)):
        inner = work[start + 1 : end - 1]
        if "SELECT" in inner:
            _validate(inner, errors)
            work = work[:start] + " " + work[end:]

    alias_map, tables = _scope_tables(work)
    scope_cols: set[str] = set()
    for t in tables:
        scope_cols |= COLUMNS_BY_TABLE.get(t, set())

    aliases_defined = set(re.findall(r"\bAS\s+([a-z_][a-z0-9_]*)", work, re.IGNORECASE))
    params = set(re.findall(r":([a-z_][a-z0-9_]*)", work))
    allowed = aliases_defined | set(alias_map) | params | tables | ALL_TABLES | _NON_COLUMN_TOKENS

    for ref_alias, col in re.findall(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)", work):
        tbl = alias_map.get(ref_alias) or (ref_alias if ref_alias in ALL_TABLES else None)
        if tbl is None:
            continue
        if col not in COLUMNS_BY_TABLE[tbl]:
            errors.append(f"{ref_alias}.{col}: '{col}' is not a column of '{tbl}'")

    bare = re.sub(r"\b[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*\b", " ", work)
    if tables:
        for tok in re.findall(r"\b([a-z_][a-z0-9_]*)\b", bare):
            if tok in allowed:
                continue
            if tok not in scope_cols:
                errors.append(
                    f"bare '{tok}' is not a column of any queried table "
                    f"({', '.join(sorted(tables))})"
                )


def _validate_insert_or_update(sql: str, errors: list[str]) -> None:
    """Validate INSERT INTO (cols)/UPDATE ... SET column references against
    the schema (P7.3) -- narrower than _validate(), which is SELECT-shaped
    (FROM/JOIN table scoping) and would silently no-op on these statements
    (no FROM/JOIN to scope against, so its bare-token check never runs).
    Handles exactly the two raw-SQL shapes found in api/routers/: a plain
    INSERT INTO tbl (col, ...), and an ON CONFLICT ... DO UPDATE SET
    col = excluded.col style upsert (the RHS ``excluded.col`` is the
    about-to-be-inserted row, not a validation target; only the LHS column
    name before ``=`` is checked).
    """
    m = re.search(r"INSERT\s+INTO\s+([a-z_][a-z0-9_]*)\s*\(([^)]*)\)", sql, re.IGNORECASE)
    if m:
        table, cols_str = m.group(1), m.group(2)
        table_cols = COLUMNS_BY_TABLE.get(table)
        if table_cols is None:
            errors.append(f"INSERT INTO references unknown table '{table}'")
            return
        for col in (c.strip() for c in cols_str.split(",")):
            if col and col not in table_cols:
                errors.append(f"INSERT INTO {table}: '{col}' is not a column of '{table}'")
        return

    m = re.search(r"UPDATE\s+([a-z_][a-z0-9_]*)", sql, re.IGNORECASE)
    set_match = _UPDATE_SET_RE.search(sql)
    if m and set_match:
        table = m.group(1)
        table_cols = COLUMNS_BY_TABLE.get(table)
        if table_cols is None:
            errors.append(f"UPDATE references unknown table '{table}'")
            return
        set_clause = sql[set_match.end():]
        where_split = re.split(r"\bWHERE\b", set_clause, maxsplit=1, flags=re.IGNORECASE)[0]
        for col in re.findall(r"\b([a-z_][a-z0-9_]*)\s*=", where_split):
            if col not in table_cols:
                errors.append(f"UPDATE {table} SET: '{col}' is not a column of '{table}'")


DASHBOARD_SQL = _iter_sql()


def test_found_sql_to_check():
    # Sanity: make sure the extractor actually found the chat tool queries.
    assert len(DASHBOARD_SQL) >= 4, f"expected to find raw SQL queries, found {len(DASHBOARD_SQL)}"


@pytest.mark.parametrize(
    "path,sql", DASHBOARD_SQL, ids=[f"{p}:{i}" for i, (p, _) in enumerate(DASHBOARD_SQL)]
)
def test_dashboard_sql_columns_exist(path, sql):
    errors: list[str] = []
    if "SELECT" in sql:
        _validate(sql, errors)
    else:
        _validate_insert_or_update(sql, errors)
    assert not errors, f"Schema mismatch in {path}:\n  " + "\n  ".join(errors)


def test_validator_rejects_known_bad_query():
    """Prove the validator catches the exact bugs the June audit fixed."""
    bad = """
        SELECT hostname, domain, resource_type, zone, status, last_seen_at
        FROM resources
        WHERE (:domain = '' OR domain = :domain)
        ORDER BY last_seen_at DESC
    """
    errors: list[str] = []
    _validate(bad, errors)
    joined = " ".join(errors)
    assert "resource_type" in joined
    assert "last_seen_at" in joined
    assert "hostname" in joined  # exists in another table, but not on resources


def test_validator_rejects_wrong_table_placement():
    """resources has no 'status' column even though other tables do."""
    bad = "SELECT (SELECT COUNT(*) FROM resources WHERE status = 'eol') AS eol_count"
    errors: list[str] = []
    _validate(bad, errors)
    assert any("status" in e for e in errors), errors


# --------------------------------------------------------------------------- #
# P7.3: INSERT/UPDATE column validation (api/routers/'s upsert statements)
# --------------------------------------------------------------------------- #

def test_insert_validator_accepts_real_columns():
    good = "INSERT INTO resources (id, name, domain, type, source) VALUES (:id, :name, :domain, :type, :source)"
    errors: list[str] = []
    _validate_insert_or_update(good, errors)
    assert errors == []


def test_insert_validator_rejects_bad_column():
    bad = "INSERT INTO resources (id, made_up_column) VALUES (:id, :x)"
    errors: list[str] = []
    _validate_insert_or_update(bad, errors)
    assert any("made_up_column" in e for e in errors), errors


def test_insert_validator_rejects_unknown_table():
    bad = "INSERT INTO not_a_real_table (id) VALUES (:id)"
    errors: list[str] = []
    _validate_insert_or_update(bad, errors)
    assert any("unknown table" in e for e in errors), errors


def test_update_on_conflict_validator_accepts_real_columns():
    good = """
        INSERT INTO resource_ownership (id, resource_id, owner_team)
        VALUES (:id, :resource_id, :owner_team)
        ON CONFLICT (resource_id)
        DO UPDATE SET owner_team = excluded.owner_team
    """
    errors: list[str] = []
    _validate_insert_or_update(good, errors)
    assert errors == []


def test_bare_update_set_validator_rejects_bad_column():
    bad = "UPDATE resources SET made_up_column = :x WHERE id = :id"
    errors: list[str] = []
    _validate_insert_or_update(bad, errors)
    assert any("made_up_column" in e for e in errors), errors


def test_bare_update_set_validator_accepts_real_column():
    good = "UPDATE resources SET name = :x WHERE id = :id"
    errors: list[str] = []
    _validate_insert_or_update(good, errors)
    assert errors == []


# --------------------------------------------------------------------------- #
# Layer 2: real PostgreSQL execution (opt-in)
# --------------------------------------------------------------------------- #
# All-zeros UUID sentinel for uuid-typed bind params. Binding "" (empty string)
# to a uuid column raises `invalid input syntax for type uuid: ""` on Postgres —
# the F-012 harness bug. The sentinel is a syntactically valid uuid that matches
# no row, so the query executes (which is all Layer 2 asserts).
_SENTINEL_UUID = "00000000-0000-0000-0000-000000000000"

_PARAM_DEFAULTS = {
    "domain": "",
    "status": "",
    "zone": "",
    "agent": "",
    "resource_type": "",
    "hours": 24,
    "limit": 10,
    "days": 30,
    "name": "",
    # text-typed params (compliance_violations lookups in chat/tools.py,
    # Phase 3 Task 1's query_compliance)
    "host": "",
    "rule": "",
    # uuid-typed params (resources.id / linux_hosts.id lookups in chat/tools.py)
    "rid": _SENTINEL_UUID,
    "hid": _SENTINEL_UUID,
    # text/int-typed params (#81's query_vulnerabilities/query_eol_status in chat/tools.py)
    "severity": "",
    "cutoff_days": 30,
    # P7.3: api/routers/'s upsert statements (ResourceOwnership/HostPurposeMap)
    "id": _SENTINEL_UUID,
    "resource_id": _SENTINEL_UUID,
    "owner_team": "",
    "on_call_rotation": "",
    "criticality_tier": "",
    "source": "",
    "hostname": "",
    "purpose": "",
    "vlan": "",
    "subnet": "",
    "updated_at": datetime.now(UTC),
}


@pytest.fixture(scope="module")
def pg_engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("set TEST_DATABASE_URL=postgresql://... to run SQL execution tests")
    eng = create_engine(url)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    # P7.3: ResourceOwnership.resource_id is a real FK into resources.id --
    # the sentinel UUID _PARAM_DEFAULTS uses for every uuid-typed param
    # needs a matching parent row here, or hosts.py's _RESOURCE_OWNERSHIP_UPSERT
    # correctly fails this Layer-2 check with a FK violation that has nothing
    # to do with the column/syntax correctness this file actually verifies.
    # Inserted via the ORM (not hand-written raw SQL) so it stays correct if
    # Resource ever gains another NOT NULL column.
    from sqlalchemy.orm import Session

    from infra_brain.db.models import Resource

    with Session(eng) as session:
        session.add(
            Resource(
                id=uuid.UUID(_SENTINEL_UUID),
                domain="test",
                type="test",
                name="sql-column-test-sentinel",
                source="test",
            )
        )
        session.commit()
    return eng


@pytest.mark.parametrize(
    "path,sql", DASHBOARD_SQL, ids=[f"{p}:{i}" for i, (p, _) in enumerate(DASHBOARD_SQL)]
)
def test_dashboard_sql_executes_on_postgres(pg_engine, path, sql):
    params = {p: _PARAM_DEFAULTS.get(p, "") for p in re.findall(r"(?<!:):([a-z_][a-z0-9_]*)", sql)}
    with pg_engine.connect() as conn:
        conn.execute(text(sql), params)  # raises if a column/table/syntax is wrong


def test_sentinel_uuid_is_valid_uuid():
    """The uuid sentinel must parse as a real UUID — otherwise Postgres would
    reject it exactly like the '' it replaces (F-012)."""
    assert str(uuid.UUID(_SENTINEL_UUID)) == _SENTINEL_UUID


def test_param_defaults_cover_all_bound_params():
    """Every bind parameter in the scanned raw SQL has an explicit, type-correct
    default in _PARAM_DEFAULTS.

    Guards the F-012 failure mode: a param absent from _PARAM_DEFAULTS silently
    falls back to "" (see test_dashboard_sql_executes_on_postgres), which Postgres
    rejects for uuid- and integer-typed columns — making the sql-execution-check
    CI job fail on a harness bug instead of real column drift.
    """
    bound: set[str] = set()
    for _path, sql in DASHBOARD_SQL:
        bound |= set(re.findall(r"(?<!:):([a-z_][a-z0-9_]*)", sql))
    missing = bound - set(_PARAM_DEFAULTS)
    assert not missing, (
        f"Bind params with no explicit _PARAM_DEFAULTS entry: {sorted(missing)}. "
        "Add a type-correct default (uuid params use _SENTINEL_UUID, ints use an int)."
    )
