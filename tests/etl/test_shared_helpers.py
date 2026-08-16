"""Phase 1 / Task 4: shared write helpers on ETLConnector.

Three agents (k8s, octopus, vsphere) carried near-identical ``_resource_id``
lookups, and several agents (cloud, iac, vuln, vuln_cve, octopus) carried
near-identical per-item ``session.begin_nested()`` SAVEPOINT loops with
warn-and-continue semantics. This centralizes both patterns on
``ETLConnector`` (see src/infra_brain/etl/base.py):

- ``_resource_id(session, type_, name, *, qualify=None)`` — None-guard on
  empty name (the octopus behavior, now universal), optional qualifier hook
  (vsphere's vCenter-qualified name).
- ``_write_each(session, items, write_fn, label_fn=None) -> (written, skipped)``
  — per-item SAVEPOINT, warn-and-continue, with counts.
"""

import uuid

import pytest
from sqlalchemy.orm import Session

from infra_brain.db.models import Resource
from infra_brain.etl.base import ETLConnector

from tests.support.pg import make_engine


class _FakeConnector(ETLConnector):
    """Minimal concrete ETLConnector for exercising the shared helpers.

    __init__ is deliberately NOT called (it builds Settings/callbacks); tests
    only need ``domain`` and the mixin methods under test.
    """

    domain = "widget"

    def collect(self, scope: str):
        return []


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def connector():
    return _FakeConnector.__new__(_FakeConnector)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s
        s.rollback()


def _make_resource(session, *, domain="widget", type_="widget_thing", name="thing-1") -> uuid.UUID:
    rid = uuid.uuid4()
    session.add(
        Resource(
            id=rid,
            name=name,
            domain=domain,
            type=type_,
            zone="corporate",
            source="test",
        )
    )
    session.commit()
    return rid


# --- _resource_id ------------------------------------------------------------


def test_resource_id_found(connector, session):
    rid = _make_resource(session)
    assert connector._resource_id(session, "widget_thing", "thing-1") == rid


def test_resource_id_missing_returns_none(connector, session):
    assert connector._resource_id(session, "widget_thing", "no-such-thing") is None


def test_resource_id_empty_name_returns_none(connector, session):
    # Universal None-guard (previously octopus-only): must not even query.
    assert connector._resource_id(session, "widget_thing", "") is None
    assert connector._resource_id(session, "widget_thing", None) is None


def test_resource_id_qualify_hook_applied(connector, session):
    rid = _make_resource(session, name="vc1/thing-1")
    qualified = connector._resource_id(
        session, "widget_thing", "thing-1", qualify=lambda n: f"vc1/{n}"
    )
    assert qualified == rid
    # Without the qualifier the bare name does not resolve.
    assert connector._resource_id(session, "widget_thing", "thing-1") is None


def test_resource_id_scopes_by_domain(connector, session):
    """Lookup is scoped to self.domain — a same-named resource in another
    domain must not match."""
    _make_resource(session, domain="other-domain", name="thing-1")
    assert connector._resource_id(session, "widget_thing", "thing-1") is None


# --- _write_each --------------------------------------------------------------


def test_write_each_all_good(connector, session):
    items = [{"n": 1}, {"n": 2}, {"n": 3}]
    seen = []

    def write_fn(item):
        seen.append(item["n"])

    written, skipped = connector._write_each(session, items, write_fn)
    assert (written, skipped) == (3, 0)
    assert seen == [1, 2, 3]


def test_write_each_skips_raising_item_and_continues(connector, session):
    items = [{"n": 1}, {"n": 2}, {"n": 3}]
    seen = []

    def write_fn(item):
        if item["n"] == 2:
            raise ValueError("boom")
        seen.append(item["n"])

    written, skipped = connector._write_each(session, items, write_fn)
    assert (written, skipped) == (2, 1)
    # Item 3 still ran despite item 2 raising — warn-and-continue, not abort.
    assert seen == [1, 3]


def test_write_each_counts_multiple_skips(connector, session):
    items = [{"n": n} for n in range(5)]

    def write_fn(item):
        if item["n"] % 2 == 0:
            raise ValueError(f"bad {item['n']}")

    written, skipped = connector._write_each(session, items, write_fn)
    assert (written, skipped) == (2, 3)


def test_write_each_empty_items_returns_zeros(connector, session):
    written, skipped = connector._write_each(session, [], lambda item: None)
    assert (written, skipped) == (0, 0)


def test_write_each_uses_label_fn_in_warning(connector, session, caplog):
    import logging

    items = [{"name": "bad-item"}]

    def write_fn(item):
        raise ValueError("nope")

    def label_fn(item, exc):
        return f"CustomAgent: custom message for {item['name']}: {exc}"

    with caplog.at_level(logging.WARNING):
        connector._write_each(session, items, write_fn, label_fn=label_fn)

    assert any("CustomAgent: custom message for bad-item" in r.message for r in caplog.records)


def test_write_each_actual_savepoint_isolation(connector, session):
    """A DB-level failure inside one item's SAVEPOINT must not poison later
    writes to the same session (the DL-C-5 SAVEPOINT-coverage guarantee)."""
    rid = uuid.uuid4()

    items = [
        {
            "resource": {
                "id": rid,
                "name": "dupe",
                "domain": "widget",
                "type": "widget_thing",
                "zone": "corporate",
                "source": "test",
            }
        },
        {
            "resource": {
                "id": rid,
                "name": "dupe2",
                "domain": "widget",
                "type": "widget_thing",
                "zone": "corporate",
                "source": "test",
            }
        },  # duplicate PK -> IntegrityError
        {
            "resource": {
                "id": uuid.uuid4(),
                "name": "ok",
                "domain": "widget",
                "type": "widget_thing",
                "zone": "corporate",
                "source": "test",
            }
        },
    ]

    def write_fn(item):
        with session.no_autoflush:
            pass
        r = Resource(**item["resource"])
        session.add(r)
        session.flush()

    written, skipped = connector._write_each(session, items, write_fn)
    assert (written, skipped) == (2, 1)
    session.commit()
    assert session.query(Resource).filter_by(name="dupe").count() == 1
    assert session.query(Resource).filter_by(name="ok").count() == 1
