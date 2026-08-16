"""SecretsInventoryAgent — secrets-manager INVENTORY, metadata only (issue #98).

WHAT THIS COLLECTS
==================
For each secret held in an external secrets manager: that it EXISTS, its
path/name, when it was last rotated, and which system/service-account
references it. Nothing else.

WHAT THIS MUST NEVER DO
=======================
Fetch a secret VALUE. This is a stricter bar than the repo-wide read-only-GET
boundary (docs/READONLY-MODEL.md): a GET against Vault's ``/v1/<mount>/data/…``
is a perfectly read-only request and would sail through every existing layer —
the GET-only client (tools/http_readonly.py), the boundary gate
(callbacks/boundary.py), and the audit chain — while handing us the plaintext
secret. So read-only-ness is NOT sufficient here and the enforcement is
different in kind:

  1. STRUCTURAL, ALLOW-LIST: ``_assert_metadata_only()`` below is a fail-closed
     allow-list of endpoint SHAPES. A URL that does not match a known
     metadata-only endpoint raises ``SecretValueEndpointError`` BEFORE the
     request is issued. It is an allow-list, not a deny-list, precisely so that
     an endpoint nobody thought about is refused rather than permitted.
  2. NO REACHABLE VALUE PATH: there is no function in this module — not even a
     defensive/fallback/error-handling one — that constructs a value-returning
     URL. There is deliberately no "fetch it, then discard the value" code
     path: a value that never enters the process cannot leak into a log line,
     an audit row, a DLP-scanned string, an exception message, or a traceback
     local. AWS Secrets Manager is NOT implemented for this same reason (see
     ``SecretsInventoryAgent._collect_unsupported`` — its API is POST-only and
     cannot use the GET-only client).
  3. NO RAW BODIES ANYWHERE: see "Logging discipline" below.
  4. NO VALUE-SHAPED STORAGE: ``SecretRecord``
     (db/models/secrets_inventory.py) has no value/hash/JSONB column, so even a
     hypothetical value in memory has nowhere to land.

LOGGING DISCIPLINE (the leak vector that matters most)
======================================================
A generic "log the response for debugging" line is exactly how a secret value
reaches a log file. Therefore, in this module:

  * ``response.text`` / ``response.json()`` results are NEVER interpolated into
    a log, exception, audit, or error string. Not on the happy path, not in an
    ``except`` block, not at DEBUG level.
  * The only response-derived things allowed into a message are the HTTP status
    code and the request path — both of which are ours by construction.
  * Parsed responses are PROJECTED immediately: each ``_parse_*`` helper copies
    the small, named, metadata-only fields it needs into a fresh dict and drops
    the rest. The raw body object is never returned upward, so no caller can
    stringify it.
  * Per-endpoint justification that the body is metadata-only by construction:
      - Vault ``GET /v1/<mount>/metadata/<path>?list=true`` returns
        ``{"data": {"keys": [...]}}`` — key NAMES only. Vault has no mode in
        which the metadata mount returns version data; values live exclusively
        under the ``data/`` mount, which the allow-list refuses.
      - Vault ``GET /v1/<mount>/metadata/<path>`` returns created/updated
        timestamps, version bookkeeping and ``custom_metadata``. Operator-set
        ``custom_metadata`` is the one field a careless operator could stuff a
        secret into, so we read exactly two known keys from it and cap their
        length — we never copy the whole dict.
      - Bitwarden Secrets Manager ``GET /api/organizations/<org>/secrets``
        returns the list projection (id/key/creationDate/revisionDate/projects)
        with NO ``value`` field; the value is only served by the per-secret
        ``GET /api/secrets/<id>`` endpoint, which the allow-list refuses.
    Even so, none of these bodies is ever logged — the justification is why the
    PROJECTION is safe, not a licence to print the body.

Unconfigured (the default, and the state of this dev environment) degrades to
``CollectorSkipped`` — a "skipped" collection run, not a fake-healthy empty
"completed" one — mirroring eol.py/net.py.
"""

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from langchain_core.tools import tool

from infra_brain.agents.base import BaseAgent
from infra_brain.config import get_settings
from infra_brain.db.models import BACKEND_BITWARDEN, BACKEND_VAULT, SecretRecord
from infra_brain.db.session import get_session
from infra_brain.etl.base import CollectorSkipped, CollectOutcome
from infra_brain.etl.spec import AgentSpec, Tier
from infra_brain.tools.http_readonly import ReadOnlyClient, tool_http_errors

logger = logging.getLogger(__name__)

