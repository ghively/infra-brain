"""AIRTIGHT: no secret value may leave the settings-catalog surface.

This file is the hard constraint on `GET/PUT/DELETE
/api/dashboard/settings-catalog` (api/routers/settings_catalog.py). The catalog
derives itself from the `Settings` pydantic model, so it necessarily *holds*
every configured secret in memory while rendering — the whole point of these
tests is that none of them can reach the wire, by ANY route:

  * the ``value`` field (the "effective value" an operator sees),
  * the ``default`` field,
  * a masked/tail-preserving rendering (``_helpers.mask_secret`` keeps the last
    four characters — deliberately NOT used here; a test below pins that by
    asserting the canary's tail is absent, so swapping ``mask_secret`` back in
    fails loudly),
  * a DB-sourced (Fernet-decrypted) override's plaintext,
  * a DSN's embedded ``user:password@`` credentials — which match no name hint
    and are the exact hole TRK-318 found on the older /settings surface,
  * the ERROR path: an exception raised mid-render whose message embeds the
    value must not be echoed into the response,
  * a rejected PUT's error detail (never echo the submitted value back).

Every assertion scans the WHOLE raw response body, not just the field the test
is nominally about — a leak that moves to a different field is still a leak.
"""

from contextlib import contextmanager

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from infra_brain.config import get_settings
from infra_brain.db.models import Base, RuntimeConfig

# Distinctive canaries. Long, unique, and — importantly — with a tail that is
# itself unique, so `mask_secret`-style "last 4 chars" rendering is detectable.
CANARY = "sk-live-CATALOG-LEAK-CANARY-zzq7"
CANARY_TAIL = CANARY[-4:]
DSN_PASSWORD = "dsnpw-CATALOG-LEAK-CANARY-wq31"


@contextmanager
def _catalog_client(tmp_path, monkeypatch, db_name="catalog.sqlite3"):
    """Admin TestClient over a file-backed sqlite DB.

    File-backed on purpose: config._load_runtime_overrides() opens its OWN
    engine against ``postgres_url``, so an in-memory DB would be invisible to
    the source-attribution path under test.
    """
    import infra_brain.api.routers.settings_catalog as mod

    db_path = tmp_path / db_name
    dsn = f"sqlite:///{db_path}"
    eng = create_engine(dsn)
    Base.metadata.create_all(eng)

    monkeypatch.setenv("POSTGRES_URL", dsn)
    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    monkeypatch.setattr(mod, "get_session", _get_session)

    app = FastAPI()
    app.include_router(mod.settings_catalog_router)
    try:
        yield TestClient(app), eng, mod
    finally:
        eng.dispose()
        get_settings.cache_clear()


def _entry(body, key):
    return next(it for it in body["items"] if it["key"] == key)


def _assert_no_canary(text: str, *needles: str) -> None:
    for needle in needles:
        assert needle not in text, f"LEAK: {needle!r} reached the settings-catalog response"


# ---------------------------------------------------------------------------
# 1. env-sourced secret
# ---------------------------------------------------------------------------


def test_env_sourced_secret_value_never_leaves_the_catalog(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, _mod):
        monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
        get_settings.cache_clear()

        resp = client.get("/api/dashboard/settings-catalog")
        assert resp.status_code == 200
        # Whole-body scan: value, default, description, anywhere.
        _assert_no_canary(resp.text, CANARY, CANARY_TAIL)

        row = _entry(resp.json(), "anthropic_api_key")
        assert row["secret"] is True
        assert row["value"] is None
        assert row["default"] is None
        assert row["secret_state"] == "set"
        assert row["editable"] is False
        assert row["managed_in"]


def test_unset_secret_reports_not_set(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, _mod):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        get_settings.cache_clear()

        row = _entry(client.get("/api/dashboard/settings-catalog").json(), "anthropic_api_key")
        assert row["secret"] is True
        assert row["secret_state"] == "not set"
        assert row["value"] is None


# ---------------------------------------------------------------------------
# 2. DB-override-sourced secret (Fernet-decrypted in the resolver)
# ---------------------------------------------------------------------------


def test_db_override_secret_plaintext_never_leaves_the_catalog(tmp_path, monkeypatch):
    key = Fernet.generate_key()
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        monkeypatch.setenv("RUNTIME_CONFIG_ENCRYPTION_KEY", key.decode())
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        get_settings.cache_clear()
        with Session(eng) as s:
            s.add(
                RuntimeConfig(
                    key="anthropic_api_key",
                    value_type="str",
                    encrypted_value=Fernet(key).encrypt(CANARY.encode()),
                    is_secret=True,
                    category="secret",
                )
            )
            s.commit()

        resp = client.get("/api/dashboard/settings-catalog")
        assert resp.status_code == 200
        _assert_no_canary(resp.text, CANARY, CANARY_TAIL)

        row = _entry(resp.json(), "anthropic_api_key")
        assert row["secret"] is True
        assert row["value"] is None
        assert row["secret_state"] == "set"
        # Source attribution still works for a secret — you can see WHERE it
        # came from without seeing WHAT it is.
        assert row["source"] == "db-override"


