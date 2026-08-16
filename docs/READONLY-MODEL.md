# infra-brain Read-Only Model (post Wave 1)

Resolves F-004's over-claim ("every tool call routes through the safety callback
chain") with the R2 three-layer model: structural read-only construction, pre-execution
boundary gates, and audit-only observation — each detailed below.

## Layer 1 — Structural (read-only by construction)

| Surface | Mechanism |
|---|---|
| GitLab reads (cicd, iac, inventory_reconcile) | `tools/gitlab.py` → `readonly_get` (`tools/http_readonly.py`) — non-GET raises `ReadOnlyHTTPError` |
| Octopus reads | `tools/octopus_tool.py` → `readonly_get` |
| Rapid7 reads | `tools/rapid7.py` → `ReadOnlyClient` |
| endoflife.date reads | `tools/eol.py` → `ReadOnlyClient` |
| Context7 reads | `tools/context7.py` → `readonly_get` |
| NL→SQL agent | SELECT-only DB role (`POSTGRES_READONLY_URL`) + AST guard (`agents/query.py`) — item 1.6 |

## Layer 2 — Boundary gates (fail-closed, pre-execution, outside callbacks)

| Surface | Mechanism |
|---|---|
| LLM tool calls (`LLMAgent._run_tool`, chat tools node) | `callbacks/boundary.py::enforce_tool_gate` — raises `PermissionError` BEFORE the tool executes |
| External writes (GitLab MR / Jira / Confluence / digest) | `callbacks/write_gate.py::gate_external_write` — DLP scan + `agent_action_log` row (real verdict) BEFORE any `httpx.post/put` |
| nmap / `ip route` subprocesses (netdiscovery) | `_gate_nmap_targets` + per-invocation `agent_action_log` rows — item 1.5 |

## Layer 3 — Audit (observation only; never the gate)

`AuditCallbackHandler` / `ObservationCallbackHandler` via `build_callbacks()` — attached to
every agent, `raise_error=False` by design. `ReadOnlyToolValidator` / `DLPCallbackHandler`
additionally set `raise_error=True` as a belt-and-braces interim (F-004.1).

## Read-only by CONVENTION (honest list — no structural guarantee)

| Surface | Why structural is not feasible | Compensating control |
|---|---|---|
| vSphere (`tools/vsphere.py`, pyvmomi) | SOAP API — no HTTP-verb seam | code review; connector only calls `Retrieve*/Query*` methods |
| Kubernetes (`agents/k8s.py`) | official client lib | only `CoreV1Api/AppsV1Api list_*` calls used |
| netdiscovery subprocesses | `nmap` / `ip route` are processes, not HTTP | boundary gate + audit rows (item 1.5); exclusion CIDRs fail closed |
| WinRM (`tools/winrm_client.py`, pywinrm — MR-J item 4) | remote-shell protocol — no HTTP-verb seam, and `client.run_ps(...)` calls in `agents/windows.py` are direct pywinrm invocations, NOT routed through `.invoke()`/`build_callbacks()` (no boundary gate, no DLP scan, no per-call audit row — code review is the only compensating control, weaker than netdiscovery's audited pattern above) | code review; every one-liner in `agents/windows.py` is a `Get-*`/`Search`-only cmdlet (never `Set-*`/`New-*`/`Remove-*`/`Install*`) — see `tests/agents/test_windows_winrm_collectors.py` |
| DB-only agents (drift, fleet_health, ...) | they write to infra-brain's OWN Postgres (that is their job) | infra-brain's DB is not "infrastructure"; guarantee scope is external systems |
| MCP server tool surface | separate FastMCP process; direct in-process invocation (e.g., `docker exec`) bypasses audit middleware — use HTTP client connection instead | **known open P0 (F-025) — Wave 1.5a**, not covered by this wave; see `skills/infra-brain-mcp-operations/SKILL.md` "Audit boundary" for the explicit rule |

## PCI/DLP in action — a real detection (2026-07-13)

A concrete, real-world example of the DLP layer preventing cardholder data (PAN)
from ever entering infra-brain — the kind of evidence a PCI DSS assessor wants to
see (Req. 3: protect stored account data; Req. 4/data-flow: don't propagate PANs
into out-of-scope systems).

**What happened.** When the Octopus collector's deep **audit-events** phase ran
against the deployment target, `DLPCallbackHandler.on_tool_end`
(`callbacks/dlp.py`) fail-closed with `PermissionError: [DLP] PAN detected in tool
output (Luhn valid)` and the run stopped. Investigation of the flagged values
(shape only, digits masked) showed **16-digit, Luhn-valid, Visa-IIN numbers**
(`4242…`, `4912…`) sitting in **PAN-labeled fields** of the event payload —
`<entirepan>`, `<epan>`, `expdate`, `SEPARATOR CARD` track-data markers. These are
almost certainly *test* card numbers captured from pipeline/deployment logs, but
they are structurally real PANs.

**Why this is the system working, not a bug.**

- **Layer 1 (structural):** the collector reached Octopus through `readonly_get`
  only — it *read* the events, it could never write to Octopus regardless of the
  API key's permissions (the key on this target could not be scoped).
- **Layer 2/3 (DLP, fail-closed):** every tool output is scanned by
  `DLPCallbackHandler`. A Luhn-valid candidate that also matches a real card
  network (`is_probable_pan` = Luhn **and** IIN/length; Visa/16 qualifies) is
  treated as a PAN, an `AuditLog` denial row is written, and — because
  `dlp_fail_closed` is on — the call raises. **No cardholder data was ever
  persisted to infra-brain's database or logs.** (Contrast: the Rapid7 sweep's
  Luhn-valid values were X.509 cert serials / agent IDs — prefix `00`/`58`, no
  card IIN — so `is_probable_pan` correctly does **not** flag them. Luhn alone
  false-positives; the IIN corroboration is what makes the control precise.)

**Resolution (no control weakened).** The fix was NOT to relax DLP. The Octopus
audit-event stream is simply not ingested by default: `octopus_collect_events`
defaults **False** (`config.py`), so `agents/octopus.py::_write_octopus_deep`
skips the events phase. Octopus **inventory** collection (projects, environments,
machines, deployment processes, variable *metadata* — values already dropped)
continues normally. Enable the events phase only if that stream is known to be
PAN-free.

**Takeaway for compliance evidence.** infra-brain's read-only + DLP layers
detected and blocked account data at the ingestion boundary, recorded the denial
in the audit log, and defaulted the offending data source off — cardholder data
never crossed into the system.
