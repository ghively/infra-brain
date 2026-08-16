---
name: agent-register
description: >
  Full workflow for registering a new domain agent into the infra-brain system.
  Creates the agent file and test via /agent-scaffold, then wires it into
  supervisor.py (AGENT_REGISTRY + SKIP_HOOK) and scheduler.py (_DEFAULT_SCHEDULES).
  Runs the lc-agent-completeness subagent to verify all four wiring points.
disable-model-invocation: false
---

# /agent-register <name> [description] [--skip-hook] [--schedule <cron>]

Full registration workflow for a new infra-brain domain agent.

## Arguments
- `<name>` — snake_case domain name (e.g. `storage`, `hsm`, `backup`)
- `[description]` — one-line description of what the agent collects
- `[--skip-hook]` — include in SKIP_HOOK (for analysis/system agents that should NOT
  trigger drift detection + notifications; omit for data-collection agents)
- `[--schedule <cron>]` — override the default cron schedule. Default: `0 2 * * *`
  (daily 2am UTC for daily scans; use `0 */6 * * *` for high-frequency, `0 3 * * 0` for weekly)

---

## Step 1: Create Agent and Test Files

Run the `/agent-scaffold` skill:

```
/agent-scaffold <name> [description]
```

This creates:
- `src/infra_brain/agents/<name>.py`
- `tests/agents/test_<name>.py`

Verify both files exist and the test runs:
```bash
.venv/bin/python -m pytest tests/agents/test_<name>.py -v
```

---

## Step 2: Wire into AGENT_REGISTRY (supervisor.py)

Add the import at the top of `src/infra_brain/supervisor.py` (keep alphabetical order):

```python
from infra_brain.agents.<name> import <ClassName>Agent
```

Add to AGENT_REGISTRY:

```python
AGENT_REGISTRY["<name>"] = <ClassName>Agent
```

**SKIP_HOOK decision:**
- **Omit from SKIP_HOOK** if: this agent collects infrastructure state (VMs, hosts, pods,
  networks, vulnerabilities). It SHOULD trigger drift detection + notifications.
- **Add to SKIP_HOOK** if: this agent is analysis/system-level (produces reports, reads
  existing data, doesn't create ResourceSnapshot rows). Examples: DriftDetector, RootCauseAgent.

If adding to SKIP_HOOK, edit the set in `build_supervisor()`:
```python
SKIP_HOOK = {
    "drift", "notification", ...,
    "<name>",   # add here
}
```

---

## Step 3: Add Default Schedule (scheduler.py)

Add to `_DEFAULT_SCHEDULES` in `src/infra_brain/scheduler.py`:

```python
"<name>": "<cron_expression>",
```

**Schedule guidelines:**

| Agent Type | Cron | UTC Description |
|---|---|---|
| High-frequency infra scan (hosts, networks) | `0 */6 * * *` | Every 6 hours |
| Daily heavy scan (cloud, CI/CD, vuln) | `0 2 * * *` | 2am daily |
| Inventory/reconcile | `0 5 * * *` | 5am daily |
| LLM-driven weekly analysis | `0 3 * * 0` | Sunday 3am |
| Post-analysis (runs after another agent) | Schedule 1h after its dependency |

Verify no two agents share the exact same time (they'll compete for Redis locks and
one will skip). Check existing schedules in the dict before choosing a time.

---

## Step 4: Update CLAUDE.md (if supervisor.py agent count changed)

Update the agent count reference in `CLAUDE.md`:
```
Routes all 25 agents → Routes all 26 agents
```

---

## Step 5: Validate All Four Wiring Points

Invoke the `lc-agent-completeness` subagent to verify everything is correct:

```
Use the lc-agent-completeness subagent to review the <name> agent registration.
```

The subagent will check:
- Agent file structure and read-only constraint
- Test file coverage (5 required test cases)
- AGENT_REGISTRY key and import
- SKIP_HOOK classification
- Schedule timing and cron validity
- Domain string consistency across all files

---

## Step 6: Run Tests

```bash
# New agent tests
.venv/bin/python -m pytest tests/agents/test_<name>.py -v

# Supervisor routing (ensures AGENT_REGISTRY is consistent)
.venv/bin/python -m pytest tests/agents/test_supervisor.py -v

# Full suite (check nothing regressed)
.venv/bin/python -m pytest tests/ -q --tb=short
```

---

## Step 7: Optional — Add to Tools Registry

If this agent needs to be invokable from `DiscoveryAgent` or `IntegrationAgent`'s
tool list, add it to `src/infra_brain/agents/discovery.py`:

```python
from infra_brain.tools.<name>_tools import <Name>Tool
# ...
_DISCOVERY_TOOLS = [
    ...,
    <Name>Tool(),
]
```

---

## Checklist

- [ ] `src/infra_brain/agents/<name>.py` created with correct structure
- [ ] `tests/agents/test_<name>.py` created (5 required test cases)
- [ ] Imported in `supervisor.py`
- [ ] Added to `AGENT_REGISTRY`
- [ ] SKIP_HOOK decision made and implemented
- [ ] Added to `_DEFAULT_SCHEDULES` with valid cron
- [ ] No schedule time collision with existing agents
- [ ] lc-agent-completeness subagent: COMPLETE verdict
- [ ] All tests pass
- [ ] CLAUDE.md agent count updated (if changed)

## Notes
- Do NOT skip the lc-agent-completeness review for production agents
- The `domain` string on the class attribute must exactly match the AGENT_REGISTRY key
- For agents requiring external credentials, add the config field to `config.py` and
  `.env.example` before creating the agent — the agent will fail at first run otherwise
