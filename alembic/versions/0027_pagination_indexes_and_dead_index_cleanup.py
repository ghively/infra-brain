"""Pagination-pushdown indexes, dead-index cleanup, and risk_score widening.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-07

Follow-up to 0026 (c9d0e1f2a3b4). 0026 closed every diff surfaced by the failed
2026-07-07 deploy (pipeline 1065): it added the module-level indexes that never
had a migration (resource_configs.collected_at, snapshots.collected_at,
snapshots.run_id), added the vuln_queue / eol_registry indexes that 0008 silently
skipped, converted the net_discovery unique *indexes* to unique *constraints*,
made snapshots.resource_id nullable, and tightened collection_runs.detail_rows_written
to NOT NULL. This migration (0027) adds ONLY what 0026 did not touch.

Like 0026, every op is guarded with an inspector existence check, so this
migration is a safe, idempotent no-op on a fresh CI database (which gets every
declared index directly from 0001/0014's ``Base.metadata.create_all`` against
today's models) and applies its changes only to a real, drifted deploy target.

Scope
=====
1. Pagination-pushdown indexes (S-10/11/12) — support the API filter/sort columns
   that lacked an index:
     * octopus_deployments.completed_at   (ORDER BY completed_at DESC NULLS LAST)
     * octopus_deployments.project_id     (optional WHERE)
     * octopus_deployments.environment_id (optional WHERE)
     * r7_software.vendor                 (SELECT DISTINCT vendor dropdown, ~335k rows)

2. Retention prune indexes (S-17 / DL-C-2) — back the run-cascade queries in
   retention.prune_expired that walked unindexed FK columns:
     * resource_configs.run_id            (delete(ResourceConfig).where(run_id.in_(...)))
     * drift_events.collection_run_id     (update(DriftEvent).where(collection_run_id.in_(...)))

3. Dead / redundant index cleanup — drop indexes confirmed unused by any query
   (the ORM declarations were removed in the same MR so the schema stays in sync):
     * ix_custom_views_share_token   (share_token is unique=True → redundant plain index)
     * ix_observations_agent_tool    (composite never read)
     * ix_octopus_events_occurred    (occurred never filtered/sorted)
     * ix_octopus_interruptions_task_id (task_id never a predicate)
     * ix_r7_vuln_resource           (legacy orphan; model uses ix_r7_vulnerabilities_resource_id)
     * ix_r7_solution_resource       (legacy orphan; model uses ix_r7_solutions_resource_id)

4. risk_score Float widening (defensive) — the r7 risk_score columns are declared
   Float in the models AND created Float by 0010/0011, so no drift is expected;
   but if a live DB ever narrowed them to an integer type (silently truncating the
   float scores the collectors write), widen them back. Guarded to fire ONLY when
   the live column is actually an integer type — a pure no-op everywhere else.

Safety review (project conventions)
-----------------------------------
  * No NOT NULL added to an existing table.
  * No DROP TABLE / DROP COLUMN.
  * Index creation is plain (non-CONCURRENT) to match this repo's established
    migration style (0026/0018 do the same) and to run inside the migration
    transaction. On the large tables (r7_software ~335k, octopus_deployments)
    a maintenance-window deploy is assumed; see the MR-F report for the
    CONCURRENTLY note if these must be built online.
  * Every op is inspector-guarded → idempotent and safe to re-run.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


# New indexes this migration is responsible for (table -> [(name, [cols]), ...]).
_ADD_INDEXES: dict[str, list[tuple[str, list[str]]]] = {
    "octopus_deployments": [
        ("ix_octopus_deployments_completed_at", ["completed_at"]),
        ("ix_octopus_deployments_project_id", ["project_id"]),
        ("ix_octopus_deployments_environment_id", ["environment_id"]),
    ],
    "r7_software": [
        ("ix_r7_software_vendor", ["vendor"]),
    ],
    "resource_configs": [
        ("ix_resource_configs_run_id", ["run_id"]),
    ],
    "drift_events": [
        ("ix_drift_events_collection_run_id", ["collection_run_id"]),
    ],
}

# Dead / redundant indexes to drop (table -> [names]).
_DROP_INDEXES: dict[str, list[str]] = {
    "custom_views": ["ix_custom_views_share_token"],
    "observations": ["ix_observations_agent_tool"],
    "octopus_events": ["ix_octopus_events_occurred"],
    "octopus_interruptions": ["ix_octopus_interruptions_task_id"],
    "r7_vulnerabilities": ["ix_r7_vuln_resource"],
    "r7_solutions": ["ix_r7_solution_resource"],
}

# risk_score columns that must be a float type (table -> [columns]).
_FLOAT_COLUMNS: dict[str, list[str]] = {
    "r7_assets": ["risk_score", "raw_risk_score"],
    "r7_sites": ["risk_score"],
    "r7_vulnerabilities": ["risk_score"],
}


def _index_names(inspector, table: str) -> set[str]:
    try:
        return {ix["name"] for ix in inspector.get_indexes(table)}
    except Exception:
        return set()


def _columns(inspector, table: str) -> dict:
    try:
        return {c["name"]: c for c in inspector.get_columns(table)}
    except Exception:
        return {}


def _is_integer_type(col_type) -> bool:
    """True if a reflected column type is an integer type (not float/numeric)."""
    text = str(col_type).upper()
    if any(tok in text for tok in ("FLOAT", "DOUBLE", "REAL", "NUMERIC", "DECIMAL")):
        return False
    return "INT" in text  # INTEGER, BIGINT, SMALLINT


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    is_sqlite = bind.dialect.name == "sqlite"
    tables = set(inspector.get_table_names())

    # ---------------------------------------------------------------- #
    # 1 + 2. Add pagination / retention indexes (guarded, idempotent).
    # ---------------------------------------------------------------- #
    for table, specs in _ADD_INDEXES.items():
        if table not in tables:
            continue
        existing = _index_names(inspector, table)
        cols = _columns(inspector, table)
        for name, index_cols in specs:
            if name in existing:
                continue
            if not all(c in cols for c in index_cols):
                # Column absent on this (older) DB — skip rather than error.
                continue
            op.create_index(name, table, index_cols)

    # ---------------------------------------------------------------- #
    # 3. Drop dead / redundant indexes (guarded, idempotent).
    # ---------------------------------------------------------------- #
    for table, names in _DROP_INDEXES.items():
        if table not in tables:
            continue
        existing = _index_names(inspector, table)
        for name in names:
            if name in existing:
                op.drop_index(name, table_name=table)

    # ---------------------------------------------------------------- #
    # 4. Widen any integer-typed risk_score column back to float.
    #    No-op unless the live DB actually narrowed it (defensive heal).
    # ---------------------------------------------------------------- #
    if not is_sqlite:
        for table, columns in _FLOAT_COLUMNS.items():
            if table not in tables:
                continue
            cols = _columns(inspector, table)
            for column in columns:
                info = cols.get(column)
                if info is None:
                    continue
                if _is_integer_type(info["type"]):
                    op.alter_column(
                        table,
                        column,
                        type_=sa.Float(),
                        existing_nullable=info.get("nullable", True),
                        postgresql_using=f"{column}::double precision",
                    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # Re-create the dropped dead indexes (best-effort, guarded).
    for table, names in _DROP_INDEXES.items():
        if table not in tables:
            continue
        existing = _index_names(inspector, table)
        cols = _columns(inspector, table)
        for name in names:
            if name in existing:
                continue
            # Reconstruct the single-column indexes these names covered.
            recreate = {
                "ix_custom_views_share_token": ("custom_views", ["share_token"]),
                "ix_observations_agent_tool": ("observations", ["agent", "tool"]),
                "ix_octopus_events_occurred": ("octopus_events", ["occurred"]),
                "ix_octopus_interruptions_task_id": ("octopus_interruptions", ["task_id"]),
                "ix_r7_vuln_resource": ("r7_vulnerabilities", ["resource_id"]),
                "ix_r7_solution_resource": ("r7_solutions", ["resource_id"]),
            }.get(name)
            if recreate and all(c in cols for c in recreate[1]):
                op.create_index(name, recreate[0], recreate[1])

    # Drop the indexes this migration added.
    for table, specs in _ADD_INDEXES.items():
        if table not in tables:
            continue
        existing = _index_names(inspector, table)
        for name, _cols in specs:
            if name in existing:
                op.drop_index(name, table_name=table)

    # risk_score widening is intentionally NOT reverted: narrowing float→int on
    # downgrade would silently truncate live scores. The columns are declared
    # Float in the models, so leaving them Float is the correct resting state.
