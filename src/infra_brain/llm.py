"""Provider-agnostic chat-model factory for Infra Brain.

Every LLM in the codebase is constructed through :func:`get_chat_model` so the
backing provider can be switched with a single env var (``LLM_PROVIDER``) — no
code changes. Two providers are supported:

- ``anthropic`` (default) — Anthropic API (or an Anthropic-compatible proxy via
  ``ANTHROPIC_BASE_URL``), needs ``ANTHROPIC_API_KEY``.
- ``bedrock`` — AWS Bedrock via ``ChatBedrockConverse`` (the Converse API,
  which—unlike the legacy ``ChatBedrock``—supports tool calling and streaming
  correctly). Auth is IAM: env keys, a named profile, or an instance role.
- ``openai`` — OpenAI or any OpenAI-compatible endpoint (OpenRouter, LiteLLM,
  Groq, Together, vLLM, …). Point ``OPENAI_BASE_URL`` at the gateway and set
  ``OPENAI_MODEL`` to that gateway's model id (e.g. ``anthropic/claude-sonnet-4-6``
  on OpenRouter).

Callbacks are forwarded unchanged so ReadOnlyToolValidator / DLP / audit
enforcement fires identically regardless of provider.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel

from infra_brain.config import get_settings

VALID_PROVIDERS = ("anthropic", "bedrock", "openai")


def _model_for_role(settings: Any, provider: str, role: str | None) -> str:
    """Resolve the model id for an agent role under the active provider.

    Bedrock has always supported a per-agent model override (e.g. MiniMax for
    discovery, Qwen Coder for SQL, GLM Flash for chat). B7 (Finding 6): the
    Anthropic/OpenAI branches now follow the same role-map pattern — purely
    additive/opt-in. Each per-role setting defaults to "" (falls back to the
    provider's single configured model, i.e. today's exact behavior); set one
    only if you actually want a cheaper/faster model for that role.
    """
    if provider == "anthropic":
        role_models = {
            "chat": settings.anthropic_model_chat,
            "sql": settings.anthropic_model_sql,
            "rootcause": settings.anthropic_model_rootcause,
        }
        return (role and role_models.get(role)) or settings.llm_model
    if provider == "openai":
        role_models = {
            "chat": settings.openai_model_chat,
            "sql": settings.openai_model_sql,
            "rootcause": settings.openai_model_rootcause,
        }
        return (role and role_models.get(role)) or settings.openai_model
    # bedrock — map role → per-agent override, falling back to the global default
    role_models = {
        "discovery": settings.bedrock_model_discovery,
        "sql": settings.bedrock_model_sql,
        "chat": settings.bedrock_model_chat,
        "remediation": settings.bedrock_model_remediation,
        "triage": settings.bedrock_model_triage,
        "rootcause": settings.bedrock_model_rootcause,
        "coverage": settings.bedrock_model_coverage,  # same tier as discovery — strategy reasoning
    }
    return (role and role_models.get(role)) or settings.bedrock_model_id


def get_chat_model(
    *,
    role: str | None = None,
    model: str | None = None,
    callbacks: Any | None = None,
    streaming: bool = False,
    temperature: float | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """Return a chat model for the configured provider.

    Args:
        role: Logical agent role ("discovery" | "sql" | "chat"). On Bedrock this
            selects a per-agent model override; ignored on Anthropic/OpenAI.
        model: Explicit model id; overrides both ``role`` and provider defaults.
        callbacks: LangChain callbacks/handlers (carries safety enforcement).
        streaming: Enable token streaming (Anthropic). Bedrock Converse streams
            via ``astream_events`` regardless, so this is a no-op there.
        temperature: Sampling temperature; only passed through when set.
        **kwargs: Extra provider constructor args (e.g. ``max_tokens``).
    """
    settings = get_settings()
    provider = (settings.llm_provider or "anthropic").lower()

    if provider not in VALID_PROVIDERS:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{provider}'. Valid values: {list(VALID_PROVIDERS)}"
        )

    resolved_model = model or _model_for_role(settings, provider, role)

    if temperature is not None:
        kwargs["temperature"] = temperature

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        if settings.anthropic_base_url:
            kwargs["base_url"] = settings.anthropic_base_url
        # Per-call timeout (TRK-109 residual). ChatAnthropic exposes it via the
        # `timeout` alias (field: default_request_timeout). Only set when a caller
        # hasn't passed an explicit timeout in **kwargs, and only when non-zero.
        if settings.llm_request_timeout_seconds and "timeout" not in kwargs:
            kwargs["timeout"] = settings.llm_request_timeout_seconds
        return ChatAnthropic(
            model=resolved_model,
            anthropic_api_key=settings.anthropic_api_key,
            callbacks=callbacks,
            streaming=streaming,
            **kwargs,
        )

    if provider == "openai":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise ImportError(
                f"LLM_PROVIDER is set to 'openai' but the 'langchain-openai' package is not "
                f"installed. Install it with: pip install langchain-openai>=0.2\n"
                f"Original error: {exc}"
            ) from exc

        api_key = settings.openai_api_key
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
            # OpenAI-compatible local gateways (Ollama, vLLM, LiteLLM) authenticate
            # by network reachability, not a key — but the openai SDK still requires
            # a NON-EMPTY api_key or it raises at construction. Supply a harmless
            # placeholder so a keyless local endpoint works without stashing a fake
            # secret in .env. A real gateway key (OpenRouter etc.) still wins.
            if not api_key:
                api_key = "sk-noauth-local-gateway"
        headers: dict[str, str] = {}
        if settings.openai_http_referer:
            headers["HTTP-Referer"] = settings.openai_http_referer
        if settings.openai_app_title:
            headers["X-Title"] = settings.openai_app_title
        if headers:
            kwargs["default_headers"] = headers
        # Per-call timeout (TRK-109 residual). ChatOpenAI exposes it via the
        # `timeout` alias (field: request_timeout) — this is the critical path for
        # the deployed Ollama-via-OpenAI gateway, where a stuck local call would
        # otherwise run until the collect() wall-clock guard kills the whole run.
        if settings.llm_request_timeout_seconds and "timeout" not in kwargs:
            kwargs["timeout"] = settings.llm_request_timeout_seconds
        return ChatOpenAI(
            model=resolved_model,
            api_key=api_key,
            callbacks=callbacks,
            streaming=streaming,
            **kwargs,
        )

    # provider == "bedrock"
    from langchain_aws import ChatBedrockConverse

    # Per-call timeout (TRK-109 residual). ChatBedrockConverse (langchain-aws
    # >=1.6) has a first-class `timeout` int field that sets both connect_timeout
    # and read_timeout on the botocore Config it builds. That field only applies
    # when the class constructs its OWN client, so on the pre-built-client profile
    # path below we instead push the timeout down into the boto3 client's
    # botocore Config directly.
    timeout_s = settings.llm_request_timeout_seconds
    client_kwargs: dict[str, Any] = {}
    if settings.bedrock_profile:
        import boto3

        session = boto3.Session(
            profile_name=settings.bedrock_profile,
            region_name=settings.bedrock_region,
        )
        client_config = None
        if timeout_s:
            from botocore.config import Config

            client_config = Config(connect_timeout=timeout_s, read_timeout=timeout_s)
        client_kwargs["client"] = session.client("bedrock-runtime", config=client_config)
    else:
        client_kwargs["region_name"] = settings.bedrock_region
        if timeout_s and "timeout" not in kwargs:
            client_kwargs["timeout"] = timeout_s

    return ChatBedrockConverse(
        model=resolved_model,
        callbacks=callbacks,
        **client_kwargs,
        **kwargs,
    )
