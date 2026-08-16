"""Wave 4 item 4.1b: deterministic collectors are pure ETL connectors.

Each migrated collector must (a) subclass ETLConnector, (b) be structurally
unable to construct an LLM (no .llm attribute anywhere on the class), and
(c) contain no LLM/graph imports in its module source. This is the CI guard
that keeps the ETL/agent split from regressing (F-009, F-004.4/R1).
"""

import importlib
import inspect

import pytest

ETL_COLLECTORS = {
    "cicd": "CICDAgent",
    "cloud": "CloudAgent",
    "eol": "EOLAgent",
    "fleet_health": "FleetHealthReporter",
    "iac": "IaCAgent",
    "k8s": "K8sAgent",
    "linux": "LinuxAgent",
    "net": "NetAgent",
    "octopus": "OctopusAgent",
    "vsphere": "VsphereAgent",
    "vuln": "VulnAgent",
    "windows": "WindowsAgent",
}


@pytest.mark.parametrize("module_name,class_name", sorted(ETL_COLLECTORS.items()))
def test_collector_is_llm_free_etl_connector(module_name, class_name):
    from infra_brain.etl.base import ETLConnector

    mod = importlib.import_module(f"infra_brain.agents.{module_name}")
    cls = getattr(mod, class_name)
    assert issubclass(cls, ETLConnector)
    assert not hasattr(cls, "llm"), (
        f"{class_name} must not be able to construct an LLM (ETL split, F-009)"
    )
    src = inspect.getsource(mod)
    assert "from infra_brain.llm import" not in src
    assert "llm_base" not in src
    assert "StateGraph" not in src
