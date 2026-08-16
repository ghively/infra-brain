import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import event
from sqlalchemy.orm import Session

from infra_brain.agents.drift import DriftDetector, diff_snapshots
from infra_brain.db.models import (
    AgentActionLog,
    CollectionRun,
    DriftEvent,
    Resource,
    Snapshot,
)

from tests.support.pg import make_engine


def test_diff_snapshots_detects_config_change():
    old = {"instance_type": "t3.small", "state": "running"}
    new = {"instance_type": "t3.medium", "state": "running"}
    diffs = diff_snapshots(old, new)
    assert len(diffs) == 1
    assert diffs[0]["field"] == "instance_type"
    assert diffs[0]["old_value"] == "t3.small"
    assert diffs[0]["new_value"] == "t3.medium"
    assert diffs[0]["drift_type"] == "config_drift"


def test_diff_snapshots_no_change():
    snap = {"instance_type": "t3.small", "state": "running"}
    diffs = diff_snapshots(snap, snap)
    assert diffs == []


def test_diff_snapshots_detects_added_field():
    old = {"name": "web01"}
    new = {"name": "web01", "new_tag": "production"}
    diffs = diff_snapshots(old, new)
    assert any(d["field"] == "new_tag" for d in diffs)


def test_diff_snapshots_nested_dict_change_produces_correct_field_and_values():
    """AA-C-6: naive string-replace mangled nested paths.

    ``root['tags']['env']`` used to become the field name "tagsenv" (both
    dicts' bracket/quote characters stripped with no separator preserved),
    and old/new_value lookups via ``dict.get(field)`` on the mangled key
    always returned None for the added/removed cases.
    """
    old = {"tags": {"env": "prod"}}
    new = {"tags": {"env": "staging"}}
    diffs = diff_snapshots(old, new)
    assert len(diffs) == 1
    assert diffs[0]["field"] == "tags.env"
    assert diffs[0]["old_value"] == "prod"
    assert diffs[0]["new_value"] == "staging"


def test_diff_snapshots_nested_list_index_change():
    old = {"nested": {"list": [{"a": 1}, {"a": 2}]}}
    new = {"nested": {"list": [{"a": 1}, {"a": 3}]}}
    diffs = diff_snapshots(old, new)
    assert len(diffs) == 1
    assert diffs[0]["field"] == "nested.list[1].a"
    assert diffs[0]["old_value"] == 2
    assert diffs[0]["new_value"] == 3


def test_diff_snapshots_nested_added_field_resolves_real_value():
    """The added-field branch must resolve the actual nested value, not
    ``new.get(field)`` on a mangled/dotted field name (which always misses
    for anything but a top-level key)."""
    old = {"tags": {}}
    new = {"tags": {"owner": "team-infra"}}
    diffs = diff_snapshots(old, new)
    assert len(diffs) == 1
    assert diffs[0]["field"] == "tags.owner"
    assert diffs[0]["old_value"] is None
    assert diffs[0]["new_value"] == "team-infra"


def test_diff_snapshots_nested_removed_field_resolves_real_value():
    old = {"tags": {"owner": "team-infra"}}
    new = {"tags": {}}
    diffs = diff_snapshots(old, new)
    assert len(diffs) == 1
    assert diffs[0]["field"] == "tags.owner"
    assert diffs[0]["old_value"] == "team-infra"
    assert diffs[0]["new_value"] is None


def test_detect_all_writes_drift_events():
    """detect_all() persists a DriftEvent when a resource's last two snapshots
    differ.

    REWRITTEN 2026-08-12 from a rigid MagicMock-chain test that pinned the
    exact query().filter().distinct() call SHAPE rather than the behaviour --
    it broke whenever the query was legitimately restructured (it survived
    TRK-239's window rewrite only by re-pinning, and broke again when
    event-shaped filtering was added) while proving nothing a mock cannot
    prove about SQL. Real engine, real rows, same guarantee, shape-agnostic.
    """
    from sqlalchemy.orm import Session

    from infra_brain.db.models import CollectionRun, Resource, Snapshot

    eng = make_engine()

    with Session(eng) as s:
        r = Resource(id=uuid.uuid4(), domain="cloud", type="instance", name="i-1", source="t")
        run1 = CollectionRun(id=uuid.uuid4(), domain="cloud", trigger_type="scheduled",
                             status="completed")
        run2 = CollectionRun(id=uuid.uuid4(), domain="cloud", trigger_type="scheduled",
                             status="completed")
        s.add_all([r, run1, run2])
        s.flush()
        s.add(Snapshot(id=uuid.uuid4(), resource_id=r.id, run_id=run1.id,
                       snapshot={"instance_type": "t3.small"}))
        s.add(Snapshot(id=uuid.uuid4(), resource_id=r.id, run_id=run2.id,
                       snapshot={"instance_type": "t3.medium"}))
        s.commit()

    from contextlib import contextmanager

    @contextmanager
    def _get():
        with Session(eng) as sess:
            yield sess
            sess.commit()

    with patch("infra_brain.agents.drift.get_session", _get):
        detector = DriftDetector.__new__(DriftDetector)
        events = detector.detect_all()

    assert len(events) == 1
    with Session(eng) as s:
        row = s.query(DriftEvent).one()
        assert row.field == "instance_type"
        assert row.drift_type == "config_drift"


