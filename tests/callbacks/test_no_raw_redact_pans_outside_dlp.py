"""Every PAN-scrub of persisted or displayed text must use the UUID-preserving
variant.

TRK-346/TRK-347: ``dlp._PAN_RE`` accepts ``-`` as a digit-group separator, and
a canonical UUID's first three groups are exactly 16 hyphen-separated
characters — so roughly 1 in 40,000 UUIDs is all-digit, Luhn-valid and
IIN-matching, and raw ``redact_pans()`` rewrites it as ``****-REDACTED-****``.
On UUID-bearing text (audit rows, tool results, operator notes referencing
drift/resource ids) that silently corrupts the identifier the text exists to
record. It manifested as a "flaky test" that was actually a 3%-per-batch
audit-corruption bug.

``redact_pans_preserving_uuids`` applies the identical scrub to every span
OUTSIDE a complete 8-4-4-4-12 hex token, so no DLP strength is lost — a real
PAN is 13–19 digits and can never be a fixed-grouping 32-hex-char token.

This lint pins the fix repo-wide: outside ``callbacks/dlp.py`` itself, no
CODE may call raw ``redact_pans(``. Comments and docstrings may mention it.
If a new call site genuinely can never see a UUID, using the preserving
variant anyway costs nothing — so there is no allowlist.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "infra_brain"
_CALL = re.compile(r"(?<![\w.])redact_pans\(")


def _code_lines(path: Path):
    """Yield (lineno, line) for lines that are code, not comment/docstring.

    Cheap heuristic sufficient here: strips full-line comments and tracks
    triple-quoted blocks. A call hidden in a string literal would be dead
    code anyway.
    """
    in_doc = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        quotes = stripped.count('"""') + stripped.count("'''")
        if in_doc:
            if quotes:
                in_doc = False
            continue
        if quotes == 1:
            in_doc = True
            continue
        if stripped.startswith("#"):
            continue
        # strip trailing comment before matching
        yield n, line.split("#", 1)[0]


def test_no_raw_redact_pans_call_outside_dlp():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "dlp.py" and path.parent.name == "callbacks":
            continue
        for n, line in _code_lines(path):
            if _CALL.search(line) and "redact_pans_preserving_uuids" not in line:
                offenders.append(f"{path.relative_to(SRC)}:{n}: {line.strip()[:90]}")
    assert not offenders, (
        "raw redact_pans() mangles ~1 in 40,000 UUIDs (TRK-346/347); use "
        "redact_pans_preserving_uuids() instead:\n  " + "\n  ".join(offenders)
    )
