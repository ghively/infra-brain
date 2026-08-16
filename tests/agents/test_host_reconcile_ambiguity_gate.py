"""KG-2 ambiguity persistence for HostReconcileAgent, and the gate it feeds.

This file used to have two halves. Half (a) drove the agent's own
``_emit_is_same_as_edges`` / ``_emit_cross_hostname_ip_edges`` and asserted they
consulted the ambiguous legs and the cross-store pair gate BEFORE asserting a
0.95 IS_SAME_AS edge — the fix for a reconciler that asserted the coin-flip
winner of a same-source collision while the very same tie sat pending human
review.

P5 (2026-08-12) removed those emitters entirely: identity has ONE writer,
``graph_phase3.resolve_entities``, so half (a) is gone with the code it tested
rather than re-pointed at the resolver. The gates themselves did not go
anywhere — they moved from "consulted before this agent asserts" to "the only
place anything asserts" — and their tests live where the behaviour now is:

* ``tests/test_graph_phase3_resolution.py`` — pair_gate, the veto/re-ask ladder,
  the review queue.
* ``tests/test_graph_phase3_ambiguity_counter_evidence.py`` — an unsettled leg
  as counter-evidence, including the fail-closed load.
* ``tests/test_p5_issameas_resolver_coverage.py`` — the end-to-end proof that an
  unsettled leg still blocks the merge after the writer switch.

What remains here is half (b) and the contract that connects the two modules:

(b) ``identity_ambiguous_sources`` must be a real persisted column on
    ``host_identities`` — it previously lived only on the in-memory merged dict
    and died when ``run()`` returned, which made its docstring's promise of
    "downstream consumers can tell this source's leg is unsettled" false. That
    column is now this agent's PRIMARY contribution to identity: it is what
    ``graph_phase3.ambiguous_leg_index`` reads back.

...plus the proof that the batched gate and the single-pair predicate are one
decision procedure, not two — the shape of the original bug, and still worth
holding even though this agent no longer calls the batched form.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.host_reconcile import HostReconcileAgent
from infra_brain.db.models import (
    GraphNode,
    GraphNodeType,
    HostIdentity,
    ProposedAction,
    Resource,
)
from infra_brain.graph_phase3 import (
    REVIEW_ACTION_TYPE,
    REVIEW_AGENT,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


def _make_agent():
    agent = HostReconcileAgent.__new__(HostReconcileAgent)
    agent.settings = MagicMock()
    agent.callbacks = []
    return agent


def _session_ctx(engine):
    @contextmanager
    def _get():
        with Session(engine) as s:
            yield s

    return _get


def _seed_two_source_host(session, *, short="web01"):
    """Two resources on one merged host; returns (r7_resource_id, vsphere_resource_id)."""
    r7 = Resource(domain="rapid7", name=short, type="asset", source="rapid7")
    vs = Resource(domain="vsphere", name=short, type="vm", source="vsphere")
    session.add_all([r7, vs])
    session.flush()
    return r7.id, vs.id


def _seed_nodes(session, r7_rid, vs_rid, *, short="web01"):
    """One graph node per resource, in two of the resolver's host node types."""
    r7_node = GraphNode(
        node_type=GraphNodeType.R7_ASSET.value,
        natural_key=f"r7-{short}",
        name=short,
        source="rapid7",
        resource_id=r7_rid,
    )
    vs_node = GraphNode(
        node_type=GraphNodeType.VSPHERE_VM.value,
        natural_key=f"vc-01:vm-{short}",
        name=short,
        source="vsphere",
        resource_id=vs_rid,
    )
    session.add_all([r7_node, vs_node])
    session.flush()
    return r7_node, vs_node


def _pending_question(anchor_node, candidate_node, *, status="pending"):
    return ProposedAction(
        agent=REVIEW_AGENT,
        action_type=REVIEW_ACTION_TYPE,
        target=f"same-as:{anchor_node.node_type}:{anchor_node.natural_key}",
        payload={"candidate_matches": [{"node_id": str(candidate_node.id)}]},
        status=status,
    )


# ---------------------------------------------------------------------------
# (b) identity_ambiguous_sources persistence
# ---------------------------------------------------------------------------


def test_identity_ambiguous_sources_persisted_on_new_row(engine):
    """A same-source collision flag on the merged record must land on the
    host_identities row, not die with the in-memory dict."""
    get_session = _session_ctx(engine)
    agent = _make_agent()

    merged = {
        "web01": {
            "short_hostname": "web01",
            "fqdn": "web01",
            "ip_addresses": [],
            "identity_ambiguous_sources": ["vsphere"],
        }
    }
    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent._upsert_identities(merged)

    with Session(engine) as s:
        row = s.query(HostIdentity).filter_by(short_hostname="web01").one()
        assert row.identity_ambiguous_sources == ["vsphere"]


def test_identity_ambiguous_sources_recomputed_and_cleared(engine):
    """Recomputed each run, not accumulated: once the collision stops being
    observed the persisted flag must clear so it self-heals."""
    get_session = _session_ctx(engine)
    agent = _make_agent()

    base = {
        "short_hostname": "web01",
        "fqdn": "web01",
        "ip_addresses": [],
    }
    with patch("infra_brain.agents.host_reconcile.get_session", get_session):
        agent._upsert_identities({"web01": {**base, "identity_ambiguous_sources": ["vsphere"]}})
        with Session(engine) as s:
            assert s.query(HostIdentity).one().identity_ambiguous_sources == ["vsphere"]
        # Next run: collision gone.
        agent._upsert_identities({"web01": dict(base)})

    with Session(engine) as s:
        assert not s.query(HostIdentity).one().identity_ambiguous_sources


# ---------------------------------------------------------------------------
# The batching layer is the SAME decision logic, not a second one
# ---------------------------------------------------------------------------


def test_batched_gate_agrees_with_the_single_pair_predicate(engine):
    """``resource_pair_gate_index`` is a query-shape optimisation over
    ``pair_gate``, not a parallel implementation of it. Whatever the landed
    single-pair predicate says about a node pair, the preloaded index must say
    about the resources those nodes belong to — otherwise host_reconcile and
    graph_phase3 are back to two answers for one question, which is the exact
    shape of the bug."""
    from infra_brain import graph_phase3

    with Session(engine) as s:
        r7_rid, vs_rid = _seed_two_source_host(s)
        r7_node, vs_node = _seed_nodes(s, r7_rid, vs_rid)
        s.add(_pending_question(r7_node, vs_node))
        s.commit()

    with Session(engine) as s:
        r7_node = s.query(GraphNode).filter_by(natural_key="r7-web01").one()
        vs_node = s.query(GraphNode).filter_by(natural_key="vc-01:vm-web01").one()
        expected = graph_phase3.pair_gate(s, r7_node, vs_node)
        index = graph_phase3.resource_pair_gate_index(s, [r7_rid, vs_rid])
        assert expected == "pending_review"
        assert index.reason(r7_rid, vs_rid) == expected
        assert graph_phase3.resource_pair_gate(s, r7_rid, vs_rid) == expected
