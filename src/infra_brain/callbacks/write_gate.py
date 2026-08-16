"""Pre-write gate for EVERY external write path (F-004.3 / R2 layer 2).

The external-mutation functions (GitLab MR, Jira ticket, Confluence page) are
plain functions — never LangChain @tools — so the callback chain never sees
them. This gate is the mandatory choke point they all call BEFORE any
httpx.post/put leaves the process:

1. DLP scan of the full outbound payload (probable-PAN detection via
   ``dlp.is_probable_pan`` — Luhn AND card-network IIN/length — the same
   patterns and corroborated test as DLPCallbackHandler).
2. One durable AgentActionLog row per call with the REAL verdict
   ("allow" | "deny") — not the unconditional "allow" F-004.3 flagged.

Fail-closed: a deny raises PermissionError and nothing is sent.
"""

from __future__ import annotations

import logging
import re

from infra_brain.callbacks.dlp import _PAN_RE, is_probable_pan
from infra_brain.db.models import AgentActionLog
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)


def gate_external_write(
    system: str,
    operation: str,
    payload: str,
    agent_name: str,
) -> None:
    """DLP-scan *payload* and audit the decision; raise PermissionError on deny.

    Args:
        system: target system, e.g. "gitlab" | "jira" | "confluence".
        operation: function-level operation name, e.g. "create_inventory_mr".
        payload: every outbound text field concatenated (title, body, commit
            message, description, file content...).
        agent_name: attribution for the audit row.
    """
    reason: str | None = None
    for match in _PAN_RE.finditer(str(payload)):
        digits = re.sub(r"\D", "", match.group(0))
        # Same corroborated test DLPCallbackHandler._check()/redact_pans() use:
        # Luhn AND a real card-network IIN+length. Luhn alone false-positives on
        # X.509 cert serials, Rapid7 agent/asset IDs and epoch-ns timestamps —
        # exactly the identifiers these outbound payloads routinely embed — and
        # would block sanctioned writes. See dlp._matches_card_network.
        if is_probable_pan(digits):
            reason = "PAN detected in outbound write payload (Luhn valid)"
            break

    verdict = "deny" if reason else "allow"
    with get_session() as session:
        session.add(
            AgentActionLog(
                agent=agent_name,
                tool=f"{system}:{operation}",
                args_summary=f"payload_len={len(str(payload))}",
                verdict=verdict,
                status="error" if reason else "ok",
                error=reason,
            )
        )
        session.commit()

    if reason:
        logger.warning("[write-gate] BLOCKED %s:%s — %s", system, operation, reason)
        raise PermissionError(f"[write-gate] {reason}")
