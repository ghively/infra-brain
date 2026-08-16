"""MR5 relational-model tests for CloudAgent.

Exercise the detail-write phase: cloud_resources is UPSERTed keyed on
(provider, cloud_id) with resolved/typed fields, re-running produces no
duplicates, unmapped data lands in ``details``, resource_id links the canonical
Resource, idle (aws disabled) writes nothing, and a detail-write failure is
surfaced on the CollectionRun.

All tests run against in-memory SQLite via the shared session_patcher /
sqlite_engine fixtures. AWS is fully mocked — no boto3 / live AWS required.
"""

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.cloud import CloudAgent
from infra_brain.db.models import CloudResource, CollectionRun, Resource

MODULE = "infra_brain.agents.cloud"


def _settings(aws_enabled=True):
    return SimpleNamespace(aws_enabled=aws_enabled)


def _agent(settings=None):
    agent = CloudAgent.__new__(CloudAgent)
    agent.callbacks = []
    agent.settings = settings or _settings()
    return agent


def _items():
    return [
        {
            "name": "web-01",
            "type": "ec2_instance",
            "data": {
                "instance_id": "i-0abc",
                "instance_type": "t3.micro",
                "state": "running",
                "region": "us-east-1",
                "private_ip": "10.0.0.5",
                "platform": "linux",
            },
        },
        {
            "name": "prod-vpc",
            "type": "vpc",
            "data": {
                "vpc_id": "vpc-123",
                "cidr_block": "10.0.0.0/16",
                "is_default": False,
                "region": "us-east-1",
            },
        },
        {
            "name": "web-sg",
            "type": "security_group",
            "data": {
                "group_id": "sg-999",
                "description": "web tier",
                "vpc_id": "vpc-123",
                "region": "us-east-1",
            },
        },
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
                    domain="cloud",
                    type=it["type"],
                    name=it["name"],
                    source="CloudAgent",
                    zone="corpor",
                )
            )
        s.commit()


def test_upserts_each_cloud_type(patched):
    engine = patched
    items = _items()
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    agent._write_cloud_details()

    with Session(engine) as s:
        assert s.query(CloudResource).count() == 3
        ec2 = s.query(CloudResource).filter_by(cloud_id="i-0abc").one()
        assert ec2.cloud_type == "ec2_instance"
        assert ec2.provider == "aws"
        assert ec2.name == "web-01"
        assert ec2.region == "us-east-1"
        assert ec2.state == "running"
        assert ec2.resource_id is not None
        # unmapped fields land in details (instance_id excluded — it's the key)
        assert ec2.details and ec2.details.get("instance_type") == "t3.micro"
        assert ec2.details.get("private_ip") == "10.0.0.5"
        assert "instance_id" not in ec2.details

        sg = s.query(CloudResource).filter_by(cloud_id="sg-999").one()
        assert sg.cloud_type == "security_group"
        assert sg.details.get("description") == "web tier"


def test_idempotent(patched):
    engine = patched
    items = _items()
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    agent._write_cloud_details()
    agent._write_cloud_details()
    with Session(engine) as s:
        assert s.query(CloudResource).count() == 3


def test_updates_in_place_on_state_change(patched):
    engine = patched
    items = _items()[:1]
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    agent._write_cloud_details()

    changed = _items()[:1]
    changed[0]["data"]["state"] = "stopped"
    agent._last_items = changed
    agent._write_cloud_details()

    with Session(engine) as s:
        rows = s.query(CloudResource).all()
        assert len(rows) == 1
        assert rows[0].state == "stopped"


def test_idle_no_writes_when_aws_disabled(patched):
    engine = patched
    agent = _agent(settings=_settings(aws_enabled=False))
    agent._last_items = _items()  # even if items lingered, idle guard short-circuits
    agent._write_cloud_details()
    with Session(engine) as s:
        assert s.query(CloudResource).count() == 0


def test_bad_item_skips_without_aborting_rest(patched):
    engine = patched
    good = _items()[0]
    bad = {"name": "no-id", "type": "ec2_instance", "data": {}}  # missing instance_id
    items = [bad, good]
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    agent._write_cloud_details()
    with Session(engine) as s:
        rows = s.query(CloudResource).all()
        assert len(rows) == 1
        assert rows[0].cloud_id == "i-0abc"
    # Task 4: _write_cloud_details migrated to the shared _write_each helper,
    # which tracks (written, skipped) counts on the agent.
    assert agent._cloud_details_written == 1
    assert agent._cloud_details_skipped == 1


# ---------------------------------------------------------------------------
# GitLab #148: _write_cloud_details must RETURN the row count so
# ETLConnector._write_details can persist CollectionRun.detail_rows_written.
# It used to fall off the end (implicit ``return None``), leaving the
# counter at 0 on every run even though cloud_resources was being populated
# — the exact bug vsphere.py's _write_vsphere_details documents fixing.
# ---------------------------------------------------------------------------


def test_write_cloud_details_returns_written_count(patched):
    engine = patched
    items = _items()
    _seed_resources(engine, items)
    agent = _agent()
    agent._last_items = items
    count = agent._write_cloud_details()

    assert count == 3
    assert count == agent._cloud_details_written


def test_write_cloud_details_returns_zero_when_idle(patched):
    agent = _agent(settings=_settings(aws_enabled=False))
    agent._last_items = _items()
    assert agent._write_cloud_details() == 0


def test_write_cloud_details_returns_zero_when_no_items(patched):
    agent = _agent()
    agent._last_items = []
    assert agent._write_cloud_details() == 0


def test_detail_count_persisted_on_collection_run(sqlite_engine):
    """End-to-end through _write_details: the returned count must land on
    CollectionRun.detail_rows_written (GitLab #148)."""
    engine = sqlite_engine
    run_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id,
                domain="cloud",
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
        run_id=run_id, domain="cloud", resources_found=3, drift_count=0, status="completed"
    )

    with patch("infra_brain.etl.base.get_session", _get_session):
        agent._write_details(result, lambda: 3)

    assert result.detail_rows_written == 3
    with Session(engine) as s:
        run = s.get(CollectionRun, run_id)
        assert run.detail_rows_written == 3


def test_detail_write_failure_marks_run_failed(sqlite_engine):
    engine = sqlite_engine
    run_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id,
                domain="cloud",
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
        run_id=run_id, domain="cloud", resources_found=1, drift_count=0, status="completed"
    )

    def _boom():
        raise RuntimeError("simulated cloud relational write failure")

    with patch("infra_brain.etl.base.get_session", _get_session):
        agent._write_details(result, _boom)

    assert result.status == "failed"
    assert any("simulated cloud relational write failure" in e for e in result.errors)
    with Session(engine) as s:
        run = s.get(CollectionRun, run_id)
        assert run.status == "failed"
        assert "simulated cloud relational write failure" in (run.error_message or "")
