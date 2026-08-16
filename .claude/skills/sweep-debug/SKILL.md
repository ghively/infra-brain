---
name: sweep-debug
description: >
  Diagnose a failed, empty, or anomalous infra-brain sweep. Walks through the 5-layer
  triage stack: collection_runs DB rows, audit_log DLP/readonly violations, Redis dedup
  state, agent exception logs, and LangSmith trace. Use when a sweep produces unexpected
  results or silently returns nothing.
disable-model-invocation: false
---

# /sweep-debug [domain] [run_id]

Diagnose why a sweep produced empty results, failed, or produced unexpected output.

## Arguments
- `[domain]` — agent domain to debug (e.g. `linux`, `cloud`, `vuln`). Omit to check
  the most recent failed run across all domains.
- `[run_id]` — specific `collection_runs.id` UUID. Omit to use the most recent run.

---

## Layer 1: Collection Run Status

Check what the DB recorded about the run:

```sql
-- Most recent run for a domain:
SELECT id, domain, trigger_type, scope, status, resources_found,
       started_at, completed_at,
       EXTRACT(EPOCH FROM (completed_at - started_at)) AS duration_sec,
       error_message
FROM collection_runs
WHERE domain = '<domain>'
ORDER BY started_at DESC
LIMIT 5;

-- Or check a specific run_id:
SELECT * FROM collection_runs WHERE id = '<run_id>';
```

**What to look for:**
- `status = 'failed'` → check `error_message` column
- `status = 'running'` with old `started_at` → stuck job (Redis lock may be held)
- `resources_found = 0` with `status = 'ok'` → upstream returned nothing (see Layer 3)
- `duration_sec > 300` → agent timeout or slow upstream

---

## Layer 2: Audit Log — DLP and Read-Only Violations

```sql
-- Check for callback violations during the run window:
SELECT event_type, agent_domain, tool_name, blocked, redacted_fields,
       created_at, details
FROM audit_log
WHERE agent_domain = '<domain>'
  AND created_at >= (SELECT started_at FROM collection_runs WHERE id = '<run_id>')
ORDER BY created_at;
```

**What to look for:**
- `blocked = true` + `event_type = 'readonly_violation'` → agent tried to mutate infra;
  `ReadOnlyToolValidator` blocked it. The collect() method has a write call that shouldn't
  be there.
- `redacted_fields` non-empty → DLP blocked PII/secret from reaching the LLM.
  If the redaction was incorrect, check `dlp.py` patterns.
- `event_type = 'tool_call'` volume much higher than expected → agent is in a loop

---

## Layer 3: Redis Dedup State

The dedup lock prevents concurrent sweeps for the same domain. A stuck lock means
subsequent sweeps skip silently.

```bash
# Check if a dedup lock is held for the domain:
.venv/bin/python -c "
from infra_brain.dedup import get_redis
r = get_redis()
key = f'infra_brain:lock:{domain}'  # replace {domain}
ttl = r.ttl(key)
val = r.get(key)
if val:
    print(f'LOCK HELD: {key!r} = {val!r}, TTL={ttl}s')
else:
    print(f'No lock held for {key!r}')
"

# Force-release a stuck lock (only if you're sure the job is dead):
# r.delete(key)
```

**What to look for:**
- TTL = -1 → lock has no expiry (set without TTL — bug in dedup.py)
- TTL = -2 → key doesn't exist (no lock, not the problem)
- TTL > 0 → job is running or was killed without releasing the lock

---

## Layer 4: Agent Exception in Application Logs

```bash
# Check recent logs for the domain (adjust log path or use kubectl):
grep -i "domain=${DOMAIN}\|\\[${DOMAIN}\\]" logs/infra_brain.log | tail -50

# In Kubernetes:
kubectl logs -n infra-brain deployment/agent-core --since=1h | grep -i "${DOMAIN}"

# For the scheduler pod:
kubectl logs -n infra-brain deployment/scheduler --since=1h | grep -i "${DOMAIN}"
```

**Common error patterns:**
- `ConnectionRefusedError` → upstream (VMware, GitLab, SNMP host) is unreachable
- `AuthenticationError` / `401` / `403` → credential expired or rotated in Bitwarden
  but not reloaded (pod needs restart to pick up new secrets from bws_bootstrap.sh)
- `TimeoutError` → upstream too slow; check `config.py` timeout settings for this domain
- `PermissionError` from ReadOnlyToolValidator → see Layer 2

---

## Layer 5: LangSmith Trace (LLM-Driven Agents Only)

For agents that use LLM reasoning (DiscoveryAgent, DriftLearningAgent, VulnTriageAgent,
ComplianceAgent, RootCauseAgent, LearningFeedbackAgent, RemediationAgent, InventoryReconcileAgent):

```python
# Check if LangSmith tracing is enabled:
.venv/bin/python -c "
from infra_brain.config import get_settings
s = get_settings()
print('LangSmith enabled:', s.langsmith_tracing)
print('Project:', s.langsmith_project)
print('Endpoint:', s.langsmith_endpoint)
"
```

If enabled, find the trace at: `https://smith.langchain.com/projects/<langsmith_project>`

**What to look for in the trace:**
- LLM call with empty tool_calls → agent couldn't decide what tool to use; the prompt
  may be too vague or the tool descriptions aren't matching the query
- Tool call with error in output → tool raised an exception that was swallowed
- Very high token count on a single call → context window stuffed with unfiltered data
- Zero LLM calls → agent exited before reaching the LLM (early return in collect())

---

## Triage Decision Tree

```
sweep returns empty / fails
│
├─ collection_runs.status = 'failed'
│  └─ error_message → fix the exception
│
├─ collection_runs.status = 'ok', resources_found = 0
│  ├─ audit_log has blocked=true → fix the read-only/DLP violation
│  ├─ Redis lock stuck → force-release, investigate why job didn't release
│  └─ No violations → upstream returned nothing (check credentials, connectivity)
│
├─ No row in collection_runs at all
│  ├─ Redis lock held from prior run → force-release
│  └─ Domain not in AGENT_REGISTRY → check supervisor.py
│
└─ Anomalous data (unexpected resources, wrong counts)
   ├─ LangSmith trace → LLM hallucinated resource data
   └─ Scope mismatch → check trigger_type and scope in the run row
```

---

## Quick Credential Rotation Check

If the agent was working before and now returns empty or 401s:

```bash
.venv/bin/python -c "
from infra_brain.config import get_settings
s = get_settings()
# Print non-secret config for the relevant domain:
print('gitlab_url:', s.gitlab_url)
print('vsphere_host:', s.vsphere_host)
# Add domain-specific settings here
"
```

If credentials were rotated in Bitwarden, the pod must be restarted to re-run
`bws_bootstrap.sh` and pick up the new values. Rolling restart:

```bash
kubectl rollout restart deployment/agent-core -n infra-brain
kubectl rollout restart deployment/scheduler -n infra-brain
```