def test_drift_detector_run_calls_detection_methods():
    """DriftDetector.run() must call detect_all() and detect_state_drift()."""
    from infra_brain.agents.drift import DriftDetector

    detector = DriftDetector.__new__(DriftDetector)

    with (
        patch.object(detector, "detect_all", return_value=[]) as mock_detect_all,
        patch.object(detector, "detect_state_drift", return_value=[]) as mock_detect_state,
        patch("infra_brain.agents.drift.get_session") as mock_ctx,
    ):
        # Provide a minimal session so CollectionRun DB writes don't fail
        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock()
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        # run() needs self.domain and self.settings (collection_disabled_domains check)
        detector.domain = "drift"
        detector.settings = MagicMock(collection_disabled_domains="")
        result = detector.run()

    mock_detect_all.assert_called_once()
    mock_detect_state.assert_called_once()
    assert result is not None


def test_drift_detector_run_honours_collection_disabled_domains():
    """AA-R-11/12: DriftDetector.run() fully overrides ETLConnector.run() and
    must still respect collection_disabled_domains, same as the standard flow."""
    from infra_brain.agents.drift import DriftDetector

    detector = DriftDetector.__new__(DriftDetector)

    with (
        patch.object(detector, "detect_all") as mock_detect_all,
        patch.object(detector, "detect_state_drift") as mock_detect_state,
        patch("infra_brain.agents.drift.get_session") as mock_ctx,
        # M-1: the run-row finalize/guard moved into the shared
        # ETLConnector._finalize_run / run_row_guard helpers, which resolve
        # get_session from infra_brain.etl.base — patch that too or the
        # finalize escapes this mock and hits the real (tableless) engine.
        patch("infra_brain.etl.base.get_session", new=mock_ctx),
    ):
        mock_session = MagicMock()
        mock_run = MagicMock()
        mock_session.get.return_value = mock_run
        mock_ctx.return_value.__enter__ = lambda s: mock_session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        detector.domain = "drift"
        detector.settings = MagicMock(collection_disabled_domains="drift,other")
        result = detector.run()

    mock_detect_all.assert_not_called()
    mock_detect_state.assert_not_called()
    assert result.status == "skipped"
    assert mock_run.status == "skipped"


# --------------------------------------------------------------------------- #
# Task 3: partial-sweep drift suppression                                     #
# --------------------------------------------------------------------------- #


def _engine():
    eng = make_engine()
    return eng


def _patched_get_session(eng):
    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    return patch("infra_brain.agents.drift.get_session", _get_session)


def _seed_two_domains_no_current_snapshot(eng):
    """Each domain has one completed CollectionRun and one Resource. Each
    resource WAS observed once (a Snapshot tied to an older completed run) but
    has no Snapshot tied to the domain's latest run — both would normally read
    as "disappeared" (GitLab #137: Resource.retired_at stamped) unless
    excluded via succeeded_domains.
    """
    now = datetime.now(UTC)
    with Session(eng) as s:
        for domain, rtype, name, source in (
            ("vsphere", "vm", "vm01", "vsphere"),
            ("linux", "host", "host01", "linux"),
        ):
            old_run = CollectionRun(
                id=uuid.uuid4(),
                domain=domain,
                trigger_type="scheduled",
                status="completed",
                started_at=now - timedelta(days=1, minutes=10),
                finished_at=now - timedelta(days=1, minutes=5),
            )
            last_run = CollectionRun(
                id=uuid.uuid4(),
                domain=domain,
                trigger_type="scheduled",
                status="completed",
                started_at=now - timedelta(minutes=10),
                finished_at=now - timedelta(minutes=5),
            )
            res = Resource(id=uuid.uuid4(), domain=domain, type=rtype, name=name, source=source)
            s.add_all([old_run, last_run, res])
            s.flush()
            # Observed in the OLDER run only — absent from the latest run.
            s.add(Snapshot(resource_id=res.id, run_id=old_run.id, snapshot={"name": name}))
        s.commit()


def _flagged_domains(eng) -> set[str]:
    """Domains of resources retired by the disappearance scan (GitLab #137:
    retirement is recorded on Resource.retired_at, never as a DriftEvent) —
    queried fresh so we never touch objects detached from a closed session."""
    with Session(eng) as s:
        rows = s.query(Resource.domain).filter(Resource.retired_at.isnot(None)).all()
        return {domain for (domain,) in rows}


def test_detect_state_drift_none_scans_all_domains_existing_behavior():
    """Pinning existing behavior: succeeded_domains=None (the default, used by
    the supervisor hook and the daily catch-up job) must scan every domain,
    unchanged from before Task 3."""
    eng = _engine()
    _seed_two_domains_no_current_snapshot(eng)
    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        detector.detect_state_drift()
    domains_flagged = _flagged_domains(eng)
    assert domains_flagged == {"vsphere", "linux"}


def test_detect_state_drift_succeeded_domains_excludes_failed_domain():
    """A vSphere outage (retry_exhausted this sweep) must not manufacture a
    phantom 'resource disappeared' event for vsphere — only linux (which
    succeeded this sweep) gets scanned for disappearance."""
    eng = _engine()
    _seed_two_domains_no_current_snapshot(eng)
    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        detector.detect_state_drift(succeeded_domains={"linux"})
    domains_flagged = _flagged_domains(eng)
    assert domains_flagged == {"linux"}
    assert "vsphere" not in domains_flagged


