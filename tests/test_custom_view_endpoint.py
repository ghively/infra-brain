"""Tests for POST /api/dashboard/custom-view."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra_brain.api.routers.ui import ui_router
from infra_brain.dashboard_api import router
from infra_brain.dashboard_auth import require_session


def _make_app(override_auth: bool = False) -> FastAPI:
    app = FastAPI()
    if override_auth:
        app.dependency_overrides[require_session] = lambda: {"user": "test"}
    app.include_router(router)
    app.include_router(ui_router)
    return app


def test_custom_view_requires_auth(monkeypatch):
    """Without session override, endpoint returns 401 or 403 when auth is enforced."""
    monkeypatch.setenv("UI_COOKIE_SECRET", "test-secret-enforces-auth")
    app = _make_app(override_auth=False)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/dashboard/custom-view", json={"prompt": "show resources"})
    assert resp.status_code in (401, 403)


def test_custom_view_returns_stream():
    """With auth, endpoint returns 200 with NDJSON content-type."""

    async def _mock_astream(messages, **kwargs):
        from langchain_core.messages import AIMessageChunk

        yield AIMessageChunk(content="<FleetStatCard ")
        yield AIMessageChunk(content='title="Resources" value={10} />')

    mock_llm = MagicMock()
    mock_llm.astream = _mock_astream

    app = _make_app(override_auth=True)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("infra_brain.llm.get_chat_model", return_value=mock_llm):
        resp = client.post(
            "/api/dashboard/custom-view",
            json={"prompt": "show me all resources"},
        )
    assert resp.status_code == 200
    assert "ndjson" in resp.headers.get("content-type", "")


@pytest.fixture
def authed_client():
    """Test client with authentication already mocked."""
    app = _make_app(override_auth=True)
    return TestClient(app, raise_server_exceptions=False)


def test_custom_view_prompt_in_messages(authed_client, monkeypatch):
    """The user's prompt is passed to the LLM messages."""
    captured_messages = []

    def _mock_get_chat_model(**kwargs):
        mock_llm = MagicMock()

        async def _mock_astream(messages, **stream_kwargs):
            captured_messages.extend(messages)
            from langchain_core.messages import AIMessageChunk

            yield AIMessageChunk(content="<FleetStatCard title='test' value={1} />")

        mock_llm.astream = _mock_astream
        return mock_llm

    with patch("infra_brain.llm.get_chat_model", side_effect=_mock_get_chat_model):
        with patch("infra_brain.callbacks.registry.build_async_callbacks", return_value=[]):
            resp = authed_client.post(
                "/api/dashboard/custom-view",
                json={"prompt": "show total resource count"},
            )
            assert resp.status_code == 200

    # At least one message contains the user's prompt
    contents = [m.content for m in captured_messages if hasattr(m, "content")]
    assert len(captured_messages) > 0, f"No messages captured: {captured_messages}"
    assert any("show total resource count" in c for c in contents)


def test_custom_view_ndjson_format(authed_client, monkeypatch):
    """Each chunk of the response is a valid NDJSON line."""

    async def _mock_astream(messages, **kwargs):
        from langchain_core.messages import AIMessageChunk

        yield AIMessageChunk(content="<FleetStatCard ")
        yield AIMessageChunk(content='title="Test" value={5} />')

    mock_llm = MagicMock()
    mock_llm.astream = _mock_astream

    with patch("infra_brain.llm.get_chat_model", return_value=mock_llm):
        with patch("infra_brain.callbacks.registry.build_async_callbacks", return_value=[]):
            resp = authed_client.post(
                "/api/dashboard/custom-view",
                json={"prompt": "show a stat card"},
            )

    assert resp.status_code == 200
    lines = [line for line in resp.text.strip().split("\n") if line.strip()]
    # Every line must be valid JSON
    import json as json_mod

    for line in lines:
        obj = json_mod.loads(line)
        assert "token" in obj or "done" in obj


def test_custom_view_redacts_pan_in_stream(authed_client, monkeypatch):
    """N-3: a Luhn-valid PAN in a streamed token must be scrubbed before it
    reaches the client — parity with the chat SSE stream's redact_pans()."""

    async def _mock_astream(messages, **kwargs):
        from langchain_core.messages import AIMessageChunk

        yield AIMessageChunk(content="card on file: 4111111111111111")

    mock_llm = MagicMock()
    mock_llm.astream = _mock_astream

    with patch("infra_brain.llm.get_chat_model", return_value=mock_llm):
        with patch("infra_brain.callbacks.registry.build_async_callbacks", return_value=[]):
            resp = authed_client.post(
                "/api/dashboard/custom-view",
                json={"prompt": "show a card"},
            )

    assert resp.status_code == 200
    assert "4111111111111111" not in resp.text
    assert "REDACTED" in resp.text


def test_custom_view_does_not_leak_raw_exception_text(authed_client, monkeypatch):
    """N-3: an LLM stream failure must not leak the raw exception message to
    the client — only a generic error token, with the real error logged
    server-side."""

    async def _mock_astream(messages, **kwargs):
        raise RuntimeError("super-secret-internal-detail")
        yield  # pragma: no cover - unreachable, makes this an async generator

    mock_llm = MagicMock()
    mock_llm.astream = _mock_astream

    with patch("infra_brain.llm.get_chat_model", return_value=mock_llm):
        with patch("infra_brain.callbacks.registry.build_async_callbacks", return_value=[]):
            resp = authed_client.post(
                "/api/dashboard/custom-view",
                json={"prompt": "trigger failure"},
            )

    assert resp.status_code == 200
    assert "super-secret-internal-detail" not in resp.text
    assert "error" in resp.text.lower()


def test_custom_view_haiku_model(authed_client, monkeypatch):
    """The endpoint uses the claude-haiku-4-5-20251001 model."""
    captured_kwargs = {}

    def _mock_get_chat_model(**kwargs):
        captured_kwargs.update(kwargs)
        mock = MagicMock()

        async def _astream(messages, **kw):
            from langchain_core.messages import AIMessageChunk

            yield AIMessageChunk(content="")

        mock.astream = _astream
        return mock

    with patch("infra_brain.llm.get_chat_model", side_effect=_mock_get_chat_model):
        with patch("infra_brain.callbacks.registry.build_async_callbacks", return_value=[]):
            authed_client.post("/api/dashboard/custom-view", json={"prompt": "test"})

    assert captured_kwargs.get("model") == "claude-haiku-4-5-20251001"
