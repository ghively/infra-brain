"""Tests for scripts/flag_stale_wrong_direction_proposals.py (GitLab #142 /
TRK-263 follow-up).

Uses a temp-file SQLite DB via POSTGRES_URL + a reset db.session._engine
singleton — same pattern as tests/test_e2e_smoke.py — since the script uses
plain sync SQLAlchemy through infra_brain.db.session.get_session(), not the
async app stack.
"""

from __future__ import annotations

import os

import sqlalchemy as sa

from infra_brain.db.models import ProposedAction
from infra_brain.db.session import get_session
from scripts.flag_stale_wrong_direction_proposals import main as flag_main


def _point_db_at_sqlite(tmp_path):
    db_path = tmp_path / "flag_stale.db"
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


def _seed(session, *, field, old, new, status="pending", target="drift:aaaa"):
    action = ProposedAction(
        agent="RemediationAgent",
        action_type="config_fix",
        target=target,
        payload={
            "drift_event_id": "aaaa",
            "host": "host-1",
            "field": field,
            "old": old,
            "new": new,
            "plan": "revert",
        },
        confidence=0.8,
        status=status,
    )
    session.add(action)
    session.flush()
    return action.id


def test_first_observation_proposal_gets_flagged(tmp_path, monkeypatch):
    _point_db_at_sqlite(tmp_path)
    try:
        with get_session() as session:
            first_obs_id = _seed(
                session,
                field="os_vendor",
                old={"v": None},
                new={"v": "Microsoft"},
                target="drift:first-obs",
            )
            session.commit()

        monkeypatch.setattr("sys.argv", ["flag_stale_wrong_direction_proposals.py"])
        rc = flag_main()
        assert rc == 0

        with get_session() as session:
            row = session.get(ProposedAction, first_obs_id)
            # Dry-run: nothing changed.
            assert row.status == "pending"
    finally:
        _reset_db_env()


def test_legitimate_proposal_not_flagged(tmp_path, monkeypatch):
    _point_db_at_sqlite(tmp_path)
    try:
        with get_session() as session:
            legit_id = _seed(
                session,
                field="os_vendor",
                old={"v": "Microsoft"},
                new={"v": "RedHat"},
                target="drift:legit",
            )
            session.commit()

        monkeypatch.setattr("sys.argv", ["flag_stale_wrong_direction_proposals.py", "--apply"])
        rc = flag_main()
        assert rc == 0

        with get_session() as session:
            row = session.get(ProposedAction, legit_id)
            assert row.status == "pending"
            assert "_stale_wrong_direction" not in (row.payload or {})
    finally:
        _reset_db_env()


def test_dry_run_makes_no_db_change(tmp_path, monkeypatch):
    _point_db_at_sqlite(tmp_path)
    try:
        with get_session() as session:
            action_id = _seed(
                session,
                field="os_vendor",
                old=None,
                new={"v": "Microsoft"},
                target="drift:dry-run",
            )
            session.commit()

        monkeypatch.setattr("sys.argv", ["flag_stale_wrong_direction_proposals.py"])
        rc = flag_main()
        assert rc == 0

        with get_session() as session:
            row = session.get(ProposedAction, action_id)
            assert row.status == "pending"
            assert "_stale_wrong_direction" not in (row.payload or {})
    finally:
        _reset_db_env()


def test_apply_commits_the_change(tmp_path, monkeypatch):
    _point_db_at_sqlite(tmp_path)
    try:
        with get_session() as session:
            action_id = _seed(
                session,
                field="os_vendor",
                old=None,
                new={"v": "Microsoft"},
                target="drift:apply",
            )
            session.commit()

        monkeypatch.setattr("sys.argv", ["flag_stale_wrong_direction_proposals.py", "--apply"])
        rc = flag_main()
        assert rc == 0

        with get_session() as session:
            row = session.get(ProposedAction, action_id)
            assert row.status == "rejected"
            assert row.payload["_stale_wrong_direction"]["reason"] == "first_observation"
    finally:
        _reset_db_env()


def test_telemetry_field_proposal_gets_flagged(tmp_path, monkeypatch):
    _point_db_at_sqlite(tmp_path)
    try:
        with get_session() as session:
            action_id = _seed(
                session,
                # GitLab #162: the old field name "uptime" never matched the
                # real vsphere.py column (uptime_seconds), so this test was
                # silently exercising a no-op match. drift_taxonomy.py fixed
                # the field-name mismatch; use the real column name here.
                field="uptime_seconds",
                old={"v": 100},
                new={"v": 5},
                target="drift:telemetry",
            )
            session.commit()

        monkeypatch.setattr("sys.argv", ["flag_stale_wrong_direction_proposals.py", "--apply"])
        rc = flag_main()
        assert rc == 0

        with get_session() as session:
            row = session.get(ProposedAction, action_id)
            assert row.status == "rejected"
            assert row.payload["_stale_wrong_direction"]["reason"] == "telemetry_drift"
    finally:
        _reset_db_env()


def test_non_pending_rows_are_ignored(tmp_path, monkeypatch):
    _point_db_at_sqlite(tmp_path)
    try:
        with get_session() as session:
            action_id = _seed(
                session,
                field="os_vendor",
                old=None,
                new={"v": "Microsoft"},
                status="approved",
                target="drift:already-approved",
            )
            session.commit()

        monkeypatch.setattr("sys.argv", ["flag_stale_wrong_direction_proposals.py", "--apply"])
        rc = flag_main()
        assert rc == 0

        with get_session() as session:
            row = session.get(ProposedAction, action_id)
            # Untouched: not status="pending" at scan time.
            assert row.status == "approved"
    finally:
        _reset_db_env()


def test_report_prints_matched_rows(tmp_path, monkeypatch, capsys):
    _point_db_at_sqlite(tmp_path)
    try:
        with get_session() as session:
            _seed(
                session,
                field="os_vendor",
                old=None,
                new={"v": "Microsoft"},
                target="drift:printed",
            )
            session.commit()

        monkeypatch.setattr("sys.argv", ["flag_stale_wrong_direction_proposals.py"])
        flag_main()
        out = capsys.readouterr().out
        assert "drift:printed" in out
        assert "first_observation" in out
        assert "matched rows: 1" in out
    finally:
        _reset_db_env()


def test_engine_smoke_uses_real_sqlalchemy_url(tmp_path):
    """Sanity: _point_db_at_sqlite actually redirects the shared engine."""
    sqlite_url = _point_db_at_sqlite(tmp_path)
    try:
        engine = sa.create_engine(sqlite_url)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    finally:
        _reset_db_env()
