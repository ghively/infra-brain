"""The Hosts page must not present a machine nothing can see as a current host.

TRK-348. On 2026-08-11 two hosts (media_host, storage_node) went dark. The
drift pass retired their linux_host Resources; KG-8 cleared their identity
legs — and the Hosts page kept listing them as ordinary hosts, with
``last_reconciled`` refreshing every 30 minutes, indistinguishable from live
machines. The one page an operator would check to ask "is this host okay?"
was the one place the disappearance could not be seen.

``observed`` is computed at read time from the identity legs (a HostIdentity
with every source leg NULL is, by definition, seen by no collector), so it can
never go stale; ``retired_at`` resolves SINCE WHEN from the name-matched
retired host Resource.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from infra_brain.db.models import HostIdentity, Resource

from tests.support.pg import make_engine


@pytest.fixture
def client(monkeypatch):
    from infra_brain.config import get_settings

    monkeypatch.setenv("INFRA_BRAIN_DEV", "1")
    get_settings.cache_clear()

    eng = make_engine()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    import infra_brain.api.routers.hosts as hosts_mod

    monkeypatch.setattr(hosts_mod, "get_session", _get_session)

    app = FastAPI()
    app.include_router(hosts_mod.hosts_router)

    def _seed(short: str, *, linux_leg: bool, retired: bool):
        with Session(eng) as s:
            leg_id = None
            if linux_leg or retired:
                r = Resource(
                    id=uuid.uuid4(),
                    domain="linux",
                    type="linux_host",
                    name=short,
                    source="LinuxAgent",
                    retired_at=datetime(2026, 8, 11, 18, 3, tzinfo=UTC) if retired else None,
                )
                s.add(r)
                s.flush()
                leg_id = r.id if linux_leg else None
            s.add(
                HostIdentity(
                    id=uuid.uuid4(),
                    short_hostname=short,
                    linux_resource_id=leg_id,
                )
            )
            s.commit()

    c = TestClient(app)
    c.seed = _seed  # type: ignore[attr-defined]
    yield c
    get_settings.cache_clear()


def _rows(client):
    resp = client.get("/api/dashboard/hosts")
    assert resp.status_code == 200
    return {i["short_hostname"]: i for i in resp.json()["items"]}


def test_a_host_with_a_live_leg_is_observed(client):
    client.seed("node_a", linux_leg=True, retired=False)
    row = _rows(client)["node_a"]
    assert row["observed"] is True
    assert row["retired_at"] is None


def test_a_legless_identity_is_unobserved_with_since_when(client):
    """The media_host case: legs cleared, resource retired — the page must say so."""
    client.seed("media_host", linux_leg=False, retired=True)
    row = _rows(client)["media_host"]
    assert row["observed"] is False, (
        "an identity every one of whose source legs is NULL is seen by NO "
        "collector — presenting it as an ordinary current host is the dashboard "
        "lying about the fleet"
    )
    assert row["retired_at"] is not None and row["retired_at"].startswith("2026-08-11"), (
        "when the name-matched host Resource is retired, the operator gets "
        "SINCE WHEN, not just a flag"
    )


def test_a_legless_identity_without_a_retired_resource_still_flags(client):
    """Even with no resolvable since-when, unobserved must still be visible."""
    client.seed("ghost", linux_leg=False, retired=False)
    row = _rows(client)["ghost"]
    assert row["observed"] is False
    assert row["retired_at"] is None