# Supported backend identifiers for ``settings.secrets_inventory_backend``.
_SUPPORTED_BACKENDS = frozenset({BACKEND_VAULT, BACKEND_BITWARDEN})

# Cap on any operator-supplied metadata string we persist (referencing_system),
# so a mis-used custom_metadata field can never dump a large blob into the DB.
_MAX_METADATA_LEN = 256


class SecretValueEndpointError(PermissionError):
    """A URL outside the metadata-only allow-list was about to be requested.

    A ``PermissionError`` on purpose: that is the exception type the R2 boundary
    treats as a hard denial (see tools/http_readonly.ReadOnlyHTTPError and
    ``tool_http_errors``, which deliberately re-raises PermissionError rather
    than softening it into a ToolException).
    """


def _assert_metadata_only(url: str) -> None:
    """Fail closed unless *url* is a known METADATA-ONLY endpoint shape.

    ALLOW-LIST, not deny-list. Anything unrecognized raises. This runs before
    every request this module makes; there is no request path that skips it.

    Allowed shapes:
      * Vault KV-v2 metadata:  ``/v1/<mount…>/metadata[/<secret path…>]``
        The KV "verb" slot is the first ``data``-or-``metadata`` segment after
        ``v1``; it must be ``metadata``. A secret literally NAMED "data" nested
        below that slot is still fine, because only the FIRST occurrence
        decides. ``/v1/<mount…>/data/…`` — the value endpoint — is refused.
      * Bitwarden Secrets Manager org listing:
        ``/api/organizations/<org id>/secrets``
        The value-bearing ``/api/secrets/<id>`` shape is refused (it has no
        ``organizations`` segment, so it simply never matches).

    Traversal guard (found in self-review, and it was a REAL reachable value
    path): the secret names fed into these URLs come from the upstream LIST
    response, not from us. A key named ``../../data/foo`` would build
    ``/v1/kv/metadata/apps/../../data/foo`` — whose FIRST data-or-metadata
    segment is ``metadata``, so the segment check alone would have allowed it,
    while Vault normalizes the path on receipt and serves
    ``/v1/kv/data/foo``: the value endpoint. Dot segments, backslashes and
    percent-encoding (which could hide either) are therefore rejected outright
    before the shape check. None of them is ever needed by a legitimate secret
    path, so refusing them costs nothing and closes the hole.
    """
    parts = urlsplit(url)
    raw_path = parts.path
    if "%" in raw_path or "\\" in raw_path:
        raise SecretValueEndpointError(
            f"[secrets-inventory] refused path with percent-encoding or backslash "
            f"{raw_path!r}: it could hide a traversal into the value endpoint"
        )
    segments = [s for s in raw_path.split("/") if s]
    if any(s in (".", "..") for s in segments):
        raise SecretValueEndpointError(
            f"[secrets-inventory] refused path with dot segments {raw_path!r}: "
            "server-side normalization could redirect it to the value endpoint"
        )

    # Vault
    if segments[:1] == ["v1"]:
        for seg in segments[1:]:
            if seg in ("data", "metadata"):
                if seg == "metadata":
                    return
                break
        raise SecretValueEndpointError(
            f"[secrets-inventory] refused non-metadata Vault path {parts.path!r}: "
            "only /v1/<mount>/metadata/... may be requested"
        )

    # Bitwarden Secrets Manager
    if (
        len(segments) == 4
        and segments[0] == "api"
        and segments[1] == "organizations"
        and segments[3] == "secrets"
    ):
        return

    raise SecretValueEndpointError(
        f"[secrets-inventory] refused unrecognized path {parts.path!r}: "
        "endpoint is not on the metadata-only allow-list"
    )


def _get_metadata(url: str, *, headers: dict, params: dict | None = None):
    """Issue the ONE kind of request this module is allowed to make.

    Every caller goes through here, and here goes through
    ``_assert_metadata_only`` first, so the allow-list cannot be bypassed by
    adding another call site. Uses ``ReadOnlyClient`` (GET/HEAD only) and does
    not follow redirects — an upstream 3xx must never silently relocate a
    metadata request onto some other (possibly value-bearing) path.

    Raises for a non-2xx status with the status code and path only — NEVER the
    response body (see the module docstring's logging discipline).
    """
    _assert_metadata_only(url)
    settings = get_settings()
    with ReadOnlyClient(
        verify=settings.secrets_inventory_ssl_verify,
        timeout=settings.api_timeout_seconds,
        follow_redirects=False,
    ) as client:
        response = client.get(url, headers=headers, params=params)
    if response.status_code >= 400:
        # Status + path only. The body of a 4xx from a secrets manager is not
        # known to be value-free by construction, so it is not touched at all.
        raise RuntimeError(
            f"[secrets-inventory] HTTP {response.status_code} for {urlsplit(url).path}"
        )
    try:
        return response.json()
    except Exception as exc:
        # Re-raised WITHOUT chaining (``from None``) and without the original
        # message: a decoder's error text can embed a fragment of the document
        # it failed on, and a chained __cause__ would carry that fragment into
        # any traceback that gets logged. Only the exception TYPE is reported.
        raise RuntimeError(
            f"[secrets-inventory] unparseable response ({type(exc).__name__}) "
            f"for {urlsplit(url).path}"
        ) from None


