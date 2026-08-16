import logging
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from infra_brain.config import get_settings
from infra_brain.db.models import Base

import threading

log = logging.getLogger(__name__)

_engine = None
_engine_lock = threading.Lock()
_readonly_engine = None

# Pool hardening (F-035): the engine is shared by long-lived processes (app,
# scheduler, MCP). Without pre_ping, a Postgres restart or idle-connection
# reap leaves stale sockets in the pool and the NEXT request 500s. recycle
# forces connections to be re-established every 30 min; sizing is explicit so
# pool exhaustion is a deliberate, observable limit rather than a default.
# TRK-092: pool_size / max_overflow are now sourced from Settings
# (DB_POOL_SIZE / DB_MAX_OVERFLOW) instead of hardcoded literals. Defaults are
# unchanged (5 / 10); right-sizing for concurrent sweep + dashboard load is a
# follow-up ops decision for the user, not this code's job. Built lazily inside
# get_engine() so get_settings() is resolved at engine-creation time, not import.


def _pg_pool_kwargs() -> dict:
    settings = get_settings()
    return {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
    }


def get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                url = get_settings().postgres_url
                # SQLite (tests) uses pools that reject pool_size/max_overflow.
                kwargs = _pg_pool_kwargs() if url.startswith("postgresql") else {}
                _engine = create_engine(url, **kwargs)
    return _engine


def get_readonly_engine():
    """Engine bound to the SELECT-only DB role (POSTGRES_READONLY_URL).

    Wave 1 item 1.6: the NL->SQL QueryAgent must run under a role that CANNOT
    mutate, so a sanitizer bypass fails at the DB with permission-denied.
    Falls back to the primary engine — with a loud warning — when the read-only
    DSN is unset, so environments without the role keep working.
    """
    global _readonly_engine
    settings = get_settings()
    if not settings.postgres_readonly_url:
        log.warning(
            "POSTGRES_READONLY_URL unset — SQL agent falls back to the primary "
            "DB role (read-only enforced by sanitizer only)"
        )
        return get_engine()
    if _readonly_engine is None:
        with _engine_lock:
            if _readonly_engine is None:
                _readonly_engine = create_engine(settings.postgres_readonly_url)
    return _readonly_engine


def create_tables():
    Base.metadata.create_all(get_engine())


@contextmanager
def get_session():
    with Session(get_engine()) as session:
        yield session
