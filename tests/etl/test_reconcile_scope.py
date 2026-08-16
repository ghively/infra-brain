"""Unit tests for ``ReconcileScope`` — the shared observed/failed reconciliation guard.

C-1: a reconciliation pass (close / prune / NULL-out / delete-then-reinsert) must
only ever operate over units whose upstream fetch is PROVEN to have succeeded. A
unit whose fetch raised looks identical to a unit that legitimately returned zero
rows, and treating the two the same is destructive: it deletes/closes live data on
a transient upstream 500.

``ReconcileScope`` is the primitive that keeps the two apart. These tests pin its
behaviour directly (no agent, no DB), because six more collectors will build their
reconciliation scope on it.
"""

import pytest

from infra_brain.etl.base import CollectOutcome, ReconcileScope


def test_empty_scope_is_empty_and_error_free():
    scope = ReconcileScope()
    assert scope.safe_scope == set()
    assert scope.errors == []
    assert not scope.has_failures
    assert scope.observed_count == 0
    assert scope.failed_count == 0


def test_observed_keys_land_in_safe_scope():
    scope = ReconcileScope()
    scope.observed("a")
    scope.observed("b")
    assert scope.safe_scope == {"a", "b"}
    assert scope.errors == []
    assert scope.observed_count == 2


def test_observing_the_same_key_twice_is_idempotent():
    scope = ReconcileScope()
    scope.observed("a")
    scope.observed("a")
    assert scope.safe_scope == {"a"}
    assert scope.observed_count == 1


def test_failed_key_is_excluded_from_safe_scope():
    """The whole point: a failed fetch must NOT license reconciliation of that unit."""
    scope = ReconcileScope()
    scope.observed("ok")
    scope.failed("broken", RuntimeError("upstream 500"))
    assert scope.safe_scope == {"ok"}
    assert scope.failed_keys == {"broken"}
    assert scope.has_failures
    assert scope.failed_count == 1


def test_failure_is_sticky_regardless_of_call_order():
    """Fail-closed: once a unit's fetch has failed, a later partial success for the
    same key does not restore it to safe_scope (and vice versa). A half-observed
    unit cannot prove the absence of a row, which is exactly what reconciliation
    asserts when it closes/deletes."""
    fail_then_observe = ReconcileScope()
    fail_then_observe.failed("k", RuntimeError("boom"))
    fail_then_observe.observed("k")

    observe_then_fail = ReconcileScope()
    observe_then_fail.observed("k")
    observe_then_fail.failed("k", RuntimeError("boom"))

    for scope in (fail_then_observe, observe_then_fail):
        assert scope.safe_scope == set()
        assert scope.failed_keys == {"k"}
        assert scope.has_failures


def test_errors_carry_key_label_and_exception_detail():
    scope = ReconcileScope(label="asset")
    scope.failed(42, RuntimeError("HTTP 500 from Rapid7"))
    assert scope.errors == ["asset 42 fetch failed: RuntimeError: HTTP 500 from Rapid7"]


def test_errors_default_label_is_generic():
    scope = ReconcileScope()
    scope.failed("k", ValueError("nope"))
    assert scope.errors == ["unit k fetch failed: ValueError: nope"]


def test_error_for_exception_with_empty_message_still_names_the_type():
    scope = ReconcileScope(label="host")
    scope.failed("h1", TimeoutError())
    assert scope.errors == ["host h1 fetch failed: TimeoutError"]


def test_failed_accepts_a_plain_string_reason():
    scope = ReconcileScope(label="asset")
    scope.failed(7, "non-200 response with no body")
    assert scope.errors == ["asset 7 fetch failed: non-200 response with no body"]


def test_errors_are_dsn_scrubbed():
    """SEC-2: error strings funnel into CollectionRun.error_message (persisted and
    rendered on the dashboard) — embedded DSN credentials must be redacted here
    exactly as they are at every other collector error site."""
    scope = ReconcileScope(label="asset")
    scope.failed(
        1,
        RuntimeError("could not connect: postgresql://svc_user:sup3rs3cret@db.internal:5432/infra"),
    )
    (msg,) = scope.errors
    assert "sup3rs3cret" not in msg
    assert "svc_user" not in msg
    assert "postgresql://[REDACTED]@db.internal:5432/infra" in msg


def test_errors_preserve_call_order_one_entry_per_failure():
    scope = ReconcileScope(label="asset")
    scope.failed(1, RuntimeError("first"))
    scope.failed(2, RuntimeError("second"))
    scope.failed(1, RuntimeError("first again"))
    assert scope.errors == [
        "asset 1 fetch failed: RuntimeError: first",
        "asset 2 fetch failed: RuntimeError: second",
        "asset 1 fetch failed: RuntimeError: first again",
    ]


def test_errors_property_returns_a_copy_not_the_internal_list():
    scope = ReconcileScope()
    scope.failed("k", RuntimeError("boom"))
    scope.errors.append("injected")
    assert len(scope.errors) == 1


def test_safe_scope_returns_a_copy_not_the_internal_set():
    scope = ReconcileScope()
    scope.observed("a")
    scope.safe_scope.add("b")
    assert scope.safe_scope == {"a"}


def test_two_scopes_do_not_share_state():
    """Guards the classic mutable-default-argument trap on a dataclass."""
    a = ReconcileScope()
    b = ReconcileScope()
    a.observed("x")
    a.failed("y", RuntimeError("boom"))
    assert b.safe_scope == set()
    assert b.errors == []


def test_keys_may_be_any_hashable_including_uuid_like_objects():
    import uuid as _uuid

    rid = _uuid.uuid4()
    scope = ReconcileScope(label="resource")
    scope.observed(rid)
    assert scope.safe_scope == {rid}


def test_unhashable_key_is_rejected_loudly():
    scope = ReconcileScope()
    with pytest.raises(TypeError):
        scope.observed(["not", "hashable"])


def test_errors_feed_collect_outcome_partial_not_failed():
    """The documented integration contract: dropping ``scope.errors`` into
    ``CollectOutcome.errors`` reuses the EXISTING ok/partial/failed machinery —
    a run that fetched some units and failed others reports "partial", never a
    silent "completed". No new signalling channel is introduced."""
    scope = ReconcileScope(label="asset")
    scope.observed(1)
    scope.failed(2, RuntimeError("boom"))

    outcome = CollectOutcome(items=[{"name": "a"}], errors=scope.errors)
    assert outcome.status == "partial"


def test_all_units_failing_with_no_data_is_a_failed_outcome():
    scope = ReconcileScope(label="asset")
    scope.failed(1, RuntimeError("boom"))
    scope.failed(2, RuntimeError("boom"))

    outcome = CollectOutcome(items=[], errors=scope.errors)
    assert outcome.status == "failed"


def test_clean_scope_yields_an_ok_outcome():
    scope = ReconcileScope(label="asset")
    scope.observed(1)
    outcome = CollectOutcome(items=[{"name": "a"}], errors=scope.errors)
    assert outcome.status == "ok"
