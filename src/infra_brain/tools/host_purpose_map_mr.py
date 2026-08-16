"""host_purpose_map_mr.py — the SINGLE sanctioned GitLab-MR helper for the
curated ``host_purpose_map.yml`` (a dedicated fleet-inventory GitLab project).

Both write paths call THIS helper, so there is exactly one place that knows how
to turn a single host's purpose/VLAN/subnet into a GitLab MR:

  * **Human persistence** (``proposed=False``) — a dashboard edit that has
    ALREADY been applied to the infra-brain DB; the MR persists it into version
    control.
  * **Agent proposal** (``proposed=True``) — an agent-inferred change; the MR
    is opened for a human to merge and **no DB write happens here**.

Safety (why this is safe-by-construction): every outbound write is performed by
``infra_brain.tools.gitlab_mr.create_inventory_mr`` — the ONLY sanctioned GitLab
write path — which calls ``gate_external_write`` (DLP scan + durable audit,
**fail-closed**) BEFORE any ``httpx.post``/``put`` leaves the process. This
helper does NOT reimplement any GitLab plumbing or the gate; it only builds the
new file content and delegates. It additionally writes one ``AgentActionLog``
row per call, mirroring ``agents/inventory_mr.py``.

The current file is READ through the same read-only GitLab client the other
tools use (``tools.gitlab.gitlab_get`` -> ``readonly_get``); a missing file is
treated as an empty map (start fresh), not an error.
"""

from __future__ import annotations

import base64
import logging
import re
import urllib.parse

import httpx
import yaml

from infra_brain.config import get_settings
from infra_brain.db.models import AgentActionLog
from infra_brain.db.session import get_session
from infra_brain.tools.gitlab import gitlab_get
from infra_brain.tools.gitlab_mr import create_inventory_mr
from infra_brain.tools.host_purpose_map import DEFAULT_FILE_PATH

logger = logging.getLogger(__name__)

# Default target project when ``host_purpose_map_project_id`` is unset (0 = idle,
# the config default). Production sets it to 6 (the fleet-inventory repo); we
# fall back to 6 so this helper always targets the curated map's real home.
_DEFAULT_PROJECT_ID = 6


