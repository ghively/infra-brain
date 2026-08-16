# Lessons Learned: The Model↔DB Drift Incident

This is a condensed writeup of a real production incident and the durable fixes it
produced — kept because the root cause is a general lesson, not because the specific
code paths it describes still look like this (the file layout has since changed
significantly; treat this as a case study, not a current-state reference).

## The incident

A production 500 traced back to a column (`resource_id` on a vulnerability table) that
the ORM model declared but the live database didn't actually have. The chain of
failures that let this ship:

1. A migration adding the column existed and was reachable from `head` — the migration
   chain itself was fine. But on the live deployment, that migration had never actually
   applied (a compose service was missing an `env_file`, so its database URL resolved
   to an empty/wrong default at startup).
2. The seed script then ran `Base.metadata.create_all()` — which only issues
   `CREATE TABLE` for tables that don't exist yet; it **never** issues `ALTER TABLE` on
   an existing table. The table already existed (just missing the column), so
   `create_all()` silently did nothing and exited 0.
3. The deploy health check only ran `SELECT 1` — it never touched the actual drifted
   column, so the deploy reported healthy.
4. The failure only surfaced later, in production, the first time a real query touched
   that column.

**The core problem: every gate checked *structure* (does the migration chain look
right, does the build produce a table, is the process alive) but nothing checked
*applied state* — whether what the code declares actually matches what's live in the
database.** Drift could pass every gate green and still ship a broken deploy.

## The durable fix

Two layers, not one — either alone leaves a gap:

- **Make migrations always reach the live database and fail loudly otherwise.**
  Every service touching the database gets the same environment-file wiring (no
  silent fallback to a default DSN); the database URL is a required setting, not one
  with a convenient default; and `create_all()`/`create_tables()` is removed from the
  production seed path entirely — Alembic becomes the sole schema authority outside of
  test fixtures, where `create_all()` is still fine.
- **Make drift impossible to ship undetected.** A startup check compares the live
  schema against what the ORM models declare (not just "is the migration chain at
  head" — a full column/type/index comparison) and refuses to start on any mismatch.
  A CI job does the equivalent check against a real ephemeral Postgres instance before
  merge, so drift is caught before it ships, not after.

The general lesson: **a chain of "this looks fine" checks that never inspect the
actual applied state will eventually let structural drift ship as a healthy deploy.**
Both fixes above are now part of this project's standing practice — see the Alembic
migration workflow and the CI gates described in `CLAUDE.md`.
