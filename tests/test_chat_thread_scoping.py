"""Wave 1.5d — chat checkpointer per-user scoping (F-028)."""

from infra_brain.api.routers.ui import _scoped_thread_id


def test_same_client_thread_id_isolated_per_user():
    """User A and user B supplying the SAME client thread_id must map to different
    checkpointer keys, so B cannot resume A's history."""
    a = _scoped_thread_id("alice", "shared-thread-123")
    b = _scoped_thread_id("bob", "shared-thread-123")
    assert a != b
    assert a.startswith("alice::")
    assert b.startswith("bob::")


def test_scoped_id_is_stable_for_same_user():
    """The same (user, thread) always yields the same key (history persists)."""
    assert _scoped_thread_id("alice", "t1") == _scoped_thread_id("alice", "t1")
