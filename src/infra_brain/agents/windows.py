import json
import logging
import re
from datetime import datetime, timezone, timedelta

from infra_brain.etl.base import CollectorSkipped, ETLConnector
from infra_brain.db.models import (
    DriftEvent,
    HostCertificate,
    HostSecurityPosture,
    HostShare,
    Resource,
    WindowsLocalGroupMember,
    WindowsLocalUser,
    WindowsPatchState,
    WindowsService,
    WindowsSoftware,
)
from infra_brain.db.session import get_session
from infra_brain.tools.ansible import run_windows_ansible_facts
from infra_brain.tools.winrm_client import get_winrm_client
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)


class WindowsAgent(ETLConnector):
    spec = AgentSpec(
        domain="windows",
        tier=Tier.COLLECTOR,
        schedule="5 */6 * * *",
        max_staleness=timedelta(hours=8),
        # TRK-278 / GitLab #140 (2026-07-29): Ansible Windows collection retired
        # by standing decision. 2026-08-12: migrated from dispatchable=False +
        # a commented-out supervisor._AGENT_SPECS entry onto the first-class
        # switch, which is what that decision always meant — the domain stays
        # visible in the roster as deliberately off instead of disappearing,
        # and re-enabling is one setting instead of two source edits:
        # COLLECTION_REVIVED_DOMAINS=windows (collect() still self-skips
        # without WinRM credentials, per F-021).
        #
        # P5 REVIVAL OBLIGATION: windows is a HOST-BEARING leg
        # (host_reconcile._SOURCE_KEYS "windows"). Reviving it means its hosts
        # become cross-source identity candidates, and the identity resolver
        # sees only what is declared: reviving WITHOUT adding a
        # NodeSpec(..., is_host_identity=True) here AND the matching entry in
        # graph_phase3.HOST_NODE_TYPES leaves every windows↔anything pair
        # invisible — not queued for a human, not scored, silently absent. No
        # speculative NodeSpec is declared now (zero rows, ever), which is why
        # this obligation is written down instead.
        retired=True,
    )

    def collect(self, scope: str = "all") -> list[dict]:
        # F-021: WinRM credentials are required for Windows fact gathering.
        # Empty/whitespace ansible_win_password → CollectorSkipped (clean
        # no-op, not a failure); BaseAgent.run() records status="skipped".
        win_password = (self.settings.ansible_win_password or "").strip()
        if not win_password:
            raise CollectorSkipped(
                "ansible_win_password not configured; set ANSIBLE_WIN_PASSWORD to enable "
                "Windows fact collection"
            )
        win_user = (self.settings.ansible_win_user or "Administrator").strip()
        inventory = self.settings.ansible_inventory_path
        # Password injected into subprocess env only — never in argv (F-021).
        extra_env = {"ANSIBLE_WIN_PASSWORD": win_password}  # win_password is already stripped
        raw = run_windows_ansible_facts(
            target=scope,
            inventory=inventory,
            win_user=win_user,
            extra_env=extra_env,
        )
        # If the function raised (non-zero exit, timeout, unreachability) the
        # exception propagates to BaseAgent.run() which records status="failed"
        # with error_message.  A successful call that returns {} (legitimate
        # empty inventory) reaches here and produces an empty list — that is NOT
        # an error and stays status="completed"/resources_found=0.
        self._last_raw = raw  # cache for _write_windows_details

        items = []
        for hostname, host_data in raw.items():
            facts = host_data.get("ansible_facts", {})
            items.append(
                {
                    "name": hostname,
                    "type": "windows_host",
                    "data": {
                        "os_name": facts.get("ansible_os_name", ""),
                        "os_version": facts.get("ansible_os_version", ""),
                        "architecture": facts.get("ansible_architecture", ""),
                        "hostname": facts.get("ansible_hostname", hostname),
                        "domain": facts.get("ansible_domain", ""),
                        "winrm_status": "reachable",
                        "services": [
                            {
                                "name": svc_name,
                                "state": svc_data.get("state", ""),
                                "start_mode": svc_data.get("start_mode", ""),
                            }
                            for svc_name, svc_data in facts.get("ansible_services", {}).items()
                        ],
                        "pending_updates": facts.get("ansible_pending_updates", []),
                    },
                }
            )
        return items

    _recompute_drift_after_details = True

    def _detail_writers(self, scope, result):
        # Write the windows_patch_state detail rows after the base run upserts
        # the generic Resource rows, via ETLConnector.run()'s _write_details so a
        # failure is SURFACED on the CollectionRun/result (not silently swallowed).
        # AA-C-2: this phase writes service-drift DriftEvent rows AFTER the base
        # run computed drift_count, so _recompute_drift_after_details=True makes
        # run() recompute it afterwards (result not left stale).
        return [lambda: self._write_windows_details(scope, result.run_id)]

    def _write_windows_details(self, scope: str, run_id=None) -> int:
        """Populate windows_patch_state, windows_services, windows_software, and
        (MR-J / INV-4) host_certificates, host_security_posture, host_shares,
        windows_local_users, windows_local_group_members from the collected
        facts.

        Mirrors LinuxAgent._write_linux_details: link to the windows Resource by
        hostname, then upsert one WindowsPatchState row per host keyed on the
        unique resource_id. Re-running updates in place (idempotent — no dupes).

        MR-J item 4: ``_winrm_client`` was a phantom attribute nothing ever set
        (getattr(..., None) always returned None) — every WinRM collector below
        was permanently a no-op regardless of credentials. Fixed by actually
        constructing a per-host client via tools/winrm_client.get_winrm_client.
        Every collector remains a documented no-op when the client is None
        (pywinrm not installed, or credentials unset) — this is unchanged
        behavior for the credential-not-configured case (F-021/CollectorSkipped
        already gates collect() itself), it just now ACTUALLY RUNS once
        credentials + pywinrm are both available.

        Returns the number of host detail rows written so _write_details can
        accumulate a nonzero persisted count on CollectionRun.detail_rows_written.
        """
        raw = getattr(self, "_last_raw", None)
        if not raw:
            return 0

        win_user = (self.settings.ansible_win_user or "Administrator").strip()
        win_password = (self.settings.ansible_win_password or "").strip()

        rows_written = 0
        with get_session() as session:
            for hostname, host_data in raw.items():
                facts = host_data.get("ansible_facts", {})
                resource = (
                    session.query(Resource).filter_by(domain="windows", name=hostname).first()
                )
                if not resource:
                    continue
                rows_written += 1

                winrm_client = get_winrm_client(hostname, win_user, win_password)

                # --- Patch state: prefer real WinRM data, fall back to Ansible facts ---
                winrm_patch = self._collect_patch_state_winrm(winrm_client)
                if winrm_patch is not None:
                    kb_list = winrm_patch["kb_list"]
                    pending_count = winrm_patch["pending_count"]
                    last_patched = winrm_patch["last_patched"]
                else:
                    pending = facts.get("ansible_pending_updates", []) or []
                    # KB list: pending_updates entries may be plain KB strings or dicts
                    # carrying a kb/KB/id field — normalize to a list of identifiers.
                    kb_list = [kb for kb in (_pending_kb(u) for u in pending) if kb]
                    pending_count = len(pending)
                    # last_patched is not in `ansible -m setup` facts; left null
                    # unless an enriched gather supplies ansible_last_patched.
                    last_patched = facts.get("ansible_last_patched")

                self._upsert_detail(
                    session,
                    WindowsPatchState,
                    {
                        "resource_id": resource.id,
                        "hostname": facts.get("ansible_hostname", hostname),
                        "kb_list": kb_list,
                        "pending_count": pending_count,
                        "last_patched": last_patched,
                        "winrm_status": "reachable",
                    },
                    key_fields=["resource_id"],
                )

                # --- Services: populate from Ansible facts when available ---
                # ansible_services is present in enriched Ansible Windows facts as
                # {service_name: {state: ..., start_mode: ...}}. WinRM collection
                # (Get-Service via _collect_services) is preferred when a client
                # is wired in; this path handles the Ansible-only case.
                ansible_services = facts.get("ansible_services", {}) or {}
                if ansible_services:
                    self._write_services_from_facts(session, resource.id, ansible_services, run_id)

                # WinRM-based collection (no-op when client=None).
                self._collect_services(session, resource.id, winrm_client, run_id)
                self._collect_software(session, resource.id, winrm_client)
                # MR-J / INV-4: PCI-relevant posture data, also WinRM-based.
                self._collect_certificates(session, resource.id, winrm_client)
                self._collect_security_posture(session, resource.id, winrm_client)
                self._collect_shares(session, resource.id, winrm_client)
                self._collect_local_accounts(session, resource.id, winrm_client)

            session.commit()
        return rows_written

    def _write_services_from_facts(
        self, session, resource_id, ansible_services: dict, run_id=None
    ) -> None:
        """Write WindowsService rows from Ansible ``ansible_services`` facts.

        Snapshots the previous service states for drift comparison before
        delete-reinsert.
        """
        prev_services: dict[str, str] = {
            svc.name: (svc.state or "")
            for svc in session.query(WindowsService).filter_by(resource_id=resource_id).all()
        }
        session.query(WindowsService).filter_by(resource_id=resource_id).delete()
        current_services: dict[str, str] = {}
        for svc_name, svc_data in ansible_services.items():
            state = str(svc_data.get("state", "") or "")
            start_type = str(svc_data.get("start_mode", "") or "")
            session.add(
                WindowsService(
                    resource_id=resource_id,
                    name=svc_name,
                    state=state,
                    start_type=start_type,
                )
            )
            current_services[svc_name] = state
        session.flush()
        if prev_services:
            self._emit_service_drift(session, resource_id, current_services, prev_services, run_id)

    def _collect_services(self, session, resource_id, client, run_id=None) -> None:
        """Collect Windows services via WinRM (Get-Service).

        No-op when ``client`` is None (WinRM not configured for this host).
        Snapshots the previous service states for drift comparison before
        delete-reinsert so drift events fire on each scan.
        """
        if client is None:
            return
        try:
            result = client.run_ps(
                "Get-Service | Select-Object Name,Status,StartType | ConvertTo-Json -Compress"
            )
            services = json.loads(result.std_out.strip() or "[]")
            if isinstance(services, dict):
                services = [services]

            # Snapshot BEFORE delete for drift comparison.
            prev_services: dict[str, str] = {
                svc.name: (svc.state or "")
                for svc in session.query(WindowsService).filter_by(resource_id=resource_id).all()
            }
            session.query(WindowsService).filter_by(resource_id=resource_id).delete()

            current_services: dict[str, str] = {}
            for svc in services:
                name = svc.get("Name") or svc.get("name", "")
                if not name:
                    continue
                state = str(svc.get("Status", "") or "")
                start_type = str(svc.get("StartType", "") or "")
                session.add(
                    WindowsService(
                        resource_id=resource_id,
                        name=name,
                        state=state,
                        start_type=start_type,
                    )
                )
                current_services[name] = state
            session.flush()
            if prev_services:
                self._emit_service_drift(
                    session, resource_id, current_services, prev_services, run_id
                )
        except Exception as e:
            logger.warning("Service collection failed for %s: %s", resource_id, e)

    def _emit_service_drift(
        self,
        session,
        resource_id,
        current_services: dict[str, str],
        prev_services: dict[str, str],
        run_id=None,
    ) -> None:
        """Emit DriftEvent rows for new services and for Running→stopped transitions."""
        for name, new_state in current_services.items():
            old_state = prev_services.get(name)
            if old_state is None:
                session.add(
                    DriftEvent(
                        resource_id=resource_id,
                        collection_run_id=run_id,
                        drift_type="new_windows_service",
                        field="service_name",
                        old_value=None,
                        new_value={"service_name": name},
                        detected_at=datetime.now(timezone.utc),
                        status="open",
                    )
                )
            elif old_state == "Running" and new_state != "Running":
                session.add(
                    DriftEvent(
                        resource_id=resource_id,
                        collection_run_id=run_id,
                        drift_type="service_stopped",
                        field=f"service:{name}",
                        old_value={"state": old_state},
                        new_value={"state": new_state},
                        detected_at=datetime.now(timezone.utc),
                        status="open",
                    )
                )

    def _collect_software(self, session, resource_id, client) -> None:
        """Collect installed software via WMI (Win32_Product).

        No-op when ``client`` is None (WinRM not configured for this host).
        Uses delete-reinsert for idempotency; WMI may be slow on large estates.
        """
        if client is None:
            return
        try:
            ps_cmd = (
                "Get-WmiObject Win32_Product "
                "| Select-Object Name,Version,Vendor,InstallDate "
                "| ConvertTo-Json -Compress"
            )
            result = client.run_ps(ps_cmd)
            items = json.loads(result.std_out.strip() or "[]")
            if isinstance(items, dict):
                items = [items]

            session.query(WindowsSoftware).filter_by(resource_id=resource_id).delete()
            for item in items:
                name = item.get("Name") or item.get("name", "")
                if not name:
                    continue
                session.add(
                    WindowsSoftware(
                        resource_id=resource_id,
                        name=name[:512],
                        version=str(item.get("Version") or "")[:128] or None,
                        publisher=str(item.get("Vendor") or "")[:256] or None,
                        install_date=str(item.get("InstallDate") or "")[:32] or None,
                    )
                )
            session.flush()
        except Exception as e:
            logger.warning(
                "Software collection failed for %s (WMI may be slow): %s", resource_id, e
            )

    # ------------------------------------------------------------------ #
    # MR-J item 4 / INV-1 / INV-4: WinRM-based collectors.
    #
    # Every PowerShell one-liner below is read-only (Get-*/Search — the
    # Windows Update search below uses IsInstalled=0 which enumerates without
    # installing anything). Every collector is a documented no-op when
    # ``client`` is None, and swallows its own exceptions into a warning log
    # (mirrors _collect_services/_collect_software above) so one collector's
    # failure never blocks the others or the primary collection.
    #
    # NONE of the exact PowerShell cmdlet output shapes below have been
    # verified against a real WinRM endpoint in this change (no live Windows
    # host was reachable from this worktree) — see the MR-J report for what
    # is real-and-unit-tested (the plumbing, JSON parsing, and DB writes) vs.
    # what remains to be confirmed once ANSIBLE_WIN_PASSWORD is live.
    # ------------------------------------------------------------------ #

    def _run_ps_json(self, client, ps_cmd: str, label: str):
        """Run a PowerShell one-liner and parse its stdout as JSON.

        Returns None on any failure (client is None, non-zero exit, invalid
        JSON) — every caller below treats None as "nothing collected", not a
        fatal error.
        """
        if client is None:
            return None
        try:
            result = client.run_ps(ps_cmd)
            stdout = getattr(result, "std_out", b"") or b""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            stdout = stdout.strip()
            if not stdout:
                return None
            return json.loads(stdout)
        except Exception as e:
            logger.warning("%s failed: %s", label, e)
            return None

    def _collect_patch_state_winrm(self, client) -> dict | None:
        """Pending Windows Updates (search-only, never installs) + last-patched
        date via Get-HotFix. Returns None when unavailable."""
        ps_cmd = (
            "$searcher = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher();"
            "$pending = $searcher.Search('IsInstalled=0').Updates;"
            "$lastHotfix = Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 1;"
            "[PSCustomObject]@{"
            "PendingCount = $pending.Count;"
            "KbList = @($pending | ForEach-Object { ($_.KBArticleIDs -join ',') });"
            "LastPatched = if ($lastHotfix -and $lastHotfix.InstalledOn) "
            "{ $lastHotfix.InstalledOn.ToString('o') } else { $null }"
            "} | ConvertTo-Json -Compress"
        )
        data = self._run_ps_json(client, ps_cmd, "Patch-state (WinRM) collection")
        if not isinstance(data, dict):
            return None
        kb_list = [kb for kb in (data.get("KbList") or []) if kb]
        return {
            "pending_count": int(data.get("PendingCount") or 0),
            "kb_list": kb_list,
            "last_patched": _parse_iso_datetime(data.get("LastPatched")),
        }

    def _collect_certificates(self, session, resource_id, client) -> None:
        """HostCertificate rows from LocalMachine My/Root/CA (INV-4)."""
        ps_cmd = (
            "Get-ChildItem Cert:\\LocalMachine\\My, Cert:\\LocalMachine\\Root, "
            "Cert:\\LocalMachine\\CA -Recurse -ErrorAction SilentlyContinue | "
            "Select-Object "
            "@{n='Store';e={($_.PSParentPath -replace '.*::','')}}, "
            "Subject, Issuer, Thumbprint, "
            "@{n='NotBefore';e={$_.NotBefore.ToString('o')}}, "
            "@{n='NotAfter';e={$_.NotAfter.ToString('o')}} "
            "| ConvertTo-Json -Compress -Depth 4"
        )
        data = self._run_ps_json(client, ps_cmd, "Certificate collection")
        if data is None:
            return
        items = data if isinstance(data, list) else [data]
        try:
            session.query(HostCertificate).filter_by(resource_id=resource_id).delete()
            now = datetime.now(timezone.utc)
            for item in items:
                thumbprint = str(item.get("Thumbprint") or "")
                if not thumbprint:
                    continue
                not_after = _parse_iso_datetime(item.get("NotAfter"))
                days_left = (not_after - now).days if not_after else None
                session.add(
                    HostCertificate(
                        resource_id=resource_id,
                        store=str(item.get("Store") or "")[:128],
                        subject=item.get("Subject"),
                        issuer=item.get("Issuer"),
                        thumbprint=thumbprint[:64],
                        not_before=_parse_iso_datetime(item.get("NotBefore")),
                        not_after=not_after,
                        days_until_expiry=days_left,
                        is_expired=bool(days_left is not None and days_left < 0),
                    )
                )
            session.flush()
        except Exception as e:
            logger.warning("Certificate write failed for %s: %s", resource_id, e)

    def _collect_security_posture(self, session, resource_id, client) -> None:
        """HostSecurityPosture row (firewall/AV/RDP/UAC) (INV-4)."""
        ps_cmd = (
            "$fw = @(Get-NetFirewallProfile -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Enabled -eq $true });"
            "$av = Get-MpComputerStatus -ErrorAction SilentlyContinue;"
            "$rdp = (Get-ItemProperty -Path "
            "'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server' "
            "-Name fDenyTSConnections -ErrorAction SilentlyContinue).fDenyTSConnections;"
            "$uac = (Get-ItemProperty -Path "
            "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System' "
            "-Name EnableLUA -ErrorAction SilentlyContinue).EnableLUA;"
            "[PSCustomObject]@{"
            "FirewallEnabled = ($fw.Count -gt 0);"
            "AvEnabled = $av.AntivirusEnabled;"
            "AvProduct = $av.AMServiceVersion;"
            "AvSignatureDate = if ($av -and $av.AntivirusSignatureLastUpdated) "
            "{ $av.AntivirusSignatureLastUpdated.ToString('o') } else { $null };"
            "RdpEnabled = if ($null -ne $rdp) { $rdp -eq 0 } else { $null };"
            "UacEnabled = if ($null -ne $uac) { $uac -eq 1 } else { $null }"
            "} | ConvertTo-Json -Compress"
        )
        data = self._run_ps_json(client, ps_cmd, "Security-posture collection")
        if not isinstance(data, dict):
            return
        try:
            self._upsert_detail(
                session,
                HostSecurityPosture,
                {
                    "resource_id": resource_id,
                    "firewall_enabled": data.get("FirewallEnabled"),
                    "firewall_service": "Windows Firewall",
                    "av_enabled": data.get("AvEnabled"),
                    "av_product": data.get("AvProduct"),
                    "av_signature_date": _parse_iso_datetime(data.get("AvSignatureDate")),
                    "rdp_enabled": data.get("RdpEnabled"),
                    "uac_enabled": data.get("UacEnabled"),
                },
                key_fields=["resource_id"],
            )
            session.flush()
        except Exception as e:
            logger.warning("Security-posture write failed for %s: %s", resource_id, e)

    def _collect_shares(self, session, resource_id, client) -> None:
        """HostShare rows from Get-SmbShare (+ per-share access) (INV-4)."""
        ps_cmd = (
            "Get-SmbShare -ErrorAction SilentlyContinue | ForEach-Object {"
            "$share = $_;"
            "$access = @(Get-SmbShareAccess -Name $share.Name -ErrorAction SilentlyContinue | "
            "Select-Object AccountName, AccessRight, AccessControlType);"
            "[PSCustomObject]@{ Name = $share.Name; Path = $share.Path; Access = $access }"
            "} | ConvertTo-Json -Compress -Depth 4"
        )
        data = self._run_ps_json(client, ps_cmd, "Share collection")
        if data is None:
            return
        items = data if isinstance(data, list) else [data]
        try:
            session.query(HostShare).filter_by(resource_id=resource_id, share_type="smb").delete()
            for item in items:
                name = item.get("Name")
                if not name:
                    continue
                access = item.get("Access") or []
                if isinstance(access, dict):
                    access = [access]
                session.add(
                    HostShare(
                        resource_id=resource_id,
                        share_type="smb",
                        name=str(name)[:256],
                        path=item.get("Path"),
                        permissions=access,
                    )
                )
            session.flush()
        except Exception as e:
            logger.warning("Share write failed for %s: %s", resource_id, e)

    def _collect_local_accounts(self, session, resource_id, client) -> None:
        """WindowsLocalUser + WindowsLocalGroupMember rows (INV-4)."""
        ps_cmd = (
            "$users = @(Get-LocalUser -ErrorAction SilentlyContinue | Select-Object "
            "Name, Enabled, PasswordRequired, "
            "@{n='PasswordNeverExpires';e={$_.PasswordExpires -eq $null}}, "
            "@{n='LastLogon';e={ if ($_.LastLogon) { $_.LastLogon.ToString('o') } else { $null } }});"
            "$admins = @(Get-LocalGroupMember -Group 'Administrators' -ErrorAction SilentlyContinue | "
            "Select-Object Name);"
            "[PSCustomObject]@{ Users = $users; Admins = @($admins | ForEach-Object { $_.Name }) } "
            "| ConvertTo-Json -Compress -Depth 4"
        )
        data = self._run_ps_json(client, ps_cmd, "Local-account collection")
        if not isinstance(data, dict):
            return
        try:
            admins = {str(a).rsplit("\\", 1)[-1] for a in (data.get("Admins") or [])}
            users = data.get("Users") or []
            if isinstance(users, dict):
                users = [users]

            session.query(WindowsLocalUser).filter_by(resource_id=resource_id).delete()
            for u in users:
                username = u.get("Name")
                if not username:
                    continue
                session.add(
                    WindowsLocalUser(
                        resource_id=resource_id,
                        username=str(username)[:256],
                        enabled=u.get("Enabled"),
                        is_admin=username in admins,
                        last_logon=_parse_iso_datetime(u.get("LastLogon")),
                        password_required=u.get("PasswordRequired"),
                        password_never_expires=u.get("PasswordNeverExpires"),
                    )
                )

            session.query(WindowsLocalGroupMember).filter_by(
                resource_id=resource_id, group_name="Administrators"
            ).delete()
            for member in admins:
                session.add(
                    WindowsLocalGroupMember(
                        resource_id=resource_id,
                        group_name="Administrators",
                        member_name=member[:256],
                    )
                )
            session.flush()
        except Exception as e:
            logger.warning("Local-account write failed for %s: %s", resource_id, e)


def _pending_kb(update) -> str:
    """Extract a KB/identifier from a pending-update entry (str or dict)."""
    if isinstance(update, str):
        return update
    if isinstance(update, dict):
        return update.get("kb") or update.get("KB") or update.get("id") or update.get("title") or ""
    return ""


_ISO_MS_RE = re.compile(r"^/Date\((-?\d+)\)/$")


def _parse_iso_datetime(text) -> datetime | None:
    """Best-effort parse of a PowerShell-emitted date string.

    Accepts ISO-8601 (the format every one-liner above requests via
    ``.ToString('o')``) and, defensively, the legacy WCF ``/Date(ms)/`` form
    some PowerShell/.NET JSON paths still emit. Returns None (never raises)
    on anything else — a date this code can't parse is treated as "unknown",
    not a collection failure.
    """
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    m = _ISO_MS_RE.match(text)
    if m:
        try:
            return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