def test_detect_state_drift_empty_set_scans_nothing_all_failed_sweep():
    """Total sweep failure (succeeded_domains=set()) must scan NOTHING — the
    highest-consequence edge: absence of every domain is evidence of collector
    failure, not disappearance. Guards against a future `if succeeded_domains:`
    truthiness refactor, which would treat set() like None and resurrect
    fleet-wide phantom disappearance events on an all-failed sweep."""
    eng = _engine()
    _seed_two_domains_no_current_snapshot(eng)
    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        detector.detect_state_drift(succeeded_domains=set())
    assert _flagged_domains(eng) == set()


def test_detect_state_drift_last_run_lookup_is_bounded_not_full_history_scan():
    """M-4: detect_state_drift used to fetch EVERY completed CollectionRun
    EVER (across every domain, unbounded) --
    ``session.query(CollectionRun).filter_by(status="completed")
    .order_by(...).all()`` -- just to keep the single most-recent run per
    domain. collection_runs grows without bound (every sweep of every
    domain, forever).

    Real-behavior check (not the shape of the patch): seed far more
    completed runs per domain than are needed, capture the actual SQL sent
    to the DB, and assert the "most recent run per domain" selection is
    pushed into SQL (a window function) rather than an unbounded full-table
    SELECT. Also pins correctness: the actual most-recent run per domain is
    still the one used (proven indirectly via the pre-existing disappearance
    scan, which only fires when the true latest run's Snapshot is missing).
    """
    from sqlalchemy import event

    eng = _engine()
    now = datetime.now(UTC)
    with Session(eng) as s:
        for domain, rtype, name, source in (
            ("vsphere", "vm", "vm01", "vsphere"),
            ("linux", "host", "host01", "linux"),
        ):
            # 20 historical completed runs per domain -- far more than the
            # single "most recent" that's actually needed.
            for i in range(20, 0, -1):
                s.add(
                    CollectionRun(
                        id=uuid.uuid4(),
                        domain=domain,
                        trigger_type="scheduled",
                        status="completed",
                        started_at=now - timedelta(minutes=10 * i + 5),
                        finished_at=now - timedelta(minutes=10 * i),
                    )
                )
            # The TRUE most-recent completed run -- no Snapshot tied to it,
            # so the resource should read as "disappeared" (only correct if
            # THIS run, not one of the 20 stale ones, is what gets used).
            last_run = CollectionRun(
                id=uuid.uuid4(),
                domain=domain,
                trigger_type="scheduled",
                status="completed",
                started_at=now - timedelta(minutes=10),
                finished_at=now - timedelta(minutes=5),
            )
            res = Resource(id=uuid.uuid4(), domain=domain, type=rtype, name=name, source=source)
            s.add_all([last_run, res])
            s.flush()
            # Observed only in the OLDEST of the 20 historical runs -- absent
            # from the true latest run.
            oldest_run_id = (
                s.query(CollectionRun.id)
                .filter_by(domain=domain, status="completed")
                .order_by(CollectionRun.finished_at.asc())
                .first()[0]
            )
            s.add(Snapshot(resource_id=res.id, run_id=oldest_run_id, snapshot={"name": name}))
        s.commit()

    captured_sql: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured_sql.append(statement)

    event.listen(eng, "before_cursor_execute", _capture)
    try:
        with _patched_get_session(eng):
            detector = DriftDetector.__new__(DriftDetector)
            detector.detect_state_drift()
    finally:
        event.remove(eng, "before_cursor_execute", _capture)

    # Correctness unaffected: both resources are flagged disappeared, which
    # only happens if the TRUE most-recent run per domain (not a stale one
    # of the 20) was actually used for the presence check.
    assert _flagged_domains(eng) == {"vsphere", "linux"}

    cr_selects = [
        sql
        for sql in captured_sql
        if sql.strip().lower().startswith("select") and "collection_runs" in sql.lower()
    ]
    assert cr_selects, "expected detect_state_drift to query collection_runs at all"
    assert any(
        "over" in sql.lower() and "partition" in sql.lower() for sql in cr_selects
    ), (
        "detect_state_drift must push the per-domain latest-run selection into "
        "SQL (a window function) instead of pulling every completed "
        "CollectionRun ever into Python"
    )


# --------------------------------------------------------------------------- #
# GitLab #137: retirement bookkeeping must never land in drift_events          #
# --------------------------------------------------------------------------- #


def test_detect_state_drift_writes_no_drift_events():
    """A disappeared, previously-observed resource gets Resource.retired_at
    stamped — and NOT a state_drift/presence DriftEvent row (GitLab #137: the
    old rows had NULL collection_run_id and flooded the open-drift feed)."""
    eng = _engine()
    _seed_two_domains_no_current_snapshot(eng)
    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        retired = detector.detect_state_drift()
    assert len(retired) == 2
    with Session(eng) as s:
        assert s.query(DriftEvent).count() == 0
        retired_names = {
            name for (name,) in s.query(Resource.name).filter(Resource.retired_at.isnot(None))
        }
        assert retired_names == {"vm01", "host01"}


