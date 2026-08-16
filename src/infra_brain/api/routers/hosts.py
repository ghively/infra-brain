"""infra_brain.api.routers.hosts — Host Identity + Resources/EoL API routers.

Extracted from dashboard_api.py (Task 9 — Phase 3 god-file split).
Handler bodies are byte-identical to the originals; no logic changes.

TWO routers are declared here:

* ``hosts_router`` — the existing ``/api/dashboard/hosts`` router (GET "", GET
  /{hostname}/vulns, GET /{hostname}); prefix was ``/api/dashboard/hosts`` in the
  original.

* ``resources_router`` — the /resources* and /eol* handlers that lived on the
  shared ``router`` (prefix ``/api/dashboard``).  Declared with the same prefix
  and dependencies as the original ``router`` so every full path (e.g.
  ``/api/dashboard/resources``) is byte-identical.

Import rules:
  * MAY import from infra_brain.api.schemas, infra_brain.api._helpers,
    infra_brain.db.models, infra_brain.db.session, infra_brain.dashboard_auth.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * Must NOT import from other router modules.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, timedelta

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Uuid, bindparam, func, or_, text

from infra_brain.api._envelope import paginate
from infra_brain.api._helpers import _now, _seed_resource_row, _sla_string
from infra_brain.api.schemas import (
    KV,
    BulkSeedBody,
    BulkSeedOut,
    EolMigrationOut,
    EolMigrationUpdate,
    EolOut,
    EolPageOut,
    HostCertificateOut,
    HostFirewallRuleOut,
    HostIdentityOut,
    HostPostureOut,
    HostPurposeEntry,
    HostPurposeMapOut,
    HostPurposeUpdate,
    HostPurposeWriteResult,
    HostSecurityPostureOut,
    HostShareOut,
    HostsPageOut,
    HostVulnHeaderOut,
    HostVulnItemOut,
    HostVulnsOut,
    LinuxCronOut,
    LinuxDetailOut,
    LinuxMountOut,
    LinuxNicOut,
    LinuxPackageOut,
    LinuxPendingUpdateOut,
    LinuxPortOut,
    LinuxServiceOut,
    LinuxUserOut,
    ResourceOut,
    ResourceOwnershipOut,
    ResourceOwnershipUpdate,
    ResourcePageOut,
    SeedResourceBody,
    SeedResourceOut,
    SnapshotOut,
    SnapshotPageOut,
    WindowsLocalGroupMemberOut,
    WindowsLocalUserOut,
    WindowsPatchOut,
    WindowsServiceOut,
)
from infra_brain.dashboard_auth import current_user, require_admin, require_session
from infra_brain.db.models import (
    DriftEvent,
    EolRegistry,
    HostCertificate,
    HostFirewallRule,
    HostIdentity,
    HostPurposeMap,
    HostSecurityPosture,
    HostShare,
    LinuxHost,
    R7Asset,
    R7Solution,
    R7VulnCve,
    R7Vulnerability,
    R7VulnSolution,
    Resource,
    ResourceOwnership,
    Snapshot,
    VulnQueueItem,
    WindowsLocalGroupMember,
    WindowsLocalUser,
    WindowsPatchState,
    WindowsService,
)
from infra_brain.db.session import get_session
from infra_brain.tools.host_purpose_map_mr import open_host_purpose_map_mr
from infra_brain.tools.hostmatch import normalize_host

# ─────────────────────────────────────────────────────────────────────────────
# resources_router — /api/dashboard/resources* and /api/dashboard/eol*
# Same prefix + dependencies as the original shared ``router`` so every full
# path is byte-identical (e.g. GET /api/dashboard/resources).
# ─────────────────────────────────────────────────────────────────────────────
resources_router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_session)],
)


@resources_router.get("/host-purpose-map", response_model=list[HostPurposeMapOut])
def list_host_purpose_map():
    """MR-J item 3: the curated host purpose/VLAN map, queryable via the API
    instead of only existing as tribal knowledge in the fleet-ansible
    playbook's inline vars. Populated by
    InventoryReconcileAgent._sync_host_purpose_map (idle until
    HOST_PURPOSE_MAP_PROJECT_ID is configured)."""
    with get_session() as s:
        rows = s.query(HostPurposeMap).order_by(HostPurposeMap.hostname.asc()).all()
        return [
            HostPurposeMapOut(
                hostname=r.hostname,
                purpose=r.purpose,
                vlan=r.vlan,
                subnet=r.subnet,
                source=r.source,
                updated_at=r.updated_at,
            )
            for r in rows
        ]


@resources_router.get("/resources", response_model=ResourcePageOut)
def list_resources(
    domain: str | None = None,
    zone: str | None = None,
    resource_type: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(Resource)
        if domain:
            q = q.filter(Resource.domain == domain)
        if zone:
            q = q.filter(Resource.zone == zone)
        if resource_type:
            q = q.filter(Resource.type == resource_type)
        total = q.count()
        resources = q.order_by(Resource.last_seen.desc()).offset(offset).limit(limit).all()

        # FE-8 (one EOL definition): a resource is "EOL" iff its registry row's
        # eol_date is set and in the past — the same past-eol_date test used by
        # the /counts eol_overdue aggregate (fleet.py), the /eol list's overdue
        # flag (list_eol below), and the compliance agent's eol_overdue rule.
        # Previously this marked status="eol" for *any* registry membership
        # (incl. future-dated / null eol_date rows), so the Resources/Home "EOL"
        # pills disagreed with every date-gated surface. Registry membership as
        # a distinct "tracked for EOL" metric still lives in /counts eol_total.
        eol_ids = {
            r[0]
            for r in s.query(EolRegistry.resource_id)
            .filter(EolRegistry.eol_date.isnot(None))
            .filter(EolRegistry.eol_date < _now())
            .all()
        }
        out: list[ResourceOut] = []
        for r in resources:
            drift_count = (
                s.query(DriftEvent)
                .filter(DriftEvent.resource_id == r.id, DriftEvent.status == "open")
                .count()
            )
            meta = [
                KV(k=str(k), v=str(v))
                for k, v in (r.metadata_ or {}).items()
                if isinstance(v, (str, int, float, bool))
            ]
            out.append(
                ResourceOut(
                    id=str(r.id),
                    hostname=r.name,
                    domain=r.domain,
                    resource_type=r.type,
                    zone=r.zone,
                    status="eol" if r.id in eol_ids else "healthy",
                    last_seen_at=r.last_seen,
                    drift_count=drift_count,
                    meta=meta,
                )
            )
        return ResourcePageOut(items=out, total=total, limit=limit, offset=offset)


@resources_router.get("/resources/{resource_id}/snapshots", response_model=SnapshotPageOut)
def resource_snapshots(resource_id: str, limit: int = 5, offset: int = 0):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    try:
        rid = uuid.UUID(resource_id)
    except ValueError:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Resource not found")
    with get_session() as s:
        q = (
            s.query(Snapshot)
            .filter(Snapshot.resource_id == rid)
            .order_by(Snapshot.collected_at.desc())
        )
        rows, total = paginate(q, limit=limit, offset=offset)
    items = [
        SnapshotOut(ts=row.collected_at, label="current" if i == 0 else f"−{i}")
        for i, row in enumerate(rows)
    ]
    return SnapshotPageOut(items=items, total=total, limit=limit, offset=offset)


# ─────────────────────────────────────────────────────────────────────────────
# Resource ownership / on-call / criticality (issue #116)
#
# GET  /resources/{resource_id}/ownership — 404 for a malformed/unknown
#      resource_id, else 200 (all-null fields when no ownership row exists
#      yet), mirroring the HostPurposeMap-adjacent posture endpoint's "root
#      entity must exist, overlay row is optional" contract.
# PUT  /resources/{resource_id}/ownership — human-authoritative dashboard
#      edit, upserted via raw-SQL ON CONFLICT (resource_id) DO UPDATE (same
#      shape as _HOST_PURPOSE_UPSERT below). No curated source-of-truth file
#      exists for this data (unlike host_purpose_map.yml), so unlike
#      put_host_purpose there is no trailing GitLab MR to open.
# ─────────────────────────────────────────────────────────────────────────────


@resources_router.get("/resources/{resource_id}/ownership", response_model=ResourceOwnershipOut)
def get_resource_ownership(resource_id: str) -> ResourceOwnershipOut:
    """Return the ownership/on-call/criticality overlay entry for one resource.

    Read-only. A malformed or unknown resource_id 404s (the resource is the
    real join key here, unlike HostPurposeMap's free-form hostname string); a
    known resource with no ownership row yet returns 200 with all-null
    fields, so the dashboard can render an empty editable form.
    """
    try:
        rid = uuid.UUID(resource_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Resource not found")
    with get_session() as s:
        resource = s.get(Resource, rid)
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        row = s.query(ResourceOwnership).filter(ResourceOwnership.resource_id == rid).first()
        if row is None:
            return ResourceOwnershipOut(
                resource_id=str(rid),
                owner_team=None,
                on_call_rotation=None,
                criticality_tier=None,
                source=None,
                updated_at=None,
            )
        return ResourceOwnershipOut(
            resource_id=str(rid),
            owner_team=row.owner_team,
            on_call_rotation=row.on_call_rotation,
            criticality_tier=row.criticality_tier,
            source=row.source or None,
            updated_at=row.updated_at,
        )


# Atomic upsert for human-authoritative ownership edits — prevents
# UniqueViolation when two concurrent first-PUTs for the same new resource_id
# both pass a select-then-insert existence check (same TRK-115-follow-up
# pattern as _HOST_PURPOSE_UPSERT). ON CONFLICT targets
# uq_resource_ownership_resource_id; the existing row keeps its id.
_RESOURCE_OWNERSHIP_UPSERT = text(
    """
    INSERT INTO resource_ownership
        (id, resource_id, owner_team, on_call_rotation, criticality_tier, source, updated_at)
    VALUES
        (:id, :resource_id, :owner_team, :on_call_rotation, :criticality_tier, :source, :updated_at)
    ON CONFLICT (resource_id)
    DO UPDATE SET
        owner_team       = excluded.owner_team,
        on_call_rotation = excluded.on_call_rotation,
        criticality_tier = excluded.criticality_tier,
        source           = excluded.source,
        updated_at       = excluded.updated_at
    """
).bindparams(
    bindparam("id", type_=Uuid(as_uuid=True)),
    bindparam("resource_id", type_=Uuid(as_uuid=True)),
)


@resources_router.put("/resources/{resource_id}/ownership", response_model=ResourceOwnershipOut)
async def put_resource_ownership(
    resource_id: str,
    body: ResourceOwnershipUpdate,
    request: Request,
) -> ResourceOwnershipOut:
    """Human-authoritative edit of a resource's ownership/on-call/criticality.

    Session-gated (``require_session`` on the router) BY DESIGN, not
    ``require_admin`` — matching put_host_purpose's approved
    human-authoritative trust model: any authenticated operator may correct
    this metadata, and the row is provenance-stamped ``ui:<username>``. No
    trailing external MR (no curated source-of-truth file exists for this
    table, unlike host_purpose_map.yml). Async handler — the blocking DB
    session runs in a worker thread (asyncio.to_thread; CLAUDE.md #2/#3).
    """
    try:
        rid = uuid.UUID(resource_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Resource not found")

    user = current_user(request) or {}
    username = user.get("username") or "dashboard"
    source = f"ui:{username}"

    def _worker() -> ResourceOwnershipOut:
        with get_session() as s:
            resource = s.get(Resource, rid)
            if resource is None:
                raise HTTPException(status_code=404, detail="Resource not found")
            s.execute(
                _RESOURCE_OWNERSHIP_UPSERT,
                {
                    "id": uuid.uuid4(),
                    "resource_id": rid,
                    "owner_team": body.owner_team,
                    "on_call_rotation": body.on_call_rotation,
                    "criticality_tier": body.criticality_tier,
                    "source": source,
                    "updated_at": _now(),
                },
            )
            s.commit()
            row = (
                s.query(ResourceOwnership)
                .filter(ResourceOwnership.resource_id == rid)
                .one()
            )
            return ResourceOwnershipOut(
                resource_id=str(rid),
                owner_team=row.owner_team,
                on_call_rotation=row.on_call_rotation,
                criticality_tier=row.criticality_tier,
                source=row.source or None,
                updated_at=row.updated_at,
            )

    return await asyncio.to_thread(_worker)


# ─────────────────────────────────────────────────────────────────────────────
# Admin: manual resource seeding
# ─────────────────────────────────────────────────────────────────────────────


@resources_router.post("/resources", response_model=SeedResourceOut, tags=["admin"])
def create_resource(
    body: SeedResourceBody,
    _: None = Depends(require_admin),
):
    """Admin endpoint: manually seed a resource."""
    with get_session() as session:
        resource_id, created = _seed_resource_row(session, body)
        session.commit()
    return {"resource_id": str(resource_id), "created": created, "hostname": body.hostname}


@resources_router.post("/resources/bulk", response_model=BulkSeedOut, tags=["admin"])
def bulk_create_resources(
    body: BulkSeedBody,
    _: None = Depends(require_admin),
):
    """Admin endpoint: bulk seed resources from a list or YAML string."""
    items = body.resources
    if items is None and body.resources_yaml:
        try:
            items = yaml.safe_load(body.resources_yaml)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"YAML parse error: {exc}")
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="Provide 'resources' list or 'resources_yaml'")

    created_count = 0
    updated_count = 0
    errors: list[dict] = []

    with get_session() as session:
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append({"index": i, "error": "item is not a dict"})
                continue
            try:
                seed_body = SeedResourceBody(**item)
                _, created = _seed_resource_row(session, seed_body)
                if created:
                    created_count += 1
                else:
                    updated_count += 1
            except Exception as exc:
                errors.append({"index": i, "hostname": item.get("hostname"), "error": str(exc)})
        session.commit()

    return {"created": created_count, "updated": updated_count, "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
# Linux host detail
# ─────────────────────────────────────────────────────────────────────────────


@resources_router.get("/resources/{resource_id}/linux", response_model=LinuxDetailOut)
def get_linux_detail(resource_id: str):
    try:
        rid = uuid.UUID(resource_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Resource not found")
    with get_session() as s:
        resource = s.get(Resource, rid)
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        lh = s.query(LinuxHost).filter(LinuxHost.resource_id == rid).first()
        if lh is None:
            raise HTTPException(status_code=404, detail="No linux record for this resource")
        return LinuxDetailOut(
            resource_id=str(resource.id),
            hostname=resource.name,
            distro=lh.distro or None,
            kernel=lh.kernel or None,
            arch=lh.arch or None,
            packages=[
                LinuxPackageOut(
                    name=p.name,
                    version=p.version,
                    manager=p.manager,
                    installed_at=p.installed_at,
                )
                for p in lh.packages
            ],
            services=[
                LinuxServiceOut(
                    name=sv.name,
                    state=sv.state,
                    enabled=sv.enabled,
                    last_checked=sv.last_checked,
                )
                for sv in lh.services
            ],
            users=[
                LinuxUserOut(
                    username=u.username,
                    shell=u.shell,
                    sudo=u.sudo,
                    last_login=u.last_login,
                )
                for u in lh.users
            ],
            ports=[
                LinuxPortOut(
                    port=pt.port,
                    proto=pt.proto,
                    process=pt.process,
                    state=pt.state,
                )
                for pt in lh.ports
            ],
            crons=[
                LinuxCronOut(
                    owner=c.owner,
                    schedule=c.schedule,
                    command=c.command,
                )
                for c in lh.crons
            ],
            mounts=[
                LinuxMountOut(
                    mount=m.mount,
                    device=m.device,
                    fstype=m.fstype,
                    size_total_gb=m.size_total_gb,
                    size_available_gb=m.size_available_gb,
                )
                for m in lh.mounts
            ],
            nics=[
                LinuxNicOut(
                    name=n.name,
                    mac=n.mac,
                    ipv4=n.ipv4,
                    ipv6=n.ipv6,
                    speed_mbps=n.speed_mbps,
                )
                for n in lh.nics
            ],
            pending_updates=[
                LinuxPendingUpdateOut(
                    package=p.package,
                    current_version=p.current_version,
                    available_version=p.available_version,
                    security=p.security,
                    manager=p.manager,
                )
                for p in lh.pending_updates
            ],
        )


# ─────────────────────────────────────────────────────────────────────────────
# Windows Patch State
# ─────────────────────────────────────────────────────────────────────────────


@resources_router.get("/resources/{resource_id}/windows", response_model=WindowsPatchOut)
def get_windows_detail(resource_id: str):
    try:
        rid = uuid.UUID(resource_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Resource not found")
    with get_session() as s:
        resource = s.get(Resource, rid)
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        patch = s.query(WindowsPatchState).filter(WindowsPatchState.resource_id == rid).first()
        if patch is None:
            raise HTTPException(status_code=404, detail="No windows record for this resource")
        services = s.query(WindowsService).filter(WindowsService.resource_id == rid).all()
        return WindowsPatchOut(
            hostname=patch.hostname,
            kb_list=patch.kb_list,
            pending_count=patch.pending_count,
            last_patched=patch.last_patched,
            winrm_status=patch.winrm_status,
            services=[
                WindowsServiceOut(
                    name=sv.name,
                    state=sv.state,
                    start_type=sv.start_type,
                    path=sv.path,
                )
                for sv in services
            ],
        )


# ─────────────────────────────────────────────────────────────────────────────
# Host posture (UI-1 / #57) — certs, security posture, firewall rules, shares,
# Windows local admin/group membership. One combined GET, matching the
# resources/{id}/linux pattern above rather than five separate routes.
# ─────────────────────────────────────────────────────────────────────────────


@resources_router.get("/resources/{resource_id}/posture", response_model=HostPostureOut)
def get_host_posture(resource_id: str):
    """Combined host-posture detail for one resource.

    Read-only. Unlike get_linux_detail (which 404s when no LinuxHost root row
    exists), posture data has no single required root row across its five
    source tables — a resource that exists but has zero posture rows returns
    200 with empty lists / a null security_posture, not 404. Only an unknown
    or malformed resource_id 404s.
    """
    try:
        rid = uuid.UUID(resource_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Resource not found")
    with get_session() as s:
        resource = s.get(Resource, rid)
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        certs = (
            s.query(HostCertificate)
            .filter(HostCertificate.resource_id == rid)
            .order_by(HostCertificate.not_after.asc().nullslast())
            .all()
        )
        posture = (
            s.query(HostSecurityPosture).filter(HostSecurityPosture.resource_id == rid).first()
        )
        rules = s.query(HostFirewallRule).filter(HostFirewallRule.resource_id == rid).all()
        shares = s.query(HostShare).filter(HostShare.resource_id == rid).all()
        local_users = (
            s.query(WindowsLocalUser).filter(WindowsLocalUser.resource_id == rid).all()
        )
        group_members = (
            s.query(WindowsLocalGroupMember)
            .filter(WindowsLocalGroupMember.resource_id == rid)
            .all()
        )

        return HostPostureOut(
            resource_id=str(resource.id),
            hostname=resource.name,
            certificates=[
                HostCertificateOut(
                    store=c.store,
                    subject=c.subject,
                    issuer=c.issuer,
                    thumbprint=c.thumbprint,
                    not_before=c.not_before,
                    not_after=c.not_after,
                    days_until_expiry=c.days_until_expiry,
                    is_expired=c.is_expired,
                )
                for c in certs
            ],
            security_posture=HostSecurityPostureOut(
                firewall_enabled=posture.firewall_enabled,
                firewall_service=posture.firewall_service,
                av_enabled=posture.av_enabled,
                av_product=posture.av_product,
                av_signature_date=posture.av_signature_date,
                rdp_enabled=posture.rdp_enabled,
                uac_enabled=posture.uac_enabled,
                ssh_password_auth=posture.ssh_password_auth,
                ssh_permit_root_login=posture.ssh_permit_root_login,
                ssh_pubkey_auth=posture.ssh_pubkey_auth,
                selinux_mode=posture.selinux_mode,
                apparmor_status=posture.apparmor_status,
            )
            if posture is not None
            else None,
            firewall_rules=[
                HostFirewallRuleOut(
                    table_name=r.table_name,
                    chain=r.chain,
                    rule_text=r.rule_text,
                    action=r.action,
                    source=r.source,
                )
                for r in rules
            ],
            shares=[
                HostShareOut(
                    share_type=sh.share_type,
                    name=sh.name,
                    path=sh.path,
                    permissions=sh.permissions or [],
                )
                for sh in shares
            ],
            local_users=[
                WindowsLocalUserOut(
                    username=u.username,
                    enabled=u.enabled,
                    is_admin=u.is_admin,
                    last_logon=u.last_logon,
                    password_required=u.password_required,
                    password_never_expires=u.password_never_expires,
                )
                for u in local_users
            ],
            local_group_members=[
                WindowsLocalGroupMemberOut(group_name=g.group_name, member_name=g.member_name)
                for g in group_members
            ],
        )


# How close to its EOL date an asset must be (and not yet past it) to be
# classified "approaching" rather than merely "tracked". Matches the <90-day
# "near-term" boundary already used by EolAgent._pci_risk_score (agents/eol.py)
# to score assets, so the dashboard's proximity band lines up with the same
# window the scoring already treats as urgent.
_EOL_APPROACHING_WINDOW_DAYS = 90


@resources_router.get("/eol", response_model=EolPageOut)
def list_eol(limit: int = 200, offset: int = 0):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = (
            s.query(EolRegistry, Resource)
            .outerjoin(Resource, Resource.id == EolRegistry.resource_id)
            .order_by(EolRegistry.eol_date.asc())
        )
        # paginate() expects a single-entity query; for a join query we paginate
        # the EolRegistry entity side and count on the join result.
        total = q.count()
        join_rows = q.offset(offset).limit(limit).all()
        out: list[EolOut] = []
        now = _now()
        approaching_cutoff = now + timedelta(days=_EOL_APPROACHING_WINDOW_DAYS)
        for e, r in join_rows:
            eol_dt = (
                e.eol_date.replace(tzinfo=e.eol_date.tzinfo or UTC)
                if e.eol_date is not None
                else None
            )
            # Real proximity band, not a binary overdue/approaching flag (a
            # decade-out or entirely undated EOL is not "approaching"):
            #   unknown     — no eol_date at all
            #   overdue     — eol_date already in the past
            #   approaching — within _EOL_APPROACHING_WINDOW_DAYS of eol_date
            #   tracked     — eol_date further out than the approaching window
            if eol_dt is None:
                status = "unknown"
            elif eol_dt < now:
                status = "overdue"
            elif eol_dt < approaching_cutoff:
                status = "approaching"
            else:
                status = "tracked"
            out.append(
                EolOut(
                    id=str(e.id),
                    asset=e.asset_name,
                    host=r.name if r else "—",
                    eol=e.eol_date.date().isoformat() if e.eol_date else "—",
                    # No `or 0` default: pci_risk_score is nullable, and the
                    # frontend must be able to tell "unscored" apart from "0
                    # risk" to exclude unscored assets from the average.
                    # Field name is `pci_risk_score`, not the bare `risk` used
                    # before TRK-275/GitLab #146+#153 — this is a 0-100 score
                    # on a completely different scale than Rapid7's
                    # `risk_score` field; the explicit name prevents the two
                    # from being conflated by a reader.
                    pci_risk_score=e.pci_risk_score,
                    migration=e.migration_path or "—",
                    status=status,
                )
            )
        return EolPageOut(items=out, total=total, limit=limit, offset=offset)


@resources_router.patch("/eol/{eol_id}/migration", response_model=EolMigrationOut)
def patch_eol_migration(
    eol_id: uuid.UUID,
    body: EolMigrationUpdate,
    _: None = Depends(require_admin),
):
    with get_session() as s:
        asset = s.query(EolRegistry).filter(EolRegistry.id == eol_id).first()
        if asset is None:
            raise HTTPException(status_code=404, detail="EOL asset not found")
        asset.migration_path = body.migration_path.strip()
        s.commit()
        return {"id": str(eol_id), "migration_path": asset.migration_path}


# ─────────────────────────────────────────────────────────────────────────────
# hosts_router — /api/dashboard/hosts (unified host identity view)
# Prefix preserved byte-identical from the original in dashboard_api.py.
# ─────────────────────────────────────────────────────────────────────────────
hosts_router = APIRouter(
    prefix="/api/dashboard/hosts",
    tags=["hosts"],
    dependencies=[Depends(require_session)],
)


_IDENTITY_LEGS = (
    "r7_resource_id",
    "vsphere_resource_id",
    "octopus_resource_id",
    "linux_resource_id",
    "windows_resource_id",
    "net_resource_id",
    "cloud_resource_id",
    "k8s_resource_id",
    "netdevice_resource_id",
)


def _host_identity_dict(row: HostIdentity, retired_at: str | None = None) -> dict:
    # A HostIdentity with EVERY source leg NULL means no collector currently
    # observes this machine. That state was invisible on the dashboard: on
    # 2026-08-11 two real hosts (media_host, storage_node) went dark, the drift
    # pass retired their linux_host Resources, KG-8 cleared their identity
    # legs -- and the Hosts page kept listing them as ordinary current hosts,
    # indistinguishable from live ones, with last_reconciled refreshing every
    # 30 minutes. "observed" is computed, not stored, so it can never go
    # stale; "retired_at" (when resolvable from the name-matched retired
    # Resource) says SINCE WHEN, which is what an operator actually asks.
    observed = any(getattr(row, leg) is not None for leg in _IDENTITY_LEGS)
    return {
        "observed": observed,
        "retired_at": retired_at,
        "id": str(row.id),
        "short_hostname": row.short_hostname,
        "fqdn": row.fqdn,
        "ip_addresses": row.ip_addresses or [],
        "r7_resource_id": str(row.r7_resource_id) if row.r7_resource_id else None,
        "vsphere_resource_id": str(row.vsphere_resource_id) if row.vsphere_resource_id else None,
        "octopus_resource_id": str(row.octopus_resource_id) if row.octopus_resource_id else None,
        "linux_resource_id": str(row.linux_resource_id) if row.linux_resource_id else None,
        "windows_resource_id": str(row.windows_resource_id) if row.windows_resource_id else None,
        "os_family": row.os_family,
        "risk_score": row.risk_score,
        "vuln_count": row.vuln_count,
        "patch_status": row.patch_status,
        "vsphere_power_state": row.vsphere_power_state,
        "octopus_machine_status": row.octopus_machine_status,
        "last_reconciled": row.last_reconciled.isoformat() if row.last_reconciled else None,
    }


@hosts_router.get("", response_model=HostsPageOut)
def list_hosts(
    q: str = "",
    os_family: str = "",
    patch_status: str = "",
    limit: int = 200,
    offset: int = 0,
) -> HostsPageOut:
    """Paged, filtered canonical host identity rows with denormalized display
    fields.

    Read-only. ``q`` is a substring over short_hostname/fqdn; ``os_family`` and
    ``patch_status`` are exact matches. Pagination + count are pushed to SQL.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        query = s.query(HostIdentity)
        if q:
            query = query.filter(
                or_(
                    HostIdentity.short_hostname.ilike(f"%{q}%"),
                    HostIdentity.fqdn.ilike(f"%{q}%"),
                )
            )
        if os_family:
            query = query.filter(HostIdentity.os_family == os_family)
        if patch_status:
            query = query.filter(HostIdentity.patch_status == patch_status)

        total = query.count()
        rows = query.order_by(HostIdentity.short_hostname).offset(offset).limit(limit).all()

        # For unobserved identities (every leg NULL), resolve SINCE WHEN from
        # the name-matched retired host Resource. One IN-query for the page,
        # not per-row; name-match is exact on short_hostname, which is how
        # the linux/windows collectors name their host resources.
        unobserved_names = [
            r.short_hostname
            for r in rows
            if not any(getattr(r, leg) is not None for leg in _IDENTITY_LEGS)
        ]
        retired_by_name: dict[str, str] = {}
        if unobserved_names:
            for name, retired_at in (
                s.query(Resource.name, Resource.retired_at)
                .filter(
                    Resource.name.in_(unobserved_names),
                    Resource.type.in_(("linux_host", "windows_host")),
                    Resource.retired_at.isnot(None),
                )
                .all()
            ):
                retired_by_name[name] = retired_at.isoformat()

        return HostsPageOut(
            items=[
                _host_identity_dict(row, retired_at=retired_by_name.get(row.short_hostname))
                for row in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


@hosts_router.get("/{hostname}/vulns", response_model=HostVulnsOut)
def get_host_vulns(
    hostname: str,
    severity: str = "",
    status: str = "open",
    sla_overdue: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> HostVulnsOut:
    """Per-host vulnerability list: a risk-band header (from r7_assets) plus a
    paginated, filterable list of the host's CVEs enriched with real CVSS,
    exploit/PCI flags, and a remediation summary.

    Read-only. Walks the canonical link
    ``host_identities → r7_resource_id → vuln_queue → r7_vuln_cves →
    r7_vulnerabilities → r7_vuln_solutions → r7_solutions``.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    # short_hostname is stored in normalized (lowercased, first-DNS-label) form;
    # normalize_host() mirrors that so a full FQDN still matches (Bug A / #121).
    hostname_lower = normalize_host(hostname)

    with get_session() as s:
        host_id = (
            s.query(HostIdentity).filter(HostIdentity.short_hostname == hostname_lower).first()
        )
        if not host_id:
            raise HTTPException(status_code=404, detail=f"Host '{hostname}' not found")

        resource_id = host_id.r7_resource_id

        # Band header: prefer the live r7_assets counts, fall back to the
        # denormalized host_identities risk score.
        r7_asset = (
            s.query(R7Asset).filter(R7Asset.resource_id == resource_id).first()
            if resource_id
            else None
        )
        header = HostVulnHeaderOut(
            hostname=hostname,
            risk_score=float(r7_asset.risk_score or 0)
            if r7_asset
            else float(host_id.risk_score or 0),
            vuln_critical=int(r7_asset.vuln_critical or 0) if r7_asset else 0,
            vuln_severe=int(r7_asset.vuln_severe or 0) if r7_asset else 0,
            vuln_moderate=int(r7_asset.vuln_moderate or 0) if r7_asset else 0,
        )

        if not resource_id:
            return HostVulnsOut(header=header, items=[], total=0, limit=limit, offset=offset)

        vq = s.query(VulnQueueItem).filter(VulnQueueItem.resource_id == resource_id)
        if status:
            vq = vq.filter(VulnQueueItem.status == status)
        if severity:
            vq = vq.filter(VulnQueueItem.severity.ilike(f"%{severity}%"))
        if sla_overdue:
            vq = vq.filter(VulnQueueItem.sla_due.isnot(None)).filter(VulnQueueItem.sla_due < _now())

        total = vq.count()
        vuln_rows = (
            vq.order_by(VulnQueueItem.sla_due.asc().nullslast()).offset(offset).limit(limit).all()
        )

        cve_ids = [v.cve_id for v in vuln_rows]
        best: dict[str, R7Vulnerability] = {}
        slug_map: dict[str, list[str]] = {}
        solutions_map: dict[str, str] = {}

        if cve_ids:
            bridge = s.query(R7VulnCve).filter(R7VulnCve.cve_id.in_(cve_ids)).all()
            for br in bridge:
                slug_map.setdefault(br.cve_id, []).append(br.r7_vuln_id)
            all_slugs = list({sl for ss in slug_map.values() for sl in ss})
            if all_slugs:
                rv_rows = (
                    s.query(R7Vulnerability).filter(R7Vulnerability.r7_vuln_id.in_(all_slugs)).all()
                )
                rv_by_slug = {v.r7_vuln_id: v for v in rv_rows}
                for cid, slugs in slug_map.items():
                    candidates = [rv_by_slug[sl] for sl in slugs if sl in rv_by_slug]
                    if candidates:
                        best[cid] = max(candidates, key=lambda v: v.cvss_v3_score or 0)

                # Remediation summary per CVE (first solution found via any slug).
                sol_links = (
                    s.query(R7VulnSolution).filter(R7VulnSolution.r7_vuln_id.in_(all_slugs)).all()
                )
                sol_ids = list({sl.r7_solution_id for sl in sol_links})
                if sol_ids:
                    sol_rows = (
                        s.query(R7Solution).filter(R7Solution.r7_solution_id.in_(sol_ids)).all()
                    )
                    sol_by_id = {sol.r7_solution_id: sol for sol in sol_rows}
                    link_by_slug = {sl.r7_vuln_id: sl.r7_solution_id for sl in sol_links}
                    for cid, slugs in slug_map.items():
                        for slug in slugs:
                            sol_id = link_by_slug.get(slug)
                            if sol_id and sol_id in sol_by_id:
                                solutions_map[cid] = sol_by_id[sol_id].summary or ""
                                break

        items: list[HostVulnItemOut] = []
        for v in vuln_rows:
            rv = best.get(v.cve_id)
            slug = (slug_map.get(v.cve_id) or [""])[0]
            items.append(
                HostVulnItemOut(
                    cve_id=v.cve_id,
                    kb_id=v.kb_id or "",
                    severity=v.severity or "",
                    cvss_v3=float(rv.cvss_v3_score or 0) if rv else 0.0,
                    title=(rv.title or "") if rv else "",
                    exploits=int(rv.exploits or 0) if rv else 0,
                    fix_available=bool(rv.fix_available) if rv else False,
                    pci_fail=bool(rv.pci_fail) if rv else False,
                    sla=_sla_string(v.sla_due),
                    sla_due=v.sla_due,
                    status=v.status or "open",
                    last_updated=v.last_updated,
                    r7_vuln_id=(rv.r7_vuln_id if rv else slug) or "",
                    solution_summary=solutions_map.get(v.cve_id, ""),
                )
            )

        return HostVulnsOut(header=header, items=items, total=total, limit=limit, offset=offset)


# ─────────────────────────────────────────────────────────────────────────────
# Human-authoritative host purpose map (instant DB write + trailing MR)
# ─────────────────────────────────────────────────────────────────────────────


def _purpose_provenance(source: str | None) -> str:
    """Derive provenance from a HostPurposeMap.source value.

    Empty/None -> "unset"; a ``ui:<username>`` source (a human dashboard edit) ->
    "ui"; anything else (repo-sync rows carry ``<project_id>:<file>``) -> "repo".
    """
    if not source:
        return "unset"
    if source.startswith("ui:"):
        return "ui"
    return "repo"


@hosts_router.get("/{hostname}/purpose", response_model=HostPurposeEntry)
async def get_host_purpose(hostname: str) -> HostPurposeEntry:
    """Return the curated purpose/VLAN/subnet entry for one host.

    Read-only. A missing row returns 200 with all-null fields and
    ``provenance="unset"`` (rather than 404), so the dashboard can render an empty
    editable form. Async handler — the sync DB read runs in a worker thread
    (asyncio.to_thread; CLAUDE.md #2/#3) since ``get_session()`` is synchronous.
    """

    def _read() -> HostPurposeEntry:
        with get_session() as s:
            # Case-insensitive compare (Bug B / #121) — HostPurposeMap.hostname has
            # no case-insensitive unique index, so a case-sensitive `==` here would
            # miss an existing row seeded/written under different casing.
            row = (
                s.query(HostPurposeMap)
                .filter(func.lower(HostPurposeMap.hostname) == hostname.strip().lower())
                .first()
            )
            if row is None:
                return HostPurposeEntry(
                    hostname=hostname,
                    purpose=None,
                    vlan=None,
                    subnet=None,
                    source=None,
                    updated_at=None,
                    provenance="unset",
                )
            return HostPurposeEntry(
                hostname=row.hostname,
                purpose=row.purpose,
                vlan=row.vlan,
                subnet=row.subnet,
                source=row.source or None,
                updated_at=row.updated_at,
                provenance=_purpose_provenance(row.source),
            )

    return await asyncio.to_thread(_read)


# Atomic upsert for human-authoritative purpose edits — prevents UniqueViolation
# when two concurrent first-PUTs for the same new hostname both pass a
# select-then-insert existence check (TRK-115 follow-up). ON CONFLICT targets
# uq_host_purpose_map_hostname; the existing row keeps its id.
#
# Compatible with both PostgreSQL (production) and SQLite 3.24+ (test in-memory
# engine) — same pattern as agents/compliance.py's _COMPLIANCE_UPSERT.
_HOST_PURPOSE_UPSERT = text(
    """
    INSERT INTO host_purpose_map
        (id, hostname, purpose, vlan, subnet, source, updated_at)
    VALUES
        (:id, :hostname, :purpose, :vlan, :subnet, :source, :updated_at)
    ON CONFLICT (hostname)
    DO UPDATE SET
        purpose    = excluded.purpose,
        vlan       = excluded.vlan,
        subnet     = excluded.subnet,
        source     = excluded.source,
        updated_at = excluded.updated_at
    """
).bindparams(
    # Typed bindparam converts uuid.UUID to each backend's storage format
    # (CHAR(32) hex in SQLite, native UUID in PostgreSQL) so raw-SQL inserts
    # stay readable by subsequent ORM queries.
    bindparam("id", type_=Uuid(as_uuid=True)),
)


@hosts_router.put("/{hostname}/purpose", response_model=HostPurposeWriteResult)
async def put_host_purpose(
    hostname: str,
    body: HostPurposeUpdate,
    request: Request,
) -> HostPurposeWriteResult:
    """Human-authoritative edit of a host's purpose/VLAN/subnet.

    The DB write is authoritative and is committed IMMEDIATELY. A trailing GitLab
    MR then persists the same edit into version control via the sanctioned
    ``open_host_purpose_map_mr`` helper (fail-closed write-gate inside). ORDER
    MATTERS and is guaranteed here:

      1. Upsert the HostPurposeMap row (``source=ui:<username>``) and COMMIT.
      2. THEN open the persistence MR (proposed=False), AFTER the commit.
      3. If the MR raises, the committed DB edit is NEVER rolled back — we capture
         ``mr_error`` and return ``mr_url=None``.

    Session-gated (``require_session`` on the router) BY DESIGN, not
    ``require_admin``: per the approved human-authoritative trust model any
    authenticated operator may correct host purpose/VLAN metadata — the row is
    provenance-stamped ``ui:<username>`` and the trailing external MR is itself
    write-gated (fail-closed) — unlike higher-blast-radius mutations (EOL
    migration, resource seeding) which require admin. Async handler — ALL blocking
    work (DB session AND the sync-httpx MR call) runs in one worker thread
    (asyncio.to_thread; CLAUDE.md #2/#3). ``current_user`` is read in the async
    body (cheap cookie parse), mirroring the approve/reject handlers in
    governance_ops.py.
    """
    user = current_user(request) or {}
    username = user.get("username") or "dashboard"
    source = f"ui:{username}"

    # Case-fold the upsert/lookup key (Bug B / #121) — HostPurposeMap.hostname has
    # no case-insensitive unique index, so upserting the raw path-param casing
    # forks a second row for the same host instead of updating the existing one
    # (e.g. "WEB01" vs. "web01"). Case-folding (not the fuller normalize_host()
    # domain truncation — HostPurposeMap may legitimately store FQDNs from the
    # YAML seed source) keeps existing seeded rows matchable while collapsing
    # case variants onto one row going forward.
    hostname_fold = hostname.strip().lower()

    def _worker() -> HostPurposeWriteResult:
        # 1. Upsert + COMMIT the authoritative DB edit BEFORE touching GitLab.
        #    Atomic ON CONFLICT (not select-then-insert) so a concurrent first
        #    PUT for the same new hostname can't 500 on UniqueViolation. A
        #    case-insensitive pre-check picks the upsert key: an already-existing
        #    row's exact stored casing (so legacy mixed-case rows from before this
        #    fix, or the YAML seed, get updated in place instead of forked), or the
        #    case-folded value for a genuinely new host. Two concurrent first-PUTs
        #    for the same new host both fall back to the identical folded key, so
        #    the ON CONFLICT race safety is preserved.
        with get_session() as s:
            existing = (
                s.query(HostPurposeMap)
                .filter(func.lower(HostPurposeMap.hostname) == hostname_fold)
                .first()
            )
            hostname_key = existing.hostname if existing is not None else hostname_fold
            s.execute(
                _HOST_PURPOSE_UPSERT,
                {
                    "id": uuid.uuid4(),
                    "hostname": hostname_key,
                    "purpose": body.purpose,
                    "vlan": body.vlan,
                    "subnet": body.subnet,
                    "source": source,
                    "updated_at": _now(),
                },
            )
            s.commit()
            # Capture the persisted values while the session is still open.
            row = s.query(HostPurposeMap).filter(HostPurposeMap.hostname == hostname_key).one()
            final_hostname = row.hostname
            final_purpose = row.purpose
            final_vlan = row.vlan
            final_subnet = row.subnet
            final_source = row.source
            final_updated = row.updated_at

        # 2. THEN open the trailing persistence MR. A failure here must NOT roll
        #    back the committed DB edit — capture the error and carry on.
        mr_url: str | None = None
        mr_error: str | None = None
        try:
            result = open_host_purpose_map_mr(
                hostname,
                purpose=final_purpose,
                vlan=final_vlan,
                subnet=final_subnet,
                actor=username,
                proposed=False,
            )
            mr_url = result["mr_url"]
        except Exception as exc:  # noqa: BLE001 — MR failure must not undo the DB edit
            mr_error = str(exc)

        return HostPurposeWriteResult(
            hostname=final_hostname,
            purpose=final_purpose,
            vlan=final_vlan,
            subnet=final_subnet,
            source=final_source,
            updated_at=final_updated,
            provenance=_purpose_provenance(final_source),
            mr_url=mr_url,
            mr_error=mr_error,
        )

    return await asyncio.to_thread(_worker)


@hosts_router.get("/{hostname}", response_model=HostIdentityOut)
def get_host(hostname: str) -> dict:
    """Return a single canonical host identity row by short_hostname."""
    with get_session() as s:
        row = s.query(HostIdentity).filter_by(short_hostname=normalize_host(hostname)).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Host '{hostname}' not found")
        return _host_identity_dict(row)
