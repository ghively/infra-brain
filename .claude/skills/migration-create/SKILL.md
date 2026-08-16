---
name: migration-create
description: >
  Safe Alembic migration workflow for infra-brain. Generates a migration via autogenerate,
  reviews it for dangerous patterns (NOT NULL without default, DROP TABLE, missing CONCURRENTLY,
  head conflicts), and validates that migrations are in sync before and after.
disable-model-invocation: false
---

# /migration-create <message>

Walks through the safe migration workflow for the infra-brain PostgreSQL schema.

## Argument
- `<message>` — migration description (e.g. `add_vuln_severity_column`, `add_eol_registry`)

## Workflow

### Step 0: Verify the current state

```bash
# Confirm no existing head conflicts
.venv/bin/python -m alembic heads
# Should print exactly ONE revision. Multiple heads = merge conflict (fix first).

# Confirm migrations are currently in sync with the ORM
.venv/bin/python -m alembic check
# Should print "No new upgrade operations detected."
```

If `alembic check` reports drift, there is already an uncommitted model change.
Either generate the migration for those changes first, or this run is for them.

### Step 1: Generate the migration

```bash
.venv/bin/python -m alembic revision --autogenerate -m "<message>"
```

This creates `alembic/versions/<hash>_<message>.py`. Open it and read it carefully.

### Step 2: Review the generated migration

Check for each danger pattern:

**NOT NULL without server_default on existing table:**
```python
# DANGEROUS — takes full table lock + backfill:
sa.Column('col', sa.String, nullable=False)

# SAFE alternatives:
sa.Column('col', sa.String, nullable=False, server_default='')
# OR: add nullable first, backfill data, then add constraint in a second migration
```

**DROP TABLE or DROP COLUMN:**
```python
# Flag for explicit confirmation — data loss is irreversible.
op.drop_table('old_table')
op.drop_column('resources', 'legacy_field')
```

**Index without CONCURRENTLY on large or actively-written tables (resources, snapshots,
drift_events, vuln_queue, eol_registry — the latter two are continuously upserted by
VulnAgent/EOLAgent, so a non-concurrent index build there can contend with a live
collection run; see the comment in `alembic/versions/0008_vuln_eol_writer_indexes.py` for
a case that shipped without `postgresql_concurrently=True`):**
```python
# DANGEROUS on large tables — blocks writes:
op.create_index('ix_resources_name', 'resources', ['name'])

# SAFE:
op.create_index('ix_resources_name', 'resources', ['name'], postgresql_concurrently=True)
```

**Missing downgrade():**
Every migration must have a valid `downgrade()` that is the inverse of `upgrade()`.
If it would be destructive, add a comment explaining why and what would be lost.

### Step 3: Verify lock timeout is configured

Check `alembic/env.py` — the `run_migrations_online()` function should set:
```python
connection.execute(text("SET lock_timeout = '4s'"))
```
If this is missing, add it before `context.configure()`. Without it, a migration that
can't acquire a lock will hang indefinitely, blocking all collection runs.

### Step 4: Validate after generation

```bash
# Confirm the new migration is recognized and there's still exactly one head
.venv/bin/python -m alembic heads
.venv/bin/python -m alembic check
# alembic check should now show 0 drift (the migration covers the model change)
```

### Step 5: Run the migration test

```bash
.venv/bin/python -m pytest tests/test_migration.py -v
```

This test asserts that applying all migrations produces exactly the same table set
as `Base.metadata.tables` — it catches missing tables and orphaned migrations.

### Step 6: Invoke the lc-migration-reviewer subagent

For significant schema changes (new tables, dropped columns, index changes), invoke
the `lc-migration-reviewer` subagent to do a thorough safety review before committing.

## Notes

- Never hand-edit migration files after generating them. Regenerate if needed.
- Keep migrations small and focused — one logical change per migration.
- The initial schema migration (`0001_initial_schema.py`) uses `create_all()` for
  historical reasons. All subsequent migrations must use normal `op.` calls.
- To apply locally: `alembic upgrade head`
- In production: k8s migration job runs `alembic upgrade head` as an init container
  before the app pods start. See `k8s/migration-job.yaml`.
