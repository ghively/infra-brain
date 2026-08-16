"""Fresh-head repair: normalize zone 'corporate' -> 'corpor' on the LIVE volume.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-30

Task 4.2 (ZONE_CORPORATE constant + reader/writer convergence) fixed the CODE
so every fresh row now writes zone="corpor". But a live DB may still hold
zone='corporate' rows written by pre-4.2 code, and the column's
``server_default`` (baked by migration 0019 / b2c3d4e5f6a7) was still
'corporate' until this revision. Editing 0019 in place (also done here) fixes
ONLY fresh create_all()/first-run DBs — it does NOT touch a DB that alembic has
already migrated up through 0023.

This is deliberately a FRESH HEAD revision rather than an edit to an
already-passed one, mirroring the lesson from
0021_repair_r7_resource_id_drift.py (the resource_id-500 root cause): a plain
``alembic upgrade head`` is a NO-OP for any DB whose ``alembic_version`` is
already at or past the target revision, so a data UPDATE embedded in an
already-shipped revision would never execute against a live volume. Because
this revision is new, ``alembic upgrade head`` is guaranteed to advance the
stamp and run the UPDATE exactly once, regardless of what revision the live DB
was previously stamped at.

Zone-bearing tables (verified against src/infra_brain/db/models/*.py on
2026-06-30):
  - resources.zone            (Resource, db/models/core.py)
  - instincts.zone            (Instinct, db/models/core.py)
  - net_discovery_hosts.zone  (NetDiscoveryHost, db/models/cloud_k8s_net.py)
  - cloud_resources           has NO zone column (CloudResource,
    db/models/cloud_k8s_net.py) — listed as a candidate and inspector-skipped;
    kept in the candidate list so a future zone column on this table is picked
    up automatically without another migration.

Safety checklist:
  All work is inspector-guarded — any candidate table missing entirely, or
    present but lacking a `zone` column, is silently skipped (no-op).
  Idempotent: re-running the UPDATE is safe (WHERE zone='corporate' matches
    nothing on a second run).
  Forward-only data normalization; downgrade restores only the column
    DEFAULT, never the data (see downgrade() docstring below).
  No DROP TABLE / DROP COLUMN.
  SQLite cannot ALTER COLUMN ... SET DEFAULT; skipped there (harmless — the
    live target is PostgreSQL; the SQLite path exists for the round-trip test).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None

_ZONE_TABLES = ["resources", "instincts", "net_discovery_hosts", "cloud_resources"]


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    is_sqlite = bind.dialect.name == "sqlite"
    tables = set(insp.get_table_names())

    for t in _ZONE_TABLES:
        if t not in tables:
            continue
        cols = {c["name"] for c in insp.get_columns(t)}
        if "zone" not in cols:
            continue
        op.execute(sa.text(f"UPDATE {t} SET zone='corpor' WHERE zone='corporate'"))
        if not is_sqlite:  # SQLite cannot ALTER COLUMN default; harmless to skip.
            op.execute(sa.text(f"ALTER TABLE {t} ALTER COLUMN zone SET DEFAULT 'corpor'"))


def downgrade() -> None:
    # Reversible + safe: restore the pre-repair column default only.
    # Do NOT re-corrupt data back to 'corporate' — 'corpor' is canonical; the
    # data normalization is intentionally irreversible (a data downgrade would
    # re-introduce the exact bug this revision fixes).
    bind = op.get_bind()
    insp = sa.inspect(bind)
    is_sqlite = bind.dialect.name == "sqlite"
    if is_sqlite:
        return
    tables = set(insp.get_table_names())
    for t in _ZONE_TABLES:
        if t in tables and "zone" in {c["name"] for c in insp.get_columns(t)}:
            op.execute(sa.text(f"ALTER TABLE {t} ALTER COLUMN zone SET DEFAULT 'corporate'"))
