---
name: validate-sql
description: >
  Run the infra-brain SQL column validator against all raw dashboard/chat queries. Catches
  column name drift between the chat tools (and any inline dashboard_api SQL) and the
  SQLAlchemy ORM schema — the class of silent bug that was found in the June 2026 audit
  (queries returning empty results in production while passing the test suite).
disable-model-invocation: false
---

# /validate-sql [file-or-query]

Runs the two-layer SQL guard from `tests/test_dashboard_sql_columns.py`. This same test
file is what the `sql-execution-check` MR gate runs in CI — one of only two MR-blocking
gates (alongside `migration-parity`), so keeping it green locally keeps the pipeline green.

## Arguments (optional)
- No argument → validate ALL raw dashboard/chat SQL (chat tools + inline dashboard_api SQL)
- `[file]` → validate SQL from a specific file
- `[sql]` → validate an ad-hoc SQL string directly

## Layer 1: Static Column Validator (always runs, no DB needed)

```bash
.venv/bin/python -m pytest tests/test_dashboard_sql_columns.py -v
```

This runs without any database connection. It:
1. Extracts all SQL strings from `src/infra_brain/chat/` and `dashboard_api.py`. Note:
   `dashboard_api.py` is now a re-export shim with no SQL of its own (post god-file split)
   — real route handlers, including some inline raw SQL (e.g. `api/routers/fleet.py`), now
   live under `src/infra_brain/api/routers/`, which this validator does **not** currently
   scan. Until `tests/test_dashboard_sql_columns.py`'s `SQL_FILES` list is updated to
   include `api/routers/`, run a manual check there when editing raw SQL — don't rely on
   this skill to catch it.
2. Parses each query — strips string literals, recurses into subqueries
3. Resolves table aliases from FROM/JOIN clauses
4. Checks every column reference against the SQLAlchemy ORM `Base.metadata`
5. Reports the exact column name and table where the mismatch occurred

If `TEST_DATABASE_URL` is set, Layer 2 also runs:

## Layer 2: Live PostgreSQL Execution (opt-in)

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/infra_brain \
  .venv/bin/python -m pytest tests/test_dashboard_sql_columns.py -v
```

This executes every query against a real Postgres schema (created fresh in a transaction
that is rolled back). Catches Postgres-specific syntax errors that SQLite ignores, and
validates the full query plan including JOINs.

## Validate an Ad-Hoc Query

To check a SQL string before adding it to the codebase:

```python
# Quick inline check:
.venv/bin/python -c "
from tests.test_dashboard_sql_columns import _validate
sql = '''
    SELECT r.name AS hostname, r.domain, de.field AS field_name
    FROM drift_events de
    LEFT JOIN resources r ON r.id = de.resource_id
    WHERE de.status = :status
'''
errors = []
_validate(sql, errors)
if errors:
    print('ERRORS:', errors)
else:
    print('OK')
"
```

## Common Failure Patterns

| Error message | Cause | Fix |
|---|---|---|
| `hostname: not a column of resources` | `resources` has `name`, not `hostname` | Use `name AS hostname` |
| `field_name: not a column of drift_events` | Column is `field`, not `field_name` | Use `field AS field_name` |
| `jira_ticket: not a column of jira_tickets` | Column is `jira_key` | Use `jira_key AS jira_ticket` |
| `status: not a column of resources` | `resources` has no status; use `drift_events.status` | Add table qualifier |
| `records_collected: not a column of collection_runs` | Column is `resources_found` | Use `resources_found AS records_collected` |

## ORM as Source of Truth

When in doubt about column names, check `src/infra_brain/db/models/` (split by domain:
core, rapid7, octopus, vsphere, cloud_k8s_net, ansible, os_inventory, governance — all
re-exported from `db/models/__init__.py`). The ORM is authoritative — never guess a column
name from the UI code or old SQL.
