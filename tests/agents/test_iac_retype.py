"""F-018 — iac (domain, name) canonicalization prevents same-name duplicates."""

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from infra_brain.agents.iac import _retype_existing_by_name
from infra_brain.db.models import Resource

from tests.support.pg import make_engine


def _make_session():
    # make_engine() already builds the full ORM schema (and on PG it is a
    # shared database), so the old single-table Resource.__table__.create()
    # would collide with the table that is already there.
    return Session(make_engine())


def test_retype_updates_existing_row_in_place():
    session = _make_session()
    session.add(
        Resource(
            id=uuid.uuid4(),
            domain="iac",
            type="ansible_inventory_file",
            name="proj/inventories/hosts.yml",
            source="IaCAgent",
            zone="corpor",
            last_seen=datetime.now(timezone.utc),
            metadata_={},
        )
    )
    session.flush()

    _retype_existing_by_name(
        session, "iac", {"name": "proj/inventories/hosts.yml", "type": "ansible_playbook"}
    )

    rows = session.query(Resource).filter_by(domain="iac").all()
    assert len(rows) == 1
    assert rows[0].type == "ansible_playbook"


def test_retype_noop_when_type_unchanged_or_missing():
    session = _make_session()
    _retype_existing_by_name(session, "iac", {"name": "absent", "type": "terraform_file"})
    assert session.query(Resource).count() == 0