def test_detect_state_drift_is_idempotent():
    """A second pass over already-retired resources retires nothing new."""
    eng = _engine()
    _seed_two_domains_no_current_snapshot(eng)
    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        first = detector.detect_state_drift()
        second = detector.detect_state_drift()
    assert len(first) == 2
    assert second == []


def test_detect_state_drift_skips_never_observed_synthetic_resources():
    """A resource with NO snapshot ever (e.g. a graph_maintenance compliance
    violation-shadow node) was never a collection observation — it cannot
    'disappear' and must be neither retired nor drift-flagged. This is the
    #137 source fix: shadows like 'stale_drift:web-01' previously got a
    presence DriftEvent each pass, feeding the stale_drift feedback loop."""
    eng = _engine()
    now = datetime.now(UTC)
    with Session(eng) as s:
        run = CollectionRun(
            id=uuid.uuid4(),
            domain="compliance",
            trigger_type="scheduled",
            status="completed",
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=5),
        )
        shadow = Resource(
            id=uuid.uuid4(),
            domain="compliance",
            type="violation",
            name="stale_drift:web-01",
            source="graph_maintenance",
        )
        s.add_all([run, shadow])
        s.commit()
    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        retired = detector.detect_state_drift()
    assert retired == []
    with Session(eng) as s:
        assert s.query(DriftEvent).count() == 0
        assert s.query(Resource).filter(Resource.retired_at.isnot(None)).count() == 0


def test_detect_state_drift_resolves_legacy_presence_rows():
    """Open state_drift/presence rows written by the pre-#137 code are
    bulk-resolved on every pass (self-heal of the 63,704-row backlog),
    even when there is no completed run yet."""
    eng = _engine()
    with Session(eng) as s:
        res = Resource(id=uuid.uuid4(), domain="linux", type="host", name="gone01", source="linux")
        s.add(res)
        s.flush()
        s.add(
            DriftEvent(
                resource_id=res.id,
                collection_run_id=None,
                drift_type="state_drift",
                field="presence",
                old_value={"v": "present"},
                new_value={"v": "absent"},
                status="open",
            )
        )
        s.commit()
    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        detector.detect_state_drift()
    with Session(eng) as s:
        row = s.query(DriftEvent).one()
        assert row.status == "resolved"


def test_detect_state_drift_reappearance_clears_retired_at():
    """A retired resource that shows up in its domain's latest completed run
    gets retired_at cleared again (reconciliation is reversible)."""
    eng = _engine()
    now = datetime.now(UTC)
    with Session(eng) as s:
        run = CollectionRun(
            id=uuid.uuid4(),
            domain="linux",
            trigger_type="scheduled",
            status="completed",
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=5),
        )
        res = Resource(
            id=uuid.uuid4(),
            domain="linux",
            type="host",
            name="back01",
            source="linux",
            retired_at=now - timedelta(days=3),
        )
        s.add_all([run, res])
        s.flush()
        s.add(Snapshot(resource_id=res.id, run_id=run.id, snapshot={"name": "back01"}))
        s.commit()
    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        retired = detector.detect_state_drift()
    assert retired == []
    with Session(eng) as s:
        assert s.query(Resource).filter_by(name="back01").one().retired_at is None


# --------------------------------------------------------------------------- #
# TRK-026: run() drift_count integrity on partial failure                     #
# --------------------------------------------------------------------------- #


def _seed_resource_with_config_drift(eng) -> None:
    """One resource with two differing snapshots — detect_all() will diff them
    and commit exactly one config_drift DriftEvent stamped with the run's id."""
    now = datetime.now(UTC)
    with Session(eng) as s:
        seed_run = CollectionRun(
            id=uuid.uuid4(),
            domain="linux",
            trigger_type="scheduled",
            status="completed",
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=5),
        )
        s.add(seed_run)
        resource = Resource(
            id=uuid.uuid4(), domain="linux", type="host", name="host01", source="linux"
        )
        s.add(resource)
        s.add(
            Snapshot(
                id=uuid.uuid4(),
                resource_id=resource.id,
                run_id=seed_run.id,
                snapshot={"instance_type": "t3.small"},
                collected_at=now - timedelta(minutes=5),
            )
        )
        s.add(
            Snapshot(
                id=uuid.uuid4(),
                resource_id=resource.id,
                run_id=seed_run.id,
                snapshot={"instance_type": "t3.medium"},
                collected_at=now,
            )
        )
        s.commit()


# --------------------------------------------------------------------------- #
# TRK-239: last-2-snapshots window must be enforced in SQL, not in Python      #
# --------------------------------------------------------------------------- #


