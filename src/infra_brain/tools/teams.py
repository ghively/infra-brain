"""Teams outgoing-webhook synchronous reply builder (GitLab #111).

Unlike Slack (async Events API needing a follow-up ``chat.postMessage`` call)
and GitLab/Jira/Confluence/ops-webhook (out-of-band ``httpx.post``), a Teams
"Outgoing Webhook" bot replies by returning its answer directly in the HTTP
response body to the original request — there is no separate outbound call
to gate.

The text still leaves the process boundary into an external Teams channel,
so it is still routed through ``gate_external_write`` (DLP scan + durable
audit row) for parity with every other sanctioned external write, even
though no ``httpx.post`` follows.
"""

from __future__ import annotations

from infra_brain.callbacks.write_gate import gate_external_write


def build_teams_reply(text: str, agent_name: str = "chatops") -> dict:
    """DLP-scan + audit *text*, then shape it as a Teams bot-reply payload.

    Raises ``PermissionError`` (via ``gate_external_write``) on a DLP hit —
    the caller must not return the raw text to Teams in that case.
    """
    gate_external_write(
        system="teams",
        operation="outgoing_webhook_reply",
        payload=text,
        agent_name=agent_name,
    )
    return {"type": "message", "text": text}
