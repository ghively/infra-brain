"""Reconcile MR-J posture/long-tail tables: unique INDEX -> unique CONSTRAINT.

Revision ID: f3a1c2b4d5e6
Revises: 1eaf3df70543
Create Date: 2026-07-08

Context (2026-07-08 production incident, pipeline job 6460):
------------------------------------------------------------
Migration ``1eaf3df70543`` (0031, MR-J) introduced eight brand-new tables:
host_certificates, host_security_posture, host_shares, windows_local_users,
windows_local_group_members, host_purpose_map, linux_mounts, linux_nics.

That migration created every natural-key uniqueness guarantee with
``op.create_index(..., unique=True)`` — i.e. as a unique **INDEX**. The
SQLAlchemy models, however, declare those same guarantees as table-level
``UniqueConstraint(...)`` (or, for host_security_posture, a column-level
``unique=True``), which ``Base.metadata`` renders as a unique **CONSTRAINT**.

On Postgres a unique index and a unique constraint are functionally equivalent
but are NOT the same catalog object, so alembic ``compare_metadata`` reports
each one as a paired ``remove_index`` + ``add_constraint`` diff. The startup
guard ``assert_schema_current`` (db/schema_check.py) treats any diff as fatal
and aborted app boot — infra-brain-app-1 crash-looped on the e5c3acb deploy.

Why CI (``migration-parity``) never caught this
-----------------------------------------------
``0001_initial_schema`` runs ``Base.metadata.create_all()``, so the ephemeral
CI Postgres gets ALL tables (with unique CONSTRAINTS, straight from the models)
before any later migration runs. 0031's table creation is guarded by
``if "<table>" not in existing_tables``, so on the CI DB those guards are False
and 0031's ``create_index(unique=True)`` statements never execute — the CI DB
matches the models by construction. The real deploy target only ever ran the
actual 0031 DDL (the tables did not pre-exist there), so it alone carries the
index-vs-constraint form. This is the same CI blind spot migration 0026
(``c9d0e1f2a3b4``) documented and the same reconciliation class it applied to
the net_discovery tables.

What this migration does (verified against live prod introspection 2026-07-08):
-------------------------------------------------------------------------------
For each affected table, guarded by inspector existence checks so it is a no-op
on any DB already in the model-correct state (e.g. a fresh CI DB, or a re-run):

  * Named natural-key uniques (uq_*_natkey / uq_host_purpose_map_hostname):
    if the name exists as an INDEX and NOT yet as a unique CONSTRAINT,
    DROP INDEX then ADD the equally-named UNIQUE CONSTRAINT on the same columns.
  * host_security_posture.resource_id: model declares column-level
    ``unique=True`` (an UNNAMED constraint). 0031 created it as the unique index
    ``ix_host_security_posture_resource_id``. Drop that index, add an unnamed
    unique constraint (constraint_name=None) — matching how create_all renders a
    column-level unique, exactly the 0026 net_discovery_hosts.ip pattern.
  * linux_mounts / linux_nics: 0031 additionally created a plain non-unique
    index on host_id (ix_linux_mounts_host_id / ix_linux_nics_host_id) that the
    models do NOT declare. Drop it (compare_metadata flags it as remove_index).
  * host_shares.permissions: model declares ``Mapped[list[Any]]`` (NOT NULL);
    0031 created the column nullable. Backfill NULL -> '[]' then set NOT NULL.

Every operation is idempotent and Postgres-only in effect: the whole migration
is skipped on SQLite (no ALTER TABLE ADD CONSTRAINT), matching the chain
convention and the schema-check guard (assert_schema_current is postgres-only).
"""

from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "f3a1c2b4d5e6"
down_revision = "1eaf3df70543"
branch_labels = None
depends_on = None


# Named natural-key uniques the models declare as UniqueConstraint(name=...),
# but that 0031 created as unique indexes of the same name.
#   table -> (constraint/index name, [columns])
_NAMED_UNIQUES: dict[str, tuple[str, list[str]]] = {
    "host_certificates": ("uq_host_cert_natkey", ["resource_id", "store", "thumbprint"]),
    "host_shares": ("uq_host_share_natkey", ["resource_id", "share_type", "name"]),
    "windows_local_users": ("uq_windows_local_user_natkey", ["resource_id", "username"]),
    "windows_local_group_members": (
        "uq_windows_local_group_member_natkey",
        ["resource_id", "group_name", "member_name"],
    ),
    "host_purpose_map": ("uq_host_purpose_map_hostname", ["hostname"]),
    "linux_mounts": ("uq_linux_mount_natkey", ["host_id", "mount"]),
    "linux_nics": ("uq_linux_nic_natkey", ["host_id", "name"]),
}

# Plain non-unique indexes 0031 created that the models do NOT declare.
_PLAIN_INDEXES_TO_DROP: dict[str, str] = {
    "linux_mounts": "ix_linux_mounts_host_id",
    "linux_nics": "ix_linux_nics_host_id",
}


