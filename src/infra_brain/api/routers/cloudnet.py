"""infra_brain.api.routers.cloudnet — Cloud / Kubernetes / Network typed
views API router (UI Batch 7 / GitLab #63).

These three domains (cloud, k8s, net) are POC-disabled by default (TRK-041)
— the AWS collector is off, no k8s cluster context is configured, and SNMP
discovery is idle. This router is UI/route plumbing built ahead of that
decision, per the 2026-07-23 UI-and-KG-expansion design spec: it must render
a clean empty overview when every table is empty (the common case today)
and correct typed data the moment any of these domains starts collecting.

Import rules:
  * MAY import from infra_brain.api.schemas, infra_brain.db.models,
    infra_brain.db.session, infra_brain.dashboard_auth.
  * Must NOT import from infra_brain.dashboard_api (would be circular).
  * Must NOT import from other router modules.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from infra_brain.api.schemas import (
    CloudNetOverviewOut,
    CloudNetSummaryOut,
    CloudResourceOut,
    K8sDeploymentOut,
    K8sNodeOut,
    K8sPodOut,
    NetDeviceOut,
)
from infra_brain.dashboard_auth import require_session
from infra_brain.db.models import (
    CloudResource,
    K8sDeployment,
    K8sNode,
    K8sPod,
    NetDevice,
)
from infra_brain.db.session import get_session

# Dedicated Cloud/K8s/Net typed view. Same session gate as the other
# dedicated domain views (vsphere_router, iac_router) — surfaces the
# cloud_resources / k8s_nodes / k8s_pods / k8s_deployments / net_devices
# relational model with its own purpose-built response shape rather than
# squeezing it into the generic Resource contract. Read-only; renders an
# empty state cleanly while these domains stay POC-disabled (TRK-041).
cloudnet_router = APIRouter(
    prefix="/api/cloudnet",
    tags=["cloudnet"],
    dependencies=[Depends(require_session)],
)

# These domains are POC-disabled (TRK-041): a handful of rows at most in any
# realistic near-term deployment, so a flat unpaged list (capped generously)
# is simpler than full pagination and matches the vsphere_overview precedent
# for VMs (a single capped list + a summary total).
_ROW_LIMIT = 500


@cloudnet_router.get("/overview", response_model=CloudNetOverviewOut)
def cloudnet_overview() -> CloudNetOverviewOut:
    """Assemble the Cloud/K8s/Net estate for the dashboard's typed views.

    Read-only (SELECT only). Returns every collected row (capped at
    ``_ROW_LIMIT`` per table) plus a ``summary`` count block. Renders
    cleanly when every table is empty — the default state while cloud/k8s/
    net collection stays POC-disabled (TRK-041).
    """
    with get_session() as s:
        cloud_rows = s.query(CloudResource).order_by(CloudResource.name).limit(_ROW_LIMIT).all()
        node_rows = s.query(K8sNode).order_by(K8sNode.cluster, K8sNode.name).limit(_ROW_LIMIT).all()
        pod_rows = s.query(K8sPod).order_by(K8sPod.cluster, K8sPod.namespace, K8sPod.name).limit(_ROW_LIMIT).all()
        deploy_rows = (
            s.query(K8sDeployment)
            .order_by(K8sDeployment.cluster, K8sDeployment.namespace, K8sDeployment.name)
            .limit(_ROW_LIMIT)
            .all()
        )
        net_rows = s.query(NetDevice).order_by(NetDevice.name).limit(_ROW_LIMIT).all()

        return CloudNetOverviewOut(
            cloud_resources=[
                CloudResourceOut(
                    provider=r.provider,
                    cloud_type=r.cloud_type,
                    cloud_id=r.cloud_id,
                    name=r.name,
                    region=r.region or "",
                    state=r.state or "",
                )
                for r in cloud_rows
            ],
            k8s_nodes=[
                K8sNodeOut(
                    cluster=r.cluster or "",
                    name=r.name,
                    status=r.status or "",
                    roles=list(r.roles or []),
                    kubelet_version=r.kubelet_version or "",
                    arch=r.arch or "",
                )
                for r in node_rows
            ],
            k8s_pods=[
                K8sPodOut(
                    cluster=r.cluster or "",
                    namespace=r.namespace,
                    name=r.name,
                    phase=r.phase or "",
                    node_name=r.node_name or "",
                )
                for r in pod_rows
            ],
            k8s_deployments=[
                K8sDeploymentOut(
                    cluster=r.cluster or "",
                    namespace=r.namespace,
                    name=r.name,
                    replicas=r.replicas,
                    ready=r.ready,
                    available=r.available,
                )
                for r in deploy_rows
            ],
            net_devices=[
                NetDeviceOut(
                    ip=r.ip,
                    name=r.name,
                    sysname=r.sysname or "",
                    contact=r.contact or "",
                    location=r.location or "",
                )
                for r in net_rows
            ],
            summary=CloudNetSummaryOut(
                cloud_resource_count=len(cloud_rows),
                k8s_node_count=len(node_rows),
                k8s_pod_count=len(pod_rows),
                k8s_deployment_count=len(deploy_rows),
                net_device_count=len(net_rows),
            ),
        )
