"""Reconcile long-standing live-DB drift surfaced by the new full compare_metadata scan.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-07

Context: 869460d ("replace hardcoded column allowlist with full compare_metadata
scan") turned ``assert_schema_current`` from a hardcoded per-column allowlist into
a full schema diff against ``Base.metadata``. The CI ``migration-parity`` job
always passed because it builds a fresh DB via ``0001_initial_schema`` — which
runs ``Base.metadata.create_all()`` against *today's* models, not a frozen
snapshot — so a fresh DB always matches current models by construction. The real
deploy target (deploy-host-01) was never fresh: it accumulated drift over several
migrations that either (a) never existed for a given change, or (b) were
guarded by an ``if "<table>" in existing_tables`` check that silently no-opped
because the table didn't exist yet when the migration ran. The new strict check
surfaced ALL of this at once and now blocks every deploy.

This migration closes every diff reported by the failed 2026-07-07 deploy
(pipeline 1065, job 6251), item by item:

  * net_discovery_hosts.ip — migration 0023 created a named UNIQUE INDEX
    (``ix_net_discovery_hosts_ip``); the model declares ``unique=True`` on the
    column, which SQLAlchemy renders as a table-level UniqueConstraint, not an
    Index. Drop the index, add the constraint (same effective guarantee).
  * net_discovery_services (host_id, port, proto) — same class of mismatch:
    0023 created a named UNIQUE INDEX (``uq_net_discovery_services_host_port_proto``);
    the model declares a ``UniqueConstraint`` in ``__table_args__``. Drop the
    index, add the constraint.
  * resource_configs.collected_at / snapshots.collected_at / snapshots.run_id —
    module-level ``Index(...)`` calls exist in ``models/core.py`` with no
    migration ever authored for them. Add the three indexes (guarded).
  * snapshots.resource_id — model declares ``nullable=True`` (F-005 run-scoped
    partial-progress snapshots with no owning resource); live DB still has the
    original NOT NULL from before that change. Drop the NOT NULL.
  * vuln_queue / eol_registry indexes — migration 0008 guarded each
    ``create_index`` behind ``if "<table>" in existing_tables``; on the live DB
    both tables were created by a later create_all than 0008 ran, so 0008's
    guard evaluated false and both indexes were silently never created. Add
    them now (guarded so this migration is also idempotent on any DB where
    0008 DID fire correctly).
  * collection_runs.detail_rows_written — migration 0022 added the column as
    ``nullable=True`` "for a safe additive rollout"; the model has since been
    tightened to ``Mapped[int]`` (non-nullable, default=0). Backfill any NULLs
    to 0, then set NOT NULL.

All operations are guarded with inspector existence checks so this migration is
a no-op (safe to re-run) on any DB where a given piece of drift does not exist
(e.g. a fresh CI DB built via 0001's create_all, which never has any of this
drift in the first place).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


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


def _column_names(inspector, table: str) -> set[str]:
    try:
        return {c["name"] for c in inspector.get_columns(table)}
    except Exception:
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    is_sqlite = bind.dialect.name == "sqlite"
    tables = set(inspector.get_table_names())

    # ------------------------------------------------------------------ #
    # net_discovery_hosts.ip — index → unique constraint
    # ------------------------------------------------------------------ #
    if "net_discovery_hosts" in tables:
        idx = _index_names(inspector, "net_discovery_hosts")
        has_ip_unique_constraint = any(
            uc["column_names"] == ["ip"]
            for uc in inspector.get_unique_constraints("net_discovery_hosts")
        )
        if "ix_net_discovery_hosts_ip" in idx and not has_ip_unique_constraint:
            op.drop_index("ix_net_discovery_hosts_ip", table_name="net_discovery_hosts")
        if not has_ip_unique_constraint and not is_sqlite:
            # Deliberately unnamed (constraint_name=None) — the model declares
            # this via plain column-level ``unique=True``, which SQLAlchemy
            # renders unnamed. A fresh DB (0001's create_all) gets the same
            # DB-auto-generated name; naming it explicitly here would itself
            # be a name-level drift against Base.metadata.
            op.create_unique_constraint(None, "net_discovery_hosts", ["ip"])

    # ------------------------------------------------------------------ #
    # net_discovery_services (host_id, port, proto) — index → unique constraint
    # ------------------------------------------------------------------ #
    if "net_discovery_services" in tables:
        idx = _index_names(inspector, "net_discovery_services")
        ucs = _unique_constraint_names(inspector, "net_discovery_services")
        # On Postgres a named UNIQUE CONSTRAINT is backed by an index of the
        # same name, so `name in idx` is true for BOTH the legacy bare-index
        # case (0023's original artifact, no constraint yet) AND the
        # already-reconciled case (constraint already exists, its backing
        # index just shows up in get_indexes() too). Only DROP INDEX when
        # there is no constraint of that name — otherwise Postgres raises
        # DependentObjectsStillExist (the constraint requires its own index).
        if "uq_net_discovery_services_host_port_proto" in idx and \
                "uq_net_discovery_services_host_port_proto" not in ucs:
            op.drop_index(
                "uq_net_discovery_services_host_port_proto",
                table_name="net_discovery_services",
            )
        if "uq_net_discovery_services_host_port_proto" not in ucs and not is_sqlite:
            op.create_unique_constraint(
                "uq_net_discovery_services_host_port_proto",
                "net_discovery_services",
                ["host_id", "port", "proto"],
            )

    # ------------------------------------------------------------------ #
    # Missing module-level indexes (never had a migration)
    # ------------------------------------------------------------------ #
    if "resource_configs" in tables:
        idx = _index_names(inspector, "resource_configs")
        if "ix_resource_configs_collected_at" not in idx:
            op.create_index(
                "ix_resource_configs_collected_at", "resource_configs", ["collected_at"]
            )

    if "snapshots" in tables:
        idx = _index_names(inspector, "snapshots")
        if "ix_snapshots_collected_at" not in idx:
            op.create_index("ix_snapshots_collected_at", "snapshots", ["collected_at"])
        if "ix_snapshots_run_id" not in idx:
            op.create_index("ix_snapshots_run_id", "snapshots", ["run_id"])

        # snapshots.resource_id must be nullable (F-005 run-scoped snapshots).
        cols = {c["name"]: c for c in inspector.get_columns("snapshots")}
        if "resource_id" in cols and cols["resource_id"]["nullable"] is False:
            op.alter_column(
                "snapshots", "resource_id", existing_type=sa.Uuid(), nullable=True
            )

    # ------------------------------------------------------------------ #
    # vuln_queue / eol_registry — indexes 0008 silently skipped
    # ------------------------------------------------------------------ #
    if "vuln_queue" in tables:
        idx = _index_names(inspector, "vuln_queue")
        if "ix_vuln_queue_resource_cve" not in idx:
            op.create_index(
                "ix_vuln_queue_resource_cve",
                "vuln_queue",
                ["resource_id", "cve_id"],
                unique=True,
            )

    if "eol_registry" in tables:
        idx = _index_names(inspector, "eol_registry")
        if "ix_eol_registry_asset_name" not in idx:
            op.create_index(
                "ix_eol_registry_asset_name", "eol_registry", ["asset_name"], unique=False
            )

    # ------------------------------------------------------------------ #
    # collection_runs.detail_rows_written — backfill then tighten to NOT NULL
    # ------------------------------------------------------------------ #
    if "collection_runs" in tables:
        cols = {c["name"]: c for c in inspector.get_columns("collection_runs")}
        if "detail_rows_written" in cols and cols["detail_rows_written"]["nullable"] is True:
            op.execute(
                "UPDATE collection_runs SET detail_rows_written = 0 "
                "WHERE detail_rows_written IS NULL"
            )
            op.alter_column(
                "collection_runs",
                "detail_rows_written",
                existing_type=sa.Integer(),
                nullable=False,
                server_default="0",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    is_sqlite = bind.dialect.name == "sqlite"
    tables = set(inspector.get_table_names())

    if "collection_runs" in tables:
        cols = _column_names(inspector, "collection_runs")
        if "detail_rows_written" in cols:
            op.alter_column(
                "collection_runs",
                "detail_rows_written",
                existing_type=sa.Integer(),
                nullable=True,
                server_default="0",
            )

    if "eol_registry" in tables:
        idx = _index_names(inspector, "eol_registry")
        if "ix_eol_registry_asset_name" in idx:
            op.drop_index("ix_eol_registry_asset_name", table_name="eol_registry")

    if "vuln_queue" in tables:
        idx = _index_names(inspector, "vuln_queue")
        if "ix_vuln_queue_resource_cve" in idx:
            op.drop_index("ix_vuln_queue_resource_cve", table_name="vuln_queue")

    if "snapshots" in tables:
        cols = {c["name"]: c for c in inspector.get_columns("snapshots")}
        if "resource_id" in cols and cols["resource_id"]["nullable"] is True:
            op.alter_column(
                "snapshots", "resource_id", existing_type=sa.Uuid(), nullable=False
            )
        idx = _index_names(inspector, "snapshots")
        if "ix_snapshots_run_id" in idx:
            op.drop_index("ix_snapshots_run_id", table_name="snapshots")
        if "ix_snapshots_collected_at" in idx:
            op.drop_index("ix_snapshots_collected_at", table_name="snapshots")

    if "resource_configs" in tables:
        idx = _index_names(inspector, "resource_configs")
        if "ix_resource_configs_collected_at" in idx:
            op.drop_index("ix_resource_configs_collected_at", table_name="resource_configs")

    if "net_discovery_services" in tables:
        ucs = _unique_constraint_names(inspector, "net_discovery_services")
        if "uq_net_discovery_services_host_port_proto" in ucs and not is_sqlite:
            op.drop_constraint(
                "uq_net_discovery_services_host_port_proto",
                "net_discovery_services",
                type_="unique",
            )
        op.create_index(
            "uq_net_discovery_services_host_port_proto",
            "net_discovery_services",
            ["host_id", "port", "proto"],
            unique=True,
        )

    if "net_discovery_hosts" in tables:
        if not is_sqlite:
            for uc in inspector.get_unique_constraints("net_discovery_hosts"):
                if uc["column_names"] == ["ip"] and uc["name"]:
                    op.drop_constraint(
                        uc["name"], "net_discovery_hosts", type_="unique"
                    )
        op.create_index(
            "ix_net_discovery_hosts_ip", "net_discovery_hosts", ["ip"], unique=True
        )
