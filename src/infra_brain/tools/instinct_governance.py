"""Instinct governance: real promotion validation + approval/version history
+ a proper propose/promote split (P4.2c / P4.2d / P4.3b of the convergence
plan).

Replaces the ad-hoc, unvalidated write in ``mcp_server.promote_instinct``
(TRK-136) with ``promote_instinct_v2``, which enforces real input validation
AND writes a full audit trail: an ``Instinct`` row, a matching
``InstinctVersion`` snapshot (version=1), and an ``InstinctApproval`` row
(decision="approved") — all in one transaction.

These are PLAIN FUNCTIONS, not ``@mcp.tool``-decorated — MCP wiring (and the
final "resolve the real approver identity server-side" step) happens in a
later integration pass. Every function that writes data takes an
``approved_by`` / ``changed_by`` / ``proposed_by`` string parameter that MUST
be the identity the calling layer derives server-side from the authenticated
caller (e.g. the ``mcp:<key name>`` convention used elsewhere in this
codebase — see ``mcp_server._attributed_author``) — NEVER a raw, unverified
MCP tool argument passed straight through from the model. Nothing in this
module makes an external network call, so ``callbacks.write_gate
.gate_external_write`` does not apply here (it gates outbound httpx calls to
GitLab/Jira/Confluence; these functions only mutate infra-brain's own
Postgres tables).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from infra_brain.db.models import Instinct, InstinctApproval, InstinctProposal, InstinctVersion
from infra_brain.db.session import get_session


def _coerce_uuid(value: str | uuid.UUID, *, field: str = "instinct_id") -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field} must be a valid UUID; got {value!r}") from exc


def promote_instinct_v2(
    pattern: str,
    domain: str,
    citation: str,
    evidence: str,
    approved_by: str,
    zone: str = "corpor",
    confidence: float = 0.8,
) -> dict:
    """Promote a new pattern straight to an active, governed ``Instinct``.

    This is the real validation TRK-136 flagged as missing from the original
    ``mcp_server.promote_instinct``: unlike that function's ``{"error": ...}``
    return values, every guard here RAISES ``ValueError`` with a clear message
    — callers (the later MCP wrapper) are expected to translate that into
    whatever error shape their surface uses.

    Validates:
        - ``citation`` is non-empty (whitespace-only rejected).
        - ``evidence`` is non-empty (whitespace-only rejected).
        - ``0.0 <= confidence <= 1.0``.

    ``approved_by`` MUST be the server-derived caller identity, never a raw
    free-form argument — see the module docstring.

    On success, writes (all in one ``get_session()`` transaction):
        - an ``Instinct`` row: ``version=1``, ``status="active"``,
          ``source="promoted"``, ``proposed_by=approved_by``,
          ``proposed_at`` == ``promoted_at``.
        - an ``InstinctVersion`` row snapshotting version=1.
        - an ``InstinctApproval`` row with ``decision="approved"``.

    Returns a dict: ``{"promoted": <instinct id str>, "domain", "confidence",
    "version"}``.
    """
    if not citation or not citation.strip():
        raise ValueError("citation must be non-empty (whitespace-only is rejected)")
    if not evidence or not evidence.strip():
        raise ValueError("evidence must be non-empty (whitespace-only is rejected)")
    if not approved_by or not approved_by.strip():
        raise ValueError("approved_by must be non-empty (whitespace-only is rejected)")
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"confidence must satisfy 0.0 <= confidence <= 1.0; got {confidence!r}")

    with get_session() as session:
        # Explicitly stamp promoted_at and proposed_at with the SAME instant
        # (rather than relying on Instinct.promoted_at's column default and
        # leaving proposed_at NULL) so the docstring's documented invariant
        # -- proposed_at == promoted_at for everything written through this
        # promote-straight-to-active path -- actually holds.
        now = datetime.now(UTC)
        instinct = Instinct(
            zone=zone,
            domain=domain,
            pattern=pattern,
            confidence=confidence,
            promoted_by=approved_by,
            promoted_at=now,
            citation=citation,
            evidence=evidence,
            version=1,
            status="active",
            source="promoted",
            proposed_by=approved_by,
            proposed_at=now,
        )
        session.add(instinct)
        session.flush()  # assign instinct.id for the FK rows below

        session.add(
            InstinctVersion(
                instinct_id=instinct.id,
                version=1,
                pattern=pattern,
                confidence=confidence,
                changed_by=approved_by,
            )
        )
        session.add(
            InstinctApproval(
                instinct_id=instinct.id,
                approved_by=approved_by,
                decision="approved",
            )
        )
        session.commit()
        session.refresh(instinct)
        return {
            "promoted": str(instinct.id),
            "domain": domain,
            "confidence": confidence,
            "version": instinct.version,
        }


def rollback_instinct(instinct_id: str | uuid.UUID, approved_by: str, reason: str) -> dict:
    """Roll back a promoted ``Instinct`` — sets ``status="rolled_back"``.

    Writes an ``InstinctApproval`` row with ``decision="rejected"`` and
    ``notes=reason``. ``approved_by`` MUST be the server-derived caller
    identity — see the module docstring.

    Raises ``ValueError`` if ``instinct_id`` is not a valid UUID or does not
    exist.
    """
    iid = _coerce_uuid(instinct_id)
    with get_session() as session:
        instinct = session.get(Instinct, iid)
        if instinct is None:
            raise ValueError(f"Instinct {instinct_id} not found")
        instinct.status = "rolled_back"
        session.add(
            InstinctApproval(
                instinct_id=iid,
                approved_by=approved_by,
                decision="rejected",
                notes=reason,
            )
        )
        session.commit()
        return {"instinct_id": str(iid), "status": instinct.status}


def get_instinct_history(instinct_id: str | uuid.UUID) -> dict:
    """Return an ``Instinct`` plus its full version + approval history.

    Raises ``ValueError`` if ``instinct_id`` is not a valid UUID or does not
    exist. Both history lists are ordered oldest-first (by their respective
    timestamp columns).
    """
    iid = _coerce_uuid(instinct_id)
    with get_session() as session:
        instinct = session.get(Instinct, iid)
        if instinct is None:
            raise ValueError(f"Instinct {instinct_id} not found")

        versions = (
            session.query(InstinctVersion)
            .filter(InstinctVersion.instinct_id == iid)
            .order_by(InstinctVersion.changed_at.asc())
            .all()
        )
        approvals = (
            session.query(InstinctApproval)
            .filter(InstinctApproval.instinct_id == iid)
            .order_by(InstinctApproval.approved_at.asc())
            .all()
        )

        return {
            "instinct": {
                "id": str(instinct.id),
                "zone": instinct.zone,
                "domain": instinct.domain,
                "pattern": instinct.pattern,
                "confidence": instinct.confidence,
                "status": instinct.status,
                "version": instinct.version,
                "promoted_by": instinct.promoted_by,
                "promoted_at": instinct.promoted_at,
                "citation": instinct.citation,
            },
            "versions": [
                {
                    "id": str(v.id),
                    "version": v.version,
                    "pattern": v.pattern,
                    "confidence": v.confidence,
                    "changed_by": v.changed_by,
                    "changed_at": v.changed_at,
                }
                for v in versions
            ],
            "approvals": [
                {
                    "id": str(a.id),
                    "approved_by": a.approved_by,
                    "decision": a.decision,
                    "approved_at": a.approved_at,
                    "notes": a.notes,
                }
                for a in approvals
            ],
        }


def propose_instinct(
    zone: str,
    domain: str,
    pattern: str,
    confidence: float,
    proposed_by: str,
    evidence: str = "",
    citation: str = "",
) -> dict:
    """Write a pending ``InstinctProposal`` row. Does NOT touch ``instincts``.

    ``proposed_by`` MUST be the server-derived caller identity — see the
    module docstring.

    Validates (same contract as ``promote_instinct_v2`` and the other write
    functions in this layer — raises a clean ``ValueError`` instead of
    letting a bad value reach the DB as an uncaught ``IntegrityError``):
        - ``zone``, ``domain``, ``pattern`` are non-empty (whitespace-only
          rejected).
        - ``proposed_by`` is non-empty (whitespace-only rejected) —
          ``InstinctProposal.proposed_by`` is a NOT NULL column.
        - ``0.0 <= confidence <= 1.0``.

    ``evidence`` and ``citation`` remain optional, unlike
    ``promote_instinct_v2`` where they're required.

    FOLLOW-UP (out of scope for this task): there is no ``approve_proposal``
    equivalent for ``InstinctProposal`` yet — approving a proposal (creating
    the ``Instinct`` + ``InstinctVersion`` + ``InstinctApproval`` rows and
    flipping this row's ``status`` to "approved") is not implemented here and
    needs its own function in a later pass.
    """
    if not zone or not zone.strip():
        raise ValueError("zone must be non-empty (whitespace-only is rejected)")
    if not domain or not domain.strip():
        raise ValueError("domain must be non-empty (whitespace-only is rejected)")
    if not pattern or not pattern.strip():
        raise ValueError("pattern must be non-empty (whitespace-only is rejected)")
    if not proposed_by or not proposed_by.strip():
        raise ValueError("proposed_by must be non-empty (whitespace-only is rejected)")
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"confidence must satisfy 0.0 <= confidence <= 1.0; got {confidence!r}")

    with get_session() as session:
        proposal = InstinctProposal(
            zone=zone,
            domain=domain,
            pattern=pattern,
            confidence=confidence,
            evidence=evidence or None,
            citation=citation or None,
            proposed_by=proposed_by,
            status="pending",
        )
        session.add(proposal)
        session.commit()
        session.refresh(proposal)
        return {
            "proposed": str(proposal.id),
            "domain": domain,
            "status": proposal.status,
        }
