"""
Git-backed, never-delete script store (Task F1).

Every script generated or run by the agent is persisted to the DB.
When a GitLab script-library repo is configured, the script file is also
committed there.  DB persistence is NEVER conditional on GitLab being
reachable — a GitLab failure logs a warning and leaves git fields empty.
"""

import hashlib
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import httpx

from infra_brain.config import get_settings
from infra_brain.db.models import GeneratedScript
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)

# Map language name to file extension
_EXT: dict[str, str] = {
    "powershell": "ps1",
    "python": "py",
    "bash": "sh",
}


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _commit_script_to_gitlab(
    content: str,
    name: str,
    language: str,
    domain: str,
    sha256: str,
) -> tuple[str, str, str]:
    """
    Commit script file to the GitLab script-library repo via direct file API.

    Returns (git_repo, git_path, git_commit_sha) on success.
    Raises on any network/HTTP error — caller must catch and log.
    """
    s = get_settings()
    project_id = s.script_library_project_id
    branch = s.script_library_branch
    ssl = s.gitlab_ssl_verify
    timeout = s.api_timeout_seconds

    headers = {"PRIVATE-TOKEN": s.gitlab_token}
    base = f"{s.gitlab_url.rstrip('/')}/api/v4/projects/{project_id}"

    ext = _EXT.get(language, "txt")
    sha8 = sha256[:8]
    file_path = f"scripts/{domain}/{name}-{sha8}.{ext}"
    encoded_path = urllib.parse.quote(file_path, safe="")
    file_url = f"{base}/repository/files/{encoded_path}"

    commit_message = f"chore(scripts): add {name} [{language}] sha={sha8}"
    file_payload = {
        "branch": branch,
        "content": content,
        "commit_message": commit_message,
    }

    # Check if the file already exists on the target branch
    check = httpx.get(
        f"{file_url}?ref={branch}",
        headers=headers,
        verify=ssl,
        timeout=timeout,
    )
    if check.status_code == 200:
        r = httpx.put(file_url, headers=headers, json=file_payload, verify=ssl, timeout=timeout)
    else:
        r = httpx.post(file_url, headers=headers, json=file_payload, verify=ssl, timeout=timeout)
    r.raise_for_status()

    # The create/update file endpoints only ever return {file_path, branch} --
    # no commit id, on any GitLab version (verified live against the actual
    # instance). The branch's latest commit *is* the commit we just made.
    commit_resp = httpx.get(
        f"{base}/repository/commits/{urllib.parse.quote(branch, safe='')}",
        headers=headers,
        verify=ssl,
        timeout=timeout,
    )
    commit_sha = commit_resp.json().get("id", "") if commit_resp.status_code == 200 else ""
    git_repo = f"{s.gitlab_url.rstrip('/')}/{project_id}"
    return git_repo, file_path, commit_sha


def save_script(
    name: str,
    language: str,
    purpose: str,
    content: str,
    created_by_agent: str,
    domain: str,
    stdout: str = "",
    returncode: Optional[int] = None,
) -> GeneratedScript:
    """
    Upsert a script by content_sha256 and return the DB row.

    - If a row with the same hash exists: bump run_count, update last_run_at,
      last_stdout, last_returncode.
    - Otherwise: insert a new row.
    - Then attempt to commit the file to the GitLab script-library repo (if
      script_library_project_id > 0).  GitLab failure is non-fatal — the DB
      row is always committed first.
    """
    sha256 = _sha256(content)
    now = datetime.now(timezone.utc)

    with get_session() as session:
        existing = (
            session.query(GeneratedScript).filter(GeneratedScript.content_sha256 == sha256).first()
        )
        if existing:
            existing.run_count += 1
            existing.last_run_at = now
            existing.last_stdout = stdout
            existing.last_returncode = returncode
            session.flush()
            row = existing
        else:
            row = GeneratedScript(
                name=name,
                language=language,
                purpose=purpose,
                content=content,
                content_sha256=sha256,
                created_by_agent=created_by_agent,
                domain=domain,
                run_count=1,
                last_run_at=now,
                last_stdout=stdout,
                last_returncode=returncode,
                git_repo="",
                git_path="",
                git_commit_sha="",
            )
            session.add(row)
            session.flush()

        s = get_settings()
        if s.script_library_project_id > 0 and not existing:
            # Only commit to GitLab on initial insert (not on dedup bumps)
            try:
                git_repo, git_path, git_commit_sha = _commit_script_to_gitlab(
                    content=content,
                    name=name,
                    language=language,
                    domain=domain,
                    sha256=sha256,
                )
                row.git_repo = git_repo
                row.git_path = git_path
                row.git_commit_sha = git_commit_sha
                session.flush()
            except Exception as exc:
                logger.warning(
                    "scripts_store: GitLab commit failed for %s/%s — DB row saved without git fields: %s",
                    domain,
                    name,
                    exc,
                )

        session.commit()
        # Detach-safe: read scalar fields before session closes
        result = GeneratedScript(
            id=row.id,
            name=row.name,
            language=row.language,
            purpose=row.purpose,
            content=row.content,
            content_sha256=row.content_sha256,
            created_by_agent=row.created_by_agent,
            domain=row.domain,
            git_repo=row.git_repo,
            git_path=row.git_path,
            git_commit_sha=row.git_commit_sha,
            last_run_at=row.last_run_at,
            run_count=row.run_count,
            last_stdout=row.last_stdout,
            last_returncode=row.last_returncode,
            reusable=row.reusable,
            created_at=row.created_at,
        )

    return result


def find_reusable(purpose: str, language: str) -> list[GeneratedScript]:
    """
    Return prior reusable GeneratedScript rows matching purpose and language.
    """
    with get_session() as session:
        rows = (
            session.query(GeneratedScript)
            .filter(
                GeneratedScript.purpose == purpose,
                GeneratedScript.language == language,
                GeneratedScript.reusable.is_(True),
            )
            .all()
        )
        # Detach-safe copies
        results = []
        for row in rows:
            results.append(
                GeneratedScript(
                    id=row.id,
                    name=row.name,
                    language=row.language,
                    purpose=row.purpose,
                    content=row.content,
                    content_sha256=row.content_sha256,
                    created_by_agent=row.created_by_agent,
                    domain=row.domain,
                    git_repo=row.git_repo,
                    git_path=row.git_path,
                    git_commit_sha=row.git_commit_sha,
                    last_run_at=row.last_run_at,
                    run_count=row.run_count,
                    last_stdout=row.last_stdout,
                    last_returncode=row.last_returncode,
                    reusable=row.reusable,
                    created_at=row.created_at,
                )
            )
        return results