def _seed_resource_with_deep_snapshot_history(eng) -> tuple[uuid.UUID, uuid.UUID]:
    """Two resources with deep snapshot history (6 and 4 snapshots).

    ``zone`` only ever changes between OLDER snapshot pairs; ``instance_type``
    changes only between the two NEWEST. So the exact set of drift events tells
    us how far back the window actually reached: a correct "last 2 only" window
    yields one config_drift on ``instance_type`` per resource and nothing for
    ``zone``.
    """
    now = datetime.now(UTC)
    with Session(eng) as s:
        seed_run = CollectionRun(
            id=uuid.uuid4(),
            domain="linux",
            trigger_type="scheduled",
            status="completed",
            started_at=now - timedelta(hours=2),
            finished_at=now - timedelta(hours=1),
        )
        s.add(seed_run)
        res_a = Resource(
            id=uuid.uuid4(), domain="linux", type="host", name="host01", source="linux"
        )
        res_b = Resource(
            id=uuid.uuid4(), domain="linux", type="host", name="host02", source="linux"
        )
        s.add_all([res_a, res_b])

        # (minutes_ago, zone, instance_type) — newest last.
        history_a = [
            (60, "us-east-1a", "t3.small"),
            (50, "us-east-1b", "t3.small"),
            (40, "us-east-1c", "t3.small"),
            (30, "us-east-1d", "t3.small"),
            (20, "us-east-1e", "t3.small"),  # 2nd newest
            (10, "us-east-1e", "t3.medium"),  # newest: only instance_type moved
        ]
        history_b = [
            (60, "eu-west-1a", "m5.large"),
            (40, "eu-west-1b", "m5.large"),
            (20, "eu-west-1c", "m5.large"),  # 2nd newest
            (10, "eu-west-1c", "m5.xlarge"),  # newest
        ]
        for resource, history in ((res_a, history_a), (res_b, history_b)):
            for minutes_ago, zone, instance_type in history:
                s.add(
                    Snapshot(
                        id=uuid.uuid4(),
                        resource_id=resource.id,
                        run_id=seed_run.id,
                        snapshot={"zone": zone, "instance_type": instance_type},
                        collected_at=now - timedelta(minutes=minutes_ago),
                    )
                )
        s.commit()
        return res_a.id, res_b.id


def test_detect_all_considers_only_two_newest_snapshots_per_resource():
    """TRK-239 behavioral regression: with 6 and 4 snapshots on two resources,
    ``detect_all()`` must diff only the two newest per resource — one
    ``instance_type`` event each, and no ``zone`` event (``zone`` only changed
    between older pairs). Also asserts the window is enforced server-side: at
    most 2 Snapshot rows per resource are ever hydrated into the session, so the
    nightly full-fleet pass cannot pull the whole retained history into memory
    (the OOM cause behind TRK-149).
    """
    eng = _engine()
    res_a, res_b = _seed_resource_with_deep_snapshot_history(eng)

    hydrated: list[Snapshot] = []

    def _record(_session, instance):
        if isinstance(instance, Snapshot):
            hydrated.append(instance)

    event.listen(Session, "loaded_as_persistent", _record)
    try:
        with _patched_get_session(eng):
            detector = DriftDetector.__new__(DriftDetector)
            detector.detect_all()
    finally:
        event.remove(Session, "loaded_as_persistent", _record)

    with Session(eng) as s:
        events = s.query(DriftEvent).all()
        by_resource = {}
        for e in events:
            by_resource.setdefault(e.resource_id, []).append((e.field, e.old_value, e.new_value))

    assert set(by_resource) == {res_a, res_b}
    assert by_resource[res_a] == [("instance_type", {"v": "t3.small"}, {"v": "t3.medium"})]
    assert by_resource[res_b] == [("instance_type", {"v": "m5.large"}, {"v": "m5.xlarge"})]
    assert not any(field == "zone" for rows in by_resource.values() for field, _, _ in rows), (
        "a zone drift event means the window reached past the 2 newest snapshots"
    )

    # 10 snapshots exist; a correct server-side window hydrates at most 2 per
    # resource. The old unbounded query hydrated all 10.
    assert len(hydrated) <= 2 * 2, (
        f"hydrated {len(hydrated)} Snapshot rows; expected <= 4 (2 per resource)"
    )


def _committed_drift_count(eng, run_id) -> int:
    with Session(eng) as s:
        return s.query(DriftEvent).filter(DriftEvent.collection_run_id == run_id).count()


def test_drift_count_reflects_committed_events_on_partial_failure():
    """TRK-026: detect_all() commits DriftEvents, then detect_state_drift()
    raises. run() must report the drift_count actually committed to the DB
    (derived via count_drift_events_for_run), not the stale 0 left by the
    success-only ``drift_count = len(...)`` assignment."""
    eng = _engine()
    _seed_resource_with_config_drift(eng)

    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        detector.domain = "drift"
        detector.settings = MagicMock(collection_disabled_domains="")
        with patch.object(
            detector, "detect_state_drift", side_effect=RuntimeError("state phase boom")
        ):
            result = detector.run()

    committed = _committed_drift_count(eng, result.run_id)
    assert committed >= 1, "detect_all() should have committed at least one DriftEvent"
    assert result.status == "failed"
    assert result.drift_count == committed


# --------------------------------------------------------------------------- #
# TRK-256: auto-resolve drift on re-detection-back-in-spec                    #
# --------------------------------------------------------------------------- #


