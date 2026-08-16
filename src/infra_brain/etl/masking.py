"""Shared "mask-at-ingestion" PAN scrubbing for ETL collectors (DLP — F-019).

Extracted from ``agents/octopus.py`` (the original, and until now only, caller)
so any collector that ingests free-text fields from an upstream API — not just
Octopus — can scrub Luhn-valid card-number shapes out of raw payloads BEFORE
they are written to a detail table, without re-implementing the same regex +
Luhn check + logging pattern per collector.

This is deliberately separate from ``callbacks/dlp.py``, which guards a
different boundary: LLM/chat tool-call input and output. This module guards
ETL ingestion — collector detail-write payloads that never pass through an LLM
at all (e.g. Octopus release notes, task/event descriptions).
"""

import logging
import re

logger = logging.getLogger(__name__)

# Matches 14-19 digit sequences, either unbroken (card numbers without
# separators) or with a single space/hyphen between each digit (e.g.
# "4444 4444 4444 4444" or "4444-4444-4444-4444"). Separators are stripped
# before the Luhn check in mask_pan_shapes(). The boundary anchors prevent
# partial matches inside longer numbers such as Unix timestamps.
PAN_RE = re.compile(r"\b\d(?:[ -]?\d){13,18}\b")

PAN_MASK_TOKEN = "[PAN-MASKED]"


def luhn_valid(digits: str) -> bool:
    """Return True if *digits* (a pure-digit string) passes the Luhn algorithm.

    Never called with fewer than 14 or more than 19 digits (caller guarantees).
    DOES NOT log or return the digit value — the caller is responsible for
    keeping the raw value out of logs.
    """
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def mask_pan_shapes(value: object) -> object:
    """Recursively scan *value* and replace Luhn-valid 14-19-digit sequences
    with ``PAN_MASK_TOKEN``.

    SECURITY: this function NEVER logs, prints, or stores the raw matched digit
    sequence.

    Supported types: str, dict, list. All other types are returned unchanged.
    """
    if isinstance(value, str):

        def _mask_match(m: re.Match) -> str:
            # Strip separators before the Luhn check (see PAN_RE comment);
            # on a non-match the original matched text — separators intact —
            # is returned unchanged.
            digits = m.group().replace(" ", "").replace("-", "")
            return PAN_MASK_TOKEN if luhn_valid(digits) else m.group()

        return PAN_RE.sub(_mask_match, value)
    if isinstance(value, dict):
        return {k: mask_pan_shapes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_pan_shapes(item) for item in value]
    return value


def dlp_scrub(
    record: dict,
    context: str = "",
    *,
    log: logging.Logger | None = None,
    source: str = "",
) -> dict:
    """Apply PAN masking to a single API-response *record* (dict).

    Logs a WARNING (count only, no values) when at least one field was masked.
    Returns the masked dict. The caller replaces its local variable with the
    return value — never passing the original *record* to downstream code.

    ``log`` lets a caller route the warning through its OWN module logger
    (e.g. ``infra_brain.agents.octopus``) instead of this module's, so existing
    log-based assertions/alerting keyed on the caller's logger name keep
    working after this helper moved out of the caller's module.
    ``source`` is an optional short label (e.g. "octopus") prefixed onto the
    warning for collectors that want to disambiguate multiple callers sharing
    one logger.
    """
    active_log = log if log is not None else logger
    masked = mask_pan_shapes(record)
    if masked != record:
        # Count top-level fields that changed; never log raw values.
        n_changed = sum(1 for k in record if record[k] != masked.get(k))
        active_log.warning(
            "%sDLP: PAN-shaped value masked in %d field(s) of record%s",
            f"{source} " if source else "",
            n_changed,
            f" ({context})" if context else "",
        )
    return masked
