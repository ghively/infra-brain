"""Format validation for Microsoft KB (Knowledge Base) article identifiers.

TRK-193 sub-bug 2: ``RemediationAgent._draft_plan`` (``agents/remediation.py``)
drafts a free-form Markdown remediation plan via ``LLMAgent.reason()`` with no
constraint on what it recommends — the same unstructured-drafting path that an
earlier overnight Ollama audit caught inventing a "revert" recommendation for a
scanner-computed metric (see the ``TRK-context-fix`` notes in that module).
Nothing previously checked whether a KB article number the model wrote into a
patch/remediation recommendation actually looks like a real Microsoft KB
identifier (``KB`` + digits, e.g. ``KB5034122``) before it was persisted as a
``ProposedAction`` recommendation — i.e. it was "free-generated" with no
validation layer, exactly as filed.

**Scope, stated explicitly (do not overclaim beyond this):** this module can
only validate *format*, not *reality*. There is no external API call anywhere
in this codebase (Microsoft's Update Catalog is not one of the read-only
sources this project talks to) that could confirm a well-formed
``KB5034122``-shaped string is a real, published KB — let alone one that
actually applies to the OS/situation under discussion. A syntactically valid
but entirely fabricated KB number (e.g. an LLM hallucinating a plausible
7-digit number that happens to match the format but was never issued by
Microsoft) will still pass this check. Format validation only catches
obviously-fabricated *non-KB-shaped* strings — wrong digit count, letters
mixed into the digit run, a stray separator character, etc. — not
hallucinated-but-well-formed ones. Real-KB verification would need a new,
explicitly-approved outbound integration; it is out of scope here.
"""

from __future__ import annotations

import re

# Real Microsoft KB article IDs are "KB" immediately followed by a contiguous
# run of digits -- no space, hyphen, or other punctuation in between is ever
# valid in a real KB identifier. 6 digits covers older hotfix-era IDs (e.g.
# KB925673); 7 digits is the modern Windows Update Catalog format (e.g.
# KB5034122).
_VALID_KB_RE = re.compile(r"^KB\d{6,7}$")

# Looser "this text is trying to reference a KB article" detector. Tolerates a
# space or hyphen between "KB" and the digits (a plausible LLM/human typo) and
# any digit-run length, so a malformed candidate ("KB 123", "KB-503412",
# "KB12345678") is still caught and flagged rather than silently matching
# nothing (and therefore passing straight through unexamined, defeating the
# whole point of the check). Two capture groups: the separator (if any) and
# the digit run, so flag_invalid_kb_references() below can require BOTH no
# separator AND a 6-7 digit run for something to count as well-formed --
# real Microsoft KB IDs never contain a space or hyphen, so "KB-503412" is
# exactly as invalid as "KB123" is, even though its digit count alone would
# otherwise fit.
_KB_CANDIDATE_RE = re.compile(r"\bKB([\s-]?)(\d+)\b", re.IGNORECASE)


def is_valid_kb_id(candidate: str) -> bool:
    """Strict format check: ``KB`` + 6-7 digits, no separator, no extra text.

    This confirms *shape* only -- it does not and cannot confirm the KB
    article actually exists or applies to anything (see module docstring).
    """
    return bool(_VALID_KB_RE.match(candidate.strip()))


def flag_invalid_kb_references(text: str) -> tuple[str, list[str]]:
    """Scan *text* for KB-article-looking references and flag malformed ones.

    Returns ``(annotated_text, flagged)``: ``flagged`` lists the raw
    substrings that looked like an attempted KB reference (matched the loose
    candidate pattern) but did not match the real Microsoft KB identifier
    format (``KB`` immediately followed by 6-7 digits). Well-formed
    references, and any text containing no KB-looking substrings at all, are
    returned unchanged with an empty ``flagged`` list.

    A flagged reference is annotated **inline** (not silently dropped) so a
    human reviewing the proposal still sees exactly what the model wrote,
    with an explicit warning attached rather than a fabricated-looking KB
    number passing through as if it had been validated.
    """
    flagged: list[str] = []

    def _replace(match: re.Match) -> str:
        raw = match.group(0)
        separator, digits = match.group(1), match.group(2)
        # Well-formed iff there was no separator between "KB" and the digits
        # AND the digit run is 6-7 long -- matches is_valid_kb_id()'s rule,
        # checked here against the raw matched pieces (not a
        # separator-stripped normalization) so a hyphenated/spaced variant is
        # correctly treated as malformed rather than silently accepted.
        if not separator and 6 <= len(digits) <= 7:
            return raw
        flagged.append(raw)
        return (
            f"{raw} [UNVERIFIED: not a valid KB-article-ID format "
            "(expected KB + 6-7 digits, no separator)]"
        )

    annotated = _KB_CANDIDATE_RE.sub(_replace, text)
    return annotated, flagged