def _seed_resource_reverting_a_field(eng) -> uuid.UUID:
    """A resource with an open DriftEvent on field "instance_type", plus two
    fresh snapshots whose diff no longer includes that field (the newest
    snapshot's value matches the second-newest again) — the field is back in
    spec and the open event should auto-resolve on the next detection pass.
    """
    now = datetime.now(UTC)
    with Session(eng) as s:
        seed_run = CollectionRun(
            id=uuid.uuid4(),
            domain="linux",
            trigger_type="scheduled",
            status="completed",
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=5),
        )
        s.add(seed_run)
        resource = Resource(
            id=uuid.uuid4(), domain="linux", type="host", name="host01", source="linux"
        )
        s.add(resource)
        s.flush()
        # A pre-existing OPEN drift event on "instance_type" — as if a prior
        # pass detected t3.small -> t3.medium and it's still unresolved.
        s.add(
            DriftEvent(
                id=uuid.uuid4(),
                resource_id=resource.id,
                collection_run_id=seed_run.id,
                drift_type="config_drift",
                field="instance_type",
                old_value={"v": "t3.small"},
                new_value={"v": "t3.medium"},
                detected_at=now - timedelta(minutes=5),
                status="open",
            )
        )
        # Two fresh snapshots whose diff has NO instance_type change — the
        # value reverted back to the pre-drift baseline.
        s.add(
            Snapshot(
                id=uuid.uuid4(),
                resource_id=resource.id,
                run_id=seed_run.id,
                snapshot={"instance_type": "t3.small", "state": "running"},
                collected_at=now - timedelta(minutes=2),
            )
        )
        s.add(
            Snapshot(
                id=uuid.uuid4(),
                resource_id=resource.id,
                run_id=seed_run.id,
                snapshot={"instance_type": "t3.small", "state": "stopped"},
                collected_at=now,
            )
        )
        s.commit()
        return resource.id


def test_drift_auto_resolves_when_field_returns_to_spec():
    """A resource with an open DriftEvent on field X, whose newest snapshot
    diff no longer includes X (value reverted to match the pre-drift
    baseline), must have that DriftEvent auto-transition to resolved on the
    next DriftDetector pass — not stay open forever (TRK-256)."""
    eng = _engine()
    resource_id = _seed_resource_reverting_a_field(eng)

    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        with Session(eng) as s:
            detector._detect_for_resources(s, [resource_id])
            s.commit()

    with Session(eng) as s:
        event = s.query(DriftEvent).filter_by(resource_id=resource_id, field="instance_type").one()
        assert event.status == "resolved"
        # New, real evidence this pass ("state" changed running -> stopped)
        # must still be recorded as a normal open event.
        state_event = s.query(DriftEvent).filter_by(resource_id=resource_id, field="state").one()
        assert state_event.status == "open"
        # The auto-resolve batch must be audited in the same transaction.
        audit_rows = (
            s.query(AgentActionLog)
            .filter(AgentActionLog.tool == "_detect_for_resources.auto_resolve")
            .all()
        )
        assert len(audit_rows) == 1
        assert str(resource_id) in audit_rows[0].args_summary
        assert "instance_type" in audit_rows[0].args_summary


def _seed_resource_still_drifted(eng) -> uuid.UUID:
    """A resource with an open DriftEvent on "instance_type" whose newest
    snapshot pair STILL disagrees on that field — genuinely still drifted,
    must not be auto-resolved."""
    now = datetime.now(UTC)
    with Session(eng) as s:
        seed_run = CollectionRun(
            id=uuid.uuid4(),
            domain="linux",
            trigger_type="scheduled",
            status="completed",
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=5),
        )
        s.add(seed_run)
        resource = Resource(
            id=uuid.uuid4(), domain="linux", type="host", name="host02", source="linux"
        )
        s.add(resource)
        s.flush()
        s.add(
            DriftEvent(
                id=uuid.uuid4(),
                resource_id=resource.id,
                collection_run_id=seed_run.id,
                drift_type="config_drift",
                field="instance_type",
                old_value={"v": "t3.small"},
                new_value={"v": "t3.medium"},
                detected_at=now - timedelta(minutes=5),
                status="open",
            )
        )
        s.add(
            Snapshot(
                id=uuid.uuid4(),
                resource_id=resource.id,
                run_id=seed_run.id,
                snapshot={"instance_type": "t3.medium"},
                collected_at=now - timedelta(minutes=2),
            )
        )
        s.add(
            Snapshot(
                id=uuid.uuid4(),
                resource_id=resource.id,
                run_id=seed_run.id,
                snapshot={"instance_type": "t3.large"},
                collected_at=now,
            )
        )
        s.commit()
        return resource.id


def test_drift_still_open_events_are_untouched():
    """A field that's still genuinely drifted this pass must not be
    accidentally auto-resolved (TRK-256)."""
    eng = _engine()
    resource_id = _seed_resource_still_drifted(eng)

    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        with Session(eng) as s:
            detector._detect_for_resources(s, [resource_id])
            s.commit()

    with Session(eng) as s:
        events = s.query(DriftEvent).filter_by(resource_id=resource_id, field="instance_type").all()
        # The pre-existing open event stays open (still-drifted, no auto-resolve)...
        assert any(e.status == "open" and e.old_value == {"v": "t3.small"} for e in events)
        # ...and no auto-resolve audit row was written.
        audit_rows = (
            s.query(AgentActionLog)
            .filter(AgentActionLog.tool == "_detect_for_resources.auto_resolve")
            .all()
        )
        assert audit_rows == []