def _headers(backend: str, token: str) -> dict:
    if backend == BACKEND_VAULT:
        return {"X-Vault-Token": token}
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _clip(value: object) -> str | None:
    """Project an operator-set metadata string: str-coerce, strip, length-cap."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:_MAX_METADATA_LEN]


def _parse_iso(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --- tools -------------------------------------------------------------------
#
# Tools take NO credentials as arguments — they read the token from settings
# themselves. Passing a token as a tool argument would put it into the
# LangChain tool ``input_str`` that the audit/DLP callbacks serialize, i.e. it
# would write a live credential into the audit trail. Arguments here are
# path/identifier only.


@tool
@tool_http_errors
def vault_list_secret_names_tool(mount_path: str) -> list[str]:
    """List the secret NAMES under a Vault KV-v2 mount path (metadata only).

    Calls ``GET /v1/<mount_path>/metadata/?list=true``, which returns key names
    only. The value endpoint (``/v1/<mount>/data/...``) is refused by
    ``_assert_metadata_only`` and is never constructed anywhere in this module.
    """
    settings = get_settings()
    mount, _, prefix = mount_path.partition("/")
    url = f"{settings.secrets_inventory_url.rstrip('/')}/v1/{mount}/metadata/{prefix}".rstrip("/")
    body = _get_metadata(
        url,
        headers=_headers(BACKEND_VAULT, settings.secrets_inventory_token),
        params={"list": "true"},
    )
    # Projected immediately: only the key-name list survives this function.
    keys = ((body or {}).get("data") or {}).get("keys") or []
    return [str(k) for k in keys if k]


@tool
@tool_http_errors
def vault_secret_metadata_tool(mount_path: str, secret_name: str) -> dict:
    """Read one Vault KV-v2 secret's METADATA (rotation time + custom metadata).

    Calls ``GET /v1/<mount_path>/metadata/<secret_name>``. That endpoint returns
    timestamps, version bookkeeping and operator-set ``custom_metadata``; it
    cannot return version data — values are served only from the ``data/``
    mount, which the allow-list refuses.

    Returns a PROJECTION: ``{"updated_time", "created_time",
    "referencing_system"}``. The raw body is deliberately not returned, so no
    caller can stringify it into a log.
    """
    settings = get_settings()
    mount, _, prefix = mount_path.partition("/")
    base = f"{settings.secrets_inventory_url.rstrip('/')}/v1/{mount}/metadata"
    path = f"{prefix}/{secret_name}".strip("/")
    body = _get_metadata(
        f"{base}/{path}",
        headers=_headers(BACKEND_VAULT, settings.secrets_inventory_token),
    )
    data = (body or {}).get("data") or {}
    custom = data.get("custom_metadata") or {}
    # Two known keys only — never the whole custom_metadata dict, since that is
    # the one field an operator could have stuffed arbitrary content into.
    referencing = custom.get("referencing_system") or custom.get("service_account")
    return {
        "updated_time": data.get("updated_time"),
        "created_time": data.get("created_time"),
        "referencing_system": _clip(referencing),
    }


@tool
@tool_http_errors
def bitwarden_list_secret_metadata_tool(organization_id: str) -> list[dict]:
    """List an organization's secrets from Bitwarden Secrets Manager (metadata only).

    Calls ``GET /api/organizations/<organization_id>/secrets``, the LIST
    projection — it carries id/key/creationDate/revisionDate/projects and has no
    ``value`` field at all. The value-bearing per-secret endpoint
    (``GET /api/secrets/<id>``) is refused by the allow-list and is never
    constructed anywhere in this module.

    Returns a PROJECTION per secret: ``{"key", "revision_date",
    "referencing_system"}``.
    """
    settings = get_settings()
    url = (
        f"{settings.secrets_inventory_url.rstrip('/')}"
        f"/api/organizations/{organization_id}/secrets"
    )
    body = _get_metadata(url, headers=_headers(BACKEND_BITWARDEN, settings.secrets_inventory_token))
    rows = (body or {}).get("data") or []
    projected: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        projects = row.get("projects") or []
        referencing = projects[0].get("name") if projects and isinstance(projects[0], dict) else None
        projected.append(
            {
                "key": _clip(row.get("key")),
                "revision_date": row.get("revisionDate"),
                "referencing_system": _clip(referencing),
            }
        )
    return projected


# --- agent -------------------------------------------------------------------


class SecretsInventoryAgent(BaseAgent):
    """Metadata-only inventory of an external secrets manager (issue #98).

    Backend is chosen by ``settings.secrets_inventory_backend``; unconfigured
    (the default) raises ``CollectorSkipped`` so the run records "skipped"
    rather than a fake-healthy empty "completed".
    """

    spec = AgentSpec(
        domain="secrets_inventory",
        tier=Tier.COLLECTOR,
        schedule="10 5 * * *",
        max_staleness=timedelta(hours=26),
    )

    def collect(self, scope: str = "all") -> CollectOutcome:
        backend = (self.settings.secrets_inventory_backend or "").strip().lower()
        if not backend:
            raise CollectorSkipped("secrets_inventory_backend not configured")
        if backend not in _SUPPORTED_BACKENDS:
            return self._collect_unsupported(backend)
        if not self.settings.secrets_inventory_url or not self.settings.secrets_inventory_token:
            raise CollectorSkipped(
                f"secrets_inventory backend '{backend}' configured without url/token"
            )

        targets = [p.strip() for p in (self.settings.secrets_inventory_paths or "").split(",")]
        targets = [p for p in targets if p]
        if not targets:
            raise CollectorSkipped(
                f"secrets_inventory backend '{backend}' configured without "
                "secrets_inventory_paths"
            )

        if backend == BACKEND_VAULT:
            items, errors = self._collect_vault(targets)
        else:
            items, errors = self._collect_bitwarden(targets)

        self._records = items
        return CollectOutcome(items=items, errors=errors)

    @staticmethod
    def _collect_unsupported(backend: str) -> CollectOutcome:
        """AWS Secrets Manager (and any other unknown backend) is NOT implemented.

        This is a safety decision, not an oversight. AWS Secrets Manager's API
        is a SigV4-signed POST JSON-1.1 protocol — ``ListSecrets`` and
        ``DescribeSecret`` are metadata-only operations, but they are POSTs, so
        they cannot go through the GET-only ``ReadOnlyClient`` that gives this
        module its structural guarantee. Wiring in a POST-capable client (or
        boto3, which is POST-capable by construction) would put a client
        one-parameter-change away from ``GetSecretValue`` inside the very module
        whose whole point is that no such path exists. Adding AWS support
        therefore needs its own design + safety review, not a quiet extension
        here. Until then an ``aws`` setting is skipped loudly.
        """
        raise CollectorSkipped(
            f"secrets_inventory backend '{backend}' is not supported — see "
            "SecretsInventoryAgent._collect_unsupported (AWS requires a "
            "POST-capable client, which this module deliberately does not hold)"
        )

    def _collect_vault(self, mounts: list[str]) -> tuple[list[dict], list[str]]:
        _cb = {"callbacks": self.callbacks}
        cap = self.settings.secrets_inventory_max_secrets
        items: list[dict] = []
        errors: list[str] = []
        for mount_path in mounts:
            try:
                names = vault_list_secret_names_tool.invoke({"mount_path": mount_path}, config=_cb)
            except Exception as exc:
                # ``exc`` here can only carry a status code / path / transport
                # message — _get_metadata never puts a response body into the
                # error it raises, and no value-returning call exists to fail.
                logger.warning("secrets_inventory: vault list failed for %s: %s", mount_path, exc)
                errors.append(f"vault list failed for {mount_path}: {exc}")
                continue
            for name in names:
                if len(items) >= cap:
                    errors.append(f"secrets_inventory_max_secrets cap ({cap}) reached — truncated")
                    return items, errors
                if name.endswith("/"):
                    # A nested folder, not a secret. Not recursed into: keeping
                    # the traversal one level deep keeps the URL space small and
                    # fully predictable for the allow-list.
                    continue
                meta = self._vault_metadata(mount_path, name, _cb, errors)
                items.append(
                    self._item(
                        backend=BACKEND_VAULT,
                        secret_path=f"{mount_path.rstrip('/')}/{name}",
                        last_rotated=_parse_iso(meta.get("updated_time"))
                        or _parse_iso(meta.get("created_time")),
                        referencing_system=meta.get("referencing_system"),
                    )
                )
        return items, errors

    @staticmethod
    def _vault_metadata(mount_path: str, name: str, _cb: dict, errors: list[str]) -> dict:
        """Per-secret metadata read; a failure degrades that ONE secret to
        existence-only (still recorded), never aborts the whole mount.

        A ``SecretValueEndpointError`` here means the allow-list refused the URL
        this secret's NAME would have produced (traversal-shaped key, etc.). It
        is caught rather than re-raised on purpose — one malformed key must not
        abort the whole mount — but it is logged at ERROR and recorded in
        ``errors`` (which flips the run to status="partial"), because it means
        something upstream is shaped wrongly and someone should look.
        """
        try:
            return vault_secret_metadata_tool.invoke(
                {"mount_path": mount_path, "secret_name": name}, config=_cb
            )
        except SecretValueEndpointError as exc:
            logger.error(
                "secrets_inventory: REFUSED metadata URL for %s/%s (allow-list): %s",
                mount_path,
                name,
                exc,
            )
            errors.append(f"metadata URL refused by allow-list for {mount_path}/{name}: {exc}")
            return {}
        except Exception as exc:
            logger.warning(
                "secrets_inventory: vault metadata read failed for %s/%s: %s",
                mount_path,
                name,
                exc,
            )
            errors.append(f"vault metadata read failed for {mount_path}/{name}: {exc}")
            return {}

    def _collect_bitwarden(self, org_ids: list[str]) -> tuple[list[dict], list[str]]:
        _cb = {"callbacks": self.callbacks}
        cap = self.settings.secrets_inventory_max_secrets
        items: list[dict] = []
        errors: list[str] = []
        for org_id in org_ids:
            try:
                rows = bitwarden_list_secret_metadata_tool.invoke(
                    {"organization_id": org_id}, config=_cb
                )
            except Exception as exc:
                logger.warning("secrets_inventory: bitwarden list failed for %s: %s", org_id, exc)
                errors.append(f"bitwarden list failed for {org_id}: {exc}")
                continue
            for row in rows:
                if len(items) >= cap:
                    errors.append(f"secrets_inventory_max_secrets cap ({cap}) reached — truncated")
                    return items, errors
                key = row.get("key")
                if not key:
                    continue
                items.append(
                    self._item(
                        backend=BACKEND_BITWARDEN,
                        secret_path=f"{org_id}/{key}",
                        last_rotated=_parse_iso(row.get("revision_date")),
                        referencing_system=row.get("referencing_system"),
                    )
                )
        return items, errors

    @staticmethod
    def _item(
        *,
        backend: str,
        secret_path: str,
        last_rotated: datetime | None,
        referencing_system: str | None,
    ) -> dict:
        """Build the generic Resource item for one secret.

        The ``data`` dict becomes ``Resource.metadata_`` AND the run's
        ``Snapshot`` payload, so it is restricted to the same closed field set
        as ``SecretRecord`` — existence, rotation age, referencing system. No
        raw response fragment is ever placed here.
        """
        return {
            "name": f"{backend}:{secret_path}",
            "type": "secret",
            "data": {
                "backend": backend,
                "secret_path": secret_path,
                "last_rotated_at": last_rotated.isoformat() if last_rotated else None,
                "referencing_system": referencing_system,
            },
        }

    # --- detail write ------------------------------------------------------

    def _detail_writers(self, scope, result):
        return [self._write_secret_records]

    def _write_secret_records(self) -> int:
        records = getattr(self, "_records", None)
        if not records:
            return 0
        written = 0
        with get_session() as session:
            for item in records:
                data = item["data"]
                resource_id = self._resource_id(session, "secret", item["name"])
                if resource_id is None:
                    continue
                self._upsert_detail(
                    session,
                    SecretRecord,
                    {
                        "resource_id": resource_id,
                        "backend": data["backend"],
                        "secret_path": data["secret_path"],
                        "last_rotated_at": _parse_iso(data["last_rotated_at"]),
                        "referencing_system": data["referencing_system"],
                    },
                    ["backend", "secret_path"],
                )
                written += 1
            session.commit()
        logger.info("secrets_inventory: upserted %d secret metadata records", written)
        return written


__all__ = [
    "SecretValueEndpointError",
    "SecretsInventoryAgent",
    "bitwarden_list_secret_metadata_tool",
    "vault_list_secret_names_tool",
    "vault_secret_metadata_tool",
]