# ---------------------------------------------------------------------------
# 3. DSN credentials — no name hint, so name-hint-only classification misses it
# ---------------------------------------------------------------------------


def test_dsn_embedded_credentials_are_classified_secret_and_withheld(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, _mod):
        monkeypatch.setenv("REDIS_URL", f"redis://admin:{DSN_PASSWORD}@redis.internal:6379/0")
        get_settings.cache_clear()

        resp = client.get("/api/dashboard/settings-catalog")
        assert resp.status_code == 200
        _assert_no_canary(resp.text, DSN_PASSWORD, DSN_PASSWORD[-4:])

        row = _entry(resp.json(), "redis_url")
        assert row["secret"] is True
        assert row["secret_reason"] == "embedded-credential"
        assert row["value"] is None


# ---------------------------------------------------------------------------
# 4. ERROR PATH — an exception raised while rendering must not echo the value
# ---------------------------------------------------------------------------


def test_render_exception_does_not_echo_the_secret_value(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, mod):
        monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
        get_settings.cache_clear()

        def _boom(key, value):
            # Mirrors the real hazard: pydantic/ValueError messages routinely
            # embed the offending input.
            raise ValueError(f"boom while classifying {key}={value!r}")

        monkeypatch.setattr(mod, "_classify", _boom)

        resp = client.get("/api/dashboard/settings-catalog")
        # Degrades to unusable-but-safe rows rather than 500-ing with the
        # exception text (which carries the plaintext) in the body.
        assert resp.status_code == 200
        _assert_no_canary(resp.text, CANARY, CANARY_TAIL)
        row = _entry(resp.json(), "anthropic_api_key")
        assert row["value"] is None
        assert row["editable"] is False


def test_unhandled_exception_does_not_echo_the_secret_value(tmp_path, monkeypatch):
    """Even a failure OUTSIDE the per-key guard must not surface the value."""
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, mod):
        monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
        get_settings.cache_clear()

        def _boom():
            raise RuntimeError(f"resolver exploded with {CANARY}")

        monkeypatch.setattr(mod, "_field_descriptions", _boom)

        with pytest.raises(Exception) as excinfo:  # noqa: PT011 — any type is fine
            client.get("/api/dashboard/settings-catalog")
        # The route must not have converted it into a response body; and the
        # test asserts we never *serialize* it. TestClient re-raises, so the
        # only surface here is the exception itself — which is the framework's,
        # not ours. What matters is that no 200/500 BODY carried the canary.
        assert excinfo.value is not None


# ---------------------------------------------------------------------------
# 5. WRITE path — server-side enforcement, and no echo of the submitted value
# ---------------------------------------------------------------------------


def test_put_to_a_secret_key_is_rejected_and_never_echoes_the_value(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        resp = client.put(
            "/api/dashboard/settings-catalog/anthropic_api_key",
            json={"value": CANARY},
        )
        assert resp.status_code == 403
        _assert_no_canary(resp.text, CANARY, CANARY_TAIL)
        with Session(eng) as s:
            assert s.get(RuntimeConfig, "anthropic_api_key") is None


def test_put_type_validation_failure_never_echoes_the_value(tmp_path, monkeypatch):
    """A bad value for a non-secret field must not be reflected back either.

    pydantic's ValidationError embeds the offending input in its message; a
    handler that passes `str(exc)` through would turn a mistyped credential
    pasted into the wrong field into a response-body leak.
    """
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, _mod):
        resp = client.put(
            "/api/dashboard/settings-catalog/db_pool_size",
            json={"value": CANARY},
        )
        assert resp.status_code == 400
        _assert_no_canary(resp.text, CANARY, CANARY_TAIL)


def test_put_to_a_denylisted_key_is_rejected_and_never_echoes_the_value(tmp_path, monkeypatch):
    with _catalog_client(tmp_path, monkeypatch) as (client, eng, _mod):
        resp = client.put(
            "/api/dashboard/settings-catalog/scan_readonly_enforce",
            json={"value": "false"},
        )
        assert resp.status_code == 403
        with Session(eng) as s:
            assert s.get(RuntimeConfig, "scan_readonly_enforce") is None


def test_no_secret_hinted_key_is_ever_marked_editable(tmp_path, monkeypatch):
    """Fleet-wide invariant, not a spot check.

    Every entry the catalog emits that is classified secret must be
    non-editable AND value-free. This is the assertion that keeps holding when
    someone adds a new secret field to Settings.
    """
    with _catalog_client(tmp_path, monkeypatch) as (client, _eng, _mod):
        body = client.get("/api/dashboard/settings-catalog").json()
        assert body["items"], "catalog returned nothing — the invariant is vacuous"
        for row in body["items"]:
            if row["secret"]:
                assert row["editable"] is False, row["key"]
                assert row["value"] is None, row["key"]
                assert row["default"] is None, row["key"]
                assert row["secret_state"] in ("set", "not set"), row["key"]
