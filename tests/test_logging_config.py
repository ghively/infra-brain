"""Tests for the shared structured-logging setup (OB-4)."""

import json
import logging


def test_json_formatter_emits_parseable_json_with_expected_fields():
    from infra_brain.logging_config import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="infra_brain.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    parsed = json.loads(formatter.format(record))
    assert parsed["message"] == "hello world"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "infra_brain.test"
    assert "timestamp" in parsed


def test_json_formatter_includes_exc_info_when_present():
    from infra_brain.logging_config import JsonFormatter

    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="infra_brain.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    parsed = json.loads(formatter.format(record))
    assert "ValueError: boom" in parsed["exc_info"]


def test_configure_logging_installs_json_formatter_on_root_handlers():
    from infra_brain.logging_config import JsonFormatter, configure_logging

    configure_logging()
    assert logging.root.handlers, "configure_logging must install at least one handler"
    assert all(isinstance(h.formatter, JsonFormatter) for h in logging.root.handlers)


def test_json_formatter_passes_through_extra_fields():
    from infra_brain.logging_config import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="infra_brain.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="sweep tick",
        args=(),
        exc_info=None,
    )
    record.domain = "linux"
    record.sweep_id = "abc-123"
    parsed = json.loads(formatter.format(record))
    assert parsed["domain"] == "linux"
    assert parsed["sweep_id"] == "abc-123"
    assert parsed["message"] == "sweep tick"


def test_json_formatter_reprs_non_json_serializable_extra_fields():
    from infra_brain.logging_config import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="infra_brain.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="unserializable extra",
        args=(),
        exc_info=None,
    )
    record.weird = object()
    parsed = json.loads(formatter.format(record))
    assert "object" in parsed["weird"]


def test_configure_logging_uses_text_formatter_when_log_format_env_is_text(monkeypatch):
    from infra_brain.logging_config import _TextFormatter, configure_logging

    monkeypatch.setenv("LOG_FORMAT", "text")
    configure_logging()
    assert all(isinstance(h.formatter, _TextFormatter) for h in logging.root.handlers)


def test_configure_logging_defaults_to_json_when_log_format_unset(monkeypatch):
    from infra_brain.logging_config import JsonFormatter, configure_logging

    monkeypatch.delenv("LOG_FORMAT", raising=False)
    configure_logging()
    assert all(isinstance(h.formatter, JsonFormatter) for h in logging.root.handlers)


def test_text_formatter_appends_extra_fields_as_key_value_pairs():
    from infra_brain.logging_config import _TextFormatter

    formatter = _TextFormatter()
    record = logging.LogRecord(
        name="infra_brain.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.domain = "windows"
    out = formatter.format(record)
    assert "hello" in out
    assert "domain='windows'" in out


def test_configure_logging_installs_request_id_filter_on_root_handlers():
    from infra_brain.logging_config import configure_logging
    from infra_brain.request_context import RequestIdLogFilter

    configure_logging()
    assert logging.root.handlers
    assert all(
        any(isinstance(f, RequestIdLogFilter) for f in h.filters) for h in logging.root.handlers
    )
