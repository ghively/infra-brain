"""Canonical vuln_queue status vocabulary for infra-brain.

FE-9: the dashboard's "Open CVEs" metric (``/api/dashboard/counts``'s
``open_cves``) was computed as ``VulnQueueItem.status != "resolved"``. In
practice nothing ever wrote ``status="resolved"`` (or any other closed state)
onto a ``vuln_queue`` row once Rapid7 stopped reporting a CVE for an asset —
``VulnAgent._upsert_vuln_row`` only ever inserts with ``status="open"`` and
explicitly preserves whatever status a row already has on every subsequent
sweep (human triage state). So a CVE that Rapid7 had already remediated on
its next scan stayed in ``vuln_queue`` with its original open-ish status
forever, and "Open CVEs" silently counted it as still open.

The real fix (see ``agents/vuln.py::_write_vuln_queue``) is a reconciliation
step, mirroring the one ``agents/compliance.py`` already has for
``ComplianceViolation``: any row for an asset that *was* scanned this run,
whose (resource_id, cve_id) is no longer in the current Rapid7 findings, and
whose status is still one of the *system-managed* open states, gets flipped
to ``resolved``. Human-set states (``triage`` is borderline-system, but
anything a human/remediation flow sets — e.g. ``remediated``,
``accepted_risk``, ``false_positive``) are never touched, matching the
existing "do NOT overwrite status" contract on upsert.

Every reader that means "is this vuln still actionable" should filter with
:data:`OPEN_VULN_STATUSES` here rather than inventing its own ad hoc
``== "open"`` / ``!= "patched"`` / ``!= "resolved"`` predicate — those three
were each subtly different views of the same concept and are exactly the
kind of drift this module exists to prevent (same rationale as
``db/severity.py`` for the severity band vocabulary).
"""

from __future__ import annotations

#: System-managed "still open" states. A vuln_queue row starts in OPEN and
#: may move to TRIAGE (vuln_triage.py); both are states the collector/agents
#: manage automatically and are safe for VulnAgent's reconciliation pass to
#: overwrite to CLOSED_AUTO when the finding is no longer reported.
OPEN = "open"
TRIAGE = "triage"
OPEN_VULN_STATUSES: tuple[str, ...] = (OPEN, TRIAGE)

#: Auto-set when the reconciliation pass in VulnAgent no longer sees the
#: finding reported by Rapid7 for that asset (distinct from human-set closed
#: states like "remediated"/"accepted_risk", which are never auto-assigned).
CLOSED_AUTO = "resolved"

#: Human/remediation-set closed states. Never written by the reconciliation
#: pass; readers treat these the same as CLOSED_AUTO ("not open") but they
#: carry distinct provenance (a person or the remediation flow decided this,
#: not "Rapid7 stopped reporting it").
HUMAN_CLOSED_VULN_STATUSES: tuple[str, ...] = ("remediated", "accepted_risk", "false_positive")


def is_open_vuln_status(status: str | None) -> bool:
    """True if *status* is one of the system-managed open states."""
    return status in OPEN_VULN_STATUSES
