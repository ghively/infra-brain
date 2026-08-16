"""Headless runner's MCP-attributed action tools (P6.1/P6.2, D10).

Each tool here calls ``runner/mcp_client.py``'s ``call_mcp_tool`` — a real
MCP HTTP call with the runner's own scoped bearer token — so every action
attributes as ``mcp:headless-runner`` and is independently revocable,
unlike the dashboard chat agent's ``propose_host_purpose_update`` (an
in-process Python call with no MCP hop at all).

Attribution fields (``client_id`` for the state/governance tools) are
hardcoded here, not LLM-controllable parameters — same "derive, don't
trust free-form input" rule the rest of this codebase applies to every
other attribution-bearing field.

Scope matches D10 exactly: reads (via the existing chat/tools.py functions,
which call the DB in-process — no attribution concern for reads) plus these
four mutations. NOT bound: seed_*, resolve_drift_events, approve_proposal —
a nightly/weekly unattended runner should never seed inventory, bulk-resolve
drift, or approve a human-review-gated proposal.
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from infra_brain.runner.mcp_client import call_mcp_tool

logger = logging.getLogger(__name__)

_RUNNER_CLIENT_ID = "headless-runner"


@tool
async def runner_create_gitlab_issue(
    project_id: int, title: str, description: str = "", labels: list[str] | None = None
) -> dict:
    """File a GitLab issue (idempotent by exact open-issue title match).

    Args:
        project_id: the GitLab project's numeric id — NOT a path. The
            underlying MCP tool (``create_gitlab_issue(project_id: int, ...)``
            in ``mcp_server.py``, backed by ``tools/gitlab_issue.py``) is
            strictly int-typed, so a project path would fail at that
            boundary. There is no default here and none is inferred from
            config — the caller must supply a real project id. A missing or
            non-positive value is rejected below rather than risking a file
            into an unintended or hallucinated project.
        title: issue title — used as the idempotency key against existing
            open issues.
        description: issue body (markdown).
        labels: optional list of label names to apply.
    """
    if not project_id or project_id <= 0:
        msg = (
            "runner_create_gitlab_issue requires a real numeric GitLab "
            f"project_id; refusing to file into project_id={project_id!r}."
        )
        logger.error("[headless-runner] create_gitlab_issue rejected: %s", msg)
        return {"error": msg}
    return await call_mcp_tool(
        "create_gitlab_issue",
        project_id=project_id,
        title=title,
        description=description,
        labels=labels,
    )


@tool
async def runner_record_governance_event(event_type: str, payload: dict | None = None) -> dict:
    """Record a hash-chained governance event (append-only, never updated).

    Args:
        event_type: short machine-readable category, e.g.
            "drift-triage-run", "self-review-gap-found".
        payload: JSON-serializable event detail.
    """
    return await call_mcp_tool(
        "record_governance_event",
        client_id=_RUNNER_CLIENT_ID,
        event_type=event_type,
        payload=payload or {},
    )


@tool
async def runner_record_client_state(collection: str, entry_id: str, payload: dict) -> dict:
    """Record a client-state entry (collection must be a scoped, allow-listed
    name — an unrecognized collection returns a clear error, not a silent no-op).

    Args:
        collection: which state collection this entry belongs to.
        entry_id: caller-supplied id for this entry.
        payload: the entry's data.
    """
    return await call_mcp_tool(
        "record_client_state",
        collection=collection,
        entry_id=entry_id,
        payload=payload,
        client_id=_RUNNER_CLIENT_ID,
    )


@tool
async def runner_propose_instinct(
    zone: str, domain: str, pattern: str, confidence: float,
    evidence: str = "", citation: str = "",
) -> dict:
    """Draft a proposed instinct (status=pending) for later human approval —
    does not touch the live instincts table.

    Args:
        zone: deployment zone the pattern applies to.
        domain: technical domain (e.g. "linux", "gitlab-cicd").
        pattern: the observed pattern/recommendation text.
        confidence: 0.0-1.0 confidence score.
        evidence: supporting evidence text.
        citation: documentation citation, if any.
    """
    return await call_mcp_tool(
        "propose_instinct",
        zone=zone,
        domain=domain,
        pattern=pattern,
        confidence=confidence,
        evidence=evidence,
        citation=citation,
        proposer_label=_RUNNER_CLIENT_ID,
    )


RUNNER_ACTION_TOOLS = [
    runner_create_gitlab_issue,
    runner_record_governance_event,
    runner_record_client_state,
    runner_propose_instinct,
]

__all__ = [
    "runner_create_gitlab_issue",
    "runner_record_governance_event",
    "runner_record_client_state",
    "runner_propose_instinct",
    "RUNNER_ACTION_TOOLS",
]
