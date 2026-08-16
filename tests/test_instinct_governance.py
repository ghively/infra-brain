"""Tests for tools/instinct_governance.py (P4.2c/P4.2d/P4.3b).

Covers promote_instinct_v2 (success + the three validation failures),
rollback_instinct (success + not-found), get_instinct_history, and
propose_instinct.
"""

import contextlib
import uuid

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import Instinct, InstinctApproval, InstinctProposal, InstinctVersion
from infra_brain.tools import instinct_governance as ig

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_session(engine, monkeypatch):
    """Patch tools.instinct_governance.get_session to use the test engine."""

    @contextlib.contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    monkeypatch.setattr(ig, "get_session", _get_session)
    return engine


# ── promote_instinct_v2 ──────────────────────────────────────────────────


def test_promote_instinct_v2_success(patched_session):
    result = ig.promote_instinct_v2(
        pattern="always tag prod resources with owner",
        domain="ownership",
        citation="docs/policy/ownership.md#L12",
        evidence="observed in 40/40 sampled resources",
        approved_by="mcp:test-key",
    )

    assert result["domain"] == "ownership"
    assert result["confidence"] == 0.8
    assert result["version"] == 1
    instinct_id = uuid.UUID(result["promoted"])

    with Session(patched_session) as s:
        instinct = s.get(Instinct, instinct_id)
        assert instinct is not None
        assert instinct.status == "active"
        assert instinct.source == "promoted"
        assert instinct.proposed_by == "mcp:test-key"
        assert instinct.promoted_by == "mcp:test-key"
        assert instinct.version == 1
        assert instinct.evidence == "observed in 40/40 sampled resources"
        # Docstring-documented invariant: proposed_at == promoted_at for
        # everything written through this promote-straight-to-active path.
        assert instinct.proposed_at is not None
        assert instinct.proposed_at == instinct.promoted_at

        versions = s.query(InstinctVersion).filter(InstinctVersion.instinct_id == instinct_id).all()
        assert len(versions) == 1
        assert versions[0].version == 1
        assert versions[0].changed_by == "mcp:test-key"

        approvals = (
            s.query(InstinctApproval).filter(InstinctApproval.instinct_id == instinct_id).all()
        )
        assert len(approvals) == 1
        assert approvals[0].decision == "approved"
        assert approvals[0].approved_by == "mcp:test-key"


def test_promote_instinct_v2_missing_citation_raises(patched_session):
    with pytest.raises(ValueError, match="citation"):
        ig.promote_instinct_v2(
            pattern="p",
            domain="d",
            citation="   ",
            evidence="some evidence",
            approved_by="mcp:test-key",
        )
    # Nothing partially written.
    with Session(patched_session) as s:
        assert s.query(Instinct).count() == 0


def test_promote_instinct_v2_missing_evidence_raises(patched_session):
    with pytest.raises(ValueError, match="evidence"):
        ig.promote_instinct_v2(
            pattern="p",
            domain="d",
            citation="docs/x.md",
            evidence="",
            approved_by="mcp:test-key",
        )
    with Session(patched_session) as s:
        assert s.query(Instinct).count() == 0


def test_promote_instinct_v2_out_of_range_confidence_raises(patched_session):
    with pytest.raises(ValueError, match="confidence"):
        ig.promote_instinct_v2(
            pattern="p",
            domain="d",
            citation="docs/x.md",
            evidence="ev",
            approved_by="mcp:test-key",
            confidence=1.5,
        )
    with pytest.raises(ValueError, match="confidence"):
        ig.promote_instinct_v2(
            pattern="p",
            domain="d",
            citation="docs/x.md",
            evidence="ev",
            approved_by="mcp:test-key",
            confidence=-0.1,
        )
    with Session(patched_session) as s:
        assert s.query(Instinct).count() == 0


# ── rollback_instinct ─────────────────────────────────────────────────────


def test_rollback_instinct_success(patched_session):
    promoted = ig.promote_instinct_v2(
        pattern="p",
        domain="d",
        citation="docs/x.md",
        evidence="ev",
        approved_by="mcp:promoter",
    )
    instinct_id = promoted["promoted"]

    result = ig.rollback_instinct(instinct_id, approved_by="mcp:reviewer", reason="false positive")
    assert result["status"] == "rolled_back"

    with Session(patched_session) as s:
        instinct = s.get(Instinct, uuid.UUID(instinct_id))
        assert instinct.status == "rolled_back"
        rejections = (
            s.query(InstinctApproval)
            .filter(
                InstinctApproval.instinct_id == uuid.UUID(instinct_id),
                InstinctApproval.decision == "rejected",
            )
            .all()
        )
        assert len(rejections) == 1
        assert rejections[0].notes == "false positive"
        assert rejections[0].approved_by == "mcp:reviewer"


