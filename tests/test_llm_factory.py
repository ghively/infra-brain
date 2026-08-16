"""Tests for the provider-agnostic chat-model factory (infra_brain.llm)."""

import pytest

from infra_brain.config import get_settings
from infra_brain.llm import get_chat_model


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _set(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


def test_default_provider_is_anthropic(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    _set(monkeypatch, ANTHROPIC_API_KEY="sk-test", LLM_MODEL="claude-sonnet-4-6")
    llm = get_chat_model()
    assert type(llm).__name__ == "ChatAnthropic"
    assert llm.model == "claude-sonnet-4-6"


def test_anthropic_forwards_temperature_and_streaming(monkeypatch):
    _set(monkeypatch, LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-test")
    llm = get_chat_model(temperature=0, streaming=True)
    assert type(llm).__name__ == "ChatAnthropic"
    assert llm.temperature == 0
    assert llm.streaming is True


def test_bedrock_provider_builds_converse(monkeypatch):
    pytest.importorskip("langchain_aws")
    _set(
        monkeypatch,
        LLM_PROVIDER="bedrock",
        BEDROCK_MODEL_ID="us.anthropic.claude-sonnet-4-5-20251001-v1:0",
        BEDROCK_REGION="us-west-2",
        # ensure no profile branch / no real call
        BEDROCK_PROFILE="",
        AWS_ACCESS_KEY_ID="x",
        AWS_SECRET_ACCESS_KEY="y",
    )
    llm = get_chat_model()
    assert type(llm).__name__ == "ChatBedrockConverse"
    assert llm.model_id == "us.anthropic.claude-sonnet-4-5-20251001-v1:0"
    assert llm.region_name == "us-west-2"


def test_openai_provider_builds_chatopenai(monkeypatch):
    pytest.importorskip("langchain_openai")
    _set(
        monkeypatch,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-test",
        OPENAI_MODEL="gpt-4o",
        OPENAI_BASE_URL="",
    )
    llm = get_chat_model()
    assert type(llm).__name__ == "ChatOpenAI"
    assert llm.model_name == "gpt-4o"


def test_openai_compatible_endpoint_openrouter(monkeypatch):
    pytest.importorskip("langchain_openai")
    _set(
        monkeypatch,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-or-test",
        OPENAI_MODEL="anthropic/claude-sonnet-4-6",
        OPENAI_BASE_URL="https://openrouter.ai/api/v1",
        OPENAI_HTTP_REFERER="https://example.com",
        OPENAI_APP_TITLE="Infra Brain",
    )
    llm = get_chat_model()
    assert type(llm).__name__ == "ChatOpenAI"
    assert llm.model_name == "anthropic/claude-sonnet-4-6"
    assert str(llm.openai_api_base) == "https://openrouter.ai/api/v1"


def test_openai_local_gateway_empty_key_gets_placeholder(monkeypatch):
    """A keyless local OpenAI-compatible endpoint (Ollama/vLLM) must still build:
    an empty OPENAI_API_KEY + a custom base_url gets a non-empty placeholder so
    the openai SDK doesn't raise at construction. Regression for the Ollama
    'Connection error'/keyless config that broke discovery & coverage."""
    pytest.importorskip("langchain_openai")
    _set(
        monkeypatch,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="",
        OPENAI_MODEL="gemma4:e4b",
        OPENAI_BASE_URL="http://host.docker.internal:11434/v1",
    )
    llm = get_chat_model()
    assert type(llm).__name__ == "ChatOpenAI"
    assert str(llm.openai_api_base) == "http://host.docker.internal:11434/v1"
    # Non-empty key was supplied so construction didn't raise.
    key = llm.openai_api_key
    key = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
    assert key


def test_openai_real_key_not_overridden_by_placeholder(monkeypatch):
    """A real gateway key must win — the placeholder only fills an EMPTY key."""
    pytest.importorskip("langchain_openai")
    _set(
        monkeypatch,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-or-real",
        OPENAI_MODEL="anthropic/claude-sonnet-4-6",
        OPENAI_BASE_URL="https://openrouter.ai/api/v1",
    )
    llm = get_chat_model()
    key = llm.openai_api_key
    key = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
    assert key == "sk-or-real"


def test_anthropic_base_url_forwarded(monkeypatch):
    _set(
        monkeypatch,
        LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="sk-test",
        ANTHROPIC_BASE_URL="https://anthropic-proxy.internal/v1",
    )
    llm = get_chat_model()
    assert type(llm).__name__ == "ChatAnthropic"
    assert str(llm.anthropic_api_url) == "https://anthropic-proxy.internal/v1"


def test_bedrock_role_selects_per_agent_model(monkeypatch):
    pytest.importorskip("langchain_aws")
    _set(
        monkeypatch,
        LLM_PROVIDER="bedrock",
        BEDROCK_MODEL_ID="global-default-v1:0",
        BEDROCK_MODEL_DISCOVERY="us.minimax.minimax-m2-5-v1:0",
        BEDROCK_MODEL_SQL="qwen.qwen3-coder-30b-a3b-v1:0",
        BEDROCK_MODEL_CHAT="zai.glm-4-7-flash-v1:0",
        BEDROCK_PROFILE="",
        AWS_ACCESS_KEY_ID="x",
        AWS_SECRET_ACCESS_KEY="y",
    )
    assert get_chat_model(role="discovery").model_id == "us.minimax.minimax-m2-5-v1:0"
    assert get_chat_model(role="sql").model_id == "qwen.qwen3-coder-30b-a3b-v1:0"
    assert get_chat_model(role="chat").model_id == "zai.glm-4-7-flash-v1:0"
    # Unknown / no role falls back to the global default.
    assert get_chat_model().model_id == "global-default-v1:0"
    assert get_chat_model(role="nope").model_id == "global-default-v1:0"


def test_explicit_model_overrides_role(monkeypatch):
    pytest.importorskip("langchain_aws")
    _set(
        monkeypatch,
        LLM_PROVIDER="bedrock",
        BEDROCK_MODEL_DISCOVERY="us.minimax.minimax-m2-5-v1:0",
        BEDROCK_PROFILE="",
        AWS_ACCESS_KEY_ID="x",
        AWS_SECRET_ACCESS_KEY="y",
    )
    llm = get_chat_model(role="discovery", model="explicit-model-v1:0")
    assert llm.model_id == "explicit-model-v1:0"


def test_unmapped_role_falls_back_to_llm_model_on_anthropic(monkeypatch):
    _set(
        monkeypatch,
        LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="sk-test",
        LLM_MODEL="claude-sonnet-4-6",
    )
    # "discovery" has no Anthropic per-role override (only chat/sql do, B7) —
    # falls back to LLM_MODEL exactly like before B7.
    assert get_chat_model(role="discovery").model == "claude-sonnet-4-6"


def test_unknown_provider_raises(monkeypatch):
    _set(monkeypatch, LLM_PROVIDER="gemini")
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        get_chat_model()


# ---------------------------------------------------------------------------
# B7: additive per-role Anthropic/OpenAI model overrides.
# ---------------------------------------------------------------------------


def test_anthropic_role_selects_per_agent_model_when_set(monkeypatch):
    """Setting ANTHROPIC_MODEL_CHAT/_SQL selects that model for that role;
    an unset (empty) per-role override still falls back to LLM_MODEL — purely
    additive/opt-in, same contract as Bedrock's existing per-role overrides."""
    _set(
        monkeypatch,
        LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="sk-test",
        LLM_MODEL="claude-sonnet-4-6",
        ANTHROPIC_MODEL_CHAT="claude-haiku-4-5",
        ANTHROPIC_MODEL_SQL="claude-haiku-4-5",
    )
    assert get_chat_model(role="chat").model == "claude-haiku-4-5"
    assert get_chat_model(role="sql").model == "claude-haiku-4-5"
    # No override configured for "rootcause" -> falls back to the global model.
    assert get_chat_model(role="rootcause").model == "claude-sonnet-4-6"
    # No role at all -> also falls back to the global model.
    assert get_chat_model().model == "claude-sonnet-4-6"


def test_anthropic_role_overrides_default_to_empty_and_are_ignored(monkeypatch):
    """With no ANTHROPIC_MODEL_* env vars set, behavior is byte-identical to
    before B7: every role uses LLM_MODEL."""
    _set(
        monkeypatch,
        LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="sk-test",
        LLM_MODEL="claude-sonnet-4-6",
    )
    assert get_chat_model(role="chat").model == "claude-sonnet-4-6"
    assert get_chat_model(role="sql").model == "claude-sonnet-4-6"


def test_anthropic_rootcause_role_selects_override_when_set(monkeypatch):
    """Phase 3 Task 1: ANTHROPIC_MODEL_ROOTCAUSE follows the same additive/
    opt-in contract as chat/sql — set it and it wins; unset falls back to
    LLM_MODEL (covered by test_anthropic_role_overrides_default_to_empty_and_are_ignored)."""
    _set(
        monkeypatch,
        LLM_PROVIDER="anthropic",
        ANTHROPIC_API_KEY="sk-test",
        LLM_MODEL="claude-sonnet-4-6",
        ANTHROPIC_MODEL_ROOTCAUSE="claude-haiku-4-5",
    )
    assert get_chat_model(role="rootcause").model == "claude-haiku-4-5"
    assert get_chat_model(role="chat").model == "claude-sonnet-4-6"


def test_openai_rootcause_role_selects_override_when_set(monkeypatch):
    pytest.importorskip("langchain_openai")
    _set(
        monkeypatch,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-test",
        OPENAI_MODEL="gpt-4o",
        OPENAI_MODEL_ROOTCAUSE="gpt-4o-mini",
        OPENAI_BASE_URL="",
    )
    assert get_chat_model(role="rootcause").model_name == "gpt-4o-mini"
    assert get_chat_model(role="chat").model_name == "gpt-4o"


def test_openai_role_selects_per_agent_model_when_set(monkeypatch):
    pytest.importorskip("langchain_openai")
    _set(
        monkeypatch,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-test",
        OPENAI_MODEL="gpt-4o",
        OPENAI_MODEL_CHAT="gpt-4o-mini",
        OPENAI_MODEL_SQL="gpt-4o-mini",
        OPENAI_BASE_URL="",
    )
    assert get_chat_model(role="chat").model_name == "gpt-4o-mini"
    assert get_chat_model(role="sql").model_name == "gpt-4o-mini"
    # No role -> falls back to the global model, same as before B7.
    assert get_chat_model().model_name == "gpt-4o"


# ---------------------------------------------------------------------------
# TRK-109 residual: per-call LLM request timeout threaded into every provider.
# A single hung/glacial LLM call must not be able to consume the whole collect()
# budget and only surface when the 600s wall-clock guard trips (losing the run).
# ---------------------------------------------------------------------------


def test_openai_applies_default_request_timeout(monkeypatch):
    """The deployed critical path: Ollama via the OpenAI-compatible provider.
    ChatOpenAI exposes the timeout via the `timeout` alias (field:
    request_timeout); the default (120s) must be forwarded."""
    pytest.importorskip("langchain_openai")
    _set(
        monkeypatch,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-test",
        OPENAI_MODEL="gpt-4o",
        OPENAI_BASE_URL="",
    )
    llm = get_chat_model()
    assert type(llm).__name__ == "ChatOpenAI"
    assert llm.request_timeout == 120


def test_anthropic_applies_default_request_timeout(monkeypatch):
    """ChatAnthropic exposes the timeout via the `timeout` alias (field:
    default_request_timeout); the default (120s) must be forwarded."""
    _set(monkeypatch, LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-test")
    llm = get_chat_model()
    assert type(llm).__name__ == "ChatAnthropic"
    assert llm.default_request_timeout == 120


def test_llm_request_timeout_env_override(monkeypatch):
    """LLM_REQUEST_TIMEOUT_SECONDS is .env-tunable and flows through per provider."""
    pytest.importorskip("langchain_openai")
    _set(
        monkeypatch,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-test",
        OPENAI_MODEL="gpt-4o",
        OPENAI_BASE_URL="",
        LLM_REQUEST_TIMEOUT_SECONDS="45",
    )
    llm = get_chat_model()
    assert llm.request_timeout == 45


def test_llm_request_timeout_zero_disables(monkeypatch):
    """Setting the timeout to 0 restores pre-TRK-109 behavior (no per-call
    timeout) — the provider field stays at its own default (None)."""
    pytest.importorskip("langchain_openai")
    _set(
        monkeypatch,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-test",
        OPENAI_MODEL="gpt-4o",
        OPENAI_BASE_URL="",
        LLM_REQUEST_TIMEOUT_SECONDS="0",
    )
    llm = get_chat_model()
    assert llm.request_timeout is None


def test_explicit_timeout_kwarg_is_not_overridden(monkeypatch):
    """A caller passing an explicit timeout=... in **kwargs must win over the
    settings default — the settings value only fills an unset timeout."""
    pytest.importorskip("langchain_openai")
    _set(
        monkeypatch,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-test",
        OPENAI_MODEL="gpt-4o",
        OPENAI_BASE_URL="",
        LLM_REQUEST_TIMEOUT_SECONDS="120",
    )
    llm = get_chat_model(timeout=7)
    assert llm.request_timeout == 7


def test_bedrock_applies_timeout(monkeypatch):
    """ChatBedrockConverse (langchain-aws >=1.6) has a first-class `timeout` int
    field; the default (120s) must be forwarded on the non-profile path."""
    pytest.importorskip("langchain_aws")
    _set(
        monkeypatch,
        LLM_PROVIDER="bedrock",
        BEDROCK_MODEL_ID="us.anthropic.claude-sonnet-4-5-20251001-v1:0",
        BEDROCK_REGION="us-west-2",
        BEDROCK_PROFILE="",
        AWS_ACCESS_KEY_ID="x",
        AWS_SECRET_ACCESS_KEY="y",
    )
    llm = get_chat_model()
    assert type(llm).__name__ == "ChatBedrockConverse"
    assert llm.timeout == 120
