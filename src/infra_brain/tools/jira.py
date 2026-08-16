import logging
import uuid
from datetime import datetime, timezone

import httpx

from infra_brain.callbacks.write_gate import gate_external_write
from infra_brain.config import get_settings
from infra_brain.db.models import JiraTicket
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)


def ticket_exists(drift_event_id: uuid.UUID) -> str | None:
    with get_session() as session:
        ticket = session.query(JiraTicket).filter_by(drift_event_id=drift_event_id).first()
        return ticket.jira_key if ticket else None


def _post_jira_ticket(
    summary: str,
    description: str,
    *,
    operation: str,
    labels: list[str],
) -> str:
    """Shared write-gated POST used by every ticket-creation entry point.

    F-004.3: pre-write DLP + audit gate — refuses BEFORE any httpx.post.
    """
    gate_external_write(
        system="jira",
        operation=operation,
        payload=f"{summary}\n{description}",
        agent_name="jira",
    )

    s = get_settings()
    payload = {
        "fields": {
            "project": {"key": s.jira_project_key},
            "summary": summary,
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": description}]}
                ],
            },
            "issuetype": {"name": "Bug"},
            "labels": labels,
        }
    }
    resp = httpx.post(
        f"{s.jira_url}/rest/api/3/issue",
        json=payload,
        auth=(s.jira_user_email, s.jira_token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=s.api_timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()["key"]


def create_jira_ticket(
    summary: str,
    description: str,
    drift_event_id: uuid.UUID,
) -> str:
    existing = ticket_exists(drift_event_id)
    if existing:
        return existing

    jira_key = _post_jira_ticket(
        summary,
        description,
        operation="create_jira_ticket",
        labels=["infra-drift", "automated"],
    )

    with get_session() as session:
        session.add(
            JiraTicket(
                drift_event_id=drift_event_id,
                jira_key=jira_key,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    return jira_key


def create_compliance_jira_ticket(
    summary: str,
    description: str,
) -> str:
    """Create a Jira ticket for an open ComplianceViolation (GitLab issue #106).

    Reuses the exact same write-gated POST path as `create_jira_ticket` (same
    gate_external_write call, same Jira issue-create request). It intentionally
    does NOT write a `JiraTicket` row — that table's `drift_event_id` column is
    a hard FK to `drift_events.id`, and a compliance violation id is not a
    drift event id, so inserting one there would violate the FK on Postgres
    (and silently re-file a duplicate ticket every run, since the failed
    insert would prevent the caller from ever recording success). Dedup for
    compliance violations instead lives at the caller: `NotificationAgent
    .notify_all()` only queries `ComplianceViolation` rows with
    `status="open"` and flips each to `"acknowledged"` immediately after a
    successful ticket — the same status-transition mechanism already used to
    avoid re-ticketing drift events.
    """
    return _post_jira_ticket(
        summary,
        description,
        operation="create_compliance_jira_ticket",
        labels=["infra-compliance", "automated"],
    )
