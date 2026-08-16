"""Shared CVE-id extraction (F-008 P2 de-duplication).

Single source of truth for parsing canonical ``CVE-YYYY-NNNN`` ids out of
Rapid7 payloads. Real CVE ids fit ``cve_id``'s ``String(32)``; Rapid7's
asset-vuln slug ``id`` (e.g. ``microsoft-dot_net_framework-cve-2026-23666``)
does NOT and triggered StringDataRightTruncation, which rolled back whole
detail-write batches. Previously this logic was duplicated verbatim in
``agents/vuln.py`` and ``agents/vuln_cve.py``.
"""

import re

CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)


def extract_cves(vuln: dict) -> list[str]:
    """Return the canonical CVE id(s) for a Rapid7 vuln record.

    Prefers an explicit ``cves`` array (strings or dicts); falls back to a
    case-insensitive CVE regex over the slug/``id`` + ``title``. Ids are
    uppercased and de-duplicated (order preserved). A non-CVE Rapid7 check
    returns ``[]`` and is skipped by callers — never truncated into cve_id.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(text) -> None:
        if not text:
            return
        for m in CVE_RE.findall(str(text)):
            cve = m.upper()
            if cve not in seen:
                seen.add(cve)
                found.append(cve)

    cves = vuln.get("cves")
    if isinstance(cves, (list, tuple)):
        for entry in cves:
            if isinstance(entry, dict):
                _add(entry.get("id") or entry.get("cve") or entry.get("name"))
            else:
                _add(entry)
    elif isinstance(cves, str):
        _add(cves)

    if not found:
        _add(vuln.get("id"))
        _add(vuln.get("title"))

    return found
