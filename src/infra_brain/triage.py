"""Shared vulnerability prioritization (used by VulnTriageAgent and the dashboard).

Deterministic so the agent and the UI always agree on a CVE's priority.
"""

from __future__ import annotations

from datetime import datetime, timezone

from infra_brain.db.severity import CRITICAL, HIGH, LOW, MEDIUM, normalize_severity

# Priority weights keyed by canonical band (see db/severity.py).
_SEVERITY_WEIGHT = {CRITICAL: 100, HIGH: 60, MEDIUM: 30, LOW: 10}


def compute_vuln_priority(
    severity: str | None,
    sla_due: datetime | None,
    pci_risk: int | None,
    now: datetime | None = None,
) -> int:
    """Priority score = severity weight + SLA pressure + PCI exposure.

    Higher = more urgent. Overdue SLA and PCI-scoped assets push criticals to
    the top.
    """
    now = now or datetime.now(timezone.utc)
    score = _SEVERITY_WEIGHT.get(normalize_severity(severity), 10)
    if sla_due is not None:
        due = sla_due if sla_due.tzinfo else sla_due.replace(tzinfo=timezone.utc)
        if due < now:
            score += 50  # overdue
        elif (due - now).days <= 7:
            score += 20  # due soon
    score += (pci_risk or 0) * 4
    return score


def is_high_priority(
    severity: str | None, sla_due: datetime | None, now: datetime | None = None
) -> bool:
    """A CVE warrants a patch proposal if it is critical or past its SLA."""
    now = now or datetime.now(timezone.utc)
    if normalize_severity(severity) == CRITICAL:
        return True
    if sla_due is not None:
        due = sla_due if sla_due.tzinfo else sla_due.replace(tzinfo=timezone.utc)
        return due < now
    return False
