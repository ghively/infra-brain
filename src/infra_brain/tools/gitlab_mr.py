"""
GitLab MR creation — the ONLY write path to version-controlled infra repos.
Creates a branch, commits changed file, opens an MR. Never pushes to main.
"""

import urllib.parse

import httpx

from infra_brain.callbacks.write_gate import gate_external_write
from infra_brain.config import get_settings


def _headers() -> dict:
    return {"PRIVATE-TOKEN": get_settings().gitlab_token}


def _base(project_id: int) -> str:
    return f"{get_settings().gitlab_url.rstrip('/')}/api/v4/projects/{project_id}"


def create_inventory_mr(
    project_id: int,
    branch_name: str,
    file_path: str,
    new_content: str,
    commit_message: str,
    mr_title: str,
    mr_description: str,
    source_branch: str = "main",
) -> str:
    """
    Propose an inventory change via GitLab MR.

    1. Create branch from source_branch (idempotent — skips if branch exists: 400/409)
    2. Commit file to branch (create or update)
    3. Open MR (idempotent — returns existing open MR URL if already present)

    Returns the MR web URL.
    This is the ONLY sanctioned GitLab write path in infra-brain.
    """
    # F-004.3: pre-write DLP + audit gate — refuses BEFORE any httpx.post.

    gate_external_write(
        system="gitlab",
        operation="create_inventory_mr",
        payload="\n".join(
            [branch_name, file_path, new_content, commit_message, mr_title, mr_description]
        ),
        agent_name="gitlab_mr",
    )
    ssl = get_settings().gitlab_ssl_verify
    timeout = get_settings().api_timeout_seconds

    # Step 1: Create branch (ignore 400/409 if already exists)
    try:
        r = httpx.post(
            f"{_base(project_id)}/repository/branches",
            headers=_headers(),
            json={"branch": branch_name, "ref": source_branch},
            verify=ssl,
            timeout=timeout,
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (400, 409):
            raise

    # Step 2: Create or update file on branch
    encoded_path = urllib.parse.quote(file_path, safe="")
    file_url = f"{_base(project_id)}/repository/files/{encoded_path}"
    check = httpx.get(
        f"{file_url}?ref={branch_name}",
        headers=_headers(),
        verify=ssl,
        timeout=timeout,
    )
    file_payload = {
        "branch": branch_name,
        "content": new_content,
        "commit_message": commit_message,
    }
    if check.status_code == 200:
        r = httpx.put(
            file_url,
            headers=_headers(),
            json=file_payload,
            verify=ssl,
            timeout=timeout,
        )
    else:
        r = httpx.post(
            file_url,
            headers=_headers(),
            json=file_payload,
            verify=ssl,
            timeout=timeout,
        )
    r.raise_for_status()

    # Step 3: Open MR (idempotent — check for existing open MR first)
    existing = httpx.get(
        f"{_base(project_id)}/merge_requests",
        headers=_headers(),
        params={"source_branch": branch_name, "state": "opened"},
        verify=ssl,
        timeout=timeout,
    )
    if existing.status_code == 200 and existing.json():
        return existing.json()[0]["web_url"]

    mr = httpx.post(
        f"{_base(project_id)}/merge_requests",
        headers=_headers(),
        json={
            "source_branch": branch_name,
            "target_branch": source_branch,
            "title": mr_title,
            "description": mr_description,
            "remove_source_branch": True,
        },
        verify=ssl,
        timeout=timeout,
    )
    mr.raise_for_status()
    return mr.json()["web_url"]
