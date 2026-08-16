"""Wave 4 item 4.1a: the ETL/agent hierarchy split.

ETLConnector is the LLM-free collection base; BaseAgent layers the lazy model
on top; every previously importable name still resolves from agents.base.
"""

import importlib
import inspect


def _module_source(mod_name: str) -> str:
    return inspect.getsource(importlib.import_module(mod_name))


def test_etl_connector_has_no_llm():
    from infra_brain.etl.base import ETLConnector

    assert not hasattr(ETLConnector, "llm")
    src = _module_source("infra_brain.etl.base")
    assert "from infra_brain.llm import" not in src
    assert "import langgraph" not in src and "from langgraph" not in src


def test_agents_base_reexports_are_same_objects():
    from infra_brain.agents import base as agents_base
    from infra_brain.etl import base as etl_base

    assert agents_base.CollectionResult is etl_base.CollectionResult
    assert agents_base.CollectorSkipped is etl_base.CollectorSkipped
    assert agents_base.count_drift_events_for_run is etl_base.count_drift_events_for_run
    assert issubclass(agents_base.BaseAgent, etl_base.ETLConnector)


def test_base_agent_still_has_lazy_llm_property():
    from infra_brain.agents.base import BaseAgent

    assert isinstance(vars(BaseAgent).get("llm"), property)
