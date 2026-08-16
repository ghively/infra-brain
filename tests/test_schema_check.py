"""Unit tests for the startup schema-drift keystone (db/schema_check.py).

Covers the guarantees that matter for the deploy-correctness story:

  1. On a non-PostgreSQL dialect (the SQLite test/TestClient env) the assertion is
     a NO-OP — it must never false-trip the suite or block TestClient startup.
  2. On PostgreSQL with a revision mismatch it raises SchemaDriftError.
  3. On PostgreSQL when compare_metadata returns diffs it raises SchemaDriftError,
     REGARDLESS of whether the drifted column was in the old hardcoded allowlist.
  4. On PostgreSQL with head==current and zero compare_metadata diffs it passes.

The key improvement over the old allowlist-based check is tested in
``test_raises_when_unlisted_column_drift``: a drift on a table/column that was NOT
in the old ``_REQUIRED_COLUMNS`` dict (e.g. ``resources.some_new_field``) would
have been silently missed by the previous implementation but is now caught because
the full ``compare_metadata`` scan covers every table and every column.
"""

from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa

from infra_brain.db.schema_check import SchemaDriftError, assert_schema_current


def test_noop_on_sqlite():
    """SQLite dialect → assertion does nothing (cannot false-trip the suite)."""
    engine = sa.create_engine("sqlite://")
    # Must return cleanly even though there is no alembic_version / r7 tables.
    assert assert_schema_current(engine) is None


def _fake_pg_engine():
    """A MagicMock engine that reports the postgresql dialect."""
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    return engine


# ---------------------------------------------------------------------------
# Revision parity
# ---------------------------------------------------------------------------


def test_raises_on_revision_drift():
    """Postgres + stamped revision != repo head → SchemaDriftError (the no-op-upgrade case)."""
    engine = _fake_pg_engine()

    with (
        patch("infra_brain.db.schema_check._script_head", return_value="rev_head"),
        patch("infra_brain.db.schema_check._db_revision", return_value="rev_OLD"),
        patch("infra_brain.db.schema_check._full_compare", return_value=[]),
    ):
        with pytest.raises(SchemaDriftError) as ei:
            assert_schema_current(engine)

    assert "revision drift" in str(ei.value)


def test_raises_when_no_alembic_version_stamp():
    """Postgres + no alembic_version row → SchemaDriftError."""
    engine = _fake_pg_engine()

    with (
        patch("infra_brain.db.schema_check._script_head", return_value="rev_head"),
        patch("infra_brain.db.schema_check._db_revision", return_value=None),
        patch("infra_brain.db.schema_check._full_compare", return_value=[]),
    ):
        with pytest.raises(SchemaDriftError) as ei:
            assert_schema_current(engine)

    assert "never ran" in str(ei.value)


# ---------------------------------------------------------------------------
# Full compare_metadata drift detection
# ---------------------------------------------------------------------------


def test_raises_when_compare_metadata_reports_drift():
    """Postgres + compare_metadata returns any diff → SchemaDriftError.

    This is the general form: any diff from compare_metadata, regardless of which
    table/column is affected, must block startup.
    """
    engine = _fake_pg_engine()

    fake_diff = ("remove_column", "public", "r7_vulnerabilities", MagicMock(name="resource_id"))

    with (
        patch("infra_brain.db.schema_check._script_head", return_value="rev_head"),
        patch("infra_brain.db.schema_check._db_revision", return_value="rev_head"),
        patch("infra_brain.db.schema_check._full_compare", return_value=[fake_diff]),
    ):
        with pytest.raises(SchemaDriftError) as ei:
            assert_schema_current(engine)

    assert "schema diff detected" in str(ei.value)


def test_raises_when_unlisted_column_drift():
    """compare_metadata catches drift on a table NOT in the old hardcoded allowlist.

    The old implementation used _REQUIRED_COLUMNS which only covered:
      r7_vulnerabilities, r7_solutions, collection_runs, net_discovery_hosts,
      net_discovery_services.

    A missing column on ANY other table — e.g. ``resources.some_new_field`` —
    would have passed the old allowlist check silently.  This test confirms the new
    full compare_metadata approach catches it.

    Proof that the OLD logic would miss it: ``resources`` was not a key in
    ``_REQUIRED_COLUMNS``, so the allowlist loop would skip it entirely and startup
    would proceed with a broken ``resources`` table.
    """
    engine = _fake_pg_engine()

    # Simulate compare_metadata finding a missing column on 'resources' — a table
    # that was never in the old _REQUIRED_COLUMNS allowlist.
    fake_diff = (
        "remove_column",
        "public",
        "resources",
        MagicMock(name="some_new_field"),
    )

    with (
        patch("infra_brain.db.schema_check._script_head", return_value="rev_head"),
        patch("infra_brain.db.schema_check._db_revision", return_value="rev_head"),
        patch("infra_brain.db.schema_check._full_compare", return_value=[fake_diff]),
    ):
        with pytest.raises(SchemaDriftError) as ei:
            assert_schema_current(engine)

    # The error must mention 'schema diff detected' (the new format).
    assert "schema diff detected" in str(ei.value)


def test_raises_when_resource_id_column_missing():
    """Postgres + compare_metadata reports r7_vulnerabilities.resource_id drift → SchemaDriftError.

    Preserves the original r7 production-incident scenario: a missing resource_id
    column on r7_vulnerabilities is detected and blocks startup.
    """
    engine = _fake_pg_engine()

    fake_diff = ("remove_column", "public", "r7_vulnerabilities", MagicMock(name="resource_id"))

    with (
        patch("infra_brain.db.schema_check._script_head", return_value="rev_head"),
        patch("infra_brain.db.schema_check._db_revision", return_value="rev_head"),
        patch("infra_brain.db.schema_check._full_compare", return_value=[fake_diff]),
    ):
        with pytest.raises(SchemaDriftError) as ei:
            assert_schema_current(engine)

    assert "schema diff detected" in str(ei.value)


def test_raises_when_multiple_diffs():
    """Multiple compare_metadata diffs → all included in the error message."""
    engine = _fake_pg_engine()

    diff1 = ("remove_column", "public", "r7_vulnerabilities", MagicMock(name="resource_id"))
    diff2 = ("add_column", "public", "resources", MagicMock(name="extra_col"))

    with (
        patch("infra_brain.db.schema_check._script_head", return_value="rev_head"),
        patch("infra_brain.db.schema_check._db_revision", return_value="rev_head"),
        patch("infra_brain.db.schema_check._full_compare", return_value=[diff1, diff2]),
    ):
        with pytest.raises(SchemaDriftError) as ei:
            assert_schema_current(engine)

    # Both diffs should show up as separate problem lines.
    msg = str(ei.value)
    assert msg.count("schema diff detected") == 2


# ---------------------------------------------------------------------------
# Green path
# ---------------------------------------------------------------------------


def test_passes_when_current():
    """Postgres + head matches + compare_metadata returns no diffs → no raise."""
    engine = _fake_pg_engine()

    with (
        patch("infra_brain.db.schema_check._script_head", return_value="rev_head"),
        patch("infra_brain.db.schema_check._db_revision", return_value="rev_head"),
        patch("infra_brain.db.schema_check._full_compare", return_value=[]),
    ):
        assert assert_schema_current(engine) is None