def test_rollback_instinct_not_found_raises(patched_session):
    with pytest.raises(ValueError, match="not found"):
        ig.rollback_instinct(str(uuid.uuid4()), approved_by="mcp:reviewer", reason="nope")


def test_rollback_instinct_bad_uuid_raises(patched_session):
    with pytest.raises(ValueError, match="UUID"):
        ig.rollback_instinct("not-a-uuid", approved_by="mcp:reviewer", reason="nope")


# ── get_instinct_history ──────────────────────────────────────────────────


def test_get_instinct_history(patched_session):
    promoted = ig.promote_instinct_v2(
        pattern="p",
        domain="d",
        citation="docs/x.md",
        evidence="ev",
        approved_by="mcp:promoter",
    )
    instinct_id = promoted["promoted"]
    ig.rollback_instinct(instinct_id, approved_by="mcp:reviewer", reason="stale")

    history = ig.get_instinct_history(instinct_id)

    assert history["instinct"]["id"] == instinct_id
    assert history["instinct"]["status"] == "rolled_back"
    assert len(history["versions"]) == 1
    assert history["versions"][0]["version"] == 1
    assert len(history["approvals"]) == 2
    assert history["approvals"][0]["decision"] == "approved"
    assert history["approvals"][1]["decision"] == "rejected"
    assert history["approvals"][1]["notes"] == "stale"


def test_get_instinct_history_not_found_raises(patched_session):
    with pytest.raises(ValueError, match="not found"):
        ig.get_instinct_history(str(uuid.uuid4()))


# ── propose_instinct ──────────────────────────────────────────────────────


def test_propose_instinct_success(patched_session):
    result = ig.propose_instinct(
        zone="corpor",
        domain="patching",
        pattern="always patch within 30 days of CVE disclosure",
        confidence=0.6,
        proposed_by="mcp:proposer",
        evidence="seen in 5 rootcause notes",
        citation="docs/policy/patching.md",
    )

    assert result["domain"] == "patching"
    assert result["status"] == "pending"
    proposal_id = uuid.UUID(result["proposed"])

    with Session(patched_session) as s:
        proposal = s.get(InstinctProposal, proposal_id)
        assert proposal is not None
        assert proposal.status == "pending"
        assert proposal.proposed_by == "mcp:proposer"
        assert proposal.confidence == 0.6
        # propose_instinct must never write to the instincts table.
        assert s.query(Instinct).count() == 0


def test_propose_instinct_empty_evidence_and_citation_allowed(patched_session):
    """evidence/citation are optional for a proposal (unlike promote_instinct_v2)."""
    result = ig.propose_instinct(
        zone="corpor",
        domain="patching",
        pattern="p",
        confidence=0.5,
        proposed_by="mcp:proposer",
    )
    assert result["status"] == "pending"
    with Session(patched_session) as s:
        proposal = s.get(InstinctProposal, uuid.UUID(result["proposed"]))
        assert proposal.evidence is None
        assert proposal.citation is None


def test_propose_instinct_missing_proposed_by_raises(patched_session):
    """proposed_by is a NOT NULL column -- must be a clean ValueError, not an
    uncaught IntegrityError from session.commit()."""
    with pytest.raises(ValueError, match="proposed_by"):
        ig.propose_instinct(
            zone="corpor",
            domain="patching",
            pattern="p",
            confidence=0.5,
            proposed_by="   ",
        )
    with Session(patched_session) as s:
        assert s.query(InstinctProposal).count() == 0


def test_propose_instinct_blank_pattern_domain_zone_raises(patched_session):
    with pytest.raises(ValueError, match="zone"):
        ig.propose_instinct(
            zone="",
            domain="patching",
            pattern="p",
            confidence=0.5,
            proposed_by="mcp:proposer",
        )
    with pytest.raises(ValueError, match="domain"):
        ig.propose_instinct(
            zone="corpor",
            domain=" ",
            pattern="p",
            confidence=0.5,
            proposed_by="mcp:proposer",
        )
    with pytest.raises(ValueError, match="pattern"):
        ig.propose_instinct(
            zone="corpor",
            domain="patching",
            pattern="",
            confidence=0.5,
            proposed_by="mcp:proposer",
        )
    with Session(patched_session) as s:
        assert s.query(InstinctProposal).count() == 0


def test_propose_instinct_out_of_range_confidence_raises(patched_session):
    with pytest.raises(ValueError, match="confidence"):
        ig.propose_instinct(
            zone="corpor",
            domain="patching",
            pattern="p",
            confidence=5.0,
            proposed_by="mcp:proposer",
        )
    with pytest.raises(ValueError, match="confidence"):
        ig.propose_instinct(
            zone="corpor",
            domain="patching",
            pattern="p",
            confidence=-1.0,
            proposed_by="mcp:proposer",
        )
    with Session(patched_session) as s:
        assert s.query(InstinctProposal).count() == 0
