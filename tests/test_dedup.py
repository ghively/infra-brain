from unittest.mock import MagicMock, patch

import pytest
import redis as _redis

from infra_brain import dedup as dedup_mod
from infra_brain.dedup import LockBackendUnavailable, release, try_acquire


def test_try_acquire_returns_true_when_no_lock():
    mock_redis = MagicMock()
    mock_redis.set.return_value = True

    with patch("infra_brain.dedup.get_redis", return_value=mock_redis):
        result = try_acquire("linux", "all")

    # C3: now returns a token string (truthy) rather than bool True
    assert result is not None
    assert result is not False


def test_try_acquire_returns_false_when_lock_exists():
    mock_redis = MagicMock()
    mock_redis.set.return_value = None  # Redis SET NX returns None if key exists

    with patch("infra_brain.dedup.get_redis", return_value=mock_redis):
        result = try_acquire("linux", "all")

    assert result is None  # C3: returns None (falsy) when not acquired


def test_release_deletes_key():
    mock_redis = MagicMock()

    with patch("infra_brain.dedup.get_redis", return_value=mock_redis):
        release("linux", "all")

    mock_redis.delete.assert_called_once_with("infra_brain:lock:linux:all")


# --- C3: Token-safe dedup tests ---


def test_try_acquire_returns_token_string_on_success():
    """C3: try_acquire returns a hex-token string (not bool) when lock is acquired."""
    mock_redis = MagicMock()
    mock_redis.set.return_value = True

    with patch("infra_brain.dedup.get_redis", return_value=mock_redis):
        token = try_acquire("cloud", "all", ttl_seconds=30)

    assert isinstance(token, str), "try_acquire must return a token string when acquired"
    assert len(token) == 32, "token should be uuid4 hex (32 chars)"


def test_try_acquire_stores_token_as_value():
    """C3: the value stored in Redis must be the returned token, not '1'."""
    mock_redis = MagicMock()
    mock_redis.set.return_value = True

    with patch("infra_brain.dedup.get_redis", return_value=mock_redis):
        token = try_acquire("cloud", "all", ttl_seconds=30)

    call_args = mock_redis.set.call_args
    call_args.args[1] if call_args.args else call_args.kwargs.get("value", "")
    # Also check positional call: set(key, value, ...)
    all_args = list(call_args.args) + list(call_args.kwargs.values())
    assert token in all_args, "stored redis value must equal the returned token"


def test_try_acquire_returns_none_on_failure():
    """C3: try_acquire returns None when lock is not acquired (not False)."""
    mock_redis = MagicMock()
    mock_redis.set.return_value = None

    with patch("infra_brain.dedup.get_redis", return_value=mock_redis):
        result = try_acquire("cloud", "all")

    assert result is None


def test_release_uses_lua_compare_and_delete():
    """C3: release must call redis.eval with Lua script and pass the token."""
    mock_redis = MagicMock()
    mock_redis.eval.return_value = 1

    with patch("infra_brain.dedup.get_redis", return_value=mock_redis):
        release("cloud", "all", token="abc123token")

    mock_redis.eval.assert_called_once()
    call_args = mock_redis.eval.call_args
    lua_script = call_args.args[0] if call_args.args else call_args.kwargs.get("script", "")
    assert "redis.call" in lua_script, "release must use a Lua script"
    assert "abc123token" in str(call_args), "token must be passed to eval"


def test_release_without_token_falls_back_to_delete():
    """C3: release(domain, scope) with no token still works (backward compat)."""
    mock_redis = MagicMock()

    with patch("infra_brain.dedup.get_redis", return_value=mock_redis):
        release("linux", "all")  # no token

    mock_redis.delete.assert_called_once_with("infra_brain:lock:linux:all")


# --- F-029: TTL >= collect timeout; fail-loud on Redis error -----------------


def test_default_ttl_covers_collect_timeout():
    settings = MagicMock()
    settings.collect_timeout_seconds = 300
    with patch.object(dedup_mod, "get_settings", return_value=settings):
        assert dedup_mod.default_ttl_seconds() == 420


