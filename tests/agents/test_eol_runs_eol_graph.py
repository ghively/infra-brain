"""``eol`` declares ``<LinuxHost> ─RUNS_EOL→ <EolProduct>`` (TRK-359).

The SECOND junction declaration, restoring the second of P5's two accepted
losses — and the anchor-gather shape: ``eol_registry`` hangs directly off
``resources``, its ``resource_id`` IS the representative host, so the product
node's fan-out list is gathered off the junction rows' own anchors
(``RowGather(path=())``) rather than from a deeper table.

THE ORACLE IS THE FACT THE P5 RECORD NAMED, COMPUTED INDEPENDENTLY. The
accepted-loss record (db/relationships.py, then this branch's TRACKER row)
states the fact as "``eol_registry`` (asset_name, eol_date, representative
resource_id) joined to the host tables", and the deleted deriver
(``graph_maintenance`` §12, KG gap 7) derived exactly that join: one edge per
registry row anchored on a known host, none for a row whose ``resource_id``
already IS the minted ``eol/product`` resource (its self-loop guard).
``_reconstruction_runs_eol`` below is that join as a plain query over
``eol_registry`` ⋈ ``resources``, and the suite asserts the engine's emitted
edges equal it over a fixture carrying the awkward shapes: one product on two
hosts, one host on two products, a product whose asset_name matches NO host
(a minted-anchor row), a retired host, and the spelling variants the ``host``
normaliser exists for.

WHAT THE DECLARATION DELIBERATELY DOES NOT REPRODUCE, on the record:
  * the deriver's get-or-create of ``eol/product`` shadow RESOURCES — the
    product is a graph node now (``graph_nodes``), not a minted resource row;
  * edges for non-linux representative hosts (vsphere/windows/r7) — retired
    domains on this estate, zero live rows, same revival note as
    ANSIBLE_MANAGES' Windows note;
  * confidence 1.0 — the graph-side join is by host NAME, a display-name
    join, priced 0.900 under the honesty rule the legacy store never had.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain.agents.eol import EOLAgent
from infra_brain.db.models import EolRegistry, GraphEdge, GraphNode, Resource

from tests.support.pg import make_engine

RUNS_EOL = "RUNS_EOL"
PRODUCT_NODE = "EolProduct"
HOST_NODE = "LinuxHost"


@pytest.fixture()
def session():
    engine = make_engine()
    with Session(engine) as s:
        yield s
        s.rollback()


class _LinuxSpecStub:
    """``LinuxAgent.spec`` without importing the agent — same device and same
    keep-in-step guard as tests/agents/test_iac_member_of_graph.py."""

    from infra_brain.etl.spec import AgentSpec, NodeSpec, Tier

    spec = AgentSpec(
        domain="linux",
        tier=Tier.COLLECTOR,
        schedule=None,
        max_staleness=None,
        emits_nodes=(NodeSpec(type=HOST_NODE, resource_type="linux_host", natural_key="name"),),
    )


def _specs():
    return {"eol": EOLAgent.spec, "linux": _LinuxSpecStub.spec}


# --- fixture ----------------------------------------------------------------


def _host(session, name, *, retired=False):
    res = Resource(
        id=uuid.uuid4(),
        domain="linux",
        type="linux_host",
        name=name,
        source="LinuxAgent",
        metadata_={"distro": "Ubuntu"},
        retired_at=datetime.now(UTC) if retired else None,
    )
    session.add(res)
    session.flush()
    return res


def _product_resource(session, asset_name):
    """The minted ``eol/product`` anchor a manual/unresolved registry row has."""
    res = Resource(
        id=uuid.uuid4(),
        domain="eol",
        type="product",
        name=asset_name,
        source="EOLAgent",
        metadata_={},
    )
    session.add(res)
    session.flush()
    return res


def _registry_row(session, asset_name, resource, eol="2025-04-02"):
    session.add(
        EolRegistry(
            id=uuid.uuid4(),
            resource_id=resource.id,
            asset_name=asset_name,
            eol_date=datetime.fromisoformat(eol).replace(tzinfo=UTC) if eol else None,
            pci_risk_score=90,
            last_updated=datetime.now(UTC),
        )
    )
    session.flush()


def _fixture(session):
    """The realistic shape, every edge case labelled.

    * "Ubuntu 20.04" is run by TWO hosts (two registry rows, same asset_name —
      the non-unique-by-design index at work);
    * ``node_a`` runs TWO products (host fan-in);
    * "Windows Server 2012" anchors on a minted ``eol/product`` resource — a
      product with NO known host, which must produce NO node and NO edge;
    * "CentOS 6" anchors on a RETIRED host — excluded like every retired row;
    * ``media_host`` is an underscore/case spelling of a machine other sources
      call ``media-host`` — the ``host`` normaliser's case (the LinuxHost node
      here is named the same way, so the fold must hold on both sides).
    """
    hosts = {}
    for name in ("node_a", "media_host", "git_runner"):
        hosts[name] = _host(session, name)
    dead = _host(session, "retired_host", retired=True)

    _registry_row(session, "Ubuntu 20.04", hosts["node_a"])
    _registry_row(session, "Ubuntu 20.04", hosts["media_host"])
    _registry_row(session, "CentOS 7", hosts["git_runner"])
    _registry_row(session, "Debian 10", hosts["node_a"])
    _registry_row(session, "CentOS 6", dead)
    _registry_row(session, "Windows Server 2012", _product_resource(session, "Windows Server 2012"))
    return hosts


# --- the oracle -------------------------------------------------------------


def _reconstruction_runs_eol(session) -> set[tuple[uuid.UUID, str]]:
    """The accepted-loss fact, computed independently of the engine.

    ``eol_registry`` joined to the host table it anchors on: one
    ``(host resource id, product)`` pair per registry row whose representative
    resource is a live linux host. Rows anchored on the minted ``eol/product``
    resource fall out of the join exactly as they fell to the deriver's
    self-loop guard.
    """
    rows = session.execute(
        select(Resource.id, EolRegistry.asset_name)
        .select_from(EolRegistry)
        .join(Resource, Resource.id == EolRegistry.resource_id)
        .where(
            Resource.domain == "linux",
            Resource.type == "linux_host",
            Resource.retired_at.is_(None),
        )
    ).all()
    return {(rid, asset_name) for rid, asset_name in rows}


def _emitted_runs_eol(session) -> set[tuple[uuid.UUID, str]]:
    nodes = {n.id: n for n in session.execute(select(GraphNode)).scalars()}
    pairs: set[tuple[uuid.UUID, str]] = set()
    for edge in session.execute(select(GraphEdge).where(GraphEdge.edge_type == RUNS_EOL)).scalars():
        source = nodes[edge.source_id]
        target = nodes[edge.target_id]
        assert source.node_type == HOST_NODE, "RUNS_EOL must be STORED host -> product"
        assert target.node_type == PRODUCT_NODE
        pairs.add((source.resource_id, target.natural_key))
    return pairs


# --- equivalence ------------------------------------------------------------


def test_emitted_edges_equal_the_reconstruction(session):
    """Requirement 4 of TRK-359: engine output == the recorded fact, exactly."""
    from infra_brain import graph_engine

    hosts = _fixture(session)

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert errors == [], errors
    oracle = _reconstruction_runs_eol(session)
    emitted = _emitted_runs_eol(session)
    assert emitted == oracle, (
        f"symmetric difference must be EMPTY (audit standard): "
        f"engine-only={emitted - oracle}, oracle-only={oracle - emitted}"
    )
    # The fixture exercised what it claims: 4 pairs — two hosts on one
    # product, two products on one host, the minted-anchor and retired rows
    # both absent.
    assert oracle == {
        (hosts["node_a"].id, "Ubuntu 20.04"),
        (hosts["media_host"].id, "Ubuntu 20.04"),
        (hosts["git_runner"].id, "CentOS 7"),
        (hosts["node_a"].id, "Debian 10"),
    }
    assert counts["edges"][RUNS_EOL] == 4


def test_a_product_with_no_known_host_produces_no_node_and_no_edge(session):
    """The minted-anchor case, pinned on its own: no host, no graph presence.

    The product is NOT lost — it keeps its ``eol_registry`` row and its
    ``eol_cycle`` resource; what it has no basis for is a RUNS_EOL assertion,
    and the deriver's self-loop guard said the same thing less directly.
    """
    from infra_brain import graph_engine

    _registry_row(session, "Windows Server 2012", _product_resource(session, "Windows Server 2012"))

    counts, errors = graph_engine.emit_all(session, specs=_specs())

    assert errors == [], errors
    assert counts["nodes"].get(PRODUCT_NODE, 0) == 0
    assert session.execute(select(GraphNode)).scalars().all() == []
    assert session.execute(select(GraphEdge)).scalars().all() == []


def test_the_product_node_carries_its_hosts_and_no_owner(session):
    from infra_brain import graph_engine

    _fixture(session)

    graph_engine.emit_all(session, specs=_specs())

    node = session.execute(
        select(GraphNode).where(
            GraphNode.node_type == PRODUCT_NODE, GraphNode.natural_key == "Ubuntu 20.04"
        )
    ).scalar_one()
    assert node.attributes == {"product": "Ubuntu 20.04", "hosts": ["media_host", "node_a"]}
    assert node.resource_id is None, "a product two hosts run is owned by no single row"
    assert node.source == "eol"


def test_the_declaration_matches_what_this_suite_assumes():
    """Keep-in-step guard for the spec constants this oracle relies on."""
    spec = EOLAgent.spec
    (product,) = spec.emits_nodes
    assert product.type == PRODUCT_NODE
    assert (product.domain, product.resource_type) == ("linux", "linux_host"), (
        "the junction anchors on linux's host rows — the representative host, "
        "the only live host domain on this estate"
    )
    assert product.from_rows.path == (("eol_registry", "resource_id"),)
    assert product.from_rows.column == "asset_name"
    (hosts_gather,) = product.row_gathers
    assert hosts_gather.path == (), "anchor gather: the junction row's anchor IS the host"
    assert hosts_gather.column == "name"
    (edge,) = spec.emits_edges
    assert edge.type == RUNS_EOL
    assert (edge.from_node, edge.to_node) == (PRODUCT_NODE, HOST_NODE)
    assert edge.from_key == f"attributes.{hosts_gather.key}"
    assert edge.from_key_multi is True
    assert edge.is_inverse, "RUNS_EOL is STORED host -> product; the join starts at the product"
    assert edge.written_as() == (HOST_NODE, PRODUCT_NODE)
    assert edge.key_normalizer == "host"
    assert edge.method == "deterministic_match"
    assert float(edge.confidence) == 0.9

    from infra_brain.agents.linux import LinuxAgent

    real = next(n for n in LinuxAgent.spec.emits_nodes if n.type == HOST_NODE)
    stub = _LinuxSpecStub.spec.emits_nodes[0]
    assert (stub.type, stub.resource_type, stub.natural_key) == (
        real.type,
        real.resource_type,
        real.natural_key,
    ), "the stub drifted from linux's real declaration"
