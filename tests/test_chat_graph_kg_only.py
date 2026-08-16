"""The chat agent's graph answers must come from the KG store, never the legacy one.

WHY THIS IS THE LOAD-BEARING TEST FOR THE P5 CONSUMER SPLIT
-----------------------------------------------------------
``resource_relationships`` is being dropped. Before it can be, every consumer
has to be re-pointed or removed — and "re-pointed" has to mean something
stronger than "the import moved". A chat tool that still emitted the legacy
store's edges would keep answering questions correctly right up until the
migration lands, then start returning nothing (or raising) with no test having
noticed.

So this pins the property directly rather than the implementation: seed BOTH
stores with contradictory data — a real ``RUNS_ON`` edge in
``graph_nodes``/``graph_edges``, and a decoy row in ``resource_relationships``
that exists nowhere else — then assert the chat tool's answer contains the
former, never the latter, and that no SQL statement issued during the call so
much as names ``resource_relationships``. The statement counter is what makes
this survive a refactor: an implementation that reads the legacy table for any
reason (even a fallback, even a JOIN) fails here regardless of whether its
output happens to look right on this fixture.

The third assertion is about honesty rather than provenance: containment —
"what services run on this host" — is NOT expressible as a graph edge in the
KG store (they are rows in ``linux_services``, a detail table). The tool must
therefore answer that half from the fact tables and say so, instead of
silently returning a smaller graph than the legacy store used to and letting
the model infer the host runs nothing.
"""

from __future__ import annotations

import uuid as _uuid_mod
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from infra_brain.db.models import (
    GraphEdgeMethod,
    LinuxHost,
    LinuxService,
    Resource,
)

from tests.support.pg import make_engine


#: A relationship_type that exists ONLY in the legacy store in this fixture.
#: If it ever shows up in a chat answer, the tool read the table being dropped.
# ``DECOY_TYPE`` and the legacy-store decoy row are gone (P5 integration): the
# table cannot be seeded — ``create_all`` no longer builds it and the ORM shim
# refuses instantiation — so "the decoy never surfaces" is structural now. The
# ``_legacy_reads`` SQL listener is KEPT: it still proves the chat tools issue
# no statement naming the dropped table (which would crash in production).


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def seeded(engine):
    """One host with a KG neighbourhood and first-hand fact-table rows.

    KG store        : HomelabService 'litellm' --RUNS_ON--> LinuxHost 'node_a'
    Fact tables     : linux_services 'docker', 'tailscaled' on node_a
    (Legacy store   : dropped in P5 — nothing to seed.)
    """
    from infra_brain import graph_phase2

    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        host_res = Resource(
            domain="linux",
            type="linux_host",
            name="node_a",
            source="linux",
            zone="lab",
            last_seen=now,
        )
        s.add(host_res)
        s.flush()

        lh = LinuxHost(resource_id=host_res.id, distro="debian", kernel="6.1", arch="aarch64")
        s.add(lh)
        s.flush()
        s.add_all(
            [
                LinuxService(host_id=lh.id, name="docker", state="running", enabled=True),
                LinuxService(host_id=lh.id, name="tailscaled", state="running", enabled=True),
            ]
        )

        host_node = graph_phase2.upsert_node(
            s,
            node_type="LinuxHost",
            natural_key="node_a",
            name="node_a",
            source="linux",
            resource_id=host_res.id,
        )
        svc_node = graph_phase2.upsert_node(
            s,
            node_type="HomelabService",
            natural_key="node_a/litellm",
            name="litellm",
            source="homelab_services",
            attributes={"host": "node_a"},
        )
        graph_phase2.upsert_edge(
            s,
            source_id=svc_node.id,
            target_id=host_node.id,
            edge_type="RUNS_ON",
            method=GraphEdgeMethod.DETERMINISTIC_MATCH,
            confidence=Decimal("0.990"),
            source="homelab_services",
            evidence={"basis": "test fixture"},
        )

        s.commit()
        return {
            "host_resource_id": str(host_res.id),
            "host_node_id": str(host_node.id),
        }


@contextmanager
def _legacy_reads(engine):
    """Count every SQL statement issued that names ``resource_relationships``."""
    seen: list[str] = []

    def _listen(conn, cursor, statement, params, context, executemany):
        if "resource_relationships" in statement.lower():
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", _listen)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _listen)


@contextmanager
def _chat_db(engine):
    """Point every chat-tool DB accessor at the fixture engine."""

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with (
        patch("infra_brain.chat.tools.get_session", _get_session),
        patch("infra_brain.chat.tools.get_engine", lambda: engine),
    ):
        yield


# ---------------------------------------------------------------------------
# query_resource_neighborhood (chat/tools.py) — name-addressed
# ---------------------------------------------------------------------------


def test_neighborhood_by_name_reads_kg_and_never_the_legacy_table(engine, seeded):
    from infra_brain.chat.tools import query_resource_neighborhood

    with _chat_db(engine), _legacy_reads(engine) as legacy_sql:
        result = query_resource_neighborhood.invoke({"resource_name": "node_a", "depth": 1})

    assert "litellm" in result, result
    assert "RUNS_ON" in result, result
    assert legacy_sql == [], f"chat tool read the legacy store: {legacy_sql}"


