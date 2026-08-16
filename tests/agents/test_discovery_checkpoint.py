"""F-005 regression: discovery per-iteration checkpoint rows must actually persist.

Before Wave 4 item 4.3, Snapshot.resource_id was NOT NULL, so every
_checkpoint_findings() commit raised IntegrityError, was swallowed at debug
level, and zero checkpoint rows were ever written.
"""

import uuid
from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy.orm import Session

from infra_brain.db.models import CollectionRun, Snapshot

from tests.support.pg import make_engine


def test_checkpoint_findings_writes_snapshot_row():
    from infra_brain.agents.discovery import DiscoveryAgent, DiscoveryFinding

    eng = make_engine()

    @contextmanager
    def fake_get_session():
        with Session(eng) as s:
            yield s

    run_id = uuid.uuid4()
    with Session(eng) as s:
        s.add(
            CollectionRun(id=run_id, domain="discovery", trigger_type="test", status="in_progress")
        )
        s.commit()

    agent = DiscoveryAgent.__new__(DiscoveryAgent)  # skip __init__: no LLM/callbacks needed
    agent._current_run_id = run_id
    finding = DiscoveryFinding(hostname="new-host-01", confidence=0.9)

    with patch("infra_brain.agents.discovery.get_session", fake_get_session):
        agent._checkpoint_findings([finding])

    with Session(eng) as s:
        rows = s.query(Snapshot).filter(Snapshot.run_id == run_id).all()
    assert len(rows) == 1, "checkpoint row was not persisted (F-005 regression)"
    assert rows[0].resource_id is None
    assert rows[0].snapshot["_checkpoint"] is True
    assert rows[0].snapshot["partial_findings"][0]["hostname"] == "new-host-01"
