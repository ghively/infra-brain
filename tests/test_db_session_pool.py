"""F-035 — engine pool hardening tests.

Would fail before the fix: the engine was created with no kwargs, so
pool._pre_ping was False and pool sizing was the implicit default.
"""

import infra_brain.db.session as dbs


class _FakeSettings:
    def __init__(self, url: str):
        self.postgres_url = url
        # TRK-092: pool sizing is now read from Settings (defaults unchanged 5/10).
        self.db_pool_size = 5
        self.db_max_overflow = 10


def test_postgres_engine_has_pool_hardening(monkeypatch):
    monkeypatch.setattr(
        dbs, "get_settings", lambda: _FakeSettings("postgresql://u:p@127.0.0.1:1/nodb")
    )
    monkeypatch.setattr(dbs, "_engine", None)
    eng = dbs.get_engine()
    assert eng.pool._pre_ping is True
    assert eng.pool._recycle == 1800
    assert eng.pool.size() == 5
    assert eng.pool._max_overflow == 10


def test_pool_sizing_is_configurable_via_settings(monkeypatch):
    """TRK-092: non-default db_pool_size/db_max_overflow must reach the engine."""
    fake = _FakeSettings("postgresql://u:p@127.0.0.1:1/nodb")
    fake.db_pool_size = 12
    fake.db_max_overflow = 25
    monkeypatch.setattr(dbs, "get_settings", lambda: fake)
    monkeypatch.setattr(dbs, "_engine", None)
    eng = dbs.get_engine()
    assert eng.pool.size() == 12
    assert eng.pool._max_overflow == 25


def test_sqlite_engine_skips_pg_pool_kwargs(monkeypatch):
    monkeypatch.setattr(dbs, "get_settings", lambda: _FakeSettings("sqlite://"))
    monkeypatch.setattr(dbs, "_engine", None)
    # Must not raise (SQLite pools reject pool_size/max_overflow).
    eng = dbs.get_engine()
    assert eng is not None
