"""Encrypted secrets support for RuntimeConfig (TRK-303 part 4/7).

Covers:
- write path: a `category="secret"` PUT is Fernet-encrypted into
  `encrypted_value`; `value` stays NULL; the response never carries
  plaintext.
- resolver path: `_load_runtime_overrides` decrypts secret rows using
  `RUNTIME_CONFIG_ENCRYPTION_KEY` and applies them like any other override.
- degrade-quietly paths: no key configured, or a wrong/rotated key, must be
  skipped rather than raising.
"""

from contextlib import contextmanager

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from infra_brain.config import Settings, _load_runtime_overrides, get_settings
from infra_brain.db.models import Base, RuntimeConfig


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


def test_secret_write_never_returns_plaintext(monkeypatch):
    with _sqlite_session_factory() as bundle:
        client = _admin_client(bundle, monkeypatch)
        monkeypatch.setenv("RUNTIME_CONFIG_ENCRYPTION_KEY", Fernet.generate_key().decode())
        get_settings.cache_clear()

        resp = client.put(
            "/api/dashboard/runtime-config/some_api_key",
            json={"value": "super-secret-token", "value_type": "str", "category": "secret"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["value"] is None
        assert body["is_secret"] is True

        resp = client.get("/api/dashboard/runtime-config")
        row = next(r for r in resp.json()["items"] if r["key"] == "some_api_key")
        assert row["value"] is None
        assert row["is_secret"] is True


def test_secret_write_without_key_configured_fails(monkeypatch):
    with _sqlite_session_factory() as bundle:
        client = _admin_client(bundle, monkeypatch)
        monkeypatch.setenv("RUNTIME_CONFIG_ENCRYPTION_KEY", "")
        get_settings.cache_clear()

        resp = client.put(
            "/api/dashboard/runtime-config/some_api_key",
            json={"value": "super-secret-token", "value_type": "str", "category": "secret"},
        )
        assert resp.status_code == 500


def test_secret_resolves_correctly_at_runtime(monkeypatch, tmp_path):
    # A plain "sqlite://" in-memory DB is per-connection — _load_runtime_overrides
    # opens its own engine/connection against `postgres_url`, so the row must live
    # in a file-backed sqlite DB to be visible across connections.
    key = Fernet.generate_key()
    monkeypatch.setenv("RUNTIME_CONFIG_ENCRYPTION_KEY", key.decode())
    encrypted = Fernet(key).encrypt(b"super-secret-token")

    db_path = tmp_path / "runtime_config_secrets.sqlite3"
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(
            RuntimeConfig(
                key="anthropic_api_key",
                value_type="str",
                encrypted_value=encrypted,
                is_secret=True,
                category="secret",
            )
        )
        s.commit()
    eng.dispose()

    base = Settings(postgres_url=f"sqlite:///{db_path}", runtime_config_encryption_key=key.decode())
    overrides = _load_runtime_overrides(base)
    assert overrides["anthropic_api_key"] == "super-secret-token"


def test_secret_row_skipped_when_no_key_configured(tmp_path):
    encrypted = Fernet(Fernet.generate_key()).encrypt(b"super-secret-token")
    db_path = tmp_path / "no_key.sqlite3"
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(
            RuntimeConfig(
                key="anthropic_api_key",
                value_type="str",
                encrypted_value=encrypted,
                is_secret=True,
                category="secret",
            )
        )
        s.commit()
    eng.dispose()

    base = Settings(postgres_url=f"sqlite:///{db_path}", runtime_config_encryption_key="")
    overrides = _load_runtime_overrides(base)
    assert "anthropic_api_key" not in overrides


def test_secret_row_skipped_on_wrong_key(tmp_path):
    encrypted = Fernet(Fernet.generate_key()).encrypt(b"super-secret-token")
    db_path = tmp_path / "wrong_key.sqlite3"
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(
            RuntimeConfig(
                key="anthropic_api_key",
                value_type="str",
                encrypted_value=encrypted,
                is_secret=True,
                category="secret",
            )
        )
        s.commit()
    eng.dispose()

    wrong_key = Fernet.generate_key().decode()
    base = Settings(postgres_url=f"sqlite:///{db_path}", runtime_config_encryption_key=wrong_key)
    overrides = _load_runtime_overrides(base)
    assert "anthropic_api_key" not in overrides


def test_secret_row_with_no_ciphertext_skipped(tmp_path):
    db_path = tmp_path / "no_ciphertext.sqlite3"
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(
            RuntimeConfig(
                key="anthropic_api_key",
                value_type="str",
                encrypted_value=None,
                is_secret=True,
                category="secret",
            )
        )
        s.commit()
    eng.dispose()

    base = Settings(
        postgres_url=f"sqlite:///{db_path}",
        runtime_config_encryption_key=Fernet.generate_key().decode(),
    )
    overrides = _load_runtime_overrides(base)
    assert "anthropic_api_key" not in overrides
