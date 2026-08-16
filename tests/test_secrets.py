"""Tests for Bitwarden Secrets Manager bootstrap (Task 25)."""

import os
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# No-op path: BWS_ACCESS_TOKEN not set
# ---------------------------------------------------------------------------


def test_load_secrets_noop_when_no_token(monkeypatch):
    """When BWS_ACCESS_TOKEN is absent, load_secrets_into_env is a no-op."""
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    # Import fresh
    from infra_brain.secrets import load_secrets_into_env

    # Should not raise, should not change env
    before = dict(os.environ)
    load_secrets_into_env()
    after = dict(os.environ)
    assert before == after


# ---------------------------------------------------------------------------
# Injection path: BWS_ACCESS_TOKEN set, fake client injects secrets
# ---------------------------------------------------------------------------


def test_load_secrets_injects_env(monkeypatch):
    """When BWS_ACCESS_TOKEN is set, secrets are injected into os.environ."""
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "tok-abc123")
    # Remove any pre-existing key to verify injection
    monkeypatch.delenv("MY_API_KEY", raising=False)

    fake_secret = MagicMock()
    fake_secret.key = "MY_API_KEY"
    fake_secret.value = "supersecret"

    fake_client = MagicMock()
    fake_client.secrets.return_value = [fake_secret]

    with patch("infra_brain.secrets._build_bws_client", return_value=fake_client):
        from infra_brain.secrets import load_secrets_into_env

        load_secrets_into_env()

    assert os.environ.get("MY_API_KEY") == "supersecret"


def test_load_secrets_setdefault_semantics(monkeypatch):
    """Existing env vars must NOT be overwritten by Bitwarden secrets."""
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "tok-abc123")
    monkeypatch.setenv("EXISTING_KEY", "original_value")

    fake_secret = MagicMock()
    fake_secret.key = "EXISTING_KEY"
    fake_secret.value = "new_value_from_bws"

    fake_client = MagicMock()
    fake_client.secrets.return_value = [fake_secret]

    with patch("infra_brain.secrets._build_bws_client", return_value=fake_client):
        from infra_brain.secrets import load_secrets_into_env

        load_secrets_into_env()

    # Original value must be preserved
    assert os.environ["EXISTING_KEY"] == "original_value"


def test_load_secrets_clears_settings_cache(monkeypatch):
    """After injecting secrets, get_settings cache is cleared."""
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "tok-abc123")
    monkeypatch.delenv("SOME_KEY", raising=False)

    fake_secret = MagicMock()
    fake_secret.key = "SOME_KEY"
    fake_secret.value = "some_val"

    fake_client = MagicMock()
    fake_client.secrets.return_value = [fake_secret]

    with patch("infra_brain.secrets._build_bws_client", return_value=fake_client):
        with patch("infra_brain.secrets.get_settings") as mock_get_settings:
            mock_get_settings.cache_clear = MagicMock()
            from infra_brain.secrets import load_secrets_into_env

            load_secrets_into_env()
            mock_get_settings.cache_clear.assert_called_once()


def test_load_secrets_no_sdk_installed(monkeypatch):
    """If bitwarden SDK not installed and token set, should log and not crash."""
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "tok-abc123")

    with patch(
        "infra_brain.secrets._build_bws_client",
        side_effect=ImportError("bitwarden_sdk not installed"),
    ):
        from infra_brain.secrets import load_secrets_into_env

        # Should not raise — logs a warning
        load_secrets_into_env()


def test_load_secrets_client_error(monkeypatch):
    """If client.secrets() raises, should log and not crash."""
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "tok-abc123")

    fake_client = MagicMock()
    fake_client.secrets.side_effect = Exception("network error")

    with patch("infra_brain.secrets._build_bws_client", return_value=fake_client):
        from infra_brain.secrets import load_secrets_into_env

        # Should not raise
        load_secrets_into_env()