def _load_render_yaml():
    """Return the seed script's ``render_yaml`` (canonical shape + header).

    ``scripts`` is an importable package under the repo root during tests, but a
    packaged deployment may not have it on ``sys.path`` — so fall back to loading
    the file directly by absolute path derived from this module's location. We
    reuse the seed renderer (rather than reinventing it) so the on-disk shape and
    header comment stay byte-identical to the seeded file.
    """
    try:
        from scripts.seed_host_purpose_map import render_yaml

        return render_yaml
    except ImportError:
        import importlib.util
        from pathlib import Path

        # src/infra_brain/tools/host_purpose_map_mr.py -> repo root is parents[3]
        seed_path = Path(__file__).resolve().parents[3] / "scripts" / "seed_host_purpose_map.py"
        spec = importlib.util.spec_from_file_location("_seed_host_purpose_map", seed_path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.render_yaml


def _slug(hostname: str) -> str:
    """Branch-safe slug for a hostname (lowercase, non-alnum -> single dash)."""
    slug = re.sub(r"[^a-z0-9]+", "-", hostname.lower()).strip("-")
    return slug or "host"


def _s(value) -> str:
    """Coerce a scalar to the string form ``render_yaml`` expects ("" for None)."""
    return "" if value is None else str(value)


def _fetch_current_map(project_id: int, file_path: str, ref: str) -> dict:
    """Return the ordered ``host_purpose_map`` mapping from the target file.

    Reuses the read-only GitLab client (``gitlab_get`` -> ``readonly_get``). A
    missing file (404) yields ``{}`` — the caller starts from an empty map. Any
    other HTTP error propagates (a configured-but-unreachable source is a real
    failure, matching the read side's AA-R-13 convention).
    """
    encoded = urllib.parse.quote(file_path, safe="")
    try:
        data = gitlab_get(f"/api/v4/projects/{project_id}/repository/files/{encoded}?ref={ref}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return {}
        raise

    if isinstance(data, dict) and data.get("encoding") == "base64":
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    elif isinstance(data, dict):
        content = data.get("content", "") or ""
    else:  # pragma: no cover - files API always returns a dict
        content = ""

    loaded = yaml.safe_load(content) or {}
    host_map = loaded.get("host_purpose_map") if isinstance(loaded, dict) else None
    return host_map if isinstance(host_map, dict) else {}


def _to_rows(host_map: dict) -> list[dict]:
    """Convert an ordered host->value mapping into ``render_yaml`` rows.

    Insertion order is preserved. Both value shapes are accepted: a flat
    ``purpose`` string (legacy) becomes ``{purpose, "", ""}``; a nested
    ``{purpose, vlan, subnet}`` dict is carried through. ``None`` fields render
    as "".
    """
    rows: list[dict] = []
    for hostname, value in host_map.items():
        if isinstance(value, dict):
            rows.append(
                {
                    "hostname": str(hostname),
                    "purpose": _s(value.get("purpose")),
                    "vlan": _s(value.get("vlan")),
                    "subnet": _s(value.get("subnet")),
                }
            )
        else:
            rows.append(
                {
                    "hostname": str(hostname),
                    "purpose": _s(value),
                    "vlan": "",
                    "subnet": "",
                }
            )
    return rows


def _open_mr_exists(project_id: int, branch: str) -> bool:
    """True if an OPEN MR already sources from *branch* (read-only GET).

    Used only to report ``action`` ("created" vs "updated"); the actual
    idempotency is enforced by ``create_inventory_mr`` itself.
    """
    encoded = urllib.parse.quote(branch, safe="")
    try:
        mrs = gitlab_get(
            f"/api/v4/projects/{project_id}/merge_requests?source_branch={encoded}&state=opened"
        )
    except httpx.HTTPStatusError:
        return False
    return bool(mrs)


def open_host_purpose_map_mr(
    hostname: str,
    *,
    purpose: str | None,
    vlan: str | None,
    subnet: str | None,
    actor: str,
    proposed: bool,
) -> dict:
    """Open OR refresh (idempotent) an MR updating one host in host_purpose_map.yml.

    Fetches the current file, sets ``map[hostname] = {purpose, vlan, subnet}``
    (all other hosts untouched, insertion order preserved), re-renders via the
    seed ``render_yaml``, and opens/refreshes the MR through the sanctioned
    ``create_inventory_mr`` (which invokes the fail-closed write-gate).

    This helper NEVER writes to the ``HostPurposeMap`` table — for both the
    human-persistence and agent-proposal paths, the DB is the caller's concern
    (human persistence writes the DB before calling; agent proposals write
    nothing until a human merges).

    Args:
        hostname: the host whose row is added/updated (also drives the branch slug).
        purpose/vlan/subnet: the values to store (``None`` -> blank in the file).
        actor: attribution for the commit/MR description and the audit row.
        proposed: ``False`` = human persistence branch/title; ``True`` = agent
            proposal branch/title.

    Returns:
        ``{"mr_url": str, "branch": str, "action": "created"|"updated",
        "file_path": str}``.

    Raises:
        ValueError: if *hostname* is empty.
        PermissionError: propagated unchanged from the write-gate on a deny
            (fail-closed — never swallowed).
        Exception: any other GitLab/transport failure, propagated unchanged.
    """
    if not hostname or not hostname.strip():
        raise ValueError("hostname is required")

    settings = get_settings()
    project_id = settings.host_purpose_map_project_id or _DEFAULT_PROJECT_ID
    file_path = settings.host_purpose_map_file_path or DEFAULT_FILE_PATH
    target_branch = settings.host_purpose_map_branch or "main"

    slug = _slug(hostname)
    fields = (
        f"| field | value |\n|---|---|\n"
        f"| purpose | {purpose or ''} |\n"
        f"| vlan | {vlan or ''} |\n"
        f"| subnet | {subnet or ''} |\n"
    )
    if proposed:
        branch = f"infra-brain/host-purpose-propose/{slug}"
        mr_title = f"propose(host-purpose): {hostname} purpose/VLAN"
        commit_message = f"propose(host-purpose): {hostname} purpose/VLAN"
        mr_description = (
            f"**Agent-inferred proposal** for `{hostname}`.\n\n"
            f"Proposed by: `{actor}`\n\n"
            f"{fields}\n"
            f"This is an agent-inferred proposal. **No database write has been made** — "
            f"the HostPurposeMap row is updated only when a human merges this MR."
        )
    else:
        branch = f"infra-brain/host-purpose/{slug}"
        mr_title = f"chore(host-purpose): persist {hostname} purpose/VLAN (via {actor})"
        commit_message = f"chore(host-purpose): persist {hostname} purpose/VLAN"
        mr_description = (
            f"Persists a dashboard edit for `{hostname}` that has **already been applied "
            f"to the infra-brain database**.\n\n"
            f"Persisted by: `{actor}`\n\n"
            f"{fields}"
        )

    # 1. Read current map (read-only). 2. Apply the single-host change. 3. Render.
    host_map = _fetch_current_map(project_id, file_path, target_branch)
    host_map[hostname] = {"purpose": purpose, "vlan": vlan, "subnet": subnet}
    new_content = _load_render_yaml()(_to_rows(host_map))

    # Report created-vs-updated by probing for an already-open MR on the branch.
    # (create_inventory_mr enforces the real idempotency; this is display-only.)
    action = "updated" if _open_mr_exists(project_id, branch) else "created"

    args_summary = (
        f"project={project_id} branch={branch} file={file_path} host={hostname} "
        f"proposed={proposed} actor={actor}"
    )
    log_status = "ok"
    log_error: str | None = None
    log_verdict = "allow"
    original_exc: Exception | None = None
    mr_url: str | None = None
    try:
        # Reuses create_inventory_mr's write-gate call — the write is sanctioned.
        mr_url = create_inventory_mr(
            project_id=project_id,
            branch_name=branch,
            file_path=file_path,
            new_content=new_content,
            commit_message=commit_message,
            mr_title=mr_title,
            mr_description=mr_description,
            source_branch=target_branch,
        )
        args_summary += f" mr_url={mr_url}"
        logger.info(
            "host_purpose_map_mr: %s MR %s for host %s (proposed=%s)",
            action,
            mr_url,
            hostname,
            proposed,
        )
    except PermissionError as exc:
        # Write-gate denial (fail-closed): record the REAL verdict, re-raise below.
        log_verdict = "deny"
        log_status = "error"
        log_error = str(exc)
        original_exc = exc
        logger.error("host_purpose_map_mr: MR for host %s BLOCKED by write gate: %s", hostname, exc)
    except Exception as exc:
        log_status = "error"
        log_error = str(exc)
        original_exc = exc
        logger.error("host_purpose_map_mr: failed to open MR for host %s: %s", hostname, exc)
    finally:
        # Mirror agents/inventory_mr.py: one durable AgentActionLog per call
        # (pass, fail, or deny). The write-gate writes its own row too.
        with get_session() as audit_session:
            audit_session.add(
                AgentActionLog(
                    agent="host_purpose_map_mr",
                    tool="create_inventory_mr",
                    args_summary=args_summary,
                    verdict=log_verdict,
                    status=log_status,
                    error=log_error,
                )
            )
            audit_session.commit()

    if original_exc is not None:
        # Re-raise the ORIGINAL exception unchanged — a write-gate PermissionError
        # must reach the caller as PermissionError (never swallowed or downgraded).
        raise original_exc

    return {
        "mr_url": mr_url,
        "branch": branch,
        "action": action,
        "file_path": file_path,
    }
