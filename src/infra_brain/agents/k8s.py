import logging
from datetime import timedelta

from infra_brain.etl.base import CollectorSkipped, ETLConnector
from infra_brain.db.models import K8sDeployment, K8sNode, K8sPod
from infra_brain.db.session import get_session
from infra_brain.etl.spec import AgentSpec, Tier

logger = logging.getLogger(__name__)

try:
    import kubernetes
except ImportError:
    kubernetes = None

# Page size for list calls. The kubernetes API paginates large list results and
# returns a ``metadata._continue`` token; without following it, later pages are
# silently dropped on large clusters. _paginate() loops on that token.
_PAGE_LIMIT = 500


def _paginate(list_fn, **kwargs):
    """Yield every item across all pages of a kubernetes list call.

    The API returns at most ``limit`` items plus a ``metadata._continue`` token
    when more remain. We loop, feeding the token back as ``_continue`` until it is
    empty/absent. Without this, large clusters drop everything past the first page.
    """
    cont = None
    while True:
        call_kwargs = dict(kwargs)
        call_kwargs["limit"] = _PAGE_LIMIT
        if cont:
            call_kwargs["_continue"] = cont
        resp = list_fn(**call_kwargs)
        for item in resp.items:
            yield item
        cont = getattr(resp.metadata, "_continue", None) if resp.metadata else None
        # The real API returns a non-empty str token when more pages remain, or
        # None/"" when exhausted. Require a str so a non-conforming value (e.g. a
        # bare MagicMock in tests) terminates instead of looping forever.
        if not isinstance(cont, str) or not cont:
            break


