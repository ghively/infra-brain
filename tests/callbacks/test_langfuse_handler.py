import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from infra_brain.callbacks.dlp import redact_pans
from infra_brain.callbacks.langfuse_handler import _mask_otel_spans, maybe_langfuse_handler
from infra_brain.callbacks.registry import build_async_callbacks, build_callbacks


def _clear_langfuse_modules():
    for name in list(sys.modules):
        if name == "langfuse" or name.startswith("langfuse."):
            del sys.modules[name]


@pytest.fixture(autouse=True)
def _reset_handler_memo():
    """The process-wide handler memo must not leak between tests."""
    from infra_brain.callbacks import langfuse_handler as lh

    lh._HANDLER_MEMO.clear()
    yield
    lh._HANDLER_MEMO.clear()


def test_handler_memoized_across_builds():
    """One handler per process: repeated builds return the SAME instance and
    the Langfuse client is constructed exactly once (no per-agent exporter
    churn). Failure results are deliberately NOT memoized (separate test)."""
    _clear_langfuse_modules()
    fake_handler = MagicMock(name="handler")
    fake_langfuse_mod = types.ModuleType("langfuse")
    fake_langfuse_mod.Langfuse = MagicMock(name="Langfuse")
    fake_langchain_mod = types.ModuleType("langfuse.langchain")
    fake_langchain_mod.CallbackHandler = MagicMock(return_value=fake_handler)
    with (
        patch.dict(
            sys.modules, {"langfuse": fake_langfuse_mod, "langfuse.langchain": fake_langchain_mod}
        ),
        patch(
            "infra_brain.callbacks.langfuse_handler.get_settings", return_value=_EnabledSettings()
        ),
    ):
        first = maybe_langfuse_handler()
        second = maybe_langfuse_handler()
    assert first is fake_handler and second is fake_handler
    assert fake_langfuse_mod.Langfuse.call_count == 1
    assert fake_langchain_mod.CallbackHandler.call_count == 1


class _DisabledSettings:
    langfuse_enabled = False
    langfuse_host = ""
    langfuse_public_key = ""
    langfuse_secret_key = ""
    scan_readonly_enforce = True
    dlp_fail_closed = True


class _EnabledSettings:
    langfuse_enabled = True
    langfuse_host = "https://langfuse.example.com"
    langfuse_public_key = "pk-test"
    langfuse_secret_key = "sk-test"
    scan_readonly_enforce = True
    dlp_fail_closed = True


def test_disabled_returns_none_and_never_imports_langfuse():
    _clear_langfuse_modules()
    with patch(
        "infra_brain.callbacks.langfuse_handler.get_settings", return_value=_DisabledSettings()
    ):
        result = maybe_langfuse_handler()
    assert result is None
    assert "langfuse" not in sys.modules


def test_disabled_registry_unchanged_and_no_langfuse_import():
    _clear_langfuse_modules()
    with patch(
        "infra_brain.callbacks.langfuse_handler.get_settings", return_value=_DisabledSettings()
    ):
        sync_cbs = build_callbacks("TestAgent", "linux")
        async_cbs = build_async_callbacks("TestAgent", "linux")
    assert len(sync_cbs) == 4
    assert len(async_cbs) == 4
    assert "langfuse" not in sys.modules


def test_enabled_missing_keys_returns_none():
    class _PartialSettings(_EnabledSettings):
        langfuse_public_key = ""

    with patch(
        "infra_brain.callbacks.langfuse_handler.get_settings", return_value=_PartialSettings()
    ):
        assert maybe_langfuse_handler() is None


def _install_fake_langfuse_module():
    """Build fake `langfuse` / `langfuse.langchain` modules and register them
    in sys.modules so maybe_langfuse_handler()'s local imports resolve to
    controllable test doubles instead of the real SDK."""
    fake_client_cls = MagicMock(name="Langfuse")
    fake_handler_instance = MagicMock(name="CallbackHandlerInstance")
    fake_handler_cls = MagicMock(name="CallbackHandler", return_value=fake_handler_instance)

    langfuse_mod = types.ModuleType("langfuse")
    langfuse_mod.Langfuse = fake_client_cls
    langchain_mod = types.ModuleType("langfuse.langchain")
    langchain_mod.CallbackHandler = fake_handler_cls

    return langfuse_mod, langchain_mod, fake_client_cls, fake_handler_cls, fake_handler_instance


