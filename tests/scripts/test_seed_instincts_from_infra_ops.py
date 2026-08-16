"""Hermetic tests for scripts/seed_instincts_from_infra_ops.py.

Fixtures below are written in the *real* infra-ops YAML ledger shape and
layout, confirmed by reading actual ledger files under
knowledge/instincts/{eol,common,drift-discovery,ansible-authoring}/... in a
sibling infra-ops checkout before writing this test — not assumed. The layout
is flat: knowledge/instincts/<domain>/*.yml, with no zone directory level.
That shape includes:

  - simple entries with just claim/confidence/citation/promoted_by (eol,
    drift-discovery, ansible-authoring, common domains), carrying no `zone`
    field at all — those inherit the zone the import runs under
  - richer promoted entries with evidence_summary, an `evidence` list of
    {source, excerpt} dicts, and free-form nested domain-specific maps
    (pci-review's separation_model / hsm_vendor_details / vlan_topology)
  - a below-threshold-confidence entry
  - an in-zone (HSA) entry that must never import into the corpor DB
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from infra_brain.db.models import Base, Instinct
from scripts.seed_instincts_from_infra_ops import import_instincts, load_ledger_entries

# ---------------------------------------------------------------------------
# Fixture ledger tree
# ---------------------------------------------------------------------------

SIMPLE_EOL_ENTRY = """\
id: eol-remediation-cadence
domain: eol
claim: "EOL hosts require a phased migration roadmap; eol-lifecycle-planner should produce a PCI-risk-scored remediation plan each quarter rather than re-flagging the same hosts."
confidence: 0.8
evidence_summary: >
  Three hosts in the eol_servers group carry CRITICAL severity in the
  security posture, yet no remediation roadmap exists in the knowledge base.
citation: "PCI DSS Req 6.3.3; knowledge/environment.md § Security Posture (EOL hosts)"
promoted_by: youruser
promoted_at: "2026-06-18T10:45:00Z"
source_proposal: knowledge/proposals/eol/eol-remediation-cadence.yml
"""

RICH_PCI_ENTRY = """\
id: cp-dss-separation-model
domain: pci-review
claim: >
  CP and DSS work are separated at the HSM layer via the physical appliance
  split, not by a distinct logical system.
confidence: 0.80
evidence_summary: >
  environment.md Octopus lifecycle table shows Lifecycles-102 as the HSM gate.
evidence:
  - source: "knowledge/environment.md"
    excerpt: "Lifecycles-102 | HSMs | Test -> Production"
separation_model:
  principle: "CP/DSS separation is a hardware boundary, not a logical one."
  layers:
    hsm_hardware:
      description: "Physical appliance split"
citation: >
  knowledge/environment.md lines 682-718; PCI DSS Requirements 3.3, 3.6, 9.6
