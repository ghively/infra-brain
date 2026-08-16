---
name: pg-gate-check
description: >
  Use BEFORE pushing any branch that touches db/models/, alembic/versions/, dialect-specific
  column types (pgvector Vector, JSONB, enums, server_default), raw SQL in chat/tools.py or
  api/routers/, ORM queries in agents/ or etl/, or the CI postgres service image — the four hard
  MR gates (lock-freshness, migration-parity, sql-execution-check, agent-orm-check) run against
  real PostgreSQL, and the local full suite runs on sqlite,
  so "2718 passed, 0 failed" locally is NOT evidence those gates will pass. Also use when an
  MR pipeline has already failed one of those gates and you need to reproduce it locally.
disable-model-invocation: false
---

# /pg-gate-check

Replicates the four MR-blocking CI gates (`lock-freshness` + `migration-parity` +
`sql-execution-check` + `agent-orm-check`) locally against the **same Postgres service
image CI uses**, including each job's exact schema-reset ritual, before you push.
One command:

```bash
bash .claude/skills/pg-gate-check/run.sh

# schema-only, skipping the slow agent/etl ORM gate:
PG_GATE_SKIP_ORM=1 bash .claude/skills/pg-gate-check/run.sh
```

## Why this exists (real incident, 2026-07-21, MR !195 pipeline 1355)

A feature branch adding a pgvector column passed the full local suite (2718 passed,
0 failed), ruff, AND three specialist reviewers — then failed BOTH hard MR gates.
The local suite runs on sqlite; the gates run on real PostgreSQL. Two defects were
structurally invisible to every sqlite-based check:

