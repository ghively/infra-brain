"""Bitwarden Secrets Manager bootstrap.

Loads secrets from Bitwarden Secrets Manager and injects them into
``os.environ`` at startup.  Uses ``os.environ.setdefault`` so that any
value already present in the environment (e.g. from a local ``.env`` or an
explicit export) is never overwritten.

The bitwarden-sdk package is lazy-imported so the application runs normally
when the SDK is not installed (e.g. in local dev with a plain ``.env``).
"""

import logging
import os

from infra_brain.config import get_settings

log = logging.getLogger(__name__)


def _build_bws_client(access_token: str):
    """Create and return a Bitwarden Secrets Manager client.

    Lazy-imports the SDK so the rest of the app runs without it installed.
    Raises ``ImportError`` if the SDK is not available.
    """
    # bitwarden_sdk >= 1.0 exposes `BitwardenClient` and `ClientSettings`
    from bitwarden_sdk import BitwardenClient, ClientSettings  # type: ignore[import]

    client = BitwardenClient(ClientSettings())
    client.auth().login_access_token(access_token)
    return client


def load_secrets_into_env() -> None:
    """Fetch secrets from Bitwarden and inject them into ``os.environ``.

    * If ``BWS_ACCESS_TOKEN`` is not set the function is a no-op (logs at
      DEBUG level so local ``.env`` development works unchanged).
    * Existing environment variables are never overwritten (setdefault
      semantics).
    * After injection, ``get_settings.cache_clear()`` is called so that
      pydantic-settings re-reads the updated environment.
    * Any exception (missing SDK, network error, auth failure) is caught and
      logged as a warning — the app continues to start.
    """
    access_token = os.environ.get("BWS_ACCESS_TOKEN")
    if not access_token:
        log.debug("[secrets] BWS_ACCESS_TOKEN not set — skipping Bitwarden bootstrap")
        return

    log.info("[secrets] BWS_ACCESS_TOKEN detected — fetching secrets from Bitwarden")
    try:
        client = _build_bws_client(access_token)
        secrets = client.secrets()
        injected = 0
        for secret in secrets:
            prev = os.environ.get(secret.key)
            os.environ.setdefault(secret.key, secret.value)
            if prev is None:
                injected += 1
                log.debug("[secrets] Injected %s", secret.key)
            else:
                log.debug("[secrets] Skipped %s (already set)", secret.key)
        log.info("[secrets] Injected %d secret(s) from Bitwarden", injected)
    except ImportError as exc:
        log.warning("[secrets] bitwarden-sdk not installed — cannot load secrets: %s", exc)
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("[secrets] Failed to load secrets from Bitwarden: %s", exc)
        return

    # Re-read settings now that env vars may have changed
    get_settings.cache_clear()