class K8sAgent(ETLConnector):
    spec = AgentSpec(
        domain="k8s",
        tier=Tier.COLLECTOR,
        schedule="15 */6 * * *",
        max_staleness=timedelta(hours=8),
        # 2026-08-12: retired (was POC-disabled 2026-07-15 via dispatchable=False
        # plus a commented-out supervisor._AGENT_SPECS entry). Kubernetes is an
        # enterprise remnant — nothing in this home lab runs a cluster. Migrated
        # onto the first-class switch so the domain stays visible as
        # deliberately off; re-enable with COLLECTION_REVIVED_DOMAINS=k8s
        # (collect() still self-skips if the kubernetes package is absent).
        #
        # P5 REVIVAL OBLIGATION: k8s is a HOST-BEARING leg (K8sNode feeds
        # host_reconcile._SOURCE_KEYS "k8s"). Reviving it WITHOUT adding a
        # NodeSpec(..., is_host_identity=True) here AND the matching entry in
        # graph_phase3.HOST_NODE_TYPES leaves every k8s↔anything identity pair
        # invisible to the resolver — not queued for a human, silently absent.
        # No speculative NodeSpec is declared now (zero rows, ever).
        retired=True,
    )

    def collect(self, scope: str = "all") -> list[dict]:
        self._last_items = []
        if kubernetes is None:
            raise CollectorSkipped("kubernetes package not installed")

        try:
            kubernetes.config.load_incluster_config()
        except Exception:
            try:
                kubernetes.config.load_kube_config()
            except Exception as exc:
                raise CollectorSkipped(f"no kubeconfig available: {exc}") from exc

        items = []

        # Nodes (paginated)
        try:
            core_v1 = kubernetes.client.CoreV1Api()
            for node in _paginate(core_v1.list_node):
                name = node.metadata.name
                conditions = {c.type: c.status for c in (node.status.conditions or [])}
                labels = node.metadata.labels or {}
                roles = [
                    k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")
                ]
                node_info = node.status.node_info
                items.append(
                    {
                        "name": name,
                        "type": "k8s_node",
                        "data": {
                            "labels": labels,
                            "roles": roles,
                            "ready": conditions.get("Ready", "Unknown"),
                            "arch": node_info.architecture if node_info else "",
                            "os": node_info.os_image if node_info else "",
                            "kubelet_version": node_info.kubelet_version if node_info else "",
                        },
                    }
                )
        except Exception as exc:
            logger.warning("Failed to list nodes: %s", exc)

        # Pods (all namespaces or scoped; paginated)
        try:
            core_v1 = kubernetes.client.CoreV1Api()
            if scope == "all":
                pod_iter = _paginate(core_v1.list_pod_for_all_namespaces)
            else:
                pod_iter = _paginate(core_v1.list_namespaced_pod, namespace=scope)
            for pod in pod_iter:
                items.append(
                    {
                        "name": f"{pod.metadata.namespace}/{pod.metadata.name}",
                        "type": "k8s_pod",
                        "data": {
                            "namespace": pod.metadata.namespace,
                            "phase": pod.status.phase if pod.status else "Unknown",
                            "node_name": pod.spec.node_name if pod.spec else "",
                            "containers": [c.name for c in (pod.spec.containers or [])]
                            if pod.spec
                            else [],
                        },
                    }
                )
        except Exception as exc:
            logger.warning("Failed to list pods: %s", exc)

        # Deployments (paginated)
        try:
            apps_v1 = kubernetes.client.AppsV1Api()
            for dep in _paginate(apps_v1.list_deployment_for_all_namespaces):
                items.append(
                    {
                        "name": f"{dep.metadata.namespace}/{dep.metadata.name}",
                        "type": "k8s_deployment",
                        "data": {
                            "namespace": dep.metadata.namespace,
                            "replicas": dep.spec.replicas if dep.spec else 0,
                            "ready_replicas": dep.status.ready_replicas if dep.status else 0,
                            "available_replicas": dep.status.available_replicas
                            if dep.status
                            else 0,
                        },
                    }
                )
        except Exception as exc:
            logger.warning("Failed to list deployments: %s", exc)

        # Cache for the relational detail-write phase.
        self._last_items = items
        return items

    # ------------------------------------------------------------------
    # Relational detail-write phase (MR5)
    # ------------------------------------------------------------------

    def _detail_writers(self, scope, result):
        # Surface a detail-write failure on the CollectionRun (no silent data
        # loss) via ETLConnector.run()'s _write_details.
        return [self._write_k8s_details]

    def _write_k8s_details(self) -> None:
        """Populate k8s_nodes / k8s_pods / k8s_deployments from the collected items.

        Idle-safety: collect() returns [] (and caches []) when there is no cluster
        in context, so there is simply nothing to write.
        """
        items = getattr(self, "_last_items", None) or []
        if not items:
            return

        # cluster is None for the single in-context cluster (kubeconfig current-context).
        cluster = getattr(self.settings, "k8s_cluster_name", None) or None

        with get_session() as session:
            for item in items:
                try:
                    with session.begin_nested():
                        self._upsert_k8s_item(session, item, cluster)
                except Exception as exc:
                    logger.warning(
                        "K8sAgent: skipping bad %s item %r: %s",
                        item.get("type"),
                        item.get("name"),
                        exc,
                    )
            session.commit()

    def _upsert_k8s_item(self, session, item: dict, cluster) -> None:
        item_type = item.get("type", "")
        data = item.get("data", {}) or {}
        name = item.get("name", "")
        resource_id = self._resource_id(session, item_type, name)

        if item_type == "k8s_node":
            mapped = {"ready", "roles", "kubelet_version", "arch", "os"}
            row = {
                "resource_id": resource_id,
                "cluster": cluster,
                "name": name,
                "status": data.get("ready"),
                "roles": data.get("roles") or [],
                "kubelet_version": data.get("kubelet_version"),
                "arch": data.get("arch"),
                "os_image": data.get("os"),
                "details": {k: v for k, v in data.items() if k not in mapped} or None,
            }
            self._upsert_detail(session, K8sNode, row, ["cluster", "name"])

        elif item_type == "k8s_pod":
            # ``name`` is "namespace/name"; the relational name column is the bare name.
            namespace = data.get("namespace") or ""
            bare = name.split("/", 1)[1] if "/" in name else name
            mapped = {"namespace", "phase", "node_name"}
            row = {
                "resource_id": resource_id,
                "cluster": cluster,
                "namespace": namespace,
                "name": bare,
                "phase": data.get("phase"),
                "node_name": data.get("node_name") or None,
                "details": {k: v for k, v in data.items() if k not in mapped} or None,
            }
            self._upsert_detail(session, K8sPod, row, ["cluster", "namespace", "name"])

        elif item_type == "k8s_deployment":
            namespace = data.get("namespace") or ""
            bare = name.split("/", 1)[1] if "/" in name else name
            mapped = {"namespace", "replicas", "ready_replicas", "available_replicas"}
            row = {
                "resource_id": resource_id,
                "cluster": cluster,
                "namespace": namespace,
                "name": bare,
                "replicas": data.get("replicas"),
                "ready": data.get("ready_replicas"),
                "available": data.get("available_replicas"),
                "details": {k: v for k, v in data.items() if k not in mapped} or None,
            }
            self._upsert_detail(session, K8sDeployment, row, ["cluster", "namespace", "name"])
        else:
            logger.debug("K8sAgent: no relational table for type %s", item_type)
