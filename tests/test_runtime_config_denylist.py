"""Runtime-config safety denylist + secret-log redaction (final-fix A).

The ``runtime_config`` layer lets an admin override ``Settings`` fields from DB
rows resolved every TTL window. Without a denylist, that included the fields
that *implement* infra-brain's core read-only / auth guarantees — a single row
could turn the read-only boundary into warn-only or disable webhook auth.

Covers:
- ``_NON_OVERRIDABLE`` names only real ``Settings`` fields (typo guard) and
  contains the safety-critical set.
- resolver layer: a denylisted row in the DB is ignored, with its own distinct
  warning (not the generic "not a Settings field" one).
- API layer: a PUT to a denylisted key is rejected 403 and persists nothing.
- log hygiene: a secret row that fails validation never puts its decrypted
  plaintext (nor the raw exception, which echoes it) into the log.
"""

import logging
from contextlib import contextmanager

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from infra_brain.config import (
    _NON_OVERRIDABLE,
    Settings,
    _load_runtime_overrides,
    get_settings,
    runtime_config_encryption_key,
)
from infra_brain.db.models import Base, RuntimeConfig

# Fields whose override would directly defeat the read-only guarantee or an auth
# check. Spelled out here independently of config.py so a future edit that drops
# one from the frozenset fails this test rather than silently reopening the hole.
SAFETY_CRITICAL = [
    "scan_readonly_enforce",
    "dlp_fail_closed",
    "postgres_readonly_url",
    "postgres_url",
    "webhook_auth_required",
    "environment",
    "runtime_config_encryption_key",
    "infra_brain_dev",
    "ui_cookie_secret",
    "integration_approval_required",
    # netdiscovery/nmap gate inputs (final-fix re-review finding A) — emptying
    # exclude_cidrs or widening include/fragile removes the fence around
    # protected ranges within one TTL window, no redeploy required.
    "netdiscovery_exclude_cidrs",
    "netdiscovery_include_cidrs",
    "netdiscovery_fragile_cidrs",
    "netdiscovery_fragile_hosts",
    "netdiscovery_enable_port_scan",
    # audit trail retention (Layer 3 of the read-only model)
    "retention_audit_log_days",
]


def _seed_db(tmp_path, name, **row_kwargs):
    """File-backed sqlite (the resolver opens its own connection) with one row."""
    db_path = tmp_path / name
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(RuntimeConfig(**row_kwargs))
        s.commit()
    eng.dispose()
    return f"sqlite:///{db_path}"


@contextmanager
def _sqlite_session_factory():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    yield eng, _get_session


def _admin_client(eng_and_get_session, monkeypatch):
    _eng, _get_session = eng_and_get_session
    import infra_brain.api.routers.runtime_config as mod

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(mod, "get_session", _get_session)

    app = FastAPI()
    app.include_router(mod.runtime_config_router)
    return TestClient(app)


# --------------------------------------------------------------------------
# The denylist itself
# --------------------------------------------------------------------------


def test_denylist_entries_are_real_settings_fields():
    unknown = sorted(_NON_OVERRIDABLE - set(Settings.model_fields))
    assert not unknown, f"denylist names non-existent Settings fields: {unknown}"


def test_denylist_covers_safety_critical_fields():
    missing = [f for f in SAFETY_CRITICAL if f not in _NON_OVERRIDABLE]
    assert not missing, f"safety-critical fields missing from denylist: {missing}"


# --------------------------------------------------------------------------
# (a) resolver ignores a denylisted DB row
# --------------------------------------------------------------------------


def test_resolver_ignores_denylisted_row(tmp_path, caplog):
    """A row written by any route at all (direct psql, restored dump) is inert."""
    url = _seed_db(
        tmp_path,
        "denylisted.sqlite3",
        key="scan_readonly_enforce",
        value="false",
        value_type="bool",
        is_secret=False,
        category="general",
    )
    base = Settings(postgres_url=url)
    assert base.scan_readonly_enforce is True

    with caplog.at_level(logging.WARNING, logger="infra_brain.config"):
        overrides = _load_runtime_overrides(base)

    assert "scan_readonly_enforce" not in overrides
    # The read-only boundary still fails closed after the override pass.
    assert base.model_copy(update=overrides).scan_readonly_enforce is True

    text = caplog.text
    assert "non-overridable" in text
    # Distinct from the generic unknown-key warning.
    assert "not a Settings field" not in text