def test_enabled_and_configured_handler_present_and_last_in_both_chains():
    langfuse_mod, langchain_mod, fake_client_cls, fake_handler_cls, fake_handler_instance = (
        _install_fake_langfuse_module()
    )
    with (
        patch.dict(sys.modules, {"langfuse": langfuse_mod, "langfuse.langchain": langchain_mod}),
        patch(
            "infra_brain.callbacks.langfuse_handler.get_settings", return_value=_EnabledSettings()
        ),
    ):
        handler = maybe_langfuse_handler()
        assert handler is fake_handler_instance
        fake_client_cls.assert_called_once()
        assert fake_client_cls.call_args.kwargs["public_key"] == "pk-test"
        assert fake_client_cls.call_args.kwargs["host"] == "https://langfuse.example.com"

        with patch("infra_brain.callbacks.registry.get_settings") as reg_mock:
            reg_mock.return_value.scan_readonly_enforce = True
            reg_mock.return_value.dlp_fail_closed = True
            sync_cbs = build_callbacks("TestAgent", "linux")
            async_cbs = build_async_callbacks("TestAgent", "linux")

    assert len(sync_cbs) == 5
    assert len(async_cbs) == 5
    assert sync_cbs[-1] is fake_handler_instance
    assert async_cbs[-1] is fake_handler_instance


def test_construction_failure_returns_none_and_chains_intact():
    langfuse_mod, langchain_mod, fake_client_cls, fake_handler_cls, _ = (
        _install_fake_langfuse_module()
    )
    fake_client_cls.side_effect = RuntimeError("boom")

    with (
        patch.dict(sys.modules, {"langfuse": langfuse_mod, "langfuse.langchain": langchain_mod}),
        patch(
            "infra_brain.callbacks.langfuse_handler.get_settings", return_value=_EnabledSettings()
        ),
    ):
        handler = maybe_langfuse_handler()
        assert handler is None

        with patch("infra_brain.callbacks.registry.get_settings") as reg_mock:
            reg_mock.return_value.scan_readonly_enforce = True
            reg_mock.return_value.dlp_fail_closed = True
            sync_cbs = build_callbacks("TestAgent", "linux")
            async_cbs = build_async_callbacks("TestAgent", "linux")

    assert len(sync_cbs) == 4
    assert len(async_cbs) == 4


class _FakeSpan:
    def __init__(self, attributes):
        self.attributes = attributes


class _FakeParams:
    def __init__(self, spans):
        self.spans = spans


@pytest.mark.parametrize(
    "pan",
    ["4111111111111111", "4111-1111-1111-1111"],
)
def test_mask_otel_spans_scrubs_pan_fixture(pan):
    span = _FakeSpan(
        attributes={"gen_ai.prompt.0.content": f"card on file: {pan}", "other": 42, "flag": True}
    )
    params = _FakeParams(spans={"span-1": span})

    result = _mask_otel_spans(params=params)

    assert result is not None
    patch_obj = result.span_patches["span-1"]
    assert patch_obj.set_attributes["gen_ai.prompt.0.content"] == redact_pans(
        f"card on file: {pan}"
    )
    assert "****-REDACTED-****" in patch_obj.set_attributes["gen_ai.prompt.0.content"]
    # Non-string / non-PAN attributes are left out of the sparse patch.
    assert "other" not in patch_obj.set_attributes
    assert "flag" not in patch_obj.set_attributes


def test_mask_otel_spans_returns_none_when_nothing_to_redact():
    span = _FakeSpan(attributes={"gen_ai.prompt.0.content": "no sensitive data here", "count": 3})
    params = _FakeParams(spans={"span-1": span})

    result = _mask_otel_spans(params=params)

    assert result is None