def _index_names(inspector, table: str) -> set[str]:
    try:
        return {ix["name"] for ix in inspector.get_indexes(table)}
    except Exception:
        return set()


def _unique_constraint_names(inspector, table: str) -> set[str]:
    try:
        return {uc["name"] for uc in inspector.get_unique_constraints(table) if uc["name"]}
    except Exception:
        return set()


def _unique_constraint_colsets(inspector, table: str) -> list[list[str]]:
    try:
        return [uc["column_names"] for uc in inspector.get_unique_constraints(table)]
    except Exception:
        return []


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite cannot ALTER TABLE ADD CONSTRAINT; the whole class of drift this
        # migration fixes is postgres-only (schema_check is postgres-only too).
        return

    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # ------------------------------------------------------------------ #
    # Named natural-key uniques: unique INDEX -> unique CONSTRAINT (same name)
    # ------------------------------------------------------------------ #
    for table, (name, columns) in _NAMED_UNIQUES.items():
        if table not in tables:
            continue
        idx = _index_names(inspector, table)
        ucs = _unique_constraint_names(inspector, table)
        # Only drop the standalone index when there is NOT already a constraint
        # of that name (a named unique constraint on Postgres owns a backing
        # index of the same name; dropping that index directly would raise
        # DependentObjectsStillExist). If the constraint already exists we are
        # already in the model-correct state — skip entirely (idempotent).
        if name in ucs:
            continue
        if name in idx:
            op.drop_index(name, table_name=table)
        op.create_unique_constraint(name, table, columns)

    # ------------------------------------------------------------------ #
    # host_security_posture.resource_id: unique INDEX -> UNNAMED unique constraint
    # (model declares column-level unique=True; create_all renders it unnamed)
    # ------------------------------------------------------------------ #
    if "host_security_posture" in tables:
        idx = _index_names(inspector, "host_security_posture")
        has_resource_id_unique = any(
            cols == ["resource_id"]
            for cols in _unique_constraint_colsets(inspector, "host_security_posture")
        )
        if not has_resource_id_unique:
            if "ix_host_security_posture_resource_id" in idx:
                op.drop_index(
                    "ix_host_security_posture_resource_id",
                    table_name="host_security_posture",
                )
            # Deliberately unnamed (constraint_name=None): the model declares a
            # column-level unique=True, which SQLAlchemy/create_all renders as an
            # unnamed constraint. Naming it here would itself be name-level drift.
            op.create_unique_constraint(None, "host_security_posture", ["resource_id"])

    # ------------------------------------------------------------------ #
    # Plain non-unique host_id indexes the models do not declare
    # ------------------------------------------------------------------ #
    for table, index_name in _PLAIN_INDEXES_TO_DROP.items():
        if table not in tables:
            continue
        if index_name in _index_names(inspector, table):
            op.drop_index(index_name, table_name=table)

    # ------------------------------------------------------------------ #
    # host_shares.permissions: nullable -> NOT NULL (model: Mapped[list[Any]])
    # ------------------------------------------------------------------ #
    if "host_shares" in tables:
        cols = {c["name"]: c for c in inspector.get_columns("host_shares")}
        if "permissions" in cols and cols["permissions"]["nullable"] is True:
            # Backfill any NULLs to an empty JSON array before tightening, so the
            # ALTER cannot fail on existing rows (these tables are new; likely 0).
            op.execute("UPDATE host_shares SET permissions = '[]'::jsonb WHERE permissions IS NULL")
            op.alter_column("host_shares", "permissions", nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # Reverse of the permissions tightening.
    if "host_shares" in tables:
        cols = {c["name"]: c for c in inspector.get_columns("host_shares")}
        if "permissions" in cols and cols["permissions"]["nullable"] is False:
            op.alter_column("host_shares", "permissions", nullable=True)

    # Restore the plain host_id indexes.
    for table, index_name in _PLAIN_INDEXES_TO_DROP.items():
        if table not in tables:
            continue
        if index_name not in _index_names(inspector, table):
            op.create_index(index_name, table, ["host_id"])

    # host_security_posture: unnamed unique constraint -> unique index.
    if "host_security_posture" in tables:
        for uc in inspector.get_unique_constraints("host_security_posture"):
            if uc["column_names"] == ["resource_id"] and uc["name"]:
                op.drop_constraint(uc["name"], "host_security_posture", type_="unique")
        if "ix_host_security_posture_resource_id" not in _index_names(
            inspector, "host_security_posture"
        ):
            op.create_index(
                "ix_host_security_posture_resource_id",
                "host_security_posture",
                ["resource_id"],
                unique=True,
            )

    # Named natural-key uniques: unique CONSTRAINT -> unique INDEX (same name).
    for table, (name, columns) in _NAMED_UNIQUES.items():
        if table not in tables:
            continue
        ucs = _unique_constraint_names(inspector, table)
        if name in ucs:
            op.drop_constraint(name, table, type_="unique")
        if name not in _index_names(inspector, table):
            op.create_index(name, table, columns, unique=True)
