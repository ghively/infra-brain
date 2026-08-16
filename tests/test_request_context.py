"""Tests for the request-ID context primitive (#88)."""

import logging

from infra_brain.request_context import (
    RequestIdLogFilter,
    get_request_id,
    request_id_var,
)


def test_get_request_id_is_none_outside_a_request():
    assert get_request_id() is None


def test_request_id_var_set_and_reset():
    token = request_id_var.set("abc-123")
    try:
        assert get_request_id() == "abc-123"
    finally:
        request_id_var.reset(token)
    assert get_request_id() is None


def test_filter_attaches_request_id_when_set():
    record = logging.LogRecord(
        name="infra_brain.test", level=logging.INFO, pathname=__file__,
        lineno=1, msg="hello", args=(), exc_info=None,
    )
    token = request_id_var.set("req-xyz")
    try:
        result = RequestIdLogFilter().filter(record)
    finally:
        request_id_var.reset(token)
    assert result is True
    assert record.request_id == "req-xyz"


def test_filter_does_not_attach_request_id_when_unset():
    record = logging.LogRecord(
        name="infra_brain.test", level=logging.INFO, pathname=__file__,
        lineno=1, msg="hello", args=(), exc_info=None,
    )
    result = RequestIdLogFilter().filter(record)
    assert result is True
    assert not hasattr(record, "request_id")
