"""Item 1.6 — get_readonly_engine() wiring."""

import infra_brain.db.session as dbsession
from infra_brain.config import get_settings


def _reset_engines():
    dbsession._engine = None
    dbsession._readonly_engine = None
    get_settings.cache_clear()


def test_fallback_to_primary_when_readonly_url_unset(monkeypatch):
    """Unset POSTGRES_READONLY_URL -> the primary engine is returned (with a
    warning), so environments without the role keep working."""
    monkeypatch.delenv("POSTGRES_READONLY_URL", raising=False)
    _reset_engines()
    try:
        assert dbsession.get_readonly_engine() is dbsession.get_engine()
    finally:
        _reset_engines()


def test_readonly_engine_uses_readonly_url(monkeypatch, tmp_path):
    """Set POSTGRES_READONLY_URL -> a distinct engine bound to that DSN."""
    monkeypatch.setenv("POSTGRES_READONLY_URL", f"sqlite:///{tmp_path / 'ro.db'}")
    _reset_engines()
    try:
        eng = dbsession.get_readonly_engine()
        assert "ro.db" in str(eng.url)
        assert eng is not dbsession.get_engine()
    finally:
        _reset_engines()
