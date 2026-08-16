"""End-to-end smoke test: migration/schema → dispatch → Resource row in DB.

Strategy:
- Point the app at a temporary SQLite file via POSTGRES_URL env var.
- Clear the lru_cache on get_settings() and reset the _engine singleton so the
  app picks up the new URL rather than the cached postgres URL. (Wave 4 item
  4.4 removed the compiled-supervisor-graph singleton this test used to reset
  — dispatch() is now a plain typed function with no cached graph.)
- Use create_tables() (SQLAlchemy metadata) instead of alembic because Alembic
  and the app engine are independent objects; wiring them to the same file is
  possible but requires extra plumbing that test_migration.py already covers.
  Here we want to test *dispatch → collect → upsert → DB* wiring, not the
  migration itself (that is covered by test_migration.py).
- Mock LinuxAgent.collect to return one deterministic resource dict.
- Mock dedup.try_acquire / release so no Redis connection is required.
- Mock _post_collection_hook to skip DriftDetector / NotificationAgent (which
  would require a live Anthropic API key + Redis + Jira/Confluence endpoints).
- Assert: status == "completed", resources_found == 1, one row in `resources`.
"""

import os
from unittest.mock import patch

import sqlalchemy as sa


FAKE_ITEMS = [{"name": "host-1", "type": "linux_host", "data": {"os": "Ubuntu"}}]


def test_full_collection_cycle_writes_resources(tmp_path):
    """Prove collect → upsert → Resource row pipeline is wired end-to-end."""
    db_path = tmp_path / "e2e.db"
    sqlite_url = f"sqlite:///{db_path}"

    # ------------------------------------------------------------------
    # 1. Point the entire app at the temp SQLite DB.
    #    pydantic-settings reads POSTGRES_URL env var for the postgres_url field.
    # ------------------------------------------------------------------
    os.environ["POSTGRES_URL"] = sqlite_url

    # Clear the lru_cache on the exact get_settings reference that session.py holds.
    # NOTE: test_config.py::test_override_via_env does reload(infra_brain.config),
    # which creates a NEW get_settings function in the module — but session.py (and
    # other modules that imported via `from infra_brain.config import get_settings`)
    # still hold a reference to the ORIGINAL function object.  We must clear the
    # cache on the function that session.py actually calls, not the one currently
    # bound in the config module namespace.
    import infra_brain.db.session as _session_mod

    _session_mod.get_settings.cache_clear()  # clear the copy session.py imported

    # Also clear the current module-level reference (handles the case where no
    # reload has occurred and both point to the same object).
    from infra_brain.config import get_settings as _cfg_get_settings

    try:
        _cfg_get_settings.cache_clear()
    except AttributeError:
        pass  # already cleared above if they are the same object

    # Reset the engine singleton so the next get_engine() call uses the new URL.
    _session_mod._engine = None

    # ------------------------------------------------------------------
    # 2. Build the schema via create_tables() (SQLAlchemy metadata).
    #    This uses the same engine that dispatch() will use, guaranteeing
    #    the schema is present when rows are inserted.
    # ------------------------------------------------------------------
    from infra_brain.db.session import create_tables

    create_tables()

    # ------------------------------------------------------------------
    # 3. Dispatch with all external dependencies patched out.
    # ------------------------------------------------------------------
    from infra_brain.supervisor import dispatch

    def _noop_post_hook(result=None):
        """Skip DriftDetector + NotificationAgent (need API key / Redis / Jira)."""
        return None

    with (
        patch("infra_brain.agents.linux.LinuxAgent.collect", return_value=FAKE_ITEMS),
        patch("infra_brain.dedup.try_acquire", return_value=True),
        patch("infra_brain.dedup.release"),
        patch("infra_brain.supervisor._post_collection_hook", side_effect=_noop_post_hook),
    ):
        result = dispatch("linux", trigger_type="manual", scope="all")

    # ------------------------------------------------------------------
    # 4. Assert dispatch result.
    # ------------------------------------------------------------------
    assert result.status == "completed", f"dispatch failed: {result.errors}"
    assert result.resources_found == 1, f"expected 1 resource, got {result.resources_found}"

    # ------------------------------------------------------------------
    # 5. Assert the Resource row actually landed in the DB.
    # ------------------------------------------------------------------
    engine = sa.create_engine(sqlite_url)
    with engine.connect() as conn:
        count = conn.execute(
            sa.text("SELECT COUNT(*) FROM resources WHERE type='linux_host'")
        ).scalar()
    assert count == 1, f"expected 1 row in resources, got {count}"


def test_cleanup_env(tmp_path):
    """Restore global state after the smoke test so other tests are not affected."""
    # Remove env var so subsequent test sessions don't inherit SQLite URL.
    os.environ.pop("POSTGRES_URL", None)

    import infra_brain.db.session as _session_mod

    _session_mod.get_settings.cache_clear()
    from infra_brain.config import get_settings as _cfg_get_settings

    try:
        _cfg_get_settings.cache_clear()
    except AttributeError:
        pass
    _session_mod._engine = None
