---
name: agent-scaffold
description: >
  Create a new infra-brain domain agent and its test file from the project template.
  Generates src/infra_brain/agents/<name>.py and tests/agents/test_<name>.py, then
  runs ruff and pytest to verify the scaffold is clean.
disable-model-invocation: false
---

# /agent-scaffold <name> [description]

Creates a new domain agent + matching test file following the infra-brain conventions.

## Arguments
- `<name>` — snake_case agent name (e.g. `storage`, `dns`, `backup`)
- `[description]` — one-line description of what this agent collects (used in docstrings)

## Steps

### 1. Create the agent file

Create `src/infra_brain/agents/<name>.py` with this exact structure:

```python
"""<description>

Collects <domain> resources and writes ResourceSnapshot rows to Postgres.
Drift detection runs automatically via BaseAgent.run().
"""
import logging
from typing import Any

from infra_brain.agents.base import BaseAgent

logger = logging.getLogger(__name__)


class <ClassName>Agent(BaseAgent):
    domain = "<name>"

    def collect(self, scope: str = "all") -> list[dict[str, Any]]:
        """Return list of resource dicts: {name, type, data: {...}}

        Each dict becomes one Resource row + one Snapshot row in Postgres.
        This method must be side-effect-free — never mutate external state here.
        """
        resources: list[dict[str, Any]] = []

        # TODO: implement collection logic
        # Example:
        # for item in fetch_<name>_resources():
        #     resources.append({
        #         "name": item.name,
        #         "type": "<resource_type>",
        #         "data": {"key": item.value, ...},
        #     })

        logger.info("%s: collected %d resources (scope=%s)", self.domain, len(resources), scope)
        return resources
```

Name the class `<PascalCase>Agent` (e.g. `StorageAgent`, `DnsAgent`).

### 2. Create the test file

Create `tests/agents/test_<name>.py`:

```python
"""Tests for <ClassName>Agent."""
import pytest
from unittest.mock import MagicMock, patch

from infra_brain.agents.<name> import <ClassName>Agent


@pytest.fixture
def agent(mock_settings, mock_db_session):
    """Agent with injected fake LLM and disabled DB writes."""
    a = <ClassName>Agent()
    a.llm = MagicMock()  # prevent real LLM calls
    return a


class TestCollect:
    def test_returns_list(self, agent):
        """collect() must always return a list (even if empty)."""
        result = agent.collect()
        assert isinstance(result, list)

    def test_empty_when_no_resources(self, agent):
        """collect() returns [] when the upstream source has nothing."""
        # TODO: mock the upstream to return nothing
        result = agent.collect()
        assert result == []

    def test_resource_shape(self, agent):
        """Each returned item must have name, type, and data keys."""
        # TODO: mock the upstream to return one item
        result = agent.collect()
        if result:
            for item in result:
                assert "name" in item
                assert "type" in item
                assert "data" in item

    def test_collect_exception_does_not_propagate(self, agent):
        """BaseAgent.run() catches exceptions — collect() may raise freely."""
        with patch.object(agent, "collect", side_effect=RuntimeError("upstream down")):
            run_result = agent.run()
        assert run_result.status == "failed"
        assert "upstream down" in run_result.errors[0]


class TestDomain:
    def test_domain_is_set(self, agent):
        assert agent.domain == "<name>"

    def test_callbacks_wired(self, agent):
        """build_callbacks() must be called — safety layer must be active."""
        assert agent.callbacks is not None
        assert len(agent.callbacks) > 0
```

Replace all `<name>` and `<ClassName>` placeholders with the actual values.

### 3. Validate

Run both validation steps:

```bash
# Lint
.venv/bin/python -m ruff check src/infra_brain/agents/<name>.py tests/agents/test_<name>.py --fix

# Test (should pass — at least the domain/callbacks tests)
.venv/bin/python -m pytest tests/agents/test_<name>.py -v
```

### 4. Register (if using the LangGraph supervisor)

Add the new agent to `src/infra_brain/supervisor.py`:
- Import the agent class
- Add it to the agent registry / routing table
- Run `pytest tests/agents/test_supervisor.py -v` to verify routing

### 5. Schedule (optional)

Add a cron job in `src/infra_brain/scheduler.py` if the agent should run on a schedule.

## Notes
- Do NOT add `llm_role` unless this agent needs LLM reasoning (check `agents/llm_base.py`
  for the pattern). Simple collectors don't need LLM calls.
- The `collect()` method must be read-only. The safety callbacks will catch mutation
  attempts, but collecting is cleaner when the method itself makes no writes.
- `conftest.py` provides `mock_settings` and `mock_db_session` fixtures — use them.
