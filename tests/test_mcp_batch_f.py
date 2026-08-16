"""MCP Batch F — OS inventory tools."""

from contextlib import contextmanager

import pytest
from sqlalchemy.orm import Session

import infra_brain.mcp_server as mcp_server

from tests.support.pg import make_engine


@pytest.fixture
def db(monkeypatch):
    eng = make_engine()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    monkeypatch.setattr(mcp_server, "get_session", _get_session)
    with Session(eng) as s:
        yield s


def _linux_host(db, name):
    """Create a Resource + its LinuxHost; return (resource_id, linux_host_id)."""
    from infra_brain.db.models import LinuxHost, Resource

    r = Resource(domain="linux", type="host", name=name, source="ansible")
    db.add(r)
    db.flush()
    lh = LinuxHost(resource_id=r.id, distro="ubuntu", kernel="6.1", arch="x86_64")
    db.add(lh)
    db.flush()
    return r.id, lh.id


def test_get_linux_packages_join_and_filter(db):
    from infra_brain.db.models import LinuxPackage

    _, hid = _linux_host(db, "web01")
    db.add(LinuxPackage(host_id=hid, name="openssl", version="1.1", manager="apt"))
    db.add(LinuxPackage(host_id=hid, name="nginx", version="1.25", manager="apt"))
    db.commit()
    out = mcp_server.get_linux_packages()
    assert len(out) == 2
    assert all(p["host"] == "web01" for p in out)
    ssl = mcp_server.get_linux_packages(name="openssl")
    assert [p["name"] for p in ssl] == ["openssl"]
    by_host = mcp_server.get_linux_packages(hostname="web01")
    assert len(by_host) == 2


def test_get_linux_packages_empty(db):
    assert mcp_server.get_linux_packages() == []


def test_get_linux_pending_updates_security_filter(db):
    from infra_brain.db.models import LinuxPendingUpdate

    _, hid = _linux_host(db, "web01")
    db.add(LinuxPendingUpdate(host_id=hid, package="openssl", security=True))
    db.add(LinuxPendingUpdate(host_id=hid, package="vim", security=False))
    db.commit()
    assert len(mcp_server.get_linux_pending_updates()) == 2
    sec = mcp_server.get_linux_pending_updates(security_only=True)
    assert [u["package"] for u in sec] == ["openssl"]
    assert sec[0]["host"] == "web01"


def test_get_linux_pending_updates_empty(db):
    assert mcp_server.get_linux_pending_updates() == []


def test_get_linux_ports_join(db):
    from infra_brain.db.models import LinuxPort

    _, hid = _linux_host(db, "web01")
    db.add(LinuxPort(host_id=hid, port=443, proto="tcp", process="nginx", state="LISTEN"))
    db.commit()
    out = mcp_server.get_linux_ports()
    assert len(out) == 1
    assert out[0]["host"] == "web01" and out[0]["port"] == 443


def test_get_linux_ports_empty(db):
    assert mcp_server.get_linux_ports() == []


def test_get_linux_mounts_and_nics_returns_two_lists(db):
    from infra_brain.db.models import LinuxMount, LinuxNic

    _, hid = _linux_host(db, "web01")
    db.add(LinuxMount(host_id=hid, mount="/", device="/dev/sda1", fstype="ext4"))
    db.add(LinuxNic(host_id=hid, name="eth0", ipv4="10.1.2.3"))
    db.commit()
    out = mcp_server.get_linux_mounts_and_nics(hostname="web01")
    assert [m["mount"] for m in out["mounts"]] == ["/"]
    assert out["mounts"][0]["host"] == "web01"
    assert [n["name"] for n in out["nics"]] == ["eth0"]
    assert out["nics"][0]["host"] == "web01"


def test_get_linux_mounts_and_nics_empty(db):
    out = mcp_server.get_linux_mounts_and_nics()
    assert out == {"mounts": [], "nics": []}


def test_get_linux_users_and_crons_returns_two_lists(db):
    from infra_brain.db.models import LinuxCron, LinuxUser

    _, hid = _linux_host(db, "web01")
    db.add(LinuxUser(host_id=hid, username="root", shell="/bin/bash", sudo=True))
    db.add(LinuxCron(host_id=hid, owner="root", schedule="0 3 * * *", command="/usr/bin/backup"))
    db.commit()
    out = mcp_server.get_linux_users_and_crons(hostname="web01")
    assert [u["username"] for u in out["users"]] == ["root"]
    assert out["users"][0]["host"] == "web01"
    assert [c["command"] for c in out["crons"]] == ["/usr/bin/backup"]
    assert out["crons"][0]["host"] == "web01"


def test_get_linux_users_and_crons_empty(db):
    assert mcp_server.get_linux_users_and_crons() == {"users": [], "crons": []}


def test_get_windows_services_join_and_filter(db):
    from infra_brain.db.models import Resource, WindowsService

    r = Resource(domain="windows", type="host", name="win01", source="winrm")
    db.add(r)
    db.flush()
    db.add(WindowsService(resource_id=r.id, name="W3SVC", state="Running", start_type="Automatic"))
    db.add(WindowsService(resource_id=r.id, name="Spooler", state="Stopped", start_type="Manual"))
    db.commit()
    out = mcp_server.get_windows_services()
    assert len(out) == 2 and all(s["host"] == "win01" for s in out)
    running = mcp_server.get_windows_services(state="Running")
    assert [s["name"] for s in running] == ["W3SVC"]


def test_get_windows_services_empty(db):
    assert mcp_server.get_windows_services() == []


def test_get_windows_software_join_and_filter(db):
    from infra_brain.db.models import Resource, WindowsSoftware

    r = Resource(domain="windows", type="host", name="win01", source="winrm")
    db.add(r)
    db.flush()
    db.add(
        WindowsSoftware(resource_id=r.id, name="7-Zip", version="23.01", publisher="Igor Pavlov")
    )
    db.commit()
    out = mcp_server.get_windows_software()
    assert len(out) == 1 and out[0]["host"] == "win01"
    named = mcp_server.get_windows_software(name="7-Zip")
    assert [s["name"] for s in named] == ["7-Zip"]


def test_get_windows_software_empty(db):
    assert mcp_server.get_windows_software() == []
