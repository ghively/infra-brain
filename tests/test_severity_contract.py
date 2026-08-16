"""Contract test pinning the canonical severity vocabulary (MR-B / TEST-1).

Two guarantees, so the June-2026 silent-zero bug can't come back:

1. The canonical vocabulary and its normalizer behave exactly as documented —
   in particular Rapid7's "Severe" bands to "high" (SLA 7d), not the lenient
   90-day default it silently fell through to before.

2. No module filters the *band* column ``VulnQueueItem.severity`` (or any bare
   ``.severity``) against a Capitalized band literal. That casing mismatch is
   what zeroed the fleet-health and remediation metrics; this test fails if any
   reader/writer reintroduces it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from infra_brain.db import severity as sev


# ---------------------------------------------------------------------------
# 1. Vocabulary + normalizer contract
# ---------------------------------------------------------------------------


def test_canonical_vocabulary_is_lowercase_and_fixed():
    assert sev.CANONICAL_SEVERITIES == ("critical", "high", "medium", "low")
    for value in sev.CANONICAL_SEVERITIES:
        assert value == value.lower(), f"{value!r} must be lowercase"
    assert sev.HIGH_AND_CRITICAL == ("critical", "high")
    # Every canonical band has an SLA and a rank.
    for band in sev.CANONICAL_SEVERITIES:
        assert band in sev.SLA_DAYS
        assert band in sev.SEVERITY_RANK


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Critical", "critical"),
        ("critical", "critical"),
        ("CRITICAL", "critical"),
        ("Severe", "high"),  # Rapid7's "Severe" == high band (the fixed case)
        ("High", "high"),
        ("Moderate", "medium"),  # Rapid7's "Moderate" == medium
        ("Medium", "medium"),
        ("Low", "low"),
        ("informational", "low"),
        ("", None),
        ("   ", None),
        (None, None),
        ("bogus-value", None),
        # A bare "sev" prefix must NOT be guessed as high — Sev1..Sev4 is a
        # common industry convention (Sev1 = critical, Sev4 = low), so
        # greedily mapping any "sev*" string to HIGH would silently
        # misclassify a Sev1/critical finding as high (downgrade) and a
        # Sev4/low finding as high (upgrade). Unresolved strings must fall
        # through to None so callers use a CVSS-derived band instead.
        ("Sev1", None),
        ("Sev1 - critical outage", None),
        ("Sev4", None),
        ("severity unknown", None),
    ],
)
def test_normalize_severity(raw, expected):
    assert sev.normalize_severity(raw) == expected


def test_sev_prefix_is_not_silently_misclassified_as_high():
    """Regression test: a "sev"-prefixed non-alias string must not be
    stamped HIGH by an overly broad prefix fallback.

    Before the fix, ``_PREFIXES`` contained ``("sev", HIGH)``, so any raw
    string starting with "sev" (e.g. the Sev1..Sev4 convention, where Sev1
    is the highest/critical band and Sev4 is the lowest) that wasn't an
    exact alias match would be silently stamped "high" — exactly the kind
    of case-mismatch/silent-misclassification bug this module exists to
    prevent, just via a different mechanism.
    """
    for raw in ("Sev1", "Sev1 - critical outage", "Sev4", "sev-2"):
        assert sev.normalize_severity(raw) is None
        # Falls back to the lenient default SLA / rank 0, not a guessed HIGH.
        assert sev.sla_days(raw) == sev.DEFAULT_SLA_DAYS
        assert sev.rank(raw) == 0


def test_severe_bands_to_high_sla_not_lenient_default():
    """A "Severe" finding must get the 7-day HIGH SLA, never the 90-day default.

    Before MR-B, "Severe" fell through the band mapping and — absent a CVSS
    score — landed on the lenient 90-day (low) SLA.
    """
    assert sev.sla_days("Severe") == sev.SLA_DAYS[sev.HIGH] == 7
    assert sev.sla_days("Severe") != sev.DEFAULT_SLA_DAYS
    assert sev.rank("Severe") == sev.SEVERITY_RANK[sev.HIGH]


def test_sla_days_and_rank_fall_back_for_unknown():
    assert sev.sla_days("bogus") == sev.DEFAULT_SLA_DAYS
    assert sev.rank("bogus") == 0
    assert sev.rank(None) == 0


# ---------------------------------------------------------------------------
# 2. Source-scan guard: no Capitalized band literal filtered against .severity
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parent.parent / "src" / "infra_brain"

# Files that legitimately live in the *raw vendor* severity vocabulary
# (r7_vulnerabilities.severity: Critical/Severe/Moderate) or define the
# vocabulary itself — excluded from the band-casing scan.
_EXCLUDED = {
    _SRC / "db" / "severity.py",
}

# The exact bug shapes:
#   .severity.in_([... "Critical" ...])   and   .severity == "High"
_BAND_WORDS = r"Critical|High|Medium|Low|Severe"
_IN_LIST = re.compile(
    r"\.severity\s*\.in_\(\s*[\[(][^\])]*[\"'](?:" + _BAND_WORDS + r")[\"']",
)
_EQ_LITERAL = re.compile(
    r"\.severity\s*==\s*[\"'](?:" + _BAND_WORDS + r")[\"']",
)


def test_no_capitalized_band_literal_filters_severity():
    offenders: list[str] = []
    for py in _SRC.rglob("*.py"):
        if py in _EXCLUDED:
            continue
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if _IN_LIST.search(line) or _EQ_LITERAL.search(line):
                offenders.append(f"{py.relative_to(_SRC)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Severity filters must use db.severity constants (canonical lowercase "
        "bands), never Capitalized literals. Offenders:\n" + "\n".join(offenders)
    )
