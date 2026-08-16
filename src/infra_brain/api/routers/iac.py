"""infra_brain.api.routers.iac — IaC / Pipelines API router.

Extracted from dashboard_api.py (Task 4 — Phase 3 god-file split).
Handler bodies are byte-identical to the originals; no logic changes.

Import rules:
  * MAY import from infra_brain.api.schemas, infra_brain.api._helpers,
    infra_brain.db.models, infra_brain.db.session, infra_brain.dashboard_auth.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * Must NOT import from other router modules.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import aliased

from infra_brain.api._envelope import paginate
from infra_brain.api.schemas import (
    AnsibleInventoryGroupOut,
    AnsibleInventoryHostOut,
    AnsiblePlaybookPlayOut,
    AnsibleStructureOut,
    CiScheduleOut,
    CiSchedulesPageOut,
    ComposeServiceOut,
    ComposeServicesPageOut,
    IacOverviewOut,
    IacPipelineRunOut,
    IacProjectOut,
    IacSummaryOut,
    K8sManifestResourceOut,
    K8sManifestResourcesPageOut,
    TerraformResourceOut,
    TerraformResourcesPageOut,
)
from infra_brain.dashboard_auth import require_session
from infra_brain.db.models import (
    AnsibleInventoryGroup,
    AnsibleInventoryHost,
    AnsiblePlaybookPlay,
    CiPipelineRun,
    CiSchedule,
    ComposeService,
    GitlabProject,
    IacFile,
    K8sManifestResource,
    TerraformResource,
)
from infra_brain.db.session import get_session

# Dedicated IaC / CI-CD view. Same session gate, distinct prefix — surfaces the
# GitLab project × parsed-IaC-file × pipeline-run relational model (gitlab_projects,
# iac_files, ci_pipeline_runs) with its own purpose-built response shape rather than
# squeezing it into the generic Resource contract.
iac_router = APIRouter(
    prefix="/api/iac",
    tags=["iac"],
    dependencies=[Depends(require_session)],
)


@iac_router.get("/overview", response_model=IacOverviewOut)
def iac_overview() -> IacOverviewOut:
    """Assemble the GitLab/IaC estate for the dashboard's IaC / Pipelines view.

    Read-only (SELECT only). One row per GitLab project, each decorated with its
    parsed-IaC file counts (``files_by_type``) and its latest ~5 CI pipeline runs.
    The ``summary`` block carries fleet-wide totals for the header strip.
    """
    with get_session() as s:
        projects = s.query(GitlabProject).order_by(GitlabProject.name).all()

        # --- File-type counts per project (gitlab_project_id → {type: count}) --
        # M-4: this used to be `s.query(IacFile).all()` -- every parsed IaC
        # file row across every project, fully materialized into Python just
        # to tally per-project/per-type counts. Pushed into SQL via a
        # GROUP BY instead: only (project x file_type) group rows -- a much
        # smaller result set than one row per file -- ever cross into Python.
        files_by_type_by_proj: dict[int, dict[str, int]] = {}
        total_files_by_type: dict[str, int] = {}
        total_file_count = 0
        ftype_expr = func.coalesce(IacFile.file_type, "unknown")
        for proj_id, ftype, cnt in (
            s.query(IacFile.gitlab_project_id, ftype_expr, func.count())
            .group_by(IacFile.gitlab_project_id, ftype_expr)
            .all()
        ):
            files_by_type_by_proj.setdefault(proj_id, {})[ftype] = cnt
            total_files_by_type[ftype] = total_files_by_type.get(ftype, 0) + cnt
            total_file_count += cnt

        # --- Recent pipeline runs per project (latest 5 by created_at) ---------
        # M-4: this used to be `s.query(CiPipelineRun).order_by(...).all()` --
        # every pipeline run of every project, unbounded, fully materialized
        # into Python just to keep 5 per project. ci_pipeline_runs grows
        # without bound (every CI run of every project, forever), so the
        # "latest 5 per project" selection is pushed into SQL via a
        # ROW_NUMBER() window instead: at most 5 rows per project ever cross
        # into Python. `pipeline_run_count` (the fleet-wide total for the
        # summary header) is a separate cheap COUNT(*), matching the
        # `total_file_count` pattern above.
        pipeline_run_count = s.query(CiPipelineRun).count()
        rn = (
            func.row_number()
            .over(
                partition_by=CiPipelineRun.gitlab_project_id,
                order_by=CiPipelineRun.created_at.desc().nullslast(),
            )
            .label("rn")
        )
        ranked_subq = s.query(CiPipelineRun, rn).subquery()
        RankedRun = aliased(CiPipelineRun, ranked_subq)
        runs_by_proj: dict[int, list[CiPipelineRun]] = {}
        # Explicit ORDER BY rn: SQL does not otherwise guarantee row order,
        # and callers below rely on each project's list already being
        # newest-first (matching the window's own DESC ordering).
        for r in (
            s.query(RankedRun)
            .filter(ranked_subq.c.rn <= 5)
            .order_by(ranked_subq.c.gitlab_project_id, ranked_subq.c.rn)
            .all()
        ):
            runs_by_proj.setdefault(r.gitlab_project_id, []).append(r)

        project_rows: list[IacProjectOut] = []
        for p in projects:
            # Already the 5 most recent per project, most-recent first
            # (ORDER BY created_at DESC inside the window) -- no re-slice
            # needed.
            recent = runs_by_proj.get(p.gitlab_project_id, [])
            project_rows.append(
                IacProjectOut(
                    gitlab_project_id=p.gitlab_project_id,
                    name=p.name,
                    path_with_namespace=p.path_with_namespace or "",
                    default_branch=p.default_branch or "",
                    visibility=p.visibility or "",
                    archived=p.archived,
                    last_pipeline_status=p.last_pipeline_status or "",
                    last_pipeline_ref=p.last_pipeline_ref or "",
                    last_activity_at=p.last_activity_at,
                    file_count=sum(files_by_type_by_proj.get(p.gitlab_project_id, {}).values()),
                    files_by_type=files_by_type_by_proj.get(p.gitlab_project_id, {}),
                    recent_pipelines=[
                        IacPipelineRunOut(
                            pipeline_id=r.pipeline_id,
                            ref=r.ref or "",
                            status=r.status or "",
                            source=r.source or "",
                            created_at=r.created_at,
                            duration=r.duration,
                            web_url=r.web_url or "",
                        )
                        for r in recent
                    ],
                )
            )

        summary = IacSummaryOut(
            project_count=len(projects),
            file_count=total_file_count,
            files_by_type=total_files_by_type,
            pipeline_run_count=pipeline_run_count,
        )
        return IacOverviewOut(projects=project_rows, summary=summary)


@iac_router.get("/compose-services", response_model=ComposeServicesPageOut)
def list_compose_services(limit: int = 200, offset: int = 0):
    """Docker Compose service listing across every parsed compose file
    (UI Batch 6 / GitLab #62).

    Read-only (SELECT only). Empty items/total=0 when no compose files have
    been parsed yet — never a 500.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = (
            s.query(ComposeService, IacFile)
            .join(IacFile, IacFile.id == ComposeService.iac_file_id)
            .order_by(IacFile.project_name, ComposeService.service_name)
        )
        total = q.count()
        rows = q.offset(offset).limit(limit).all()
        return ComposeServicesPageOut(
            items=[
                ComposeServiceOut(
                    service_name=svc.service_name,
                    image=svc.image or "",
                    ports=list(svc.ports or []),
                    project_name=f.project_name or "",
                    file_path=f.path,
                )
                for svc, f in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


@iac_router.get("/k8s-resources", response_model=K8sManifestResourcesPageOut)
def list_k8s_manifest_resources(limit: int = 200, offset: int = 0):
    """Kubernetes manifest resource listing across every parsed k8s manifest
    file (UI Batch 6 / GitLab #62).

    Read-only (SELECT only). Empty items/total=0 when no k8s manifests have
    been parsed yet — never a 500.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = (
            s.query(K8sManifestResource, IacFile)
            .join(IacFile, IacFile.id == K8sManifestResource.iac_file_id)
            .order_by(IacFile.project_name, K8sManifestResource.kind, K8sManifestResource.name)
        )
        total = q.count()
        rows = q.offset(offset).limit(limit).all()
        return K8sManifestResourcesPageOut(
            items=[
                K8sManifestResourceOut(
                    kind=r.kind,
                    name=r.name,
                    namespace=r.namespace or "",
                    api_version=r.api_version or "",
                    project_name=f.project_name or "",
                    file_path=f.path,
                )
                for r, f in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


@iac_router.get("/terraform-resources", response_model=TerraformResourcesPageOut)
def list_terraform_resources(limit: int = 200, offset: int = 0):
    """Terraform resource listing across every parsed .tf file
    (UI Batch 6 / GitLab #62).

    Read-only (SELECT only). Empty items/total=0 when no Terraform files
    have been parsed yet — never a 500.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = (
            s.query(TerraformResource, IacFile)
            .join(IacFile, IacFile.id == TerraformResource.iac_file_id)
            .order_by(IacFile.project_name, TerraformResource.resource_type, TerraformResource.resource_name)
        )
        total = q.count()
        rows = q.offset(offset).limit(limit).all()
        return TerraformResourcesPageOut(
            items=[
                TerraformResourceOut(
                    resource_type=r.resource_type,
                    resource_name=r.resource_name,
                    project_name=f.project_name or "",
                    file_path=f.path,
                )
                for r, f in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


@iac_router.get("/ansible", response_model=AnsibleStructureOut)
def ansible_structure():
    """Ansible inventory group/host tree and playbook play list across every
    parsed inventory/playbook file (UI Batch 6 / GitLab #62).

    Read-only (SELECT only). Empty groups/plays when none have been parsed
    yet — never a 500. Unlike the other IaC deep-view endpoints this one is
    not paged: inventory/playbook structure is naturally small per project
    and reads better as one grouped tree.
    """
    with get_session() as s:
        groups = (
            s.query(AnsibleInventoryGroup, IacFile)
            .join(IacFile, IacFile.id == AnsibleInventoryGroup.iac_file_id)
            .order_by(IacFile.project_name, AnsibleInventoryGroup.name)
            .all()
        )
        group_ids = [g.id for g, _ in groups]
        hosts_by_group: dict[Any, list[AnsibleInventoryHost]] = {}
        if group_ids:
            for h in (
                s.query(AnsibleInventoryHost)
                .filter(AnsibleInventoryHost.group_id.in_(group_ids))
                .order_by(AnsibleInventoryHost.name)
                .all()
            ):
                hosts_by_group.setdefault(h.group_id, []).append(h)

        plays = (
            s.query(AnsiblePlaybookPlay, IacFile)
            .join(IacFile, IacFile.id == AnsiblePlaybookPlay.iac_file_id)
            .order_by(IacFile.project_name, AnsiblePlaybookPlay.play_index)
            .all()
        )

        return AnsibleStructureOut(
            groups=[
                AnsibleInventoryGroupOut(
                    name=g.name,
                    project_name=f.project_name or "",
                    file_path=f.path,
                    hosts=[
                        AnsibleInventoryHostOut(name=h.name)
                        for h in hosts_by_group.get(g.id, [])
                    ],
                )
                for g, f in groups
            ],
            plays=[
                AnsiblePlaybookPlayOut(
                    name=p.name or "",
                    play_index=p.play_index,
                    hosts=list(p.hosts or []),
                    project_name=f.project_name or "",
                    file_path=f.path,
                )
                for p, f in plays
            ],
        )


@iac_router.get("/ci-schedules", response_model=CiSchedulesPageOut)
def list_ci_schedules(limit: int = 200, offset: int = 0):
    """GitLab CI pipeline schedule listing (UI Batch 6 / GitLab #62) — the
    first UI surface for CiSchedule rows (previously collected but never
    exposed beyond an internal file-type count).

    Read-only (SELECT only). Empty items/total=0 when no schedules have
    been collected yet — never a 500.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_session() as s:
        q = s.query(CiSchedule).order_by(CiSchedule.project_id, CiSchedule.schedule_id)
        items, total = paginate(q, limit=limit, offset=offset)
        return CiSchedulesPageOut(
            items=[
                CiScheduleOut(
                    project_id=r.project_id,
                    schedule_id=r.schedule_id,
                    description=r.description or "",
                    ref=r.ref or "",
                    cron=r.cron or "",
                    active=r.active,
                    created_at=r.created_at,
                )
                for r in items
            ],
            total=total,
            limit=limit,
            offset=offset,
        )
