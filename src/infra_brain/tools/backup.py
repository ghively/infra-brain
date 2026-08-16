"""Backup-system API polling — read-only by construction (GET-only client).

GitLab issue #96: poll a configured backup system (Veeam, Bacula, cloud
snapshot API) for each protected resource's last-successful-backup timestamp
and last DR/chaos restore-test date. No live target of any backend is
configured in this dev environment, so this module is deliberately
pluggable/generic rather than a Veeam- or Bacula-specific client:

  - ``backup_backend`` (config.py) selects which field-mapping in
    ``_BACKEND_FIELD_MAPS`` is applied to the polled JSON.
  - The polled endpoint is assumed to return a JSON list of per-job status
    objects at ``<base_url>/api/jobs`` — every backend covered here (Veeam
    Enterprise Manager's REST API, a Bacula web-UI/bconsole bridge, or a
    cloud provider's snapshot-policy status API) exposes some equivalent
    "list of job/policy statuses" read endpoint; the exact field NAMES differ
    per product, which is what the field map normalizes away.
  - ``_normalize_job`` maps a raw per-backend record into the common shape
    BackupAgent expects: {job_name, resource_name, last_success_at,
    last_restore_test_at, retention_days, status}. Unmapped/missing fields
    degrade to None rather than raising, so a backend whose response is
    missing e.g. restore-test data (many are) still yields a job row with
    that field blank instead of failing the whole poll.

Matches the read-only conventions used by tools/rapid7.py and tools/snmp.py:
a GET-only ``ReadOnlyClient``, no fabricated data when unconfigured or
unreachable.
"""

import logging

from langchain_core.tools import tool

from infra_brain.config import get_settings
from infra_brain.tools.http_readonly import ReadOnlyClient, tool_http_errors

logger = logging.getLogger(__name__)

# Common shape every backend's raw per-job record is normalized into.
_NORMALIZED_FIELDS = (
    "job_name",
    "resource_name",
    "last_success_at",
    "last_restore_test_at",
    "retention_days",
    "status",
)

# Per-backend raw-field -> normalized-field name mapping. Extend this dict to
# add a new backend without touching the polling/normalization logic below.
_BACKEND_FIELD_MAPS: dict[str, dict[str, str]] = {
    "veeam": {
        "job_name": "name",
        "resource_name": "objectName",
        "last_success_at": "lastSuccessTime",
        "last_restore_test_at": "lastSureBackupTime",
        "retention_days": "retentionDays",
        "status": "lastResult",
    },
    "bacula": {
        "job_name": "job_name",
        "resource_name": "client",
        "last_success_at": "last_success",
        "last_restore_test_at": "last_restore_test",
        "retention_days": "retention_days",
        "status": "job_status",
    },
    "cloud_snapshot": {
        "job_name": "policy_id",
        "resource_name": "resource_name",
        "last_success_at": "last_snapshot_at",
        "last_restore_test_at": "last_restore_test_at",
        "retention_days": "retention_days",
        "status": "state",
    },
}


def _normalize_job(backend: str, raw: dict) -> dict:
    """Map one raw per-backend job record into the common normalized shape.

    Unknown backend -> identity mapping (assumes the raw record already uses
    the normalized field names) so a locally-fronted/aggregator endpoint that
    already speaks the common shape works without an entry in
    ``_BACKEND_FIELD_MAPS``.
    """
    field_map = _BACKEND_FIELD_MAPS.get(backend, {f: f for f in _NORMALIZED_FIELDS})
    return {field: raw.get(raw_key) for field, raw_key in field_map.items()}


@tool
@tool_http_errors
def backup_jobs_tool(
    base_url: str,
    backend: str,
    ssl_verify: bool = True,
    timeout: float | None = None,
) -> list[dict]:
    """Poll a backup system's job-status endpoint (read-only GET) and return
    normalized job dicts: {job_name, resource_name, last_success_at,
    last_restore_test_at, retention_days, status}.

    Returns [] if ``base_url``/``backend`` is empty (nothing to poll) rather
    than raising — callers that need a hard failure on misconfiguration
    should check settings before invoking this tool.

    Credentials are read from settings inside this function, never taken as
    a tool argument — tool-call arguments are persisted verbatim into
    AuditLog/AgentActionLog (see loadbalancer.py / secrets_inventory.py for
    the same convention).
    """
    if not base_url or not backend:
        return []

    settings = get_settings()
    if timeout is None:
        timeout = settings.api_timeout_seconds

    api_key = settings.backup_api_key
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = f"{base_url.rstrip('/')}/api/jobs"

    with ReadOnlyClient(verify=ssl_verify, timeout=timeout) as client:
        response = client.get(url, headers=headers)
    response.raise_for_status()
    payload = response.json()

    raw_jobs = payload if isinstance(payload, list) else payload.get("jobs", [])
    jobs = []
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            continue
        jobs.append(_normalize_job(backend, raw))
    return jobs