def test_resolver_ignores_denylisted_secret_row_without_decrypting(tmp_path, caplog):
    key = Fernet.generate_key()
    url = _seed_db(
        tmp_path,
        "denylisted_secret.sqlite3",
        key="ui_cookie_secret",
        encrypted_value=Fernet(key).encrypt(b"attacker-chosen-cookie-key"),
        value_type="str",
        is_secret=True,
        category="secret",
    )
    base = Settings(postgres_url=url, runtime_config_encryption_key=key.decode())

    with caplog.at_level(logging.WARNING, logger="infra_brain.config"):
        overrides = _load_runtime_overrides(base)

    assert "ui_cookie_secret" not in overrides
    assert "non-overridable" in caplog.text
    # Denylist runs before decryption, so the plaintext is never materialized.
    assert "attacker-chosen-cookie-key" not in caplog.text


def test_resolver_still_applies_non_denylisted_row(tmp_path):
    """Guard against the denylist being over-broad / short-circuiting everything."""
    url = _seed_db(
        tmp_path,
        "allowed.sqlite3",
        key="daily_token_budget_per_user",
        value="123",
        value_type="int",
        is_secret=False,
        category="general",
    )
    overrides = _load_runtime_overrides(Settings(postgres_url=url))
    assert overrides["daily_token_budget_per_user"] == 123


# --------------------------------------------------------------------------
# (b) API rejects a PUT to a denylisted key and writes nothing
# --------------------------------------------------------------------------


def test_put_denylisted_key_is_rejected_and_persists_nothing(monkeypatch):
    with _sqlite_session_factory() as bundle:
        eng, _ = bundle
        client = _admin_client(bundle, monkeypatch)

        resp = client.put(
            "/api/dashboard/runtime-config/scan_readonly_enforce",
            json={"value": "false", "value_type": "bool", "category": "general"},
        )
        assert resp.status_code == 403
        assert "not runtime-overridable" in resp.json()["detail"]

        with Session(eng) as s:
            assert s.get(RuntimeConfig, "scan_readonly_enforce") is None
        assert client.get("/api/dashboard/runtime-config").json()["items"] == []


def test_put_denylisted_secret_key_is_rejected(monkeypatch):
    with _sqlite_session_factory() as bundle:
        eng, _ = bundle
        client = _admin_client(bundle, monkeypatch)
        monkeypatch.setenv("RUNTIME_CONFIG_ENCRYPTION_KEY", Fernet.generate_key().decode())
        get_settings.cache_clear()

        resp = client.put(
            "/api/dashboard/runtime-config/runtime_config_encryption_key",
            json={"value": "rotate-me", "value_type": "str", "category": "secret"},
        )
        assert resp.status_code == 403
        with Session(eng) as s:
            assert s.get(RuntimeConfig, "runtime_config_encryption_key") is None


def test_put_allowed_key_still_works(monkeypatch):
    with _sqlite_session_factory() as bundle:
        client = _admin_client(bundle, monkeypatch)
        resp = client.put(
            "/api/dashboard/runtime-config/daily_token_budget_per_user",
            json={"value": "42", "value_type": "int", "category": "general"},
        )
        assert resp.status_code == 200


# --------------------------------------------------------------------------
# A secret row cannot be demoted to plaintext through PUT
# --------------------------------------------------------------------------


def _put_secret(client, key, value="super-secret-token"):
    return client.put(
        f"/api/dashboard/runtime-config/{key}",
        json={"value": value, "value_type": "str", "category": "secret"},
    )


def test_put_cannot_demote_existing_secret_to_plaintext(monkeypatch):
    """UI read-only-ness is not authorization — the server must refuse itself."""
    with _sqlite_session_factory() as bundle:
        eng, _ = bundle
        client = _admin_client(bundle, monkeypatch)
        monkeypatch.setenv("RUNTIME_CONFIG_ENCRYPTION_KEY", Fernet.generate_key().decode())
        get_settings.cache_clear()

        assert _put_secret(client, "some_api_key").status_code == 200
        with Session(eng) as s:
            ciphertext_before = s.get(RuntimeConfig, "some_api_key").encrypted_value

        resp = client.put(
            "/api/dashboard/runtime-config/some_api_key",
            json={"value": "now-plaintext", "value_type": "str", "category": "tuning"},
        )
        assert resp.status_code == 400
        assert "cannot be converted to a plaintext" in resp.json()["detail"]

        with Session(eng) as s:
            row = s.get(RuntimeConfig, "some_api_key")
            assert row.is_secret is True
            assert row.category == "secret"
            assert row.value is None
            assert row.encrypted_value == ciphertext_before

        # And the demoted plaintext never becomes readable over the list endpoint.
        listed = next(
            r
            for r in client.get("/api/dashboard/runtime-config").json()["items"]
            if r["key"] == "some_api_key"
        )
        assert listed["value"] is None
        assert listed["is_secret"] is True