def test_try_acquire_uses_default_ttl():
    settings = MagicMock()
    settings.collect_timeout_seconds = 300
    fake_redis = MagicMock()
    fake_redis.set.return_value = True
    with (
        patch.object(dedup_mod, "get_settings", return_value=settings),
        patch.object(dedup_mod, "get_redis", return_value=fake_redis),
    ):
        token = dedup_mod.try_acquire("linux", "all")
    assert token
    assert fake_redis.set.call_args.kwargs["ex"] == 420


def test_try_acquire_explicit_ttl_still_honored():
    fake_redis = MagicMock()
    fake_redis.set.return_value = True
    with patch.object(dedup_mod, "get_redis", return_value=fake_redis):
        dedup_mod.try_acquire("linux", "all", ttl_seconds=99)
    assert fake_redis.set.call_args.kwargs["ex"] == 99


def test_try_acquire_raises_lock_backend_unavailable_on_redis_error():
    fake_redis = MagicMock()
    fake_redis.set.side_effect = _redis.ConnectionError("connection refused")
    with patch.object(dedup_mod, "get_redis", return_value=fake_redis):
        with pytest.raises(LockBackendUnavailable, match="linux/all"):
            dedup_mod.try_acquire("linux", "all")


def test_release_swallows_redis_error_with_log():
    fake_redis = MagicMock()
    fake_redis.eval.side_effect = _redis.ConnectionError("connection refused")
    with patch.object(dedup_mod, "get_redis", return_value=fake_redis):
        dedup_mod.release("linux", "all", token="abc")  # must not raise


# --- TRK-117 (gotcha #2): per-domain lock TTL respects the AgentSpec override --
#
# If a domain gets a per-domain collect-timeout override (graph_maintenance:
# 1200s) but the dedup lock TTL keeps using the GLOBAL default (300+120=420s),
# the lock expires while collect() is still running and a duplicate dispatch
# can acquire the lock and start a SECOND overlapping full pass. The TTL must
# track the SAME per-domain override the collect() guard uses.


def test_default_ttl_uses_per_domain_override():
    """default_ttl_seconds(domain) must reflect the domain's spec override."""
    settings = MagicMock()
    settings.collect_timeout_seconds = 300
    with patch.object(dedup_mod, "get_settings", return_value=settings):
        # graph_maintenance declares collect_timeout_seconds=1200 -> 1200+120.
        assert dedup_mod.default_ttl_seconds("graph_maintenance") == 1320
        # a domain with no override falls back to the global 300 -> 300+120.
        assert dedup_mod.default_ttl_seconds("linux") == 420
        # no domain given -> global default.
        assert dedup_mod.default_ttl_seconds() == 420


def test_default_ttl_override_beats_global_change():
    """The override is an absolute value, independent of the global setting."""
    settings = MagicMock()
    settings.collect_timeout_seconds = 30  # tiny global
    with patch.object(dedup_mod, "get_settings", return_value=settings):
        # graph_maintenance still uses its 1200s override, not the 30s global.
        assert dedup_mod.default_ttl_seconds("graph_maintenance") == 1320
        assert dedup_mod.default_ttl_seconds("linux") == 150


def test_try_acquire_applies_per_domain_ttl_to_redis():
    """try_acquire(domain, scope) must pass the per-domain TTL as Redis `ex`.

    This is the end-to-end proof the lock cannot expire mid-run for a domain
    with a longer per-domain collect timeout.
    """
    settings = MagicMock()
    settings.collect_timeout_seconds = 300
    fake_redis = MagicMock()
    fake_redis.set.return_value = True
    with (
        patch.object(dedup_mod, "get_settings", return_value=settings),
        patch.object(dedup_mod, "get_redis", return_value=fake_redis),
    ):
        token = dedup_mod.try_acquire("graph_maintenance", "all")
    assert token
    assert fake_redis.set.call_args.kwargs["ex"] == 1320, (
        "graph_maintenance lock TTL must reflect its 1200s per-domain override, "
        "not the 420s global default"
    )