def _seed_resource_persistent_no_further_change(eng) -> uuid.UUID:
    """A resource with an open DriftEvent on "instance_type" (baseline
    t3.small) whose newest TWO snapshots both sit on the drifted value
    (t3.medium == t3.medium, no further movement between them). This is the
    steady-state-still-drifted case: absence from THIS pass's diff must not
    be mistaken for "back in spec" — the field never reverted to t3.small.
    """
    now = datetime.now(UTC)
    with Session(eng) as s:
        seed_run = CollectionRun(
            id=uuid.uuid4(),
            domain="linux",
            trigger_type="scheduled",
            status="completed",
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=5),
        )
        s.add(seed_run)
        resource = Resource(
            id=uuid.uuid4(), domain="linux", type="host", name="host03", source="linux"
        )
        s.add(resource)
        s.flush()
        s.add(
            DriftEvent(
                id=uuid.uuid4(),
                resource_id=resource.id,
                collection_run_id=seed_run.id,
                drift_type="config_drift",
                field="instance_type",
                old_value={"v": "t3.small"},
                new_value={"v": "t3.medium"},
                detected_at=now - timedelta(minutes=5),
                status="open",
            )
        )
        for minutes_ago in (2, 0):
            s.add(
                Snapshot(
                    id=uuid.uuid4(),
                    resource_id=resource.id,
                    run_id=seed_run.id,
                    snapshot={"instance_type": "t3.medium"},
                    collected_at=now - timedelta(minutes=minutes_ago),
                )
            )
        s.commit()
        return resource.id


def test_drift_persistent_steady_state_is_not_auto_resolved():
    """TRK-256 regression: a field with NO change between this pass's two
    snapshots (steady state on the still-drifted value) must NOT be
    auto-resolved just because it's absent from this pass's diff — it never
    reverted to the event's recorded baseline."""
    eng = _engine()
    resource_id = _seed_resource_persistent_no_further_change(eng)

    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        with Session(eng) as s:
            detector._detect_for_resources(s, [resource_id])
            s.commit()

    with Session(eng) as s:
        event = s.query(DriftEvent).filter_by(resource_id=resource_id, field="instance_type").one()
        assert event.status == "open"
        audit_rows = (
            s.query(AgentActionLog)
            .filter(AgentActionLog.tool == "_detect_for_resources.auto_resolve")
            .all()
        )
        assert audit_rows == []


def _seed_resource_reverting_within_this_pass_diff(eng) -> uuid.UUID:
    """A resource with an open DriftEvent on "instance_type" (baseline
    t3.small), whose newest TWO snapshots themselves capture the revert
    (t3.medium -> t3.small) — the revert IS this pass's fresh diff, not an
    absence from it. Must still auto-resolve.
    """
    now = datetime.now(UTC)
    with Session(eng) as s:
        seed_run = CollectionRun(
            id=uuid.uuid4(),
            domain="linux",
            trigger_type="scheduled",
            status="completed",
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=5),
        )
        s.add(seed_run)
        resource = Resource(
            id=uuid.uuid4(), domain="linux", type="host", name="host04", source="linux"
        )
        s.add(resource)
        s.flush()
        s.add(
            DriftEvent(
                id=uuid.uuid4(),
                resource_id=resource.id,
                collection_run_id=seed_run.id,
                drift_type="config_drift",
                field="instance_type",
                old_value={"v": "t3.small"},
                new_value={"v": "t3.medium"},
                detected_at=now - timedelta(minutes=5),
                status="open",
            )
        )
        s.add(
            Snapshot(
                id=uuid.uuid4(),
                resource_id=resource.id,
                run_id=seed_run.id,
                snapshot={"instance_type": "t3.medium"},
                collected_at=now - timedelta(minutes=2),
            )
        )
        s.add(
            Snapshot(
                id=uuid.uuid4(),
                resource_id=resource.id,
                run_id=seed_run.id,
                snapshot={"instance_type": "t3.small"},
                collected_at=now,
            )
        )
        s.commit()
        return resource.id


def test_drift_revert_captured_by_this_pass_diff_still_auto_resolves():
    """TRK-256 regression: a revert to baseline that IS this pass's fresh
    diff (not merely absent from it) must still auto-resolve — the
    auto-resolve check compares against the event's own recorded baseline,
    not diff-presence."""
    eng = _engine()
    resource_id = _seed_resource_reverting_within_this_pass_diff(eng)

    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        with Session(eng) as s:
            detector._detect_for_resources(s, [resource_id])
            s.commit()

    with Session(eng) as s:
        event = s.query(DriftEvent).filter_by(resource_id=resource_id, field="instance_type").one()
        assert event.status == "resolved"