def test_secret_can_still_be_updated_as_a_secret(monkeypatch):
    with _sqlite_session_factory() as bundle:
        eng, _ = bundle
        client = _admin_client(bundle, monkeypatch)
        key = Fernet.generate_key()
        monkeypatch.setenv("RUNTIME_CONFIG_ENCRYPTION_KEY", key.decode())
        get_settings.cache_clear()

        assert _put_secret(client, "some_api_key", "first").status_code == 200
        assert _put_secret(client, "some_api_key", "second").status_code == 200

        with Session(eng) as s:
            row = s.get(RuntimeConfig, "some_api_key")
            assert row.is_secret is True
            assert Fernet(key).decrypt(row.encrypted_value) == b"second"


def test_secret_can_still_be_deleted(monkeypatch):
    with _sqlite_session_factory() as bundle:
        eng, _ = bundle
        client = _admin_client(bundle, monkeypatch)
        monkeypatch.setenv("RUNTIME_CONFIG_ENCRYPTION_KEY", Fernet.generate_key().decode())
        get_settings.cache_clear()

        assert _put_secret(client, "some_api_key").status_code == 200
        assert client.delete("/api/dashboard/runtime-config/some_api_key").status_code == 204
        with Session(eng) as s:
            assert s.get(RuntimeConfig, "some_api_key") is None


def test_new_key_may_still_be_created_as_plaintext(monkeypatch):
    """The demotion guard must only fire for an ALREADY-secret row."""
    with _sqlite_session_factory() as bundle:
        eng, _ = bundle
        client = _admin_client(bundle, monkeypatch)
        resp = client.put(
            "/api/dashboard/runtime-config/some_api_key",
            json={"value": "plain", "value_type": "str", "category": "tuning"},
        )
        assert resp.status_code == 200
        with Session(eng) as s:
            row = s.get(RuntimeConfig, "some_api_key")
            assert row.is_secret is False
            assert row.value == "plain"


# --------------------------------------------------------------------------
# Encryption key is sourced env-only on both sides (no split-brain)
# --------------------------------------------------------------------------


def test_encryption_key_helper_is_env_only(monkeypatch):
    monkeypatch.setenv("RUNTIME_CONFIG_ENCRYPTION_KEY", "env-key")
    get_settings.cache_clear()
    assert runtime_config_encryption_key() == "env-key"
    # An explicit base is honored (the resolver reuses its env-only instance).
    assert runtime_config_encryption_key(Settings(runtime_config_encryption_key="x")) == "x"


# --------------------------------------------------------------------------
# (c) secret plaintext never reaches the log on a validation failure
# --------------------------------------------------------------------------

PLAINTEXT = "sk-live-DO-NOT-LOG-ME-0123456789"


def test_secret_validation_failure_does_not_log_plaintext(tmp_path, caplog):
    key = Fernet.generate_key()
    # int-typed field + non-numeric plaintext => validation raises, and pydantic's
    # ValidationError message embeds the offending input.
    url = _seed_db(
        tmp_path,
        "secret_badvalue.sqlite3",
        key="daily_token_budget_per_user",
        encrypted_value=Fernet(key).encrypt(PLAINTEXT.encode()),
        value_type="int",
        is_secret=True,
        category="secret",
    )
    base = Settings(postgres_url=url, runtime_config_encryption_key=key.decode())

    with caplog.at_level(logging.WARNING, logger="infra_brain.config"):
        overrides = _load_runtime_overrides(base)

    assert "daily_token_budget_per_user" not in overrides
    for record in caplog.records:
        assert PLAINTEXT not in record.getMessage()
        assert PLAINTEXT not in str(record.args)
    assert PLAINTEXT not in caplog.text
    # Still actionable: the key and its declared type are reported.
    assert "daily_token_budget_per_user" in caplog.text
    assert "secret row" in caplog.text


def test_non_secret_validation_failure_still_logs_full_detail(tmp_path, caplog):
    url = _seed_db(
        tmp_path,
        "plain_badvalue.sqlite3",
        key="daily_token_budget_per_user",
        value="not-a-number",
        value_type="int",
        is_secret=False,
        category="general",
    )
    with caplog.at_level(logging.WARNING, logger="infra_brain.config"):
        overrides = _load_runtime_overrides(Settings(postgres_url=url))

    assert "daily_token_budget_per_user" not in overrides
    assert "not-a-number" in caplog.text
