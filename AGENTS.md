# infra-brain Domain Agents

> **GENERATED — edit `scripts/gen_agents_md.py` (or the agents' `spec`
> declarations), never this file.** Regenerate with
> `.venv/bin/python scripts/gen_agents_md.py`; a CI test
> (`tests/etl/test_agent_spec.py`) fails when this file is stale.

46 dispatchable domains. Each agent class declares a frozen
`AgentSpec` (`src/infra_brain/etl/spec.py`) — the single source of truth for
domain, tier, cron schedule, freshness window, and hook behavior. See
`docs/ARCHITECTURE.md` for the AgentSpec contract and tier semantics.

A **retired** domain is switched off by standing decision: its upstream system
does not exist in this environment. It is not scheduled, not a sweep member,
not freshness-monitored and not dispatchable — but it stays registered,
importable and testable, and the cron shown is the one it would resume on.
Turn one back on with `COLLECTION_REVIVED_DOMAINS=<domain>` (no code change).

| Domain | Tier | Schedule (cron) | Max staleness | Skip hook | Retired |
|---|---|---|---|---|---|
| alertmanager | Collector | */5 * * * * | 0.333333h | no | no |
| backup | Collector | 50 2 * * * | 26h | no | no |
| cicd | Collector | 5 2 * * * | 26h | no | no |
| cloud | Collector | 0 2 * * * | 26h | no | **yes** |
| container_registry | Collector | 15 4 * * * | 26h | no | no |
| dns | Collector | 55 2 * * * | 26h | no | no |
| grafana | Collector | 7,22,37,52 * * * * | 1h | no | no |
| homelab_services | Collector | 6,36 * * * * | 2h | no | no |
| iac | Collector | 20 */6 * * * | 8h | no | no |
| identity | Collector | 15 3 * * * | 26h | no | **yes** |
| k8s | Collector | 15 */6 * * * | 8h | no | **yes** |
| knowledge | Collector | 35 2 * * * | 26h | no | no |
| linux | Collector | 0 */6 * * * | 8h | no | no |
| loadbalancer | Collector | 45 2 * * * | 26h | no | no |
| local_docs | Collector | 50 3 * * * | 26h | no | no |
| netdiscovery | Collector | */15 * * * * | 1h | yes | no |
| octopus | Collector | 10 2 * * * | 26h | no | **yes** |
| personal_wiki | Collector | 35 4 * * * | 26h | no | no |
| prometheus | Collector | 3,13,23,33,43,53 * * * * | 0.5h | no | no |
| saas_inventory | Collector | 45 3 * * * | 26h | no | no |
| secrets_inventory | Collector | 10 5 * * * | 26h | no | no |
| uptime_kuma | Collector | 8,18,28,38,48,58 * * * * | 0.5h | no | no |
| vsphere | Collector | 25 */6 * * * | 2h | no | **yes** |
| vuln | Collector | 15 2 * * * | 26h | no | **yes** |
| wazuh | Collector | 12,27,42,57 * * * * | 1h | no | no |
| windows | Collector | 5 */6 * * * | 8h | no | **yes** |
| graph_maintenance | Reconciler | 50 */2 * * * | 4h | no | no |
| host_reconcile | Reconciler | */30 * * * * | 2h | yes | no |
| inventory_reconcile | Reconciler | 0 5 * * * | 26h | yes | no |
| capacity_forecast | Reasoner | 0 8 * * 0 | 8d | yes | no |
| compliance | Reasoner | 30 6 * * * | 26h | yes | no |
| drift | Reasoner | — (on-demand / hook-driven) | — | yes | no |
| drift_learning | Reasoner | 0 4 * * 0 | 8d | yes | no |
| eol | Reasoner | 20 2 * * * | 26h | no | no |
| licensing | Reasoner | 50 4 * * * | 26h | yes | no |
| notification | Reasoner | — (on-demand / hook-driven) | — | yes | no |
| pki | Reasoner | 40 2 * * * | — | yes | no |
| remediation | Reasoner | 40 6 * * * | 26h | yes | no |
| rootcause | Reasoner | 0 7 * * * | 26h | yes | no |
| vuln_triage | Reasoner | 0 6 * * * | 26h | yes | no |
| fleet_health | Reporter | 25 2 * * * | 26h | no | no |
| learning_feedback | Reporter | 30 5 * * 0 | 8d | yes | no |
| coverage | On-demand | 40 3 * * 1 | 8d | no | no |
| discovery | On-demand | 0 3 * * 0 | 8d | yes | no |
| inventory_mr | On-demand | — (on-demand / hook-driven) | — | yes | no |
| query | On-demand | — (on-demand / hook-driven) | — | no | no |
