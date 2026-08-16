"""0032 — reconcile MR-J posture/long-tail unique INDEX -> unique CONSTRAINT.

The 2026-07-08 production incident (job 6460): migration 0031 (1eaf3df70543)
created eight new tables' natural-key uniques as ``op.create_index(unique=True)``
(unique INDEXES), but the models declare them as ``UniqueConstraint`` /
column-level ``unique=True`` (unique CONSTRAINTS). alembic ``compare_metadata``
reports index-vs-constraint as drift, so the startup guard aborts app boot.

CI's ``migration-parity`` can't reproduce this: 0001 runs ``create_all()`` so the
CI DB gets constraints from the models up front and 0031's guarded create_index
never runs. These tests therefore pin the *models'* declared end-state to exactly
what 0032 produces, plus confirm 0032 is an inert no-op on SQLite. Together they
guard against the models and this migration silently re-diverging.
"""

import importlib.util
import pathlib

import sqlalchemy as sa

# The named natural-key uniques the models must declare as UniqueConstraints
# (NOT as unique indexes) — the exact set 0032 converts on the live DB.
_NAMED_UNIQUES = {
    "host_certificates": ("uq_host_cert_natkey", ("resource_id", "store", "thumbprint")),
    "host_shares": ("uq_host_share_natkey", ("resource_id", "share_type", "name")),
    "windows_local_users": ("uq_windows_local_user_natkey", ("resource_id", "username")),
    "windows_local_group_members": (
        "uq_windows_local_group_member_natkey",
        ("resource_id", "group_name", "member_name"),
    ),
    "host_purpose_map": ("uq_host_purpose_map_hostname", ("hostname",)),
    "linux_mounts": ("uq_linux_mount_natkey", ("host_id", "mount")),
    "linux_nics": ("uq_linux_nic_natkey", ("host_id", "name")),
}

# Plain host_id indexes 0031 created that the models must NOT declare (0032 drops).
_PLAIN_INDEXES_ABSENT = {
    "linux_mounts": "ix_linux_mounts_host_id",
    "linux_nics": "ix_linux_nics_host_id",
}


def _load_migration(filename: str):
    mig_path = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), mig_path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def _table(name: str):
    from infra_brain.db.models import Base

    return Base.metadata.tables[name]


def test_named_uniques_declared_as_constraints_not_indexes():
    """Each natural key must be a UniqueConstraint (right name + columns) in the
    models, and must NOT also appear as a same-named Index — otherwise 0032's
    drop-index/add-constraint reconciliation would re-diverge from the models."""
    for table_name, (uc_name, cols) in _NAMED_UNIQUES.items():
        table = _table(table_name)

        uniques = {
            c.name: tuple(col.name for col in c.columns)
            for c in table.constraints
            if isinstance(c, sa.UniqueConstraint) and c.name
        }
        assert uc_name in uniques, f"{table_name}: missing UniqueConstraint {uc_name!r}"
        assert uniques[uc_name] == cols, (
            f"{table_name}.{uc_name} columns {uniques[uc_name]} != expected {cols}"
        )

        index_names = {ix.name for ix in table.indexes}
        assert uc_name not in index_names, (
            f"{table_name}: {uc_name!r} is declared as an Index; it must be a "
            "UniqueConstraint (0032 reconciles the live DB to the constraint form)"
        )


def test_host_security_posture_resource_id_unique_constraint():
    """host_security_posture.resource_id declares column-level unique=True, which
    renders as an (unnamed) unique constraint on (resource_id) — not a unique
    index. 0032 converts the live ix_host_security_posture_resource_id to match."""
    table = _table("host_security_posture")
    unique_colsets = [
        tuple(col.name for col in c.columns)
        for c in table.constraints
        if isinstance(c, sa.UniqueConstraint)
    ]
    assert ("resource_id",) in unique_colsets, (
        "host_security_posture must declare a unique constraint on (resource_id)"
    )


def test_plain_host_id_indexes_not_declared():
    """The plain non-unique host_id indexes 0031 created are NOT model-declared,
    so 0032 drops them. Guard against a future model change re-adding them without
    a matching migration."""
    for table_name, index_name in _PLAIN_INDEXES_ABSENT.items():
        table = _table(table_name)
        assert index_name not in {ix.name for ix in table.indexes}, (
            f"{table_name}: {index_name!r} is model-declared but 0032 drops it"
        )


def test_host_shares_permissions_not_null():
    """host_shares.permissions is NOT NULL in the models; 0032 tightens the live
    column (which 0031 created nullable) to match."""
    table = _table("host_shares")
    assert table.c.permissions.nullable is False, (
        "host_shares.permissions must be NOT NULL (Mapped[list[Any]])"
    )


def test_migration_is_noop_on_sqlite():
    """0032 short-circuits on SQLite (no ALTER TABLE ADD CONSTRAINT); it must run
    cleanly and create/alter nothing, so the SQLite suite is never affected."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    mig = _load_migration("0032_reconcile_posture_longtail_index_to_constraint.py")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.upgrade()
            mig.downgrade()
    # No tables were touched/created — the guard returned before any op.
    assert sa.inspect(engine).get_table_names() == []


def test_chains_onto_0031():
    """0032 must chain directly after 1eaf3df70543 (0031) — the current head."""
    mig = _load_migration("0032_reconcile_posture_longtail_index_to_constraint.py")
    assert mig.down_revision == "1eaf3df70543"
    assert mig.revision == "f3a1c2b4d5e6"
