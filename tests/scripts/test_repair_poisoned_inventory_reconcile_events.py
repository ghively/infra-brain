"""Tests for scripts/repair_poisoned_inventory_reconcile_events.py (H-6 repair).

Same temp-file SQLite pattern as
tests/scripts/test_flag_stale_wrong_direction_proposals.py.
"""

from __future__ import annotations

import os

from infra_brain.db.models import InventoryReconcileEvent
from infra_brain.db.session import get_session
from scripts.repair_poisoned_inventory_reconcile_events import main as repair_main


def _point_db_at_sqlite(tmp_path):
    db_path = tmp_path / "repair_poisoned.db"
    sqlite_url = f"sqlite:///{db_path}"
    os.environ["POSTGRES_URL"] = sqlite_url

    import infra_brain.db.session as _session_mod

    _session_mod.get_settings.cache_clear()
    from infra_brain.config import get_settings as _cfg_get_settings

    try:
        _cfg_get_settings.cache_clear()
    except AttributeError:
        pass
    _session_mod._engine = None

    from infra_brain.db.session import create_tables

    create_tables()
    return sqlite_url


def _reset_db_env():
    os.environ.pop("POSTGRES_URL", None)
    import infra_brain.db.session as _session_mod

    _session_mod.get_settings.cache_clear()
    from infra_brain.config import get_settings as _cfg_get_settings

    try:
        _cfg_get_settings.cache_clear()
    except AttributeError:
        pass
    _session_mod._engine = None


def _seed(session, *, host, status="proposed", mr_url=None, domain="linux", group="discovered"):
    event = InventoryReconcileEvent(
        host=host, domain=domain, target_group=group, status=status, mr_url=mr_url
    )
    session.add(event)
    session.flush()
    return event.id


def test_poisoned_row_gets_flagged_in_dry_run(tmp_path, monkeypatch):
    _point_db_at_sqlite(tmp_path)
    try:
        with get_session() as session:
            poisoned_id = _seed(session, host="web-03", status="proposed", mr_url=None)
            session.commit()

        monkeypatch.setattr("sys.argv", ["repair_poisoned_inventory_reconcile_events.py"])
        rc = repair_main()
        assert rc == 0

        with get_session() as session:
            row = session.get(InventoryReconcileEvent, poisoned_id)
            # Dry-run: nothing changed.
            assert row.status == "proposed"
    finally:
        _reset_db_env()


def test_healthy_proposed_row_with_mr_url_not_touched(tmp_path, monkeypatch):
    _point_db_at_sqlite(tmp_path)
    try:
        with get_session() as session:
            healthy_id = _seed(
                session, host="web-04", status="proposed", mr_url="http://gitlab/mr/1"
            )
            session.commit()

        monkeypatch.setattr(
            "sys.argv", ["repair_poisoned_inventory_reconcile_events.py", "--apply"]
        )
        rc = repair_main()
        assert rc == 0

        with get_session() as session:
            row = session.get(InventoryReconcileEvent, healthy_id)
            assert row.status == "proposed"
            assert row.mr_url == "http://gitlab/mr/1"
    finally:
        _reset_db_env()


def test_apply_flips_poisoned_row_to_failed(tmp_path, monkeypatch):
    _point_db_at_sqlite(tmp_path)
    try:
        with get_session() as session:
            poisoned_id = _seed(session, host="web-05", status="proposed", mr_url=None)
            session.commit()

        monkeypatch.setattr(
            "sys.argv", ["repair_poisoned_inventory_reconcile_events.py", "--apply"]
        )
        rc = repair_main()
        assert rc == 0

        with get_session() as session:
            row = session.get(InventoryReconcileEvent, poisoned_id)
            assert row.status == "failed"
            assert row.mr_url is None  # nothing is fabricated
            assert row.host == "web-05"  # history preserved, not deleted
    finally:
        _reset_db_env()


def test_merged_row_not_touched(tmp_path, monkeypatch):
    _point_db_at_sqlite(tmp_path)
    try:
        with get_session() as session:
            merged_id = _seed(session, host="web-06", status="merged", mr_url=None)
            session.commit()

        monkeypatch.setattr(
            "sys.argv", ["repair_poisoned_inventory_reconcile_events.py", "--apply"]
        )
        rc = repair_main()
        assert rc == 0

        with get_session() as session:
            row = session.get(InventoryReconcileEvent, merged_id)
            # Untouched: not status="proposed" at scan time.
            assert row.status == "merged"
    finally:
        _reset_db_env()


def test_repaired_host_is_reconsidered_by_filter_already_proposed(tmp_path, monkeypatch):
    """The whole point: after repair, the host must no longer be permanently
    excluded by InventoryReconcileAgent._filter_already_proposed()."""
    _point_db_at_sqlite(tmp_path)
    try:
        with get_session() as session:
            _seed(session, host="web-07", status="proposed", mr_url=None)
            session.commit()

        monkeypatch.setattr(
            "sys.argv", ["repair_poisoned_inventory_reconcile_events.py", "--apply"]
        )
        rc = repair_main()
        assert rc == 0

        from infra_brain.agents.inventory_reconcile import InventoryReconcileAgent

        # inventory_reconcile.get_session resolves the same POSTGRES_URL-backed
        # engine _point_db_at_sqlite just pointed at the DB.session singleton, so
        # no additional patching is needed here.
        missing = InventoryReconcileAgent._filter_already_proposed(
            [("web-07", "linux", "discovered")]
        )
        assert missing == [("web-07", "linux", "discovered")]
    finally:
        _reset_db_env()


def test_report_prints_matched_rows(tmp_path, monkeypatch, capsys):
    _point_db_at_sqlite(tmp_path)
    try:
        with get_session() as session:
            _seed(session, host="web-08", status="proposed", mr_url=None)
            session.commit()

        monkeypatch.setattr("sys.argv", ["repair_poisoned_inventory_reconcile_events.py"])
        repair_main()
        out = capsys.readouterr().out
        assert "web-08" in out
        assert "matched rows: 1" in out
    finally:
        _reset_db_env()
