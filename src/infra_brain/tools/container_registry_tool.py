"""Registry-agnostic container-image tools — read-only OCI Distribution API v2.

GitLab issue #101 (roadmap item #8): registry-side image scanning, distinct
from any running-container/K8s workload data. Per the issue's own
adversarial-review correction (comment), this module is scoped ONLY to
enumerating images and their attached OCI referrers artifacts (signatures /
scan attestations) — it never claims a container's runtime placement
(HOSTED_BY stays deferred; that needs a separate running-container collector
still blocked on TRK-041).

Every call here is a GET against the standard OCI Distribution Specification
v2 API (https://github.com/opencontainers/distribution-spec), which every
compliant registry implements identically (Docker Hub, GitLab Container
Registry, Harbor, ECR, GHCR, ACR, ...) — deliberately NOT hard-coded to
GitLab Container Registry, per the issue's note about the open
GitLab-instance-migration question.

Endpoints used:
  GET /v2/_catalog                      -> {"repositories": [...]}
  GET /v2/<repo>/tags/list              -> {"tags": [...]}
  GET /v2/<repo>/manifests/<tag>        -> manifest body; digest read from the
                                            ``Docker-Content-Digest`` response
                                            header (works for both the legacy
                                            Docker and the OCI manifest media
                                            types)
  GET /v2/<repo>/referrers/<digest>     -> OCI 1.1 "Referrers API" — the
                                            registry-agnostic way to discover
                                            artifacts (signatures, SBOMs, vuln
                                            scan attestations) attached to an
                                            image, without assuming any one
                                            vendor's scanner/signing tool.
                                            Optional per-spec: registries that
                                            don't implement it 404, which
                                            callers must treat as
                                            "unsupported", not an error.

No authentication is attempted beyond an optional static bearer token
(``settings.container_registry_token`` — a single shared read-only token,
matching the anonymous-pull-enabled internal registries this was designed
against); registries requiring per-registry/OAuth token exchange are out of
scope for this first pass. The token is only ever attached when
``registry_url`` is one of the operator-configured ``container_registries``
(see ``_is_configured_registry`` below) — never to an arbitrary host, so
adding a public registry (Docker Hub, GHCR) to that same config list for
catalog browsing cannot silently ship the internal token to it.
"""

from __future__ import annotations

from urllib.parse import quote

from langchain_core.tools import tool

from infra_brain.config import get_settings
from infra_brain.tools.http_readonly import ReadOnlyClient, tool_http_errors

# Manifest media types this module knows how to ask for — both the legacy
# Docker v2 schema2 type and the OCI image manifest type, plus the "list"
# variants for multi-arch images. Sent as a comma-separated Accept header;
# the registry picks whichever it has.
_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ]
)


def _configured_registries() -> set[str]:
    raw = get_settings().container_registries or ""
    return {r.strip().rstrip("/") for r in raw.split(",") if r.strip()}


def _is_configured_registry(registry_url: str) -> bool:
    return registry_url.rstrip("/") in _configured_registries()


def _headers(registry_url: str) -> dict:
    """Bearer header for *registry_url*, bound to the configured registry set.

    Reviewer finding (Phase E integration review, GitLab #101): the shared
    token was previously attached to every call regardless of which registry
    it targeted, and ``registry_url`` was accepted but ignored — adding a
    public registry (Docker Hub, GHCR) to ``container_registries`` for
    catalog browsing would silently ship the internal registry's token to a
    third party. Only attach it when the target is one of the operator's own
    configured registries.
    """
    token = get_settings().container_registry_token
    if not token or not _is_configured_registry(registry_url):
        return {}
    return {"Authorization": f"Bearer {token}"}


def _client() -> ReadOnlyClient:
    verify = get_settings().container_registry_ssl_verify
    timeout = get_settings().api_timeout_seconds
    return ReadOnlyClient(verify=verify, timeout=timeout)


