"""
GitLab issue creation and commenting — a sanctioned write path alongside
`tools/gitlab_mr.py`'s MR creation. Every mutation flows through
`gate_external_write` (DLP scan + audit row) BEFORE any httpx call leaves
the process. A deny raises PermissionError and sends nothing.
"""

import httpx

from infra_brain.callbacks.write_gate import gate_external_write
from infra_brain.config import get_settings


def _headers() -> dict:
    return {"PRIVATE-TOKEN": get_settings().gitlab_token}


def _base(project_id: int) -> str:
    return f"{get_settings().gitlab_url.rstrip('/')}/api/v4/projects/{project_id}"


def create_gitlab_issue(
    project_id: int,
    title: str,
    description: str = "",
    labels: list[str] | None = None,
) -> dict:
    """
    Open a GitLab issue. Idempotent by exact title match against currently
    open issues (first page of `state=opened` results only — see test file
    notes for why paginating further wasn't worth it here) — if a match is
    found, return it with `_already_existed=True` instead of filing a
    duplicate. Otherwise POST a new issue and return the created issue dict
    (includes `web_url`, matching `create_inventory_mr`'s return convention).
    """
    gate_external_write(
        system="gitlab",
        operation="create_gitlab_issue",
        payload="\n".join([title, description] + (labels or [])),
        agent_name="gitlab_issue",
    )
    ssl = get_settings().gitlab_ssl_verify
    timeout = get_settings().api_timeout_seconds

    existing = httpx.get(
        f"{_base(project_id)}/issues",
        headers=_headers(),
        params={"state": "opened"},
        verify=ssl,
        timeout=timeout,
    )
    # The idempotency guarantee below depends on this GET actually succeeding —
    # GitLab's issues API has no server-side duplicate protection (unlike MRs),
    # so silently swallowing a non-200 here would create a duplicate issue with
    # no error surfaced anywhere. Fail loudly instead of falling through to POST.
    existing.raise_for_status()
    for issue in existing.json():
        if issue.get("title") == title:
            return {**issue, "_already_existed": True}

    payload: dict = {"title": title}
    if description:
        payload["description"] = description
    if labels:
        payload["labels"] = labels

    r = httpx.post(
        f"{_base(project_id)}/issues",
        headers=_headers(),
        json=payload,
        verify=ssl,
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def comment_on_gitlab_issue(project_id: int, issue_iid: int, body: str) -> dict:
    """
    Add a comment (note) to an existing GitLab issue. Not idempotent — each
    call is a genuinely new comment, so no pre-check is performed.
    """
    gate_external_write(
        system="gitlab",
        operation="comment_on_gitlab_issue",
        payload=body,
        agent_name="gitlab_issue",
    )
    ssl = get_settings().gitlab_ssl_verify
    timeout = get_settings().api_timeout_seconds

    r = httpx.post(
        f"{_base(project_id)}/issues/{issue_iid}/notes",
        headers=_headers(),
        json={"body": body},
        verify=ssl,
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()
