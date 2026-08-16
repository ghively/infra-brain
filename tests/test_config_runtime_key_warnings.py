"""config.py's resolver must not warn about deliberately-non-Settings keys.

``dispatchable__<domain>`` rows are dynamic per-domain pause levers read
directly by runtime_flags.py — they are intentionally NOT Settings fields. The
resolver's "not a Settings field" warning exists to catch typos, and firing it
for these on every TTL poll (~every 15s, per worker, forever) drowns that
signal out. Suppression must be prefix-scoped: a genuinely unknown key must
still warn.

Uses the file-backed sqlite pattern from tests/test_runtime_config_secrets.py —
``_load_runtime_overrides`` opens its own engine against ``postgres_url``, so an
in-memory ``sqlite://`` row would not be visible across connections.
"""

import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from infra_brain.config import Settings, _load_runtime_overrides
from infra_brain.db.models import Base, RuntimeConfig


@pytest.fixture
def settings_for_db(tmp_path):
    def _make(*rows: RuntimeConfig) -> Settings:
        db_path = tmp_path / "runtime_config_warnings.sqlite3"
        eng = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(eng)
        with Session(eng) as s:
            for row in rows:
                s.add(row)
            s.commit()
        eng.dispose()
        return Settings(postgres_url=f"sqlite:///{db_path}")

    return _make


def _row(key: str, value: str = "false") -> RuntimeConfig:
    return RuntimeConfig(key=key, value_type="bool", value=value, category="collector_toggle")


def test_dispatchable_key_is_ignored_without_warning(settings_for_db, caplog):
    base = settings_for_db(_row("dispatchable__linux"))

    with caplog.at_level(logging.WARNING, logger="infra_brain.config"):
        overrides = _load_runtime_overrides(base)

    assert "dispatchable__linux" not in overrides  # still not grafted onto Settings
    assert [r for r in caplog.records if "dispatchable__linux" in r.getMessage()] == []


def test_genuinely_unknown_key_still_warns(settings_for_db, caplog):
    """The typo-detection this warning exists for must survive the suppression."""
    base = settings_for_db(_row("anthropc_api_key", value="oops"))

    with caplog.at_level(logging.WARNING, logger="infra_brain.config"):
        overrides = _load_runtime_overrides(base)

    assert "anthropc_api_key" not in overrides
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "is not a Settings field" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "anthropc_api_key" in warnings[0].getMessage()


def test_suppression_is_prefix_scoped_not_substring(settings_for_db, caplog):
    """A key that merely CONTAINS the prefix later on must still warn."""
    base = settings_for_db(_row("nope_dispatchable__linux"))

    with caplog.at_level(logging.WARNING, logger="infra_brain.config"):
        _load_runtime_overrides(base)

    assert any("nope_dispatchable__linux" in r.getMessage() for r in caplog.records)
