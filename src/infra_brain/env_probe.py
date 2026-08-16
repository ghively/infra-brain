"""Environment readiness probe (P3.2).

Nothing in infra-brain checks whether a *configured* integration actually
works — 232 `.env` keys and 17 `*_enabled` flags are hand-set and silently
trusted. A misconfigured GitLab token, a stale Ansible inventory path, or an
unreachable container registry only surfaces later as a confusing collector
failure. This probes each configured integration's real reachability and
files an `IntegrationProposal` row per gap — it never mutates config itself
(that stays a human decision); it only proposes.

Deliberately NOT an LLM-driven agent (unlike CoverageAgent.discover(), which
answers a different question — "which known agent domain has no ScanPoint
yet" — via an LLM strategy call per gap). Reachability is a deterministic
yes/no; there is nothing here for an LLM to reason about.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from infra_brain.config import get_settings
from infra_brain.db.models import IntegrationProposal
from infra_brain.db.session import get_session

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 5.0


@dataclass
class ProbeResult:
    name: str  # e.g. "gitlab", "ansible_inventory", "container_registry:https://..."
    configured: bool
    reachable: bool
    detail: str


def _probe_gitlab() -> ProbeResult | None:
    settings = get_settings()
    if not settings.gitlab_url or not settings.gitlab_token:
        return None
    try:
        resp = httpx.get(
            f"{settings.gitlab_url.rstrip('/')}/api/v4/user",
            headers={"PRIVATE-TOKEN": settings.gitlab_token},
            verify=settings.gitlab_ssl_verify,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        if resp.status_code == 200:
            return ProbeResult("gitlab", True, True, f"{settings.gitlab_url}: token valid")
        return ProbeResult(
            "gitlab", True, False, f"{settings.gitlab_url}: HTTP {resp.status_code} on /api/v4/user"
        )
    except httpx.HTTPError as exc:
        return ProbeResult("gitlab", True, False, f"{settings.gitlab_url}: {exc}")


def _probe_ansible_inventory() -> ProbeResult | None:
    import os

    settings = get_settings()
    if not settings.ansible_inventory_path:
        return None
    path = settings.ansible_inventory_path
    if not os.path.exists(path):
        return ProbeResult("ansible_inventory", True, False, f"{path}: path does not exist")
    return ProbeResult("ansible_inventory", True, True, f"{path}: exists")


def _probe_llm_base_url() -> ProbeResult | None:
    """Whichever LLM gateway is actually configured (LLM_PROVIDER=openai's
    openai_base_url, or LLM_PROVIDER=anthropic's optional anthropic_base_url)
    — covers the Ollama-as-gateway case (P3.2's "Ollama" probe) without a
    dedicated Ollama setting, since infra-brain has no separate one; Ollama
    is just whatever's pointed at by openai_base_url.

    Deliberately a REAL minimal chat completion (max_tokens=1), not a bare
    GET to the base URL. Found live during P3.4's DiscoveryAgent retest: a
    bare GET against an OpenRouter gateway returns 200 (some generic/docs
    response) regardless of whether the API key is even valid — auth is only
    checked on the actual completions endpoint. A bare GET would have
    reported "reachable" for the exact stale-OpenRouter-key gap that broke a
    real collection run.
    """
    settings = get_settings()
    if settings.llm_provider == "openai":
        base_url, api_key, model = (
            settings.openai_base_url,
            settings.openai_api_key,
            settings.openai_model,
        )
        path = "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    elif settings.llm_provider == "anthropic":
        base_url, api_key, model = (
            settings.anthropic_base_url,
            settings.anthropic_api_key,
            settings.llm_model,
        )
        # Native Anthropic Messages API shape — matches what llm.py's
        # ChatAnthropic(base_url=settings.anthropic_base_url) actually sends
        # (x-api-key auth, anthropic-version header, /v1/messages), not the
        # OpenAI Chat Completions shape. A gateway that's correctly configured
        # for the real client would otherwise be wrongly flagged as a gap.
        path = "/v1/messages"
        headers = {"anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    else:
        base_url = api_key = model = ""
        path = ""
        headers = {}
        body = {}
    if not base_url:
        return None
    try:
        resp = httpx.post(
            f"{base_url.rstrip('/')}{path}",
            headers=headers,
            json=body,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        if resp.status_code == 200:
            return ProbeResult("llm_gateway", True, True, f"{base_url}: chat completion OK")
        return ProbeResult(
            "llm_gateway", True, False, f"{base_url}: HTTP {resp.status_code} on {path}"
        )
    except httpx.HTTPError as exc:
        return ProbeResult("llm_gateway", True, False, f"{base_url}: {exc}")


def _probe_container_registries() -> list[ProbeResult]:
    settings = get_settings()
    if not settings.container_registries:
        return []
    results = []
    for url in (u.strip() for u in settings.container_registries.split(",") if u.strip()):
        headers = (
            {"Authorization": f"Bearer {settings.container_registry_token}"}
            if settings.container_registry_token
            else {}
        )
        try:
            resp = httpx.get(
                f"{url.rstrip('/')}/v2/",
                headers=headers,
                verify=settings.container_registry_ssl_verify,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
            # A bare /v2/ typically 200s (anon) or 401s (auth required but
            # reachable) — both mean the registry is up. Only a connection
            # failure or 5xx means genuinely unreachable.
            reachable = resp.status_code < 500
            results.append(
                ProbeResult(f"container_registry:{url}", True, reachable, f"HTTP {resp.status_code}")
            )
        except httpx.HTTPError as exc:
            results.append(ProbeResult(f"container_registry:{url}", True, False, str(exc)))
    return results


def probe_environment() -> list[ProbeResult]:
    """Run every probe whose integration is actually configured. Unconfigured
    integrations are silently omitted — this reports on what's set up wrong,
    not on what's missing (CoverageAgent covers "missing")."""
    results = []
    for fn in (_probe_gitlab, _probe_ansible_inventory, _probe_llm_base_url):
        r = fn()
        if r is not None:
            results.append(r)
    results.extend(_probe_container_registries())
    return results


def propose_gaps(results: list[ProbeResult] | None = None) -> list[str]:
    """File an IntegrationProposal for every unreachable-but-configured probe
    result, skipping one already pending for the same source. Returns the
    list of proposal sources newly created (empty if everything is healthy
    or every gap already has a pending proposal)."""
    if results is None:
        results = probe_environment()

    created: list[str] = []
    for r in results:
        if r.reachable:
            continue
        source = f"env-probe:{r.name}"
        with get_session() as session:
            existing = (
                session.query(IntegrationProposal)
                .filter_by(source=source, status="pending")
                .first()
            )
            if existing:
                logger.debug("[env-probe] Proposal already pending for %s", source)
                continue
            session.add(
                IntegrationProposal(
                    source=source,
                    type="connectivity-gap",
                    endpoint=r.detail[:512],
                    # Deterministic fact (reachability), not a guess — full
                    # confidence, unlike CoverageAgent's LLM-derived 0.85.
                    confidence=1.0,
                )
            )
            session.commit()
        created.append(source)
        logger.warning("[env-probe] Gap found: %s — %s", r.name, r.detail)
    return created