def test_neighborhood_reports_containment_facts_the_kg_store_cannot_hold(engine, seeded):
    """RUNS_ON edges + linux_services facts, both present and both labelled.

    The KG store holds no containment edges by design (they are detail-table
    facts). Answering "what runs on node_a" from edges alone would omit every
    systemd unit on the box, which is a smaller answer than the legacy store
    gave — so the tool supplements from the fact tables.
    """
    from infra_brain.chat.tools import query_resource_neighborhood

    with _chat_db(engine), _legacy_reads(engine) as legacy_sql:
        result = query_resource_neighborhood.invoke({"resource_name": "node_a", "depth": 1})

    assert "docker" in result, result
    assert "tailscaled" in result, result
    assert legacy_sql == []


def test_neighborhood_unknown_name_says_so(engine, seeded):
    from infra_brain.chat.tools import query_resource_neighborhood

    with _chat_db(engine), _legacy_reads(engine) as legacy_sql:
        result = query_resource_neighborhood.invoke({"resource_name": "no-such-host"})

    assert "not found" in result.lower(), result
    assert legacy_sql == []


# ---------------------------------------------------------------------------
# query_graph_neighborhood (chat/agent.py) — the tool actually bound to the LLM
# ---------------------------------------------------------------------------


def test_bound_tool_accepts_a_resources_uuid_via_the_graph_nodes_bridge(engine, seeded):
    """``graph_nodes.resource_id`` is the documented bridge between the two id
    spaces; a caller holding a ``resources.id`` must still land on the right
    graph node rather than 'not found'."""
    from infra_brain.chat.agent import query_graph_neighborhood

    with _chat_db(engine), _legacy_reads(engine) as legacy_sql:
        result = query_graph_neighborhood.invoke(
            {"resource_id": seeded["host_resource_id"], "depth": 1}
        )

    assert isinstance(result, dict), result
    assert "error" not in result, result
    edge_types = {e["edge_type"] for e in result["edges"]}
    assert "RUNS_ON" in edge_types, result
    assert legacy_sql == []


def test_bound_tool_accepts_a_graph_node_uuid(engine, seeded):
    from infra_brain.chat.agent import query_graph_neighborhood

    with _chat_db(engine), _legacy_reads(engine) as legacy_sql:
        result = query_graph_neighborhood.invoke({"resource_id": seeded["host_node_id"]})

    assert "error" not in result, result
    assert {e["edge_type"] for e in result["edges"]} == {"RUNS_ON"}
    assert legacy_sql == []


def test_bound_tool_accepts_a_bare_name(engine, seeded):
    """The LLM never holds a UUID — ``query_resources`` returns hostnames, not
    ids — so the bound tool has to accept the identifier the model can actually
    produce, or the graph is unreachable from chat in practice."""
    from infra_brain.chat.agent import query_graph_neighborhood

    with _chat_db(engine), _legacy_reads(engine) as legacy_sql:
        result = query_graph_neighborhood.invoke({"resource_id": "node_a"})

    assert "error" not in result, result
    assert {e["edge_type"] for e in result["edges"]} == {"RUNS_ON"}
    assert legacy_sql == []


def test_bound_tool_unknown_identifier_returns_a_structured_miss(engine, seeded):
    from infra_brain.chat.agent import query_graph_neighborhood

    with _chat_db(engine), _legacy_reads(engine) as legacy_sql:
        result = query_graph_neighborhood.invoke({"resource_id": str(_uuid_mod.uuid4())})

    assert result.get("nodes") == [] or "not found" in str(result).lower(), result
    assert legacy_sql == []


def test_oversized_dict_result_degrades_instead_of_raising():
    """Regression: `_truncate_tool_result` used to slice the SERIALIZED json at
    `max_chars` and then `json.loads` the fragment, which raises
    ``JSONDecodeError: Unterminated string ...`` for essentially every oversized
    dict. Live symptom (2026-08-13, post-deploy): the chat agent told the
    operator "the knowledge-graph query keeps failing on that entity with a
    serialization error (Unterminated string starting at line 1 column 11999)
    ... likely a malformed node/edge record" — a truncation bug that presented
    itself as database corruption. Latent since it was written; it first fired
    when the identity-merged neighbourhood payload crossed the 12k budget.
    """
    import json as _json

    from infra_brain.chat.tools import _truncate_tool_result

    oversized = {
        "root": "node_a",
        "nodes": [{"id": i, "name": "n" * 300} for i in range(200)],
        "edges": [{"type": "RUNS_ON"} for _ in range(50)],
    }

    out = _truncate_tool_result(oversized)

    assert out["_truncated"] is True
    # The envelope must round-trip. The original code could not even BUILD this
    # — it raised while constructing the reply.
    assert _json.loads(_json.dumps(out, default=str))["_truncated"] is True

    # EVERY key survives. The first attempt at this fix dropped whole keys and
    # chose to drop `edges` — on a "what is connected to X" answer, the edges
    # are the answer, so a payload that keeps the verbose node list and discards
    # the connections is worse than a short one.
    for key in oversized:
        assert key in out, f"{key} must survive truncation, not be dropped"
    assert out["edges"], "edges must never be trimmed to nothing while nodes remain"
    # A trimmed list must say so, with counts — a partial answer has to look partial.
    assert "showing" in out["trimmed"]["nodes"]
