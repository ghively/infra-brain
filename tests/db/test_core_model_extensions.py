"""Model-shape tests for P4.2b (instincts extension) and P4.2h (documents
simplification), per the convergence plan.

P4.2b adds governed-instinct columns to the live ``instincts`` table
(evidence, evidence_summary, version, status, source, proposed_by,
proposed_at). P4.2h adds client-origin provenance columns to ``documents``
(client_origin, ingested_by) in place of the dropped four-class sensitivity
taxonomy (D7) -- ``sensitivity`` itself is kept (vestigial, not removed) to
avoid a breaking schema change.

Every new column on both tables must be nullable or server-defaulted -- both
tables carry real rows in the live deployment (200+ in ``instincts``) -- so
these tests also assert a bare ``Instinct()``/``Document()`` with only the
pre-existing required fields still constructs and flushes cleanly.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from infra_brain.db.models import Document, Instinct

# --- column-surface introspection (catches an accidental rename) -----------


def _cols(model):
    return {c.name: c for c in sa_inspect(model).columns}


def test_instinct_column_surface():
    cols = _cols(Instinct)
    assert Instinct.__tablename__ == "instincts"
    for name in (
        "evidence",
        "evidence_summary",
        "version",
        "status",
        "source",
        "proposed_by",
        "proposed_at",
    ):
        assert name in cols, f"expected new Instinct column {name!r} to exist"
    # all new columns must be nullable OR carry a server_default -- additive
    # only, 200+ existing rows must be unaffected.
    for name in ("evidence", "evidence_summary", "source", "proposed_by", "proposed_at"):
        assert cols[name].nullable, f"{name} must be nullable (no server_default given)"
    assert cols["version"].nullable or cols["version"].server_default is not None
    assert cols["status"].nullable or cols["status"].server_default is not None
    assert cols["version"].server_default is not None
    assert cols["status"].server_default is not None
    # pre-existing columns untouched
    for name in ("zone", "domain", "pattern", "confidence", "promoted_by", "promoted_at", "citation"):
        assert name in cols


def test_document_column_surface():
    cols = _cols(Document)
    assert Document.__tablename__ == "documents"
    assert "client_origin" in cols
    assert "ingested_by" in cols
    assert cols["client_origin"].nullable
    assert cols["ingested_by"].nullable
    # sensitivity kept (vestigial per D7), not removed
    assert "sensitivity" in cols
    # pre-existing provenance columns untouched
    for name in ("title", "source", "indexed_at", "external_id", "space", "url",
                 "source_version", "content_hash", "status"):
        assert name in cols


# --- (a) Instinct: constructs without new fields, sensible defaults --------


def test_instinct_constructs_without_new_fields_and_gets_defaults(db):
    inst = Instinct(
        domain="linux",
        pattern="observed pattern text",
        confidence=0.8,
        promoted_by="test_suite",
    )
    db.add(inst)
    db.flush()
    got = db.get(Instinct, inst.id)
    assert got is not None
    # python-side defaults applied at flush time
    assert got.version == 1
    assert got.status == "active"
    # new nullable columns default to None when not supplied
    assert got.evidence is None
    assert got.evidence_summary is None
    assert got.source is None
    assert got.proposed_by is None
    assert got.proposed_at is None
    # pre-existing zone default untouched
    assert got.zone == "corpor"


def test_instinct_roundtrips_new_fields_when_provided(db):
    now = datetime.now(timezone.utc)
    inst = Instinct(
        domain="vsphere",
        pattern="capacity headroom pattern",
        confidence=0.9,
        promoted_by="capacity_forecast",
        evidence="observed 12 consecutive weeks of >85% utilization",
        evidence_summary="12wk sustained >85% util",
        version=2,
        status="superseded",
        source="capacity_forecast",
        proposed_by="capacity_forecast",
        proposed_at=now,
    )
    db.add(inst)
    db.flush()
    got = db.get(Instinct, inst.id)
    assert got.evidence.startswith("observed 12")
    assert got.evidence_summary == "12wk sustained >85% util"
    assert got.version == 2
    assert got.status == "superseded"
    assert got.source == "capacity_forecast"
    assert got.proposed_by == "capacity_forecast"
    assert got.proposed_at == now


# --- (b) Document: constructs with the two new fields ----------------------


def test_document_constructs_with_new_fields(db):
    doc = Document(
        title="Homelab Ansible README",
        source="gitlab:homelab-ansible",
        client_origin="infra-ops",
        ingested_by="manual_mcp",
    )
    db.add(doc)
    db.flush()
    got = db.get(Document, doc.id)
    assert got is not None
    assert got.client_origin == "infra-ops"
    assert got.ingested_by == "manual_mcp"
    # sensitivity still defaults as before (vestigial, not removed)
    assert got.sensitivity == "internal"


def test_document_constructs_without_new_fields_defaults_to_none(db):
    doc = Document(title="Untouched legacy row", source="manual")
    db.add(doc)
    db.flush()
    got = db.get(Document, doc.id)
    assert got.client_origin is None
    assert got.ingested_by is None


# --- (c) failure / validation case: required legacy columns still enforced -


def test_instinct_missing_required_field_raises_on_flush(db):
    # domain/pattern/confidence/promoted_by remain required (non-nullable,
    # no default) -- the additive P4.2b columns must not have loosened that.
    bad = Instinct(pattern="no domain given", confidence=0.5, promoted_by="test_suite")
    db.add(bad)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_document_missing_required_field_raises_on_flush(db):
    # title is still required; omitting it must still fail even though
    # documents now has two new nullable columns.
    bad = Document(source="manual")
    db.add(bad)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
