from unittest.mock import patch, MagicMock

import pytest

from infra_brain.agents.base import CollectorSkipped
from infra_brain.agents.k8s import K8sAgent


def test_collect_raises_skipped_when_kubernetes_unavailable():
    with patch.dict("sys.modules", {"kubernetes": None}):
        import importlib
        import infra_brain.agents.k8s as k8s_mod

        importlib.reload(k8s_mod)
        agent = k8s_mod.K8sAgent.__new__(k8s_mod.K8sAgent)
        with pytest.raises(CollectorSkipped):
            agent.collect()


def test_collect_raises_skipped_when_kubeconfig_missing():
    agent = K8sAgent.__new__(K8sAgent)
    mock_k8s = MagicMock()
    mock_k8s.config.load_incluster_config.side_effect = Exception("not in cluster")
    mock_k8s.config.load_kube_config.side_effect = Exception("no kubeconfig")
    with patch("infra_brain.agents.k8s.kubernetes", mock_k8s):
        with pytest.raises(CollectorSkipped):
            agent.collect()


def test_collect_returns_nodes_and_pods():
    agent = K8sAgent.__new__(K8sAgent)
    mock_k8s = MagicMock()
    mock_k8s.config.load_incluster_config = MagicMock()

    mock_node = MagicMock()
    mock_node.metadata.name = "node1"
    mock_node.status.conditions = []
    mock_node.status.capacity = {}

    mock_pod = MagicMock()
    mock_pod.metadata.name = "pod1"
    mock_pod.metadata.namespace = "default"
    mock_pod.status.phase = "Running"
    mock_pod.spec.containers = []

    mock_k8s.client.CoreV1Api.return_value.list_node.return_value.items = [mock_node]
    mock_k8s.client.CoreV1Api.return_value.list_pod_for_all_namespaces.return_value.items = [
        mock_pod
    ]
    mock_k8s.client.AppsV1Api.return_value.list_deployment_for_all_namespaces.return_value.items = []

    with patch("infra_brain.agents.k8s.kubernetes", mock_k8s):
        result = agent.collect()

    assert isinstance(result, list)
    assert any(item.get("type") == "k8s_node" for item in result)
