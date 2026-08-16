"""Tests for GraphMaintenanceAgent — knowledge-graph edge health and gap-filling."""

from unittest.mock import MagicMock, patch

from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent


def _make_agent():
    agent = GraphMaintenanceAgent.__new__(GraphMaintenanceAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    return agent


def test_graph_maintenance_domain():
    agent = _make_agent()
    assert agent.domain == "graph_maintenance"


def test_run_returns_collection_result():
    """run() returns a CollectionResult with status completed or failed."""
    from infra_brain.agents.base import CollectionResult

    with (
        patch.object(GraphMaintenanceAgent, "_prune_stale_edges", return_value=0),
        patch.object(GraphMaintenanceAgent, "_decay_confidence", return_value=0),
        patch.object(GraphMaintenanceAgent, "_fill_cross_domain_gaps", return_value=0),
        patch.object(GraphMaintenanceAgent, "_link_new_resources", return_value=0),
        patch("infra_brain.agents.graph_maintenance.get_session"),
        patch("infra_brain.etl.base.get_session"),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.api._seeding.upsert_resource"),
    ):
        agent = GraphMaintenanceAgent()
        result = agent.run(scope="all")

    assert isinstance(result, CollectionResult)
    assert result.domain == "graph_maintenance"


def test_collect_folds_phase_timings_into_stats():
    """TRK-117: collect() must surface per-phase wall-clock timings in the
    persisted stats record so real cost is visible in collection_runs/logs
    without a live SSH profiling session.

    TRK-191: collect() no longer returns the stats as a generic items-list
    record (that record used to flow through ETLConnector.run()'s
    Resource+Snapshot pipeline, which fed graph_maintenance's own
    ever-changing internal stats into DriftAgent as fake fleet drift). It now
    writes stats directly onto the "graph-health" report Resource's metadata_
    via upsert_resource and returns a CollectOutcome with no items — so this
    test asserts against the upsert_resource call's metadata= argument instead
    of the old return value.
    """
    from infra_brain.agents.base import CollectOutcome

    with (
        patch.object(GraphMaintenanceAgent, "_prune_stale_edges", return_value={"pruned": 0}),
        patch.object(
            GraphMaintenanceAgent,
            "_decay_confidence",
            return_value={"decayed": 0, "removed": 0},
        ),
        patch.object(
            GraphMaintenanceAgent, "_reconcile_contradictory_edges", return_value={"removed": 0}
        ),
        patch.object(GraphMaintenanceAgent, "_fill_cross_domain_gaps", return_value={"filled": 0}),
        patch.object(GraphMaintenanceAgent, "_link_new_resources", return_value={"linked": 0}),
        patch("infra_brain.agents.graph_maintenance.get_session"),
        patch("infra_brain.api._seeding.upsert_resource") as mock_upsert,
    ):
        agent = _make_agent()
        outcome = agent.collect(scope="all")

    assert isinstance(outcome, CollectOutcome)
    assert outcome.items == []
    assert outcome.count_override == 1

    assert mock_upsert.call_count == 1
    _, kwargs = mock_upsert.call_args
    assert kwargs["domain"] == "graph_maintenance"
    assert kwargs["resource_type"] == "graph_maintenance_report"
    assert kwargs["name"] == "graph-health"
    data = kwargs["metadata"]
    assert "timings" in data, "stats must carry per-phase timings"
    assert set(data["timings"]) >= {
        "prune",
        "decay",
        "reconcile",
        "gaps",
        "link_new",
    }
    # P5 (rev11-T5-B): "typed_relationships" is deliberately ABSENT. The phase
    # it timed is deleted in full, and a timing key for a phase that no longer
    # runs would report 0.0s forever and read as "this got fast".
    assert all(isinstance(v, (int, float)) and v >= 0 for v in data["timings"].values())


def test_run_quick_scope_skips_fill_and_decay():
    """scope='quick' should skip fill_cross_domain_gaps and decay_confidence."""
    fill_mock = MagicMock(return_value=0)
    decay_mock = MagicMock(return_value=0)

    with (
        patch.object(GraphMaintenanceAgent, "_prune_stale_edges", return_value=0),
        patch.object(GraphMaintenanceAgent, "_decay_confidence", decay_mock),
        patch.object(GraphMaintenanceAgent, "_fill_cross_domain_gaps", fill_mock),
        patch.object(GraphMaintenanceAgent, "_link_new_resources", return_value=0),
        patch("infra_brain.agents.graph_maintenance.get_session"),
        patch("infra_brain.etl.base.get_session"),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
        patch("infra_brain.api._seeding.upsert_resource"),
    ):
        agent = GraphMaintenanceAgent()
        result = agent.run(scope="quick")

    fill_mock.assert_not_called()
    decay_mock.assert_not_called()
    assert result.domain == "graph_maintenance"


# ---------------------------------------------------------------------------
# _EdgeBuffer — TESTS DELETED WITH THEIR CLASS (P5, rev11-T5-B)
# ---------------------------------------------------------------------------
#
# Three tests lived here: chunk-boundary auto-flush, per-rel_type counting +
# empty-flush no-op, and "every flush happens inside a begin_nested savepoint".
# All three exercised ``graph_maintenance._EdgeBuffer``, the TRK-108 bounded
# accumulator that existed so the convergence pass could emit ~340k
# ``resource_relationships`` rows without OOM-killing the scheduler container.
# P5 deletes every edge emission in that module, so the class is gone and the
# OOM it guarded against cannot recur. The savepoint discipline the third test
# checked is NOT lost — it is asserted against the surviving blocks in
# tests/agents/test_graph_maintenance_savepoints.py.
