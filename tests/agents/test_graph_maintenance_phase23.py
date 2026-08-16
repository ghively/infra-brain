"""graph_phase2/phase3/role_tagging wiring into GraphMaintenanceAgent.collect().

Previously these three modules were built, tested, and merged (TRK-196,
TRK-197, GitLab #128) but never called by anything outside their own test
suites. This file locks in the wiring: the three emitters run on scope="all"
(after typed-relationships, before link_new), are skipped on scope="quick"
like the typed-relationship pass, and a failure in any one of them is
isolated (mirrors the existing F-008 per-block error pattern) rather than
aborting the other two or the whole run silently.
"""

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy.orm import sessionmaker


from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


def _session_ctx(engine):
    factory = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    return _session


def test_all_three_emitters_run_on_full_pass_and_feed_stats(engine, make_agent):
    from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent

    agent = make_agent(GraphMaintenanceAgent)
    agent.settings.is_same_as_decay_days = 14
    agent.settings.default_zone = "corporate"

    with (
        patch("infra_brain.agents.graph_maintenance.get_session", _session_ctx(engine)),
        patch("infra_brain.etl.base.get_session", _session_ctx(engine)),
        patch(
            "infra_brain.agents.graph_maintenance.graph_phase2.emit_all",
            return_value=({"hosted_on": 3}, []),
        ) as p2,
        patch(
            "infra_brain.agents.graph_maintenance.graph_role_tagging.emit_all",
            return_value=({"roles_from_hostname": 5}, []),
        ) as rt,
        patch(
            "infra_brain.agents.graph_maintenance.graph_phase3.resolve_entities",
            return_value={"deterministic_edges": 2, "nodes_considered": 10},
        ) as p3,
    ):
        result = agent.run(scope="all")

    assert result.status == "completed"
    p2.assert_called_once()
    rt.assert_called_once()
    p3.assert_called_once()


def test_emitter_failure_is_isolated_not_fatal_to_the_others(engine, make_agent):
    """One emitter raising must not stop the other two, or the commit — only
    flips the run to failed after (F-008), same as the pre-existing 16
    typed-relationship except-blocks."""
    from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent

    agent = make_agent(GraphMaintenanceAgent)
    agent.settings.is_same_as_decay_days = 14
    agent.settings.default_zone = "corporate"

    with (
        patch("infra_brain.agents.graph_maintenance.get_session", _session_ctx(engine)),
        patch("infra_brain.etl.base.get_session", _session_ctx(engine)),
        patch(
            "infra_brain.agents.graph_maintenance.graph_phase2.emit_all",
            side_effect=RuntimeError("boom phase2"),
        ),
        patch(
            "infra_brain.agents.graph_maintenance.graph_role_tagging.emit_all",
            return_value=({"roles_from_hostname": 1}, []),
        ) as rt,
        patch(
            "infra_brain.agents.graph_maintenance.graph_phase3.resolve_entities",
            return_value={"deterministic_edges": 0},
        ) as p3,
    ):
        result = agent.run(scope="all")

    assert result.status == "failed"
    assert any("graph_phase2" in e and "boom phase2" in e for e in result.errors)
    # The other two still ran despite phase2 blowing up.
    rt.assert_called_once()
    p3.assert_called_once()


def test_quick_scope_skips_all_three_emitters(engine, make_agent):
    from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent

    agent = make_agent(GraphMaintenanceAgent)
    agent.settings.default_zone = "corporate"

    with (
        patch("infra_brain.agents.graph_maintenance.get_session", _session_ctx(engine)),
        patch("infra_brain.etl.base.get_session", _session_ctx(engine)),
        patch(
            "infra_brain.agents.graph_maintenance.graph_phase2.emit_all",
            side_effect=AssertionError("must not be called on scope=quick"),
        ) as p2,
        patch(
            "infra_brain.agents.graph_maintenance.graph_role_tagging.emit_all",
            side_effect=AssertionError("must not be called on scope=quick"),
        ) as rt,
        patch(
            "infra_brain.agents.graph_maintenance.graph_phase3.resolve_entities",
            side_effect=AssertionError("must not be called on scope=quick"),
        ) as p3,
    ):
        result = agent.run(scope="quick")

    assert result.status == "completed"
    p2.assert_not_called()
    rt.assert_not_called()
    p3.assert_not_called()
