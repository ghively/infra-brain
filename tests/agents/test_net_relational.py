"""MR5 relational-model tests for NetAgent.

Exercise the detail-write phase: net_devices is UPSERTed keyed on ``ip`` (the
SNMP target) with resolved/typed fields, idempotently; unmapped data lands in
``details``; resource_id links the Resource; idle (no targets/community) writes
nothing; and a detail-write failure is surfaced on the CollectionRun.

All tests run against in-memory SQLite via the shared fixtures; SNMP is fully
mocked — no pysnmp / live device required.
"""

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.net import NetAgent
from infra_brain.db.models import CollectionRun, NetDevice, Resource

MODULE = "infra_brain.agents.net"


def _settings(targets="10.0.0.1", community="public"):
    return SimpleNamespace(
        snmp_targets=targets,
        snmp_community=community,
        snmp_port=161,
        snmp_timeout_seconds=2,
        snmp_retries=1,
    )


def _agent(settings=None):
    agent = NetAgent.__new__(NetAgent)
    agent.callbacks = []
    agent.settings = settings or _settings()
    return agent


def _items():
    return [
        {
            "name": "core-sw-01",
            "type": "net_device",
            "data": {
                "host": "10.0.0.1",
                "sys_name": "core-sw-01",
                "sys_descr": "Cisco IOS Software, C3560",
                "sys_object_id": "1.3.6.1.4.1.9.1.516",
                "sys_uptime": "12 days",
                "sys_contact": "netops@example.com",
                "sys_location": "DC1 Rack 4",
            },
        }
    ]


@pytest.fixture
def patched(session_patcher):
    with session_patcher(MODULE) as engine:
        yield engine


def _seed_resources(engine, items):
    with Session(engine) as s:
        for it in items:
            s.add(
                Resource(
                    id=uuid.uuid4(),
                    domain="net",
                    type=it["type"],
                    name=it["name"],
                    source="NetAgent",
                    zone="corpor",
                )
            )
        s.commit()


def test_upserts_net_device(patched):
    engine = patched
    items = _items()
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    agent._write_net_details()

    with Session(engine) as s:
        dev = s.query(NetDevice).one()
        assert dev.ip == "10.0.0.1"
        assert dev.name == "core-sw-01"
        assert dev.sysname == "core-sw-01"
        assert dev.descr.startswith("Cisco IOS")
        assert dev.object_id == "1.3.6.1.4.1.9.1.516"
        assert dev.uptime == "12 days"
        assert dev.contact == "netops@example.com"
        assert dev.location == "DC1 Rack 4"
        assert dev.resource_id is not None
        # all emitted fields are mapped to typed columns → no details overflow
        assert dev.details is None


def test_details_overflow_for_extra_fields(patched):
    engine = patched
    items = _items()
    items[0]["data"]["vendor_guess"] = "cisco"  # unmapped extra
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    agent._write_net_details()
    with Session(engine) as s:
        dev = s.query(NetDevice).one()
        assert dev.details and dev.details.get("vendor_guess") == "cisco"


def test_idempotent(patched):
    engine = patched
    items = _items()
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    agent._write_net_details()
    agent._write_net_details()
    with Session(engine) as s:
        assert s.query(NetDevice).count() == 1


def test_updates_in_place_on_change(patched):
    engine = patched
    items = _items()
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    agent._write_net_details()

    changed = _items()
    changed[0]["data"]["sys_uptime"] = "99 days"
    agent._last_items = changed
    agent._write_net_details()

    with Session(engine) as s:
        rows = s.query(NetDevice).all()
        assert len(rows) == 1
        assert rows[0].uptime == "99 days"


def test_idle_no_writes_when_unconfigured(patched):
    engine = patched
    agent = _agent(settings=_settings(targets="", community=""))
    agent._last_items = _items()  # idle guard must short-circuit
    agent._write_net_details()
    with Session(engine) as s:
        assert s.query(NetDevice).count() == 0


def test_bad_item_skips_without_aborting_rest(patched):
    engine = patched
    good = _items()[0]
    bad = {"name": "no-host", "type": "net_device", "data": {"sys_name": "x"}}  # no host/ip
    items = [bad, good]
    _seed_resources(engine, [good])
    agent = _agent()
    agent._last_items = items
    agent._write_net_details()
    with Session(engine) as s:
        rows = s.query(NetDevice).all()
        assert len(rows) == 1
        assert rows[0].ip == "10.0.0.1"


def test_detail_write_failure_marks_run_failed(sqlite_engine):
    engine = sqlite_engine
    run_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id,
                domain="net",
                trigger_type="scheduled",
                trigger_source="all",
                status="completed",
            )
        )
        s.commit()

    from infra_brain.agents.base import CollectionResult

    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    agent = _agent()
    result = CollectionResult(
        run_id=run_id, domain="net", resources_found=1, drift_count=0, status="completed"
    )

    def _boom():
        raise RuntimeError("simulated net relational write failure")

    with patch("infra_brain.etl.base.get_session", _get_session):
        agent._write_details(result, _boom)

    assert result.status == "failed"
    assert any("simulated net relational write failure" in e for e in result.errors)
    with Session(engine) as s:
        run = s.get(CollectionRun, run_id)
        assert run.status == "failed"
