import logging
import re
import uuid
from langchain_core.callbacks import BaseCallbackHandler
from infra_brain.config import get_settings
from infra_brain.db.models import AuditLog
from infra_brain.db.session import get_session

log = logging.getLogger(__name__)
_PAN_RE = re.compile(r"\b(\d[ -]?){13,19}\b")


def luhn_check(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if len(digits) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_REDACTION = "****-REDACTED-****"


def _matches_card_network(clean: str) -> bool:
    """True when *clean* (digits only) matches a real payment-card IIN/BIN and a
    valid length for that network.

    This is a CORROBORATING check layered ON TOP OF Luhn — it eliminates the
    Luhn false positives that plagued infra scan data: X.509 certificate serial
    numbers, Rapid7 agent/asset IDs, and epoch-nanosecond timestamps all pass
    the Luhn checksum by chance yet match no *major* card network.

    DELIBERATE COVERAGE TRADE-OFF (see lc-safety-reviewer 2026-07-13): this
    allow-list covers the four networks that account for essentially all real
    PAN exposure — Visa, Mastercard credit (51-55 / 2221-2720), Amex, Discover
    — plus Diners/JCB/UnionPay. It intentionally OMITS Maestro (BIN 50, 56-69,
    which INCLUDES the ``58`` prefix) and RuPay, because Maestro's range is
    indistinguishable by IIN+length from the 19-digit ``58…``/``55…`` X.509
    cert serials that appear in every Rapid7 sweep. Covering Maestro would
    re-introduce the false-positive storm that fails the vuln sweep closed.
    The residual risk — a real Maestro/RuPay PAN embedded in *infrastructure
    scan output* — is negligible for this system. Revisit if infra-brain ever
    ingests payment-adjacent data.
    """
    n = len(clean)
    two = int(clean[:2])
    three = int(clean[:3])
    four = int(clean[:4])
    # Visa
    if clean[0] == "4" and n in (13, 16, 19):
        return True
    # Mastercard: 51-55 or 2221-2720 (16)
    if n == 16 and (51 <= two <= 55 or 2221 <= four <= 2720):
        return True
    # American Express: 34/37 (15)
    if two in (34, 37) and n == 15:
        return True
    # Discover: 6011 / 65 / 644-649 (16 or 19)
    if n in (16, 19) and (clean[:4] == "6011" or two == 65 or 644 <= three <= 649):
        return True
    # Diners Club: 36 / 38-39 / 300-305 / 3095 (14, 16, 19)
    if n in (14, 16, 19) and (two in (36, 38, 39) or 300 <= three <= 305 or clean[:4] == "3095"):
        return True
    # JCB: 3528-3589 (16-19)
    if 3528 <= four <= 3589 and 16 <= n <= 19:
        return True
    # UnionPay: 62 (16-19)
    if two == 62 and 16 <= n <= 19:
        return True
    return False


def is_probable_pan(clean: str) -> bool:
    """A digits-only run is a probable PAN only when it is BOTH Luhn-valid AND
    matches a known card network (IIN + length). Both conditions are required —
    Luhn alone false-positives on infra identifiers (see _matches_card_network)."""
    return luhn_check(clean) and _matches_card_network(clean)


def redact_pans(text: str) -> str:
    """Mask any probable PAN in *text* (F-032: scrub chat/LLM output
    before it reaches the client). Digit runs that are not Luhn-valid AND
    matching a card network are left untouched."""

    def _mask(m: "re.Match") -> str:
        clean = re.sub(r"\D", "", m.group(0))
        return _REDACTION if is_probable_pan(clean) else m.group(0)

    return _PAN_RE.sub(_mask, str(text))


# A canonical 8-4-4-4-12 UUID token. NOTE the shape overlap that makes the
# helper below necessary: _PAN_RE accepts ``-`` as a digit-group separator, and
# a UUID's first three groups are 8+4+4 = exactly 16 hyphen-separated
# characters. Whenever those 16 happen to be all-digits AND Luhn-valid AND
# IIN-matching, redact_pans() rewrites the middle of a random identifier as
# ``****-REDACTED-****``. Measured rate on uuid4: ~2.5e-5 per id (~1 in 40,000)
# — rare enough to look like flakiness, common enough that a 1,200-id batch is
# corrupted ~3% of the time (TRK-346).
_UUID_TOKEN_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def redact_pans_preserving_uuids(text: str) -> str:
    """``redact_pans`` that cannot destroy a canonical UUID token.

    For text that carries machine-generated identifiers alongside operator free
    text — audit ``args_summary`` blobs above all, where the recorded id list IS
    the record and a mangled id makes the batch unreconstructable.

    This is an ADDITION, not a relaxation: ``redact_pans`` is unchanged and
    still applies verbatim to every span outside a UUID token, so operator prose
    embedded in the same blob is scrubbed exactly as before. The only spans
    exempted are complete 8-4-4-4-12 hex tokens, and a stored PAN is not one:
    a PAN is 13-19 digits, whereas this pattern requires exactly 32 hex
    characters in a fixed grouping. Prefer this over ``redact_pans`` anywhere
    the input is a serialized structure containing row ids.
    """
    text = str(text)
    out: list[str] = []
    pos = 0
    for match in _UUID_TOKEN_RE.finditer(text):
        out.append(redact_pans(text[pos : match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(redact_pans(text[pos:]))
    return "".join(out)


class DLPCallbackHandler(BaseCallbackHandler):
    # F-004.1: raise_error=True makes the PermissionError in _check() propagate
    # through the CallbackManager instead of being swallowed. Never set this
    # back to False.
    raise_error = True

    def __init__(self, agent_name: str):
        super().__init__()
        self.agent_name = agent_name

    def _check(self, text: str, run_id: uuid.UUID, direction: str):
        candidates = _PAN_RE.finditer(str(text))
        for match in candidates:
            clean = re.sub(r"\D", "", match.group(0))
            if is_probable_pan(clean):
                reason = f"PAN detected in {direction} (Luhn valid)"
                with get_session() as session:
                    session.add(
                        AuditLog(
                            id=uuid.uuid4(),
                            agent=self.agent_name,
                            tool="dlp-check",
                            input_hash="",
                            allowed=False,
                            denial_reason=reason,
                        )
                    )
                    session.commit()
                if get_settings().dlp_fail_closed:
                    raise PermissionError(f"[DLP] {reason}")
                else:
                    log.error("[DLP WARN] %s", reason)

    def on_tool_end(self, output: str, *, run_id: uuid.UUID, **kwargs):
        self._check(output, run_id, "tool output")

    def on_tool_start(self, serialized: dict, input_str: str, *, run_id: uuid.UUID, **kwargs):
        self._check(input_str, run_id, "tool input")

    def on_llm_end(self, response, *, run_id: uuid.UUID = None, **kwargs):
        """F-032: scan generated LLM text, not just tool I/O. Audits (and, under
        dlp_fail_closed, raises) when a Luhn-valid PAN appears in model output."""
        self._check(str(response), run_id or uuid.uuid4(), "llm output")

    # Chat models route through on_chat_model_end in some LangChain versions.
    on_chat_model_end = on_llm_end
