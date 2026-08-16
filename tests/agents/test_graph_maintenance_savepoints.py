"""F-023: SAVEPOINT-based block isolation in GraphMaintenanceAgent.

Root cause (PostgreSQL): when a write inside one block raises, PostgreSQL puts
the connection into ``InFailedSqlTransaction``. Every subsequent statement on
that connection then fails too, so one bad block silently costs the whole pass.
The fix is that each block runs inside ``session.begin_nested()`` (a SAVEPOINT),
whose ROLLBACK TO SAVEPOINT clears the failed-transaction state before the next
block starts.

P5 (rev11-T5-B) — WHAT THIS FILE USED TO TEST, AND WHY IT MOVED. Both tests
here drove ``_populate_typed_relationships``: one patched ``emit_edges_batch``
to raise and asserted that the VULNERABLE_TO failure did not stop DEPLOYS_TO,
the other asserted every surviving block set its counts key on SQLite. That
method — and every ``emit_edges_batch`` call in the module — is deleted, so
those two tests died with their code.

The INVARIANT did not die with them. ``collect()`` still runs four blocks that
each write ``graph_edges`` inside their own SAVEPOINT with a broad ``except``
that records into ``_maint_errors``: ``graph_phase2.emit_all``,
``graph_role_tagging.emit_all``, ``graph_engine.emit_all`` and
``graph_phase3.resolve_entities``. F-023 is now about THOSE, and that is what
the tests below assert: a failure in the first must not stop the other three,
must be recorded exactly once, and must still fail the run.

SQLite note: ``InFailedSqlTransaction`` is PostgreSQL-specific. These tests run
on the shared in-memory engine and verify (a) the BEGIN NESTED / ROLLBACK
SAVEPOINT path does not itself raise on SQLite, and (b) block-level isolation —
``_maint_errors`` holds exactly the failed block's entry and the later blocks
executed cleanly.
"""

from unittest.mock import MagicMock, patch

import pytest

from infra_brain.db.models import ZONE_CORPORATE
from tests.support.pg import make_engine

MODULE = "infra_brain.agents.graph_maintenance"


@pytest.fixture
def engine():
    """In-memory SQLite engine with the full schema, shared across threads."""
    return make_engine()


def _agent(make_agent):
    from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent

    agent = make_agent(GraphMaintenanceAgent)
    agent.settings.is_same_as_decay_days = 14
    agent.settings.graph_edge_decay_enabled = False
    agent.settings.default_zone = ZONE_CORPORATE
    agent.domain = "graph_maintenance"
    agent._maint_errors = []
    return agent


def _session_patch(engine):
    from contextlib import contextmanager

    from sqlalchemy.orm import Session

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    return patch(f"{MODULE}.get_session", _get_session)


def test_emitter_failure_does_not_cascade(engine, make_agent):
    """A raising ``graph_phase2.emit_all`` must not stop the three that follow.

    Without the SAVEPOINT + per-block ``except``, the exception would escape
    ``collect()`` before role-tagging, the declarative engine and the resolver
    ever ran — losing the entire graph build to one bad emitter.
    """
    agent = _agent(make_agent)

    with (
        _session_patch(engine),
        patch(
            f"{MODULE}.graph_phase2.emit_all",
            side_effect=RuntimeError("simulated phase2 failure"),
        ),
        patch(f"{MODULE}.graph_role_tagging.emit_all", return_value=({}, [])) as role,
        patch(f"{MODULE}.graph_engine.emit_all", return_value=({}, [])) as engine_emit,
        patch(f"{MODULE}.graph_phase3.resolve_entities", return_value={}) as resolve,
        patch("infra_brain.api._seeding.upsert_resource") as upsert,
    ):
        with pytest.raises(RuntimeError, match="graph-maintenance emitter"):
            agent.collect(scope="all")

    # The three later blocks ran anyway — that is the isolation claim.
    assert role.call_count == 1
    assert engine_emit.call_count == 1
    assert resolve.call_count == 1

    # Exactly one recorded error, attributed to the block that actually failed.
    assert len(agent._maint_errors) == 1
    assert agent._maint_errors[0].startswith("graph_phase2:")
    assert "simulated phase2 failure" in agent._maint_errors[0]

    # F-008: the session COMMIT happens before the raise, so the graph_edges
    # work the surviving blocks did is preserved — but the graph-health report
    # is deliberately NOT written on a failed pass, so a run that blew up cannot
    # leave a clean-looking stats row behind.
    assert upsert.call_count == 0


def test_begin_nested_does_not_raise_on_sqlite(engine, make_agent):
    """SAVEPOINT is supported by SQLite since 3.6.8 — the wrapper must be inert.

    Nothing is seeded, so every block is a clean no-op; the point is that the
    ``begin_nested()`` around each one neither raises nor records an error.
    """
    agent = _agent(make_agent)

    with (
        _session_patch(engine),
        patch(f"{MODULE}.graph_phase2.emit_all", return_value=({}, [])),
        patch(f"{MODULE}.graph_role_tagging.emit_all", return_value=({}, [])),
        patch(f"{MODULE}.graph_engine.emit_all", return_value=({}, [])),
        patch(f"{MODULE}.graph_phase3.resolve_entities", return_value={}),
        patch("infra_brain.api._seeding.upsert_resource") as upsert,
    ):
        outcome = agent.collect(scope="all")

    assert agent._maint_errors == []
    assert outcome.count_override == 1
    stats = upsert.call_args.kwargs["metadata"]
    for key in ("pruned", "decayed", "contradictory_edges_removed", "gaps_filled", "new_linked"):
        assert stats[key] == 0
    # P5: the typed-relationship keys survive as pinned-empty time-series slots.
    assert stats["typed_edges"] == {}
    assert stats["typed_edges_skipped"] == []
    assert stats["gating"] == "n/a"


def test_emitter_error_list_still_fails_the_run(engine, make_agent):
    """An emitter that RETURNS errors (rather than raising) must also fail.

    ``graph_phase2``/``graph_role_tagging``/``graph_engine`` each return
    ``(counts, errors)``; a non-empty error list is folded into
    ``_maint_errors`` and must reach the same RuntimeError as a raise.
    """
    agent = _agent(make_agent)

    with (
        _session_patch(engine),
        patch(f"{MODULE}.graph_phase2.emit_all", return_value=({}, ["bad node spec"])),
        patch(f"{MODULE}.graph_role_tagging.emit_all", return_value=({}, [])),
        patch(f"{MODULE}.graph_engine.emit_all", return_value=({}, [])),
        patch(f"{MODULE}.graph_phase3.resolve_entities", return_value={}),
        patch("infra_brain.api._seeding.upsert_resource"),
    ):
        with pytest.raises(RuntimeError) as exc:
            agent.collect(scope="all")

    assert "bad node spec" in str(exc.value)
    assert agent._maint_errors == ["graph_phase2: bad node spec"]


def test_make_agent_fixture_is_a_real_agent(make_agent):
    """Guard against the fixture silently handing back a MagicMock."""
    from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent

    agent = _agent(make_agent)
    assert isinstance(agent, GraphMaintenanceAgent)
    assert not isinstance(agent.collect, MagicMock)
