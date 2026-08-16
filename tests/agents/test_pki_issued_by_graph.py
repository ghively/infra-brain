"""``pki`` declares ``<Certificate> ─ISSUED_BY→ <CertificateAuthority>``.

One of the four relationship types P3 counted and refused to backfill because
nothing declared them (``ISSUED_BY``, ``MEMBER_OF``, ``TRIGGERED_BY``,
``RUNS_EOL`` — 21 live rows between them). Judged against the design doc's §3.1
test — *does it connect two things that can each exist, and be referred to,
independently?* — this one is genuine and, unusually, cost-free to declare:
**both ends are already pki's own ``resources`` rows**, so there is no ownership
question of the kind that held ANSIBLE_MANAGES up for two phases.

THE EQUIVALENCE RULE APPLIES HERE IN FULL, unlike its sibling migration. The
anchor does NOT move: the declared edge runs certificate → CA, exactly what the
deleted deriver stored, keyed the same way. So the discipline every earlier
migration met is available and is used —
``_deleted_deriver_issued_by`` is a VERBATIM copy of
``PKIAgent._build_issued_by_edges``' edge construction as it stood at commit
24f18b3, frozen so the claim keeps running rather than retiring the moment it
was first satisfied.

WHAT IS DELIBERATELY *NOT* DELETED ALONGSIDE IT. The deriver did two jobs in one
loop: it minted the ``security/certificate`` ``resources`` row AND it emitted the
edge. Only the second is replaced — the first is the NODE's source of truth, and
deleting it would leave the declaration pointing at rows nothing creates (the
failure mode ``ContainerImage`` was adopted to avoid). The remaining method is
renamed ``_upsert_certificate_resources`` to say so.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain.agents.pki import PKIAgent
from infra_brain.db.models import GraphEdge, GraphNode, Resource

from tests.support.pg import make_engine

ISSUED_BY = "ISSUED_BY"
CERT_NODE = "Certificate"
CA_NODE = "CertificateAuthority"

#: The live shape: two leaf certs, three CA rows — and two of those CA rows are
#: the SAME authority spelled with and without spaces after the RDN commas, a
#: real artifact of a normalisation change. The cert names one of them exactly.
_CA_SPACED = "CN = ACCVRAIZ1, OU = PKIACCV, O = ACCV, C = ES"
_CA_TIGHT = "CN=ACCVRAIZ1, OU=PKIACCV, O=ACCV, C=ES"
_CA_CONTABO = "CN = vmi-example-runner.contaboserver.net"

_CERTS = {
    "93057A8815C64FCE882FFA9116522878BC536417": _CA_TIGHT,
    "E1375F2F85EEE389A4667E2DD4C365E3036BB07C": _CA_CONTABO,
}


@pytest.fixture()
def session():
    engine = make_engine()
    with Session(engine) as s:
        yield s
        s.rollback()


def _specs():
    return {"pki": PKIAgent.spec}


def _ca(session, name, ca_type="root"):
    res = Resource(
        id=uuid.uuid4(),
        domain="pki",
        type="certificate_authority",
        name=name,
        source="PKIAgent",
        metadata_={"ca_type": ca_type, "issuer": name},
    )
    session.add(res)
    session.flush()
    return res


def _cert(session, thumbprint, issuer, subject=None):
    res = Resource(
        id=uuid.uuid4(),
        domain="security",
        type="certificate",
        name=thumbprint,
        source="PKIAgent",
        metadata_={
            "thumbprint": thumbprint,
            "subject": subject or issuer,
            "issuer": issuer,
        },
    )
    session.add(res)
    session.flush()
    return res


def _live_fixture(session):
    cas = {name: _ca(session, name) for name in (_CA_SPACED, _CA_TIGHT, _CA_CONTABO)}
    certs = {t: _cert(session, t, issuer) for t, issuer in _CERTS.items()}
    return cas, certs


# --- the two paths ----------------------------------------------------------


def _deleted_deriver_issued_by(session) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """``PKIAgent._build_issued_by_edges``' edge construction, copied verbatim.

    Reduced in exactly two ways, neither of which touches which edges exist:

      * ``cert_res = upsert_resource(...)`` becomes a lookup of the row that
        call get-or-creates — the resource write is NOT part of the migration
        (it still happens in the agent, see the module docstring) and creating
        it twice would only add noise.
      * the ``edges.append({...})`` payload is reduced to the compared pair.

    Do NOT "improve" it. In particular the exact-string ``ca_resource_ids.get(
    issuer)`` lookup is load-bearing: it is why the space-separated duplicate CA
    row attracts no edge, and the declaration reproduces that by using
    ``key_normalizer="exact"``.
    """
    pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()

    ca_resource_ids = {
        res.name: res.id
        for res in session.query(Resource)
        .filter(Resource.domain == "pki", Resource.type == "certificate_authority")
        .all()
    }
    cert_rows = (
        session.query(Resource)
        .filter(Resource.domain == "security", Resource.type == "certificate")
        .all()
    )
    for row in cert_rows:
        blob = row.metadata_ or {}
        thumb = (blob.get("thumbprint") or "").strip()
        issuer = (blob.get("issuer") or "").strip()
        if not thumb or not issuer:
            continue
        ca_id = ca_resource_ids.get(issuer)
        if ca_id is None:
            continue
        pairs.add((row.id, ca_id))
    return pairs


def _engine_issued_by(session) -> set[tuple[uuid.UUID, uuid.UUID]]:
    from infra_brain import graph_engine

    counts, errors = graph_engine.emit_all(session, specs=_specs())
    assert errors == [], errors
    pairs = set()
    for edge in session.execute(
        select(GraphEdge).where(GraphEdge.edge_type == ISSUED_BY, GraphEdge.valid_to.is_(None))
    ).scalars():
        src = session.get(GraphNode, edge.source_id)
        dst = session.get(GraphNode, edge.target_id)
        assert src.resource_id is not None and dst.resource_id is not None, (
            "both ends of ISSUED_BY are inventory items pki writes itself — a "
            "node without a resource_id means resource_backed was declared wrong"
        )
        pairs.add((src.resource_id, dst.resource_id))
    assert counts["edges"].get(ISSUED_BY, 0) == len(pairs)
    return pairs


# --- the declaration --------------------------------------------------------


def test_pki_declares_both_ends_of_the_edge_it_emits():
    """The ownership rule, satisfied trivially — which is why this one was easy."""
    spec = PKIAgent.spec
    declared = {n.type for n in spec.emits_nodes}
    edge = next(e for e in spec.emits_edges if e.type == ISSUED_BY)

    assert declared == {CERT_NODE, CA_NODE}
    assert edge.from_node in declared and edge.to_node in declared
    assert edge.written_as() == (CERT_NODE, CA_NODE), "stored certificate -> CA"
    assert edge.from_key_multi is False, "a certificate has exactly one issuer"


def test_the_certificate_node_reads_the_security_domain_not_pkis_own():
    """pki writes its cert rows into ``security``; the NodeSpec has to say so."""
    cert = next(n for n in PKIAgent.spec.emits_nodes if n.type == CERT_NODE)
    ca = next(n for n in PKIAgent.spec.emits_nodes if n.type == CA_NODE)

    assert (cert.domain, cert.resource_type) == ("security", "certificate")
    assert cert.natural_key == "name", "resources.name IS the thumbprint"
    assert "issuer" in cert.attributes, "the join key must ride onto the node"
    assert ca.domain is None, "the CA rows are in pki's own domain — no override"


def test_the_confidence_is_the_derivers_and_the_dn_is_not_folded():
    edge = next(e for e in PKIAgent.spec.emits_edges if e.type == ISSUED_BY)
    assert edge.method == "deterministic_match"
    assert float(edge.confidence) == 0.9
    assert edge.key_normalizer == "exact", (
        "a DN is a structured identifier; folding it would merge CAs that "
        "differ in O= or OU= and silently assert an identity nobody established"
    )


def test_containment_siblings_are_not_declared():
    """HAS_CRL / HAS_OCSP_RESPONDER stay facts, per §3.1.

    Their "node" is the responder URL restated as a resource — a property of
    the CA, not an entity that exists and is referred to independently of it.
    Pinned so a later pass does not declare them by symmetry.
    """
    types = {e.type for e in PKIAgent.spec.emits_edges}
    assert types == {ISSUED_BY}
    assert "HAS_CRL" not in types and "HAS_OCSP_RESPONDER" not in types


# --- equivalence ------------------------------------------------------------


def test_engine_reproduces_the_deleted_deriver_exactly(session):
    """THE gate: same fixture, both writers, identical pairs."""
    _live_fixture(session)

    deriver = _deleted_deriver_issued_by(session)
    engine = _engine_issued_by(session)

    assert deriver, "fixture produced no deriver edges — it is not exercising anything"
    assert engine == deriver


def test_equivalence_on_the_live_certificate_mix(session):
    """Counted, not trusted: 2 certs, 3 CA rows, 2 edges."""
    cas, certs = _live_fixture(session)

    engine = _engine_issued_by(session)

    assert engine == {(certs[thumb].id, cas[issuer].id) for thumb, issuer in _CERTS.items()}
    assert len(engine) == 2


def test_the_duplicate_ca_row_attracts_no_edge(session):
    """Both writers agree the whitespace-variant CA is a DIFFERENT key.

    The live store holds ACCVRAIZ1 twice, spelled with and without spaces after
    the RDN commas. Neither writer merges them, and this asserts the *engine*
    does not either — a normaliser that folded whitespace would move the edge
    onto whichever row sorted first, which is a coin flip dressed as a fact.
    """
    cas, _certs = _live_fixture(session)

    engine = _engine_issued_by(session)

    targets = {ca_id for _cert_id, ca_id in engine}
    assert cas[_CA_TIGHT].id in targets
    assert cas[_CA_SPACED].id not in targets


def test_a_cert_whose_issuer_has_no_ca_row_writes_nothing(session):
    """The engine never invents a target for a name it was merely told about."""
    _ca(session, _CA_CONTABO)
    _cert(session, "DEADBEEF", "CN = some CA nobody tracks")

    assert _engine_issued_by(session) == set()


def test_the_deriver_is_gone_but_the_resource_write_survives():
    """The half-migration guard: delete the edge, keep the node's source.

    ``ISSUED_BY``'s deriver also minted the ``security/certificate`` rows the
    declared node reads. Deleting both would have left the declaration pointing
    at rows nothing creates.
    """
    import inspect

    from infra_brain.db.relationships import MIGRATED_TO_GRAPH_EDGES, RelationshipType

    assert RelationshipType.ISSUED_BY in MIGRATED_TO_GRAPH_EDGES
    assert not hasattr(PKIAgent, "_build_issued_by_edges")

    body = inspect.getsource(PKIAgent._upsert_certificate_resources)
    assert "RelationshipType.ISSUED_BY" not in body
    assert "upsert_resource" in body and 'resource_type="certificate"' in body