1. `Base.metadata.create_all()` (the gates' schema path) never ran
   `CREATE EXTENSION vector` — only the alembic migration did. **Two schema-creation
   paths exist**: alembic migrations AND `create_all`. Any environment prerequisite a
   migration sets up (extensions especially) must also be handled for `create_all`,
   e.g. a postgres-only `event.listen(Base.metadata, "before_create", DDL(...).execute_if(...))`.
2. The migration created an HNSW index the ORM never declared, so the gate's
   `compare_metadata` parity check flagged `remove_index` drift. **Every index/column
   a migration creates must be declared on the model** (the ORM is the source of truth).

## Why gate 4 exists (real incident, 2026-08-12, MR !34 → hotfix MR !36)

The first three gates all touch real PostgreSQL — but between them they only execute
the **migration chain** and the **raw dashboard/chat SQL**. Agent-layer ORM queries
(`agents/`, `etl/`) only ever met in-memory SQLite. TRK-350's event-shaped drift filter
built a subquery with `.scalar_subquery()` and re-wrapped it in `select()`, compiling to
`IN (SELECT (SELECT ...))`. PostgreSQL evaluates that inner select as a per-row scalar
and raises `CardinalityViolation` as soon as it matches a second row. SQLite tolerates
it. 5,215 tests passed; the FIRST live drift run after deploy crashed. TRK-322's
`ROW_NUMBER()` work escaped only because those queries happened to live in files
`sql-execution-check` already covered.

`agent-orm-check` (TRK-356) closes it: `PG_GATE_DSN` flips
`tests/support/pg.py::make_engine` from in-memory SQLite to the real container, and
every module listed in **`tests/support/agent_orm_check_paths.txt`** re-runs
unchanged against PostgreSQL. **No tests are duplicated** — the same modules run on
both dialects, SQLite in `test-suite`, PostgreSQL here.

That manifest is the single source of truth for the selection: this script and the
`agent-orm-check:` CI job both read it, so the local replica cannot drift from the
gate it replicates (`tests/test_gitlab_ci_structure.py` asserts both still do, that
every listed path exists, and that every listed module actually calls
`make_engine()` rather than building its own SQLite engine). It started as
`tests/agents` + `tests/etl` + three named modules and was extended to ~96 paths —
the router/dashboard, MCP-tool, chat-tool, graph phase 2/3, `tools/` and `db/`
model-round-trip surfaces. To add a file: route its fixture through `make_engine()`,
append the path, and re-run gate 4 both ways.

Note it is `PG_GATE_DSN`, **not** `POSTGRES_URL`. `POSTGRES_URL` is what application
code reads, and `tests/conftest.py` pins it to SQLite on purpose; a fixture-only switch
cannot leak a real connection into a collector at runtime.

## The iron rule

If the diff touches any trigger surface below, a green local suite is not a
pre-push verification — **run the gate replication.** Reviewer approval does not
substitute; the reviewers in the incident above all passed the broken code.

Trigger surfaces:

| Surface | Why |
|---|---|
| `src/infra_brain/db/models/**` | parity gate compares ORM metadata vs migrated schema |
| `alembic/versions/**` | both gates run the migration chain / create_all on real PG |
| Dialect-specific types (`Vector`, `JSONB`, enums, `server_default`, `with_variant`) | sqlite silently accepts what PG rejects |
| Raw SQL in `chat/tools.py` / `api/routers/**` | sql-execution-check executes it on PG |
| ORM queries anywhere covered by `tests/support/agent_orm_check_paths.txt` — `agents/**`, `etl/**`, `api/routers/**`, `mcp_server.py`, `chat/**`, `graph_phase2/3`, `tools/**` (subqueries, window functions, `in_()`/`notin_()`, `distinct`, casts, FK writes) | agent-orm-check executes them on PG; sqlite accepts SQL PG rejects and ignores FKs PG enforces |
| `.gitlab-ci.yml` postgres `services:` image | you're changing the gate's own environment |

## What the script does (mirror of `.gitlab-ci.yml`, keep in sync)

1. Reads the postgres service image from `.gitlab-ci.yml` (never hardcodes it).
2. Starts a throwaway container of that exact image (`--rm`, off-standard port).
3. **migration-parity**: `DROP SCHEMA public CASCADE` reset (note: this also drops
   extensions — CI's self-hosted runner reuses the service DB across runs, so the
   gate must survive a from-nothing schema), then
   `POSTGRES_URL=... MIGRATION_PARITY_REQUIRE_DB=1 pytest tests/test_migration_parity.py tests/test_migration.py`.
4. **sql-execution-check**: schema reset again, then
   `TEST_DATABASE_URL=... pytest tests/test_dashboard_sql_columns.py`.
5. **agent-orm-check**: schema reset again, then `PG_GATE_DSN=... pytest <every path
   in tests/support/agent_orm_check_paths.txt>` (~5 min, ~2,990 tests). Skippable
   with `PG_GATE_SKIP_ORM=1` when you only changed schema and want the fast loop.
6. Tears the container down. Exit 0 = every gate replicated green.

Worktree gotcha (cost real time before): the venv resolves `infra_brain` from the
MAIN checkout. The script sets `PYTHONPATH=src` so it always tests the code in the
directory you run it from — run it from the branch's worktree root.

## Common mistakes

| Mistake | Reality |
|---|---|
| "Full suite is green, push it" | The suite is sqlite; the gates are PostgreSQL. Different engine, different schema path. |
| "Reviewers approved the migration" | Reviewers read code; the gates execute it. Both approved the 2026-07-21 failure. |
| "The migration creates the extension, so we're covered" | `create_all` is a second schema path that never runs migrations. Cover both. |
| "I'll just add the index in the migration" | `compare_metadata` requires the ORM to declare it too, or parity fails. |
| Testing against local `trk-pg` / stock `postgres:15` | Use the image CI actually uses (the script reads it from `.gitlab-ci.yml`). |
| Skipping the `DROP SCHEMA` reset | CI's reused service DB accumulates state; the reset is part of the gate's semantics. |
| "It's just an ORM query, SQLAlchemy is portable" | SQLAlchemy compiles portable *syntax*, not portable *semantics*. `IN (SELECT (SELECT ...))` is legal on both; only PG enforces the cardinality. That is gate 4's whole reason to exist. |
| Writing a new PG-only copy of an agent test | Don't. Build the engine with `tests.support.pg.make_engine()` and the one test runs on both dialects. |