def _seed_resource_with_foreign_open_event(eng) -> uuid.UUID:
    """A resource with an open DriftEvent whose old_value shape is NOT
    DriftDetector's own ({"v": ...}) — it mimics LinuxAgent's writes
    (linux.py: `old_value=None`, e.g. a "listening port opened" finding).
    The field never appears in either of the two newest snapshots (it's
    absent from the newest snapshot, same as the concrete failure mode
    described in the final-branch review: a still-open finding must not be
    silently closed just because DriftDetector's own baseline-comparison
    normalizes a missing field to `None` and `None == None`).
    """
    now = datetime.now(UTC)
    with Session(eng) as s:
        seed_run = CollectionRun(
            id=uuid.uuid4(),
            domain="linux",
            trigger_type="scheduled",
            status="completed",
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=5),
        )
        s.add(seed_run)
        resource = Resource(
            id=uuid.uuid4(), domain="linux", type="host", name="host05", source="linux"
        )
        s.add(resource)
        s.flush()
        # A foreign-writer open event: LinuxAgent's own DriftEvent shape,
        # NOT DriftDetector's ({"v": ...}). Field is a synthetic finding key
        # that never appears in a Snapshot's JSON body at all.
        s.add(
            DriftEvent(
                id=uuid.uuid4(),
                resource_id=resource.id,
                collection_run_id=seed_run.id,
                drift_type="listening_port_opened",
                field="listening_port.22",
                old_value=None,
                new_value={"port": 22, "proto": "tcp"},
                detected_at=now - timedelta(minutes=5),
                status="open",
            )
        )
        # Two fresh snapshots. Neither contains "listening_port.22" — the
        # field is absent from the newest snapshot, which is exactly the
        # condition the buggy version treated as "back in spec" (_MISSING ->
        # None == None baseline) for ANY open event regardless of author.
        s.add(
            Snapshot(
                id=uuid.uuid4(),
                resource_id=resource.id,
                run_id=seed_run.id,
                snapshot={"hostname": "host05", "state": "running"},
                collected_at=now - timedelta(minutes=2),
            )
        )
        s.add(
            Snapshot(
                id=uuid.uuid4(),
                resource_id=resource.id,
                run_id=seed_run.id,
                snapshot={"hostname": "host05", "state": "running"},
                collected_at=now,
            )
        )
        s.commit()
        return resource.id


def test_drift_foreign_writer_event_not_auto_resolved():
    """Final-branch-review regression: an open DriftEvent written by another
    domain agent (old_value shape != DriftDetector's own {"v": ...}) must
    NEVER be auto-resolved by DriftDetector's own re-detection pass, even
    when the field is absent from the newest snapshot. Concrete failure this
    guards: a still-open "listening port opened" finding from LinuxAgent
    getting silently closed by an unrelated DriftDetector pass."""
    eng = _engine()
    resource_id = _seed_resource_with_foreign_open_event(eng)

    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        with Session(eng) as s:
            detector._detect_for_resources(s, [resource_id])
            s.commit()

    with Session(eng) as s:
        event = (
            s.query(DriftEvent)
            .filter_by(resource_id=resource_id, field="listening_port.22")
            .one()
        )
        # Must stay open — this is the whole point of the fix.
        assert event.status == "open"
        # No auto-resolve audit row should have been written for this event.
        audit_rows = (
            s.query(AgentActionLog)
            .filter(AgentActionLog.tool == "_detect_for_resources.auto_resolve")
            .all()
        )
        assert audit_rows == []


def test_drift_auto_resolve_respects_hard_cap():
    """Final-branch-review fix: _detect_for_resources must never auto-resolve
    more than _AUTO_RESOLVE_BATCH_CAP events in one call, mirroring
    resolve_drift_events' _CLOSURE_BATCH_CAP in mcp_server.py. Seeds more
    eligible (DriftDetector-owned, reverted-to-baseline) events than the cap
    and asserts the number actually flipped to "resolved" is bounded."""
    from infra_brain.agents import drift as drift_module

    cap = drift_module._AUTO_RESOLVE_BATCH_CAP
    n_events = cap + 5
    now = datetime.now(UTC)
    eng = _engine()
    resource_ids = []
    with Session(eng) as s:
        seed_run = CollectionRun(
            id=uuid.uuid4(),
            domain="linux",
            trigger_type="scheduled",
            status="completed",
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=5),
        )
        s.add(seed_run)
        s.flush()
        for i in range(n_events):
            resource = Resource(
                id=uuid.uuid4(), domain="linux", type="host", name=f"cap-host{i}", source="linux"
            )
            s.add(resource)
            s.flush()
            resource_ids.append(resource.id)
            s.add(
                DriftEvent(
                    id=uuid.uuid4(),
                    resource_id=resource.id,
                    collection_run_id=seed_run.id,
                    drift_type="config_drift",
                    field="instance_type",
                    old_value={"v": "t3.small"},
                    new_value={"v": "t3.medium"},
                    detected_at=now - timedelta(minutes=5),
                    status="open",
                )
            )
            s.add(
                Snapshot(
                    id=uuid.uuid4(),
                    resource_id=resource.id,
                    run_id=seed_run.id,
                    snapshot={"instance_type": "t3.small"},
                    collected_at=now - timedelta(minutes=2),
                )
            )
            s.add(
                Snapshot(
                    id=uuid.uuid4(),
                    resource_id=resource.id,
                    run_id=seed_run.id,
                    snapshot={"instance_type": "t3.small"},
                    collected_at=now,
                )
            )
        s.commit()

    with _patched_get_session(eng):
        detector = DriftDetector.__new__(DriftDetector)
        with Session(eng) as s:
            detector._detect_for_resources(s, resource_ids)
            s.commit()

    with Session(eng) as s:
        resolved_count = (
            s.query(DriftEvent)
            .filter(DriftEvent.resource_id.in_(resource_ids), DriftEvent.status == "resolved")
            .count()
        )
        assert resolved_count == cap
        still_open_count = (
            s.query(DriftEvent)
            .filter(DriftEvent.resource_id.in_(resource_ids), DriftEvent.status == "open")
            .count()
        )
        assert still_open_count == n_events - cap
