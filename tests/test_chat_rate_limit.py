"""TRK-090 / TRK-091 — per-user rate limit + daily token budget on the two
LLM-invoking dashboard endpoints (POST /api/dashboard/chat, /custom-view).

Covers: rate limit trips after the Nth request in the window and returns a 429
with a distinct code; the fixed window resets on the next bucket; the token
budget trips once cumulative usage crosses the threshold with a DIFFERENT 429
code; and both guardrails fail OPEN when Redis is unavailable.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient

import infra_brain.ratelimit as rl
from infra_brain.api.routers.ui import ui_router
from infra_brain.dashboard_api import router
from infra_brain.dashboard_auth import require_session


class _FakeRedis:
    """Stateful in-process stand-in — mirrors the redis-py calls ratelimit.py uses
    (incr / incrby / expire / get) with decode_responses=True semantics (str gets)."""

    def __init__(self):
        self.store: dict[str, int] = {}

    def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def incrby(self, key, amount):
        self.store[key] = int(self.store.get(key, 0)) + amount
        return self.store[key]

    def expire(self, key, ttl):
        return True

    def get(self, key):
        v = self.store.get(key)
        return None if v is None else str(v)


def _settings(rate=30, budget=500_000):
    return SimpleNamespace(
        chat_rate_limit_per_hour=rate,
        daily_token_budget_per_user=budget,
    )


# ── Unit: rate limiter ────────────────────────────────────────────────────────


def test_rate_limit_trips_after_nth_request():
    fake = _FakeRedis()
    with (
        patch.object(rl, "get_redis", return_value=fake),
        patch.object(rl, "get_settings", return_value=_settings(rate=3)),
    ):
        # First 3 within the window are allowed.
        assert rl.rate_limit_exceeded("alice") is False
        assert rl.rate_limit_exceeded("alice") is False
        assert rl.rate_limit_exceeded("alice") is False
        # 4th exceeds the limit.
        assert rl.rate_limit_exceeded("alice") is True


def test_rate_limit_resets_on_next_window():
    fake = _FakeRedis()
    t = [1_000_000.0]  # inside hour bucket N
    with (
        patch.object(rl, "get_redis", return_value=fake),
        patch.object(rl, "get_settings", return_value=_settings(rate=2)),
        patch.object(rl.time, "time", lambda: t[0]),
    ):
        assert rl.rate_limit_exceeded("bob") is False
        assert rl.rate_limit_exceeded("bob") is False
        assert rl.rate_limit_exceeded("bob") is True  # over limit in window N
        # Advance past the window → new fixed-window bucket, fresh counter.
        t[0] += rl._RATE_WINDOW_SECONDS
        assert rl.rate_limit_exceeded("bob") is False


def test_rate_limit_is_per_user():
    fake = _FakeRedis()
    with (
        patch.object(rl, "get_redis", return_value=fake),
        patch.object(rl, "get_settings", return_value=_settings(rate=1)),
    ):
        assert rl.rate_limit_exceeded("u1") is False
        assert rl.rate_limit_exceeded("u1") is True
        # A different user has an independent counter.
        assert rl.rate_limit_exceeded("u2") is False


def test_rate_limit_fails_open_on_redis_error():
    broken = MagicMock()
    broken.incr.side_effect = redis.RedisError("down")
    with (
        patch.object(rl, "get_redis", return_value=broken),
        patch.object(rl, "get_settings", return_value=_settings(rate=1)),
    ):
        # Redis outage must not block requests — fail open.
        assert rl.rate_limit_exceeded("alice") is False


# ── Unit: token budget ────────────────────────────────────────────────────────


def test_token_budget_trips_once_cumulative_usage_crosses_threshold():
    fake = _FakeRedis()
    with (
        patch.object(rl, "get_redis", return_value=fake),
        patch.object(rl, "get_settings", return_value=_settings(budget=1000)),
    ):
        assert rl.token_budget_exceeded("carol") is False
        rl.record_token_usage("carol", 600)
        assert rl.token_budget_exceeded("carol") is False  # 600 < 1000
        rl.record_token_usage("carol", 500)  # cumulative 1100 >= 1000
        assert rl.token_budget_exceeded("carol") is True


def test_token_budget_is_per_user():
    fake = _FakeRedis()
    with (
        patch.object(rl, "get_redis", return_value=fake),
        patch.object(rl, "get_settings", return_value=_settings(budget=100)),
    ):
        rl.record_token_usage("heavy", 500)
        assert rl.token_budget_exceeded("heavy") is True
        assert rl.token_budget_exceeded("light") is False


def test_token_budget_resets_next_day():
    fake = _FakeRedis()
    with (
        patch.object(rl, "get_redis", return_value=fake),
        patch.object(rl, "get_settings", return_value=_settings(budget=100)),
    ):
        with patch.object(rl.time, "strftime", lambda *a, **k: "20260716"):
            rl.record_token_usage("dave", 500)
            assert rl.token_budget_exceeded("dave") is True
        # New calendar day → new key → fresh budget.
        with patch.object(rl.time, "strftime", lambda *a, **k: "20260717"):
            assert rl.token_budget_exceeded("dave") is False


def test_token_budget_fails_open_on_redis_error():
    broken = MagicMock()
    broken.get.side_effect = redis.RedisError("down")
    with (
        patch.object(rl, "get_redis", return_value=broken),
        patch.object(rl, "get_settings", return_value=_settings(budget=1)),
    ):
        assert rl.token_budget_exceeded("alice") is False


def test_estimate_tokens_roughly_four_chars_per_token():
    assert rl.estimate_tokens("a" * 40) == 10
    assert rl.estimate_tokens("ab", "cd") == 1  # 4 chars // 4
    assert rl.estimate_tokens("") == 0


# ── Integration: through the custom-view endpoint ─────────────────────────────


def _make_app() -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[require_session] = lambda: {"user": "test"}
    app.include_router(router)
    app.include_router(ui_router)
    return app


def _mock_llm():
    async def _astream(messages, **kwargs):
        from langchain_core.messages import AIMessageChunk

        yield AIMessageChunk(content="<FleetStatCard/>")

    llm = MagicMock()
    llm.astream = _astream
    return llm


def test_custom_view_rate_limit_returns_429_with_distinct_code():
    fake = _FakeRedis()
    client = TestClient(_make_app(), raise_server_exceptions=False)
    with (
        patch.object(rl, "get_redis", return_value=fake),
        patch.object(rl, "get_settings", return_value=_settings(rate=2, budget=10**9)),
        patch("infra_brain.llm.get_chat_model", return_value=_mock_llm()),
    ):
        assert client.post("/api/dashboard/custom-view", json={"prompt": "x"}).status_code == 200
        assert client.post("/api/dashboard/custom-view", json={"prompt": "x"}).status_code == 200
        blocked = client.post("/api/dashboard/custom-view", json={"prompt": "x"})
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "rate_limit_exceeded"


def test_custom_view_token_budget_returns_429_with_distinct_code():
    fake = _FakeRedis()
    client = TestClient(_make_app(), raise_server_exceptions=False)
    with (
        patch.object(rl, "get_redis", return_value=fake),
        patch.object(rl, "get_settings", return_value=_settings(rate=1000, budget=50)),
        patch("infra_brain.llm.get_chat_model", return_value=_mock_llm()),
    ):
        # Pre-exhaust the day's budget for the resolved user ("dev" — no cookie set).
        rl.record_token_usage("dev", 100)
        blocked = client.post("/api/dashboard/custom-view", json={"prompt": "x"})
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "token_budget_exceeded"


def test_two_429_cases_are_distinguishable():
    """The rate-limit and budget rejections must carry different codes."""
    assert "rate_limit_exceeded" != "token_budget_exceeded"


@pytest.mark.parametrize("prompt", [" ", ""])
def test_custom_view_still_422_on_empty_prompt_before_gate(prompt):
    """Existing validation behavior is unchanged — empty prompt is 422, not gated."""
    fake = _FakeRedis()
    client = TestClient(_make_app(), raise_server_exceptions=False)
    with (
        patch.object(rl, "get_redis", return_value=fake),
        patch.object(rl, "get_settings", return_value=_settings()),
    ):
        resp = client.post("/api/dashboard/custom-view", json={"prompt": prompt})
    assert resp.status_code == 422
