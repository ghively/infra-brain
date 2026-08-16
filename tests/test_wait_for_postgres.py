"""Unit tests for the deduplicated CI postgres-wait helper (CICD-4)."""

from unittest.mock import MagicMock, patch


def test_wait_for_postgres_returns_true_on_first_successful_connect():
    import sys

    sys.path.insert(0, "scripts/ci")
    from wait_for_postgres import wait_for_postgres

    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__ = MagicMock(return_value=None)
    mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    with patch("sqlalchemy.create_engine", return_value=mock_engine):
        assert wait_for_postgres("postgresql://x", timeout=5) is True


def test_wait_for_postgres_returns_false_after_timeout():
    import sys

    sys.path.insert(0, "scripts/ci")
    from wait_for_postgres import wait_for_postgres

    with (
        patch("sqlalchemy.create_engine", side_effect=Exception("connection refused")),
        patch("time.sleep"),
    ):
        assert wait_for_postgres("postgresql://x", timeout=2) is False
