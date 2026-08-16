"""Tests for MR-J item 4 / INV-4: the WinRM-based collectors on WindowsAgent
(_collect_patch_state_winrm, _collect_certificates, _collect_security_posture,
_collect_shares, _collect_local_accounts) and the get_winrm_client wiring fix
in _write_windows_details.

These use a MagicMock WinRM client (``client.run_ps(...)`` returning a fake
Response with ``.std_out``) — no real network calls, matching the pattern
already used by test_windows.py's pre-existing WinRM tests for
_collect_services/_collect_software.
"""

import json
import uuid
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from infra_brain.agents.windows import WindowsAgent, _parse_iso_datetime
from infra_brain.db.models import (
    HostCertificate,
    HostSecurityPosture,
    HostShare,
    Resource,
    WindowsLocalGroupMember,
    WindowsLocalUser,
    WindowsPatchState,
)


def _make_agent():
    agent = WindowsAgent.__new__(WindowsAgent)
    agent.settings = MagicMock()
    agent.settings.ansible_inventory_path = "/etc/ansible/hosts"
    agent.settings.ansible_win_password = "testwinpass"
    agent.settings.ansible_win_user = "Administrator"
    agent.callbacks = []
    return agent


def _fake_client(std_out: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.std_out = std_out.encode()
    client.run_ps.return_value = response
    return client


# ---------------------------------------------------------------------------
# get_winrm_client wiring: _write_windows_details must actually call it
# ---------------------------------------------------------------------------


def test_write_windows_details_constructs_client_per_host(sqlite_engine, session_patcher):
    """MR-J item 4: _winrm_client used to be a phantom attribute (always None).
    _write_windows_details must now call get_winrm_client(...) with the host's
    ansible target name + configured credentials for every host."""
    agent = _make_agent()
    agent._last_raw = {
        "win01": {
            "ansible_facts": {
                "ansible_hostname": "win01",
                "ansible_pending_updates": [],
            }
        }
    }
    with Session(sqlite_engine) as s:
        s.add(Resource(domain="windows", type="windows_host", name="win01", source="WindowsAgent"))
        s.commit()

    with (
        session_patcher("infra_brain.agents.windows"),
        patch("infra_brain.agents.windows.get_winrm_client", return_value=None) as mock_get_client,
    ):
        agent._write_windows_details("all")

    mock_get_client.assert_called_once_with("win01", "Administrator", "testwinpass")


# ---------------------------------------------------------------------------
# Patch state via WinRM (preferred over Ansible facts when available)
# ---------------------------------------------------------------------------


def test_collect_patch_state_winrm_parses_pending_and_last_patched():
    agent = _make_agent()
    client = _fake_client(
        json.dumps(
            {
                "PendingCount": 2,
                "KbList": ["KB5034123", "KB5034441"],
                "LastPatched": "2026-06-15T00:00:00+00:00",
            }
        )
    )
    result = agent._collect_patch_state_winrm(client)
    assert result["pending_count"] == 2
    assert result["kb_list"] == ["KB5034123", "KB5034441"]
    assert result["last_patched"] == _parse_iso_datetime("2026-06-15T00:00:00+00:00")


def test_collect_patch_state_winrm_none_when_client_none():
    agent = _make_agent()
    assert agent._collect_patch_state_winrm(None) is None


def test_write_windows_details_prefers_winrm_patch_state_over_ansible_facts(
    sqlite_engine, session_patcher
):
    """When WinRM data is available it must win over the (weaker) Ansible-facts
    ansible_pending_updates/ansible_last_patched path."""
    agent = _make_agent()
    agent._last_raw = {
        "win01": {
            "ansible_facts": {
                "ansible_hostname": "win01",
                # Ansible-facts path would report ONLY this one pending update —
                # WinRM's richer result below must win.
                "ansible_pending_updates": ["KB0000001"],
            }
        }
    }
    with Session(sqlite_engine) as s:
        s.add(Resource(domain="windows", type="windows_host", name="win01", source="WindowsAgent"))
        s.commit()

    client = _fake_client(
        json.dumps(
            {
                "PendingCount": 3,
                "KbList": ["KB1111111", "KB2222222", "KB3333333"],
                "LastPatched": "2026-05-01T00:00:00+00:00",
            }
        )
    )

    with (
        session_patcher("infra_brain.agents.windows"),
        patch("infra_brain.agents.windows.get_winrm_client", return_value=client),
    ):
        agent._write_windows_details("all")

    with Session(sqlite_engine) as v:
        ps = v.query(WindowsPatchState).one()
        assert ps.pending_count == 3
        assert ps.kb_list == ["KB1111111", "KB2222222", "KB3333333"]
        assert ps.last_patched is not None


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------


def test_collect_certificates_writes_rows_and_computes_expiry(sqlite_engine):
    agent = _make_agent()
    resource_id = uuid.uuid4()
    client = _fake_client(
        json.dumps(
            [
                {
                    "Store": "LocalMachine\\My",
                    "Subject": "CN=win01.corp.example.com",
                    "Issuer": "CN=Internal CA",
                    "Thumbprint": "ABC123",
                    "NotBefore": "2020-01-01T00:00:00+00:00",
                    "NotAfter": "2020-06-01T00:00:00+00:00",  # long expired
                }
            ]
        )
    )
    with Session(sqlite_engine) as s:
        s.add(
            Resource(
                id=resource_id,
                domain="windows",
                type="windows_host",
                name="win01",
                source="WindowsAgent",
            )
        )
        s.commit()
        agent._collect_certificates(s, resource_id, client)
        s.commit()

    with Session(sqlite_engine) as v:
        cert = v.query(HostCertificate).filter_by(resource_id=resource_id).one()
        assert cert.store == "LocalMachine\\My"
        assert cert.thumbprint == "ABC123"
        assert cert.is_expired is True
        assert cert.days_until_expiry is not None and cert.days_until_expiry < 0


def test_collect_certificates_noop_when_client_none(sqlite_engine):
    agent = _make_agent()
    resource_id = uuid.uuid4()
    with Session(sqlite_engine) as s:
        agent._collect_certificates(s, resource_id, None)
        s.commit()
    with Session(sqlite_engine) as v:
        assert v.query(HostCertificate).filter_by(resource_id=resource_id).count() == 0


# ---------------------------------------------------------------------------
# Security posture
# ---------------------------------------------------------------------------


def test_collect_security_posture_upserts_row(sqlite_engine):
    agent = _make_agent()
    resource_id = uuid.uuid4()
    client = _fake_client(
        json.dumps(
            {
                "FirewallEnabled": True,
                "AvEnabled": True,
                "AvProduct": "Windows Defender",
                "AvSignatureDate": "2026-07-01T00:00:00+00:00",
                "RdpEnabled": False,
                "UacEnabled": True,
            }
        )
    )
    with Session(sqlite_engine) as s:
        s.add(
            Resource(
                id=resource_id,
                domain="windows",
                type="windows_host",
                name="win01",
                source="WindowsAgent",
            )
        )
        s.commit()
        agent._collect_security_posture(s, resource_id, client)
        s.commit()

    with Session(sqlite_engine) as v:
        posture = v.query(HostSecurityPosture).filter_by(resource_id=resource_id).one()
        assert posture.firewall_enabled is True
        assert posture.av_enabled is True
        assert posture.rdp_enabled is False
        assert posture.uac_enabled is True

    # Re-running must update in place, not duplicate (idempotent upsert).
    with Session(sqlite_engine) as s:
        agent._collect_security_posture(s, resource_id, client)
        s.commit()
    with Session(sqlite_engine) as v:
        assert v.query(HostSecurityPosture).filter_by(resource_id=resource_id).count() == 1


# ---------------------------------------------------------------------------
# Shares
# ---------------------------------------------------------------------------


def test_collect_shares_writes_rows_with_permissions(sqlite_engine):
    agent = _make_agent()
    resource_id = uuid.uuid4()
    client = _fake_client(
        json.dumps(
            [
                {
                    "Name": "Backups",
                    "Path": "D:\\Backups",
                    "Access": [{"AccountName": "Everyone", "AccessRight": "Read"}],
                }
            ]
        )
    )
    with Session(sqlite_engine) as s:
        s.add(
            Resource(
                id=resource_id,
                domain="windows",
                type="windows_host",
                name="win01",
                source="WindowsAgent",
            )
        )
        s.commit()
        agent._collect_shares(s, resource_id, client)
        s.commit()

    with Session(sqlite_engine) as v:
        share = v.query(HostShare).filter_by(resource_id=resource_id).one()
        assert share.name == "Backups"
        assert share.share_type == "smb"
        assert share.permissions[0]["AccountName"] == "Everyone"


# ---------------------------------------------------------------------------
# Local accounts
# ---------------------------------------------------------------------------


def test_collect_local_accounts_writes_users_and_admin_membership(sqlite_engine):
    agent = _make_agent()
    resource_id = uuid.uuid4()
    client = _fake_client(
        json.dumps(
            {
                "Users": [
                    {
                        "Name": "Administrator",
                        "Enabled": True,
                        "PasswordRequired": True,
                        "PasswordNeverExpires": True,
                        "LastLogon": "2026-07-01T00:00:00+00:00",
                    },
                    {
                        "Name": "guest",
                        "Enabled": False,
                        "PasswordRequired": False,
                        "PasswordNeverExpires": False,
                        "LastLogon": None,
                    },
                ],
                "Admins": ["WIN01\\Administrator"],
            }
        )
    )
    with Session(sqlite_engine) as s:
        s.add(
            Resource(
                id=resource_id,
                domain="windows",
                type="windows_host",
                name="win01",
                source="WindowsAgent",
            )
        )
        s.commit()
        agent._collect_local_accounts(s, resource_id, client)
        s.commit()

    with Session(sqlite_engine) as v:
        users = {
            u.username: u
            for u in v.query(WindowsLocalUser).filter_by(resource_id=resource_id).all()
        }
        assert users["Administrator"].is_admin is True
        assert users["guest"].is_admin is False
        assert users["guest"].enabled is False

        members = v.query(WindowsLocalGroupMember).filter_by(resource_id=resource_id).all()
        assert {m.member_name for m in members} == {"Administrator"}
        assert members[0].group_name == "Administrators"


# ---------------------------------------------------------------------------
# _parse_iso_datetime
# ---------------------------------------------------------------------------


def test_parse_iso_datetime_handles_iso_and_legacy_wcf_and_garbage():
    assert _parse_iso_datetime("2026-07-01T00:00:00+00:00") is not None
    assert _parse_iso_datetime("/Date(1735689600000)/") is not None
    assert _parse_iso_datetime("not a date") is None
    assert _parse_iso_datetime(None) is None
    assert _parse_iso_datetime("") is None
