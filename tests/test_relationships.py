"""Tests for the knowledge-graph relationship vocabulary and legacy walk.

**P5 changed what this file is about.** ``resource_relationships`` was dropped
(``alembic/versions/95d988b2bc3c_*``), taking the ORM model and both write
helpers with it. What is tested here now:

* the ``RelationshipType`` taxonomy and ``RELATIONSHIP_PROPS`` registry, which
  outlived the table because they are a vocabulary, not a schema;
* the POST-DROP contract — the write helpers raise rather than no-op, the model
  is not back on ``Base.metadata``, the frozen shim refuses instantiation;
These tests use a MagicMock session where a live database is not needed, so
they run without PostgreSQL. They verify contracts rather than exact SQL text
(which would be brittle). The legacy walk (``get_neighborhood``/``_WALK_SQL``)
is gone with the store — see the epitaph at the foot of this file; its
replacement is tested in ``tests/test_graph_kg.py``.
"""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from infra_brain.db.models import Base
from infra_brain.db.relationships import (
    RelationshipType,
    ResourceRelationship,
    emit_edge,
    emit_edges_batch,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_session():
    """A MagicMock that quacks like a SQLAlchemy Session for our helpers."""
    session = MagicMock()
    session.execute.return_value = MagicMock()
    return session


# ---------------------------------------------------------------------------
# RelationshipType
# ---------------------------------------------------------------------------


def test_relationship_type_values_are_unique_strings():
    """All RelationshipType enum values must be unique, non-empty strings."""
    values = [rt.value for rt in RelationshipType]
    assert len(values) == len(set(values)), "Duplicate RelationshipType values found"
    assert all(isinstance(v, str) and v for v in values)


def test_relationship_type_minimum_count():
    """We expect at least 16 relationship types (the full taxonomy)."""
    assert len(list(RelationshipType)) >= 16


def test_relationship_type_is_same_as_has_correct_value():
    assert RelationshipType.IS_SAME_AS.value == "IS_SAME_AS"


def test_relationship_type_runs_on_has_correct_value():
    assert RelationshipType.RUNS_ON.value == "RUNS_ON"


def test_relationship_type_ansible_manages_has_correct_value():
    assert RelationshipType.ANSIBLE_MANAGES.value == "ANSIBLE_MANAGES"


def test_relationship_type_has_firewall_rule_has_correct_value():
    assert RelationshipType.HAS_FIREWALL_RULE.value == "HAS_FIREWALL_RULE"


def test_relationship_type_has_security_posture_has_correct_value():
    assert RelationshipType.HAS_SECURITY_POSTURE.value == "HAS_SECURITY_POSTURE"


def test_has_firewall_rule_registered_in_relationship_props():
    from infra_brain.db.relationships import RELATIONSHIP_PROPS

    assert RelationshipType.HAS_FIREWALL_RULE in RELATIONSHIP_PROPS


def test_has_security_posture_registered_in_relationship_props():
    from infra_brain.db.relationships import RELATIONSHIP_PROPS

    assert RelationshipType.HAS_SECURITY_POSTURE in RELATIONSHIP_PROPS


def test_relationship_type_triggered_deployment_has_correct_value():
    assert RelationshipType.TRIGGERED_DEPLOYMENT.value == "TRIGGERED_DEPLOYMENT"


def test_relationship_type_provisions_has_correct_value():
    assert RelationshipType.PROVISIONS.value == "PROVISIONS"


def test_triggered_deployment_registered_in_relationship_props():
    from infra_brain.db.relationships import RELATIONSHIP_PROPS

    assert RelationshipType.TRIGGERED_DEPLOYMENT in RELATIONSHIP_PROPS


def test_provisions_registered_in_relationship_props():
    from infra_brain.db.relationships import RELATIONSHIP_PROPS

    assert RelationshipType.PROVISIONS in RELATIONSHIP_PROPS


def test_relationship_type_owns_has_correct_value():
    assert RelationshipType.OWNS.value == "OWNS"


def test_owns_registered_in_relationship_props():
    from infra_brain.db.relationships import RELATIONSHIP_PROPS

    assert RelationshipType.OWNS in RELATIONSHIP_PROPS


def test_relationship_type_runs_service_has_correct_value():
    assert RelationshipType.RUNS_SERVICE.value == "RUNS_SERVICE"


def test_relationship_type_has_cron_has_correct_value():
    assert RelationshipType.HAS_CRON.value == "HAS_CRON"


def test_relationship_type_has_mount_has_correct_value():
    assert RelationshipType.HAS_MOUNT.value == "HAS_MOUNT"


def test_relationship_type_has_pending_update_has_correct_value():
    assert RelationshipType.HAS_PENDING_UPDATE.value == "HAS_PENDING_UPDATE"


def test_linux_os_internals_types_registered_in_relationship_props():
    from infra_brain.db.relationships import RELATIONSHIP_PROPS

    for rt in (
        RelationshipType.RUNS_SERVICE,
        RelationshipType.HAS_CRON,
        RelationshipType.HAS_MOUNT,
        RelationshipType.HAS_PENDING_UPDATE,
    ):
        assert rt in RELATIONSHIP_PROPS


# ---------------------------------------------------------------------------
# ResourceRelationship ORM model
# ---------------------------------------------------------------------------


def test_resource_relationship_tablename():
    assert ResourceRelationship.__tablename__ == "resource_relationships"


def test_resource_relationship_has_required_columns():
    """Spot-check that the mapper exposes the expected column names."""
    col_names = {c.name for c in ResourceRelationship.__table__.columns}
    expected = {
        "id",
        "from_resource_id",
        "to_resource_id",
        "relationship_type",
        "properties",
        "confidence",
        "source",
        "created_at",
        "last_seen",
    }
    assert expected <= col_names, f"Missing columns: {expected - col_names}"


def test_resource_relationship_unique_constraint_exists():
    """The table must carry the uq_resource_relationships unique constraint."""
    constraint_names = {
        c.name for c in ResourceRelationship.__table__.constraints if hasattr(c, "name")
    }
    assert "uq_resource_relationships" in constraint_names


# ---------------------------------------------------------------------------
# emit_edge / emit_edges_batch — REMOVED WITH THEIR STORE (P5)
#
# Thirteen tests used to live here, pinning the upsert parameters both helpers
# built for ``resource_relationships``: the ON CONFLICT param names, JSON
# serialisation of ``properties``, the 1.0 confidence default, string-vs-enum
# ``rel_type``, the executemany chunking, and the F-022 self-loop filter.
#
# P5 dropped the table (``95d988b2bc3c``), so every one of those assertions was
# about the shape of a query that can no longer be sent anywhere. Keeping them
# green against a mock session would have been the worst outcome available: a
# suite proving in detail that a deleted write path still formats its
# parameters correctly.
#
# They are replaced by assertions on what is TRUE NOW — a smaller contract, but
# a real one that can actually regress. Deleting them outright would have left
# "the helpers are gone, and gone LOUDLY" unpinned, and that is exactly the
# property a convenience stub would quietly undo.
# ---------------------------------------------------------------------------


def test_emit_helpers_raise_instead_of_silently_doing_nothing():
    """Both helpers must FAIL LOUDLY, not no-op.

    This is the load-bearing property of the replacement stubs. A no-op would
    let any surviving caller report a healthy collection pass while writing
    nothing at all — an empty graph discovered weeks later instead of a stack
    trace on the first run.
    """
    for helper in (emit_edge, emit_edges_batch):
        with pytest.raises(NotImplementedError) as exc:
            helper(MagicMock(), uuid4(), uuid4(), RelationshipType.RUNS_ON, source="t")
        message = str(exc.value)
        assert "resource_relationships" in message, "the stub must name the store that went away"
        assert "graph_edges" in message, "and point at the store that replaced it"


def test_emit_edges_batch_raises_even_for_an_empty_edge_list():
    """The empty-list case must raise too, and that is a deliberate change.

    The old helper returned early on ``[]`` — a sensible optimisation while
    there was somewhere to write. Now, a caller reaching this function at all is
    a caller pointed at a dropped table, and whether it happens to have zero
    edges this pass is luck, not correctness. Letting the empty case through
    would make the failure intermittent, surfacing only on the run that finally
    derived something.
    """
    with pytest.raises(NotImplementedError):
        emit_edges_batch(MagicMock(), [])


def test_resource_relationship_is_not_registered_on_base_metadata():
    """The dropped table must not come back into ``Base.metadata``.

    If it does, ``alembic check`` and the ``migration-parity`` gate start
    reporting the intentional drop as schema drift forever, and the next
    autogenerate proposes re-creating the store P5 retired. The shim that keeps
    ``ResourceRelationship`` importable is only safe BECAUSE it is unmapped, so
    that property is worth a test rather than a comment.
    """
    assert "resource_relationships" not in Base.metadata.tables


def test_shim_table_still_carries_the_constraint_that_defined_the_store():
    """The frozen shim keeps ``uq_resource_relationships``.

    Not decoration: that constraint — one row per (from, to, type), forever —
    IS the reason this store could not record history and had to be replaced. A
    reader who finds the shim should be able to see the limitation without
    going to the migration for it.
    """
    names = {c.name for c in ResourceRelationship.__table__.constraints if hasattr(c, "name")}
    assert "uq_resource_relationships" in names


def test_instantiating_the_shim_is_refused():
    """The shim exists to describe a schema, never to make rows."""
    with pytest.raises(RuntimeError, match="dropped in P5"):
        ResourceRelationship()


# ---------------------------------------------------------------------------
# get_neighborhood / _WALK_SQL — DELETED WITH THEIR SUBJECT (P5 integration)
# ---------------------------------------------------------------------------
#
# ~720 lines of walk tests stood here: the mock-session get_neighborhood
# contract tests, the SQLite transliteration of the bidirectional ``_WALK_SQL``
# (direction preservation, diamond/cycle dedup, node/edge caps, self-loop
# filtering). They were kept alive on the drop branch only so the SIBLING
# branch that retired ``get_neighborhood`` could fold independently. Both
# branches are now folded: ``get_neighborhood`` and ``_WALK_SQL`` no longer
# exist in ``db/relationships.py``, so tests importing them cannot even load.
#
# The replacement walk is ``infra_brain.graph_kg`` (iterative BFS over
# ``graph_nodes``/``graph_edges``, identical on SQLite and PostgreSQL), and its
# behaviour — including direction preservation, dedup, and caps — is tested
# directly in ``tests/test_graph_kg.py`` against the REAL implementation, not a
# transliteration. Nothing from here needed porting.
