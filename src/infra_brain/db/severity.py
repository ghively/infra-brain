"""Canonical severity vocabulary for infra-brain.

Single source of truth for the vulnerability/finding severity *band* values as
STORED in the database (``vuln_queue.severity`` and the derived comparisons the
triage / fleet-health / remediation / compliance readers make).

Two rules keep this from drifting back into the June-2026 silent-zero bug:

1. **Writers** store one of :data:`CANONICAL_SEVERITIES` (all lowercase).
2. **Readers** filter / compare against these same constants — never against a
   capitalised or vendor-raw literal like ``"Critical"`` / ``"High"``.

The bug this prevents: ``vuln.py`` lowercases severity on write, but several
readers filtered on ``["Critical", "High"]`` — a case mismatch that silently
returned zero rows. Route every reader and writer through this module.

NOTE — a *separate* raw-vendor vocabulary exists on ``r7_vulnerabilities.severity``
(``Critical`` / ``Severe`` / ``Moderate`` …), surfaced verbatim by the CVE
dashboard endpoint. That column is intentionally NOT normalised here; it is a
display-facing raw field with its own self-consistent vocabulary. This module
governs the *band* vocabulary that drives SLA/priority/eligibility logic.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Canonical stored band values (lowercase). Ordered most → least severe.
# ---------------------------------------------------------------------------
CRITICAL = "critical"
HIGH = "high"
MEDIUM = "medium"
LOW = "low"

#: The complete canonical vocabulary, most-severe first.
CANONICAL_SEVERITIES: tuple[str, ...] = (CRITICAL, HIGH, MEDIUM, LOW)

#: The "urgent" subset used by fleet-health metrics and remediation MR
#: eligibility. Readers must filter with this, never with capitalised literals.
HIGH_AND_CRITICAL: tuple[str, ...] = (CRITICAL, HIGH)

#: SLA window (days from detection) per canonical band — PCI-aligned.
SLA_DAYS: dict[str, int] = {CRITICAL: 3, HIGH: 7, MEDIUM: 30, LOW: 90}

#: Fallback SLA when a severity can't be resolved to a band.
DEFAULT_SLA_DAYS = 90

#: Ranking for "enrich the worst first" caps. Higher = more severe.
SEVERITY_RANK: dict[str, int] = {CRITICAL: 4, HIGH: 3, MEDIUM: 2, LOW: 1}

# ---------------------------------------------------------------------------
# Vendor / raw-input aliases → canonical band.
#
# Rapid7 emits Critical / Severe / Moderate as its finding-severity words;
# "Severe" is Rapid7's equivalent of "high" (matching its numeric rank), and
# "Moderate" is "medium". Anything not resolvable here returns None so callers
# can fall back to a CVSS-derived band.
# ---------------------------------------------------------------------------
_ALIASES: dict[str, str] = {
    "critical": CRITICAL,
    "crit": CRITICAL,
    "severe": HIGH,
    "high": HIGH,
    "moderate": MEDIUM,
    "medium": MEDIUM,
    "mod": MEDIUM,
    "low": LOW,
    "minor": LOW,
    "informational": LOW,
    "info": LOW,
}

# Prefix fallbacks for vendor variants ("critical severity", "moderate", ...).
#
# NOTE: there is deliberately no ("sev", HIGH) entry here. A bare "sev" prefix
# is not a reliable signal of "high" — the industry-standard Sev1..Sev4
# convention (Sev1 = highest/critical, Sev4 = lowest) also starts with "sev",
# so a greedy prefix match would silently mis-band those inputs (Sev1 -> high
# instead of critical, Sev4 -> high instead of low). Unresolvable "sev*"
# strings fall through to None so callers use a CVSS-derived band instead of
# a guess — this is exactly the kind of silent misclassification this
# module's docstring warns against.
_PREFIXES: tuple[tuple[str, str], ...] = (
    ("crit", CRITICAL),
    ("mod", MEDIUM),
)


def normalize_severity(raw: str | None) -> str | None:
    """Map a raw / vendor severity string to a canonical band, or ``None``.

    Returns ``None`` for empty / whitespace / unknown input so callers can fall
    back to a CVSS-derived band. Matching is case-insensitive.
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if s in _ALIASES:
        return _ALIASES[s]
    if s in CANONICAL_SEVERITIES:
        return s
    for prefix, canon in _PREFIXES:
        if s.startswith(prefix):
            return canon
    return None


def rank(severity: str | None) -> int:
    """Numeric severity rank (0 for unknown / None)."""
    canon = normalize_severity(severity)
    return SEVERITY_RANK.get(canon, 0) if canon else 0


def sla_days(severity: str | None) -> int:
    """SLA window in days for a severity, falling back to :data:`DEFAULT_SLA_DAYS`."""
    canon = normalize_severity(severity)
    return SLA_DAYS.get(canon, DEFAULT_SLA_DAYS) if canon else DEFAULT_SLA_DAYS
