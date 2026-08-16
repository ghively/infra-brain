"""Shared approve/reject logic for human-gated ``ProposedAction`` rows.

Extracted (mechanically — no behavior change) from
``api/routers/governance_ops.py``'s ``approve_remediation_action`` /
``reject_remediation_action`` so the dashboard route and the MCP tools
(``mcp_server.approve_proposal`` / ``reject_proposal``) share ONE
implementation and can never drift apart again.

What lives here: the guards (entity-resolution review rows, pending-status,
confidence floor), the field writes (``status`` / ``approved_by`` /
``approved_at``), the commit, and the detached snapshot the caller needs to
resume a parked remediation-interrupt graph.

What does NOT live here: transport concerns. Callers translate
:class:`ActionDecisionError` into their own error shape (HTTPException for
FastAPI, an ``{"error": ...}`` dict for MCP), and callers own the graph
resume — the DB flip has already committed by the time this returns, so a
resume failure only defers execution to the ``_execute_approved()`` poll.

Nothing here contacts an external system: these functions only mutate
infra-brain's own ``proposed_actions`` rows. The actual (sanctioned) external
write, if any, still happens later in the RemediationAgent execution path,
behind ``INFRA_BRAIN_MR_ENABLED`` and the write gate — unchanged.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from infra_brain.db.models import ProposedAction
from infra_brain.graph_phase3 import REVIEW_ACTION_TYPE

# Kept verbatim from the route so the dashboard's 409 body is byte-identical.
REVIEW_ACTION_APPROVE_DETAIL = (
    "entity-resolution review rows cannot be approved here -- use "
    "POST /api/graph/entity-resolution/{action_id}/confirm instead"
)

# The exact mirror of the approve detail above. KG-3: rejecting one of these
# rows is NOT "just a status flip" -- it must write an attributed, pair-scoped
# NOT_SAME_AS veto that every automatic identity emitter consults, and a bare
# flip here would silently produce the unattributed, pair-less rejection that
# let a later pass auto-merge the very pair a human had rejected.
REVIEW_ACTION_REJECT_DETAIL = (
    "entity-resolution review rows cannot be rejected here -- use "
    "POST /api/graph/entity-resolution/{action_id}/reject instead"
)

MIN_APPROVE_CONFIDENCE = 0.7


class ActionDecisionError(Exception):
    """A guard rejected the decision. ``status_code`` mirrors the HTTP status
    the dashboard route has always returned (404 / 409 / 422) so the route can
    re-raise it as an HTTPException without changing any response."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _snapshot(action: ProposedAction) -> SimpleNamespace:
    """Detached copy of the three fields ``resume_remediation_action`` needs —
    safe to use after the session closes."""
    return SimpleNamespace(
        id=action.id,
        thread_id=action.thread_id,
        graph_version=action.graph_version,
    )


def approve_action(
    session, action_id: uuid.UUID, approved_by: str, *, commit: bool = True
) -> SimpleNamespace:
    """Approve one pending ProposedAction inside *session*; return its snapshot.

    Raises :class:`ActionDecisionError` (writing nothing) when the action is
    missing, is an entity-resolution review row, is not ``pending``, or has
    ``confidence < 0.7``. Approval only flips status/approver fields — it never
    mutates managed infrastructure.
    """
    action = session.get(ProposedAction, action_id)
    if not action:
        raise ActionDecisionError(404, "Action not found")
    if action.action_type == REVIEW_ACTION_TYPE:
        # This generic approve only flips status -- no agent ever re-checks
        # entity_resolution_same_as rows (RemediationAgent's executor filters
        # to config_fix/vuln_patch only), so approving here would permanently
        # close the identity question with no SAME_AS edge ever written and no
        # way to recover. The real confirm/execute path is
        # POST /api/graph/entity-resolution/{action_id}/confirm.
        raise ActionDecisionError(409, REVIEW_ACTION_APPROVE_DETAIL)
    if action.status != "pending":
        raise ActionDecisionError(409, f"Action is {action.status}, not pending")
    if action.confidence < MIN_APPROVE_CONFIDENCE:
        raise ActionDecisionError(422, "Confidence < 0.7 — cannot approve")
    action.status = "approved"
    action.approved_by = approved_by
    action.approved_at = datetime.now(timezone.utc)
    snapshot = _snapshot(action)
    if commit:
        session.commit()
    return snapshot


def reject_action(session, action_id: uuid.UUID, *, commit: bool = True) -> SimpleNamespace:
    """Reject one pending ProposedAction inside *session*; return its snapshot.

    Raises :class:`ActionDecisionError` (writing nothing) when the action is
    missing, is an entity-resolution review row, or is not ``pending``. Never
    mutates managed infrastructure.

    KG-3 supersedes the previous comment here (which said rejecting a review
    row "is just a status flip with no edge-writing side effect", and so
    deliberately reused this generic path). That was the bug: a bare flip is
    unattributed and bundle-scoped, it silenced the source node forever, and
    it wrote nothing any automatic emitter could see — so the next
    ``resolve_entities`` pass would happily auto-merge the exact pair a human
    had rejected. Rejecting a review row now writes an attributed, pair-scoped
    ``NOT_SAME_AS`` veto, so it needs its own route exactly as approval does.
    This 409 is the precise mirror of :func:`approve_action`'s.

    bulk_reject_proposals' query-layer exclusion of REVIEW_ACTION_TYPE
    (mcp_server.py) is about scope of the *bulk* tool and stays as it is.
    """
    action = session.get(ProposedAction, action_id)
    if not action:
        raise ActionDecisionError(404, "Action not found")
    if action.action_type == REVIEW_ACTION_TYPE:
        raise ActionDecisionError(409, REVIEW_ACTION_REJECT_DETAIL)
    if action.status != "pending":
        raise ActionDecisionError(409, f"Action is {action.status}, not pending")
    action.status = "rejected"
    snapshot = _snapshot(action)
    if commit:
        session.commit()
    return snapshot