promoted_by: youruser
promoted_at: "2026-06-18T10:45:00Z"
source_proposal: knowledge/proposals/pci-review/cp-dss-separation-model.yml
"""

LOW_CONFIDENCE_ENTRY = """\
id: low-confidence-note
domain: common
claim: "Speculative pattern that has not been confirmed."
confidence: 0.4
citation: "unverified"
promoted_by: youruser
promoted_at: "2026-06-18T10:45:00Z"
"""

HSA_ENTRY = """\
id: winrm-https-hsa-prereq
status: promoted
zone: in-zone
domain: windows-patching
claim: WinRM HTTPS migration is a hard prerequisite for any HSA deployment.
confidence: 0.85
citation: "PCI DSS Req 4.1/4.2"
promoted_by: youruser
promoted_at: "2026-06-21T23:44:17Z"
"""

MISFILED_ZONE_ENTRY = """\
id: misfiled-hsa-entry
zone: in-zone
domain: eol
claim: "This file declares zone in-zone in a corpor import — must be rejected."
confidence: 0.9
citation: "n/a"
promoted_by: youruser
promoted_at: "2026-06-18T10:45:00Z"
"""

NOT_A_MAPPING = "- just\n- a\n- list\n"

UNPARSEABLE_YAML = "claim: [unclosed\n"


@pytest.fixture
def ledger_root(tmp_path: Path) -> Path:
    """Build the flat knowledge/instincts/<domain>/*.yml tree matching the real ledger."""
    root = tmp_path / "knowledge" / "instincts"

    eol = root / "eol"
    eol.mkdir(parents=True)
    (eol / "eol-remediation-cadence.yml").write_text(SIMPLE_EOL_ENTRY, encoding="utf-8")

    pci = root / "pci-review"
    pci.mkdir(parents=True)
    (pci / "cp-dss-separation-model.yml").write_text(RICH_PCI_ENTRY, encoding="utf-8")

    common = root / "common"
    common.mkdir(parents=True)
    (common / "low-confidence-note.yml").write_text(LOW_CONFIDENCE_ENTRY, encoding="utf-8")
    # Misfiled: sits in the flat ledger but declares zone: in-zone.
    (common / "misfiled-hsa-entry.yml").write_text(MISFILED_ZONE_ENTRY, encoding="utf-8")
    (common / "not-a-mapping.yml").write_text(NOT_A_MAPPING, encoding="utf-8")
    (common / "unparseable.yml").write_text(UNPARSEABLE_YAML, encoding="utf-8")

    windows = root / "windows-patching"
    windows.mkdir(parents=True)
    (windows / "winrm-https-hsa-prereq.yml").write_text(HSA_ENTRY, encoding="utf-8")

    return root


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        yield session
        session.rollback()


# ---------------------------------------------------------------------------
# load_ledger_entries
# ---------------------------------------------------------------------------


def test_loads_simple_and_rich_entries(ledger_root):
    entries = load_ledger_entries(ledger_root, zone="corpor")
    by_id = {e["source_id"]: e for e in entries}
    assert "eol-remediation-cadence" in by_id
    assert "cp-dss-separation-model" in by_id
    assert by_id["eol-remediation-cadence"]["domain"] == "eol"
    assert by_id["eol-remediation-cadence"]["confidence"] == 0.8
    assert "PCI DSS Req 6.3.3" in by_id["eol-remediation-cadence"]["citation"]


def test_rich_entry_extracts_claim_as_pattern_ignoring_nested_maps(ledger_root):
    entries = load_ledger_entries(ledger_root, zone="corpor")
    rich = next(e for e in entries if e["source_id"] == "cp-dss-separation-model")
    assert "CP and DSS work are separated" in rich["pattern"]
    assert rich["confidence"] == 0.80
    # Nested domain-specific maps (separation_model/evidence) are not part of
    # the returned dict at all — only the seven Instinct-shaped fields are.
    assert set(rich.keys()) == {
        "source_path",
        "source_id",
        "zone",
        "domain",
        "pattern",
        "confidence",
        "promoted_by",
        "promoted_at",
        "citation",
    }


def test_misfiled_zone_entry_is_rejected(ledger_root):
    entries = load_ledger_entries(ledger_root, zone="corpor")
    ids = {e["source_id"] for e in entries}
    assert "misfiled-hsa-entry" not in ids


def test_in_zone_entries_never_load_under_corpor_zone(ledger_root):
    entries = load_ledger_entries(ledger_root, zone="corpor")
    ids = {e["source_id"] for e in entries}
    assert "winrm-https-hsa-prereq" not in ids


def test_declared_in_zone_entries_load_under_an_in_zone_run(ledger_root):
    entries = load_ledger_entries(ledger_root, zone="in-zone")
    ids = {e["source_id"] for e in entries}
    assert "winrm-https-hsa-prereq" in ids
    assert "misfiled-hsa-entry" in ids


def test_zoneless_files_inherit_the_target_zone(ledger_root):
    """The real flat ledger carries no `zone` field, so the zone an entry
    lands under is the one the import runs with."""
    entries = load_ledger_entries(ledger_root, zone="corpor")
    eol = next(e for e in entries if e["source_id"] == "eol-remediation-cadence")
    assert eol["zone"] == "corpor"


def test_non_mapping_and_unparseable_files_are_skipped_not_fatal(ledger_root):
    # Must not raise despite a list-shaped file and an invalid-YAML file
    # sitting in the same directory as valid entries.
    entries = load_ledger_entries(ledger_root, zone="corpor")
    ids = {e["source_id"] for e in entries}
    assert "eol-remediation-cadence" in ids  # good files still load
    assert "not-a-mapping" not in ids
    assert "unparseable" not in ids


def test_missing_ledger_root_returns_empty(tmp_path):
    entries = load_ledger_entries(tmp_path / "knowledge" / "instincts", zone="corpor")
    assert entries == []


# ---------------------------------------------------------------------------
# import_instincts (ORM write path)
# ---------------------------------------------------------------------------


def test_import_inserts_via_instinct_model(db, ledger_root):
    result = import_instincts(db, ledger_root, zone="corpor", min_confidence=0.7)
    assert len(result["inserted"]) == 2  # eol + pci-review; low-confidence excluded

    rows = db.query(Instinct).all()
    assert len(rows) == 2
    domains = {r.domain for r in rows}
    assert domains == {"eol", "pci-review"}
    for r in rows:
        assert r.zone == "corpor"
        assert isinstance(r.id, uuid.UUID)


def test_import_respects_min_confidence(db, ledger_root):
    result = import_instincts(db, ledger_root, zone="corpor", min_confidence=0.7)
    low_conf_ids = {e["source_id"] for e in result["skipped_low_confidence"]}
    assert "low-confidence-note" in low_conf_ids
    assert db.query(Instinct).filter(Instinct.domain == "common").count() == 0


def test_import_never_writes_in_zone_rows_into_corpor_run(db, ledger_root):
    import_instincts(db, ledger_root, zone="corpor", min_confidence=0.7)
    assert db.query(Instinct).filter(Instinct.zone == "in-zone").count() == 0


def test_import_is_idempotent_on_rerun(db, ledger_root):
    """The bug this test guards against: Instinct.id is a fresh UUID on every
    row, so a naive 'INSERT ... ON CONFLICT DO NOTHING' against the primary
    key never conflicts and duplicates every row on a second run. Content
    dedup on (zone, domain, pattern) must make a second run a no-op."""
    first = import_instincts(db, ledger_root, zone="corpor", min_confidence=0.7)
    assert len(first["inserted"]) == 2

    second = import_instincts(db, ledger_root, zone="corpor", min_confidence=0.7)
    assert len(second["inserted"]) == 0
    assert len(second["skipped_duplicate"]) == 2

    # Still exactly 2 rows in the DB, not 4.
    assert db.query(Instinct).count() == 2


def test_dry_run_writes_nothing(db, ledger_root):
    result = import_instincts(db, ledger_root, zone="corpor", min_confidence=0.7, dry_run=True)
    assert len(result["inserted"]) == 2
    assert db.query(Instinct).count() == 0


def test_import_preserves_citation_and_promoted_by(db, ledger_root):
    import_instincts(db, ledger_root, zone="corpor", min_confidence=0.7)
    eol_row = db.query(Instinct).filter(Instinct.domain == "eol").one()
    assert eol_row.promoted_by == "youruser"
    assert "PCI DSS Req 6.3.3" in eol_row.citation


def test_import_against_missing_ledger_root_is_a_noop(db, tmp_path):
    result = import_instincts(
        db, tmp_path / "knowledge" / "instincts", zone="corpor", min_confidence=0.7
    )
    assert result == {"inserted": [], "skipped_duplicate": [], "skipped_low_confidence": []}
    assert db.query(Instinct).count() == 0
