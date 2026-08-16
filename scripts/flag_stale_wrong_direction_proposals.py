"""Follow-up cleanup for GitLab #142 / TRK-263.

Commit ``4e278aa`` added ``RemediationAgent._is_first_observation()`` /
``_is_never_actionable_drift()`` so NEW remediation proposals are never drafted for
(a) a field going from never-collected to its first observed value (there is
nothing to "reconcile back" to — approving such a proposal would erase
correct, freshly-collected data) or (b) collector/operational telemetry
fields that are not configuration drift in the remediation sense.

That fix does nothing for the ~184/200 proposals that were already drafted
and persisted (``status="pending"``) BEFORE it landed. Each still sits in the
operator approval queue recommending exactly the wrong-direction reconcile —
a live hazard, not just noise.

This script identifies existing ``proposed_actions`` rows that WOULD have
been suppressed had the fix existed when they were drafted, by re-applying
the same two predicates to the payload each row was drafted with (see
``RemediationAgent._draft_proposals`` — ``payload["old"]``/``payload["field"]``
are copied verbatim from the ``DriftEvent`` at draft time, so no join back to
``drift_events`` is needed; the row is self-describing).

READ FIRST: run with --dry-run (default) to see what would change; pass
--apply to actually write. Prints a per-row report either way.

Nothing is ever deleted. --apply flips matched rows to
``status="rejected"`` (the existing reject vocabulary already used by
``action_decisions.reject_action`` / the dashboard's reject route / the MCP
``reject_proposal`` tool — a rejected proposal is simply never executed, and
the dashboard's pending-approval queue naturally stops surfacing it) and
stamps a reason onto the row's own JSONB ``payload`` (an
``_stale_wrong_direction`` key holding the reason and a timestamp) so the
historical record is preserved, not destroyed, and it's obvious on inspection
why the row was rejected.

Only rows already in ``status="pending"`` are touched — approved/executed/
already-rejected rows are left alone.

Usage:  python scripts/flag_stale_wrong_direction_proposals.py [--apply]
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from infra_brain.agents.remediation import _is_first_observation, _is_never_actionable_drift
from infra_brain.db.models import ProposedAction
from infra_brain.db.session import get_session

REASON_FIRST_OBSERVATION = "first_observation"
REASON_TELEMETRY_DRIFT = "telemetry_drift"


def _match_reason(payload: dict) -> str | None:
    """Return why *payload* matches the stale-wrong-direction pattern, or
    None if it doesn't match at all.

    Mirrors the two ``_draft_proposals`` suppression checks (GitLab #142),
    applied retroactively to a persisted proposal's own payload rather than
    to the live ``DriftEvent`` it was drafted from.
    """
    field = payload.get("field")
    if _is_first_observation(payload.get("old")):
        return REASON_FIRST_OBSERVATION
    if isinstance(field, str) and _is_never_actionable_drift(field):
        return REASON_TELEMETRY_DRIFT
    return None


def _report_row(action: ProposedAction, reason: str) -> None:
    payload = action.payload or {}
    host = payload.get("host", "?")
    field = payload.get("field", "?")
    old = payload.get("old")
    new = payload.get("new")
    print(f"  {action.id} [{reason}] {host}: {field} {old!r} -> {new!r} (target={action.target})")


def main() -> int:
    apply = "--apply" in sys.argv
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[flag-stale-wrong-direction] mode={mode}")

    flagged = 0
    by_reason: dict[str, int] = {REASON_FIRST_OBSERVATION: 0, REASON_TELEMETRY_DRIFT: 0}

    with get_session() as session:
        candidates = (
            session.query(ProposedAction)
            .filter(
                ProposedAction.action_type == "config_fix",
                ProposedAction.status == "pending",
                ProposedAction.target.like("drift:%"),
            )
            .order_by(ProposedAction.created_at.asc())
        )

        for action in candidates:
            payload = action.payload or {}
            reason = _match_reason(payload)
            if reason is None:
                continue
            _report_row(action, reason)
            flagged += 1
            by_reason[reason] += 1

            if apply:
                new_payload = dict(payload)
                new_payload["_stale_wrong_direction"] = {
                    "reason": reason,
                    "note": (
                        "Flagged by scripts/flag_stale_wrong_direction_proposals.py "
                        "(GitLab #142 / TRK-263 follow-up): this proposal was drafted "
                        "before the wrong-direction-drafting fix (commit 4e278aa) "
                        "landed and recommends a reconciliation that would erase "
                        "correct, freshly-collected data (or is non-remediable "
                        "telemetry). Do not approve."
                    ),
                    "flagged_at": datetime.now(UTC).isoformat(),
                }
                action.payload = new_payload
                action.status = "rejected"

        print(f"[flag-stale-wrong-direction] matched rows: {flagged}")
        for reason, count in by_reason.items():
            print(f"  {reason}: {count}")

        if apply:
            session.commit()
            print(f"[flag-stale-wrong-direction] committed — {flagged} row(s) rejected")
        else:
            session.rollback()
            print(
                f"[flag-stale-wrong-direction] dry-run — would reject {flagged} row(s) (rolled back)"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