def _safe_repo_path(repo: str) -> str:
    """URL-encode a (possibly multi-segment) repo name for use in a path.

    Registry-returned repo names (from ``/v2/_catalog``) are remote-controlled
    input. Repo names are legitimately multi-segment (``team/project/image``),
    so ``/`` is preserved as a separator, but each segment is percent-encoded
    individually and any ``.``/``..``/empty segment is rejected outright —
    closing off the path-traversal primitive a hostile or compromised
    registry could otherwise use to steer subsequent GETs elsewhere.
    """
    parts = repo.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"unsafe repo path segment in {repo!r}")
    return "/".join(quote(p, safe="") for p in parts)


def _safe_segment(value: str) -> str:
    """URL-encode a single non-separator path segment (tag or digest)."""
    if value in ("", ".", ".."):
        raise ValueError(f"unsafe path segment {value!r}")
    return quote(value, safe="")


@tool
@tool_http_errors
def registry_catalog_tool(registry_url: str, n: int = 200) -> dict:
    """List repositories in a registry (GET /v2/_catalog, read-only).

    ``registry_url`` is the registry's base URL, e.g. ``https://registry.example.com``.
    Returns ``{"repositories": [...]}`` (empty list if the registry reports none).
    """
    with _client() as client:
        resp = client.get(
            f"{registry_url.rstrip('/')}/v2/_catalog",
            params={"n": n},
            headers=_headers(registry_url),
        )
        resp.raise_for_status()
        return resp.json()


@tool
@tool_http_errors
def registry_tags_tool(registry_url: str, repo: str) -> dict:
    """List tags for one repository (GET /v2/<repo>/tags/list, read-only).

    Returns ``{"tags": [...]}`` (empty list if the repo has no tags).
    """
    with _client() as client:
        resp = client.get(
            f"{registry_url.rstrip('/')}/v2/{_safe_repo_path(repo)}/tags/list",
            headers=_headers(registry_url),
        )
        resp.raise_for_status()
        return resp.json()


@tool
@tool_http_errors
def registry_manifest_tool(registry_url: str, repo: str, tag: str) -> dict:
    """Fetch one image's manifest digest (GET /v2/<repo>/manifests/<tag>, read-only).

    Returns ``{"digest": <str>}`` — the content-addressable digest read from the
    ``Docker-Content-Digest`` response header (falls back to hashing the raw
    manifest body if a registry omits that header, per the OCI spec allowance).
    """
    with _client() as client:
        resp = client.get(
            f"{registry_url.rstrip('/')}/v2/{_safe_repo_path(repo)}/manifests/{_safe_segment(tag)}",
            headers={**_headers(registry_url), "Accept": _MANIFEST_ACCEPT},
        )
        resp.raise_for_status()
        digest = resp.headers.get("Docker-Content-Digest", "")
        if not digest:
            import hashlib

            digest = "sha256:" + hashlib.sha256(resp.content).hexdigest()
        return {"digest": digest}


@tool
@tool_http_errors
def registry_referrers_tool(registry_url: str, repo: str, digest: str) -> dict:
    """List OCI referrer artifacts attached to an image digest (read-only).

    GET /v2/<repo>/referrers/<digest> — the OCI 1.1 Referrers API. This is the
    registry-agnostic mechanism for discovering signatures / SBOMs / vuln-scan
    attestations attached to an image, without assuming any one vendor's
    scanner or signing tool. Optional per-spec: a 404 means the registry does
    not implement it, returned here as ``{"manifests": [], "supported": False}``
    rather than raised as an error (the caller must not treat "not supported"
    the same as "supported and empty").
    """
    with _client() as client:
        resp = client.get(
            f"{registry_url.rstrip('/')}/v2/{_safe_repo_path(repo)}/referrers/{_safe_segment(digest)}",
            headers={**_headers(registry_url), "Accept": "application/vnd.oci.image.index.v1+json"},
        )
        if resp.status_code == 404:
            return {"manifests": [], "supported": False}
        resp.raise_for_status()
        body = resp.json() if resp.content else {}
        manifests = body.get("manifests", []) if isinstance(body, dict) else []
        return {"manifests": manifests, "supported": True}
