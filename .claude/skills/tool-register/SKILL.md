---
name: tool-register
description: >-
  Register a new LangChain @tool for an infra-brain domain agent.
  Creates src/infra_brain/tools/<name>.py from the project template,
  creates tests/tools/test_<name>.py with 4 required test cases, wires
  the tool into the target agent's tool list, runs lc-safety-reviewer,
  and runs the test suite. Parallel workflow to /agent-register.
disable-model-invocation: false
---

# /tool-register `<name>` `<agent>` `[description]`

Register a new read-only `@tool` for an infra-brain domain agent. This skill
creates the tool file, test file, wires the import, reviews for safety, and
verifies tests pass — end to end.

## Arguments

- `<name>` — snake_case tool name (e.g. `ansible_disk_usage`, `gitlab_pipeline_status`)
- `<agent>` — domain key of the agent that uses this tool (e.g. `linux`, `cicd`, `k8s`)
- `[description]` — LLM-visible docstring for the `@tool` decorator.
  If omitted, one will be inferred from the name.

---

## Step 1: Research Existing Patterns

Before writing anything, read the tool most similar to what you're building:

- **Ansible/SSH-based**: read `src/infra_brain/tools/ansible.py`
- **HTTP/REST API**: read `src/infra_brain/tools/gitlab.py`
- **SNMP/network**: read `src/infra_brain/tools/snmp.py`
- **vSphere/VMware**: read `src/infra_brain/tools/vsphere.py`

Also read the target agent to understand how tools are currently wired:
```bash
cat src/infra_brain/agents/<agent>.py
```

---

## Step 2: Create `src/infra_brain/tools/<name>.py`

Follow this template exactly. Input validation via `_SAFE_INPUT` regex is non-negotiable
— every tool that accepts user-supplied input must reject unsafe characters before
passing them to external systems.

```python
import re
from langchain_core.tools import tool

_SAFE_INPUT = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def _<name>_impl(target: str) -> dict:
    """Internal logic — separated from @tool wrapper for testability."""
    if not _SAFE_INPUT.match(target):
        raise ValueError(f"[read-only] unsafe input characters: {target!r}")
    # TODO: implementation
    return {}


@tool
def <name>_tool(target: str) -> dict:
    """<description> — read-only."""
    return _<name>_impl(target)
```

**Rules (enforced by lc-safety-reviewer in Step 5):**
- Input validation regex must appear before any external call
- No `subprocess.Popen` with `shell=True`
- No write/create/delete/destroy operations
- Return type must be `dict` or `list[dict]`
- Docstring becomes the LLM-visible tool description — make it precise

---

## Step 3: Create `tests/tools/test_<name>.py`

Four test cases are required. The `test-coverage-guard.py` hook will warn if this file
is missing when the tool file is created.

```python
import pytest
from unittest.mock import patch
from infra_brain.tools.<name> import <name>_tool, _<name>_impl


def test_<name>_success():
    """Valid input returns structured dict."""
    with patch("infra_brain.tools.<name>._<name>_impl") as mock:
        mock.return_value = {"key": "value"}
        result = <name>_tool.invoke({"target": "valid-target"})
    assert result == {"key": "value"}


def test_<name>_empty():
    """Valid input with no data returns empty dict (not None, not exception)."""
    with patch("infra_brain.tools.<name>._<name>_impl") as mock:
        mock.return_value = {}
        result = <name>_tool.invoke({"target": "empty-target"})
    assert result == {}


def test_<name>_backend_exception():
    """Backend failure raises RuntimeError that propagates."""
    with patch("infra_brain.tools.<name>._<name>_impl") as mock:
        mock.side_effect = RuntimeError("backend unavailable")
        with pytest.raises(RuntimeError, match="backend unavailable"):
            <name>_tool.invoke({"target": "valid-target"})


def test_<name>_unsafe_input():
    """Input with shell-unsafe characters raises ValueError."""
    with pytest.raises(ValueError, match="unsafe input"):
        _<name>_impl("../../etc/passwd")
```

---

## Step 4: Wire into Agent

Add the tool import and include it in the agent's tool list.

Locate the agent file:
```bash
cat src/infra_brain/agents/<agent>.py
```

Find where tools are defined (typically a list passed to the ReAct agent or used
in the `collect()` method). Add:

```python
from infra_brain.tools.<name> import <name>_tool

# In the class or collect() method where tools are assembled:
tools = [...existing tools..., <name>_tool]
```

If the agent uses a `create_react_agent` pattern:
```python
self._agent = create_react_agent(self.llm, tools=[..., <name>_tool])
```

---

## Step 5: Safety Review

Spawn `lc-safety-reviewer` on the new tool file:

```
Agent(
  description="Safety review of new <name>_tool",
  subagent_type="lc-safety-reviewer",
  prompt="Review src/infra_brain/tools/<name>.py for safety violations.
          Focus on: input validation regex coverage, read-only enforcement,
          no shell injection via subprocess, structured return type."
)
```

Address any CRITICAL or HIGH findings before proceeding.

---

## Step 6: Run Tests

```bash
python -m pytest tests/tools/test_<name>.py -v --tb=short
```

Also run the agent's own tests to confirm the wiring didn't break anything:
```bash
python -m pytest tests/agents/test_<agent>.py -v --tb=short
```

---

## Checklist

- [ ] Tool file created at `src/infra_brain/tools/<name>.py`
- [ ] `_SAFE_INPUT` regex present and applied before any external call
- [ ] Docstring is precise and describes read-only behavior
- [ ] Test file created at `tests/tools/test_<name>.py` (4 cases)
- [ ] Tool imported and added to `<agent>` tool list
- [ ] `lc-safety-reviewer` run — no CRITICAL/HIGH findings
- [ ] `pytest tests/tools/test_<name>.py` green
- [ ] `pytest tests/agents/test_<agent>.py` still green
