import logging
import threading
import time

from pydantic import Field, TypeAdapter, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    # Provider selector: "anthropic" | "bedrock" | "openai".
    llm_provider: str = "anthropic"
    # Anthropic (direct) model id — used when llm_provider="anthropic".
    llm_model: str = "claude-sonnet-4-6"
    anthropic_api_key: str = ""
    # Optional Anthropic-compatible proxy/gateway base URL (empty → official API).
    anthropic_base_url: str = ""
    # B7 (Finding 6): per-role Anthropic model overrides (additive/opt-in —
    # empty string means "no override, fall back to llm_model" exactly like
    # today). Previously Anthropic/OpenAI always paid for one configured
    # model regardless of role — deliberate at the time ("role-specific
    # Bedrock model ids do not exist there"), but means a prod deployment
    # running Anthropic has no cheap-tier option for chat/SQL/routing the way
    # Bedrock already does. Only set the ones you actually want a cheaper/
    # faster model for; anything left "" keeps today's single-model behavior.
    anthropic_model_chat: str = ""
    anthropic_model_sql: str = ""
    # Phase 3 Task 1: rootcause role override (RootCauseAgent becomes an LLM
    # reasoner). Defaults to "" — same as chat/sql above — so falls back to
    # llm_model (today's exact behavior) until explicitly set.
    anthropic_model_rootcause: str = ""

    # AWS Bedrock (used when llm_provider="bedrock"). Auth is IAM: set
    # AWS_* env keys, a named profile below, or rely on an instance role.
    # NOTE: exact Bedrock model / inference-profile IDs are region- and
    # account-specific and change often. VERIFY every id below before deploy:
    #   aws bedrock list-inference-profiles --region <your-region>
    # and enable model access in the Bedrock console first.
    #
    # bedrock_model_id is the GLOBAL DEFAULT for any LLM agent without a
    # per-role override (global default = MiniMax M2.5).
    bedrock_model_id: str = "us.minimax.minimax-m2-5-v1:0"
    # Per-agent model overrides (empty → falls back to bedrock_model_id):
    #   discovery = MiniMax M2.5  (agentic tool-loop + strict JSON)
    #   sql       = Qwen3 Coder 30B A3B  (NL→SQL via create_sql_agent)
    #   chat      = GLM 4.7 Flash  (interactive streaming chat)
    bedrock_model_discovery: str = "us.minimax.minimax-m2-5-v1:0"
    bedrock_model_sql: str = "qwen.qwen3-coder-30b-a3b-v1:0"
    bedrock_model_chat: str = "zai.glm-4-7-flash-v1:0"
    #   remediation = Qwen3 Coder 30B (drafts Ansible/Terraform fixes)
    #   triage      = MiniMax M2.5    (vuln prioritization reasoning)
    #   rootcause   = MiniMax M2.5    (event correlation)
    bedrock_model_remediation: str = "qwen.qwen3-coder-30b-a3b-v1:0"
    bedrock_model_triage: str = "us.minimax.minimax-m2-5-v1:0"
    bedrock_model_rootcause: str = "us.minimax.minimax-m2-5-v1:0"
    #   coverage = Amazon Nova Lite  (strategy reasoning; same tier as discovery)
    bedrock_model_coverage: str = "us.amazon.nova-lite-v1:0"
    bedrock_region: str = "us-east-1"
    bedrock_profile: str = ""  # optional named AWS profile; empty → default chain

    # OpenAI / OpenAI-compatible (used when llm_provider="openai"). Covers
    # OpenAI proper plus any compatible gateway (OpenRouter, LiteLLM, Groq,
    # Together, vLLM, …) — just point openai_base_url at the gateway.
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"  # e.g. "anthropic/claude-sonnet-4-6" on OpenRouter
    openai_base_url: str = ""  # empty → official OpenAI; else gateway URL
    openai_http_referer: str = ""  # optional; OpenRouter ranking header (HTTP-Referer)
    openai_app_title: str = ""  # optional; OpenRouter ranking header (X-Title)
    # B7 (Finding 6): per-role OpenAI/OpenAI-compatible model overrides —
    # same additive/opt-in contract as the anthropic_model_* fields above.
    # Particularly useful on a gateway (OpenRouter/LiteLLM/...) where a
    # genuinely cheaper model id is available for chat/SQL roles.
    openai_model_chat: str = ""
    openai_model_sql: str = ""
    # Phase 3 Task 1: rootcause role override — same additive/opt-in contract,
    # defaults to "" (falls back to openai_model) until explicitly set.
    openai_model_rootcause: str = ""

    # Per-call LLM request timeout, in seconds (TRK-109 residual). Bounds a SINGLE
    # LLM client call so one hung/glacial request can't silently eat the whole
    # collect() budget and only surface when the 600s wall-clock guard trips —
    # losing the entire run. MR !168 added per-run count caps + a 60%-of-budget
    # wall-clock check BEFORE each call in coverage/remediation, but nothing
    # bounded a call already in flight; this closes that gap. Threaded into every
    # provider that supports it: ChatAnthropic / ChatOpenAI via their `timeout=`
    # kwarg, Bedrock via ChatBedrockConverse's `timeout=` field (or a botocore
    # connect/read-timeout Config on the pre-built-client profile path). Local
    # Ollama on CPU can legitimately take 60-120s per call, so keep this generous
    # — too low a value turns a slow-but-fine call into a lost run. 0 disables (no
    # per-call timeout, pre-TRK-109 behavior). .env: LLM_REQUEST_TIMEOUT_SECONDS.
    llm_request_timeout_seconds: int = 120

    # Database
    # REQUIRED — no production default. A missing/empty DSN must abort loud, not
    # silently fall back to a throwaway localhost DB. Historically the localhost
    # default let the `migrate` service "upgrade" an empty localhost database
    # (when DATABASE_URL was unresolved) while the live volume stayed unmigrated —
    # the deploy went green and prod 500'd on a drifted column. Validated below.
    postgres_url: str = ""
    # SELECT-only DB role for the NL->SQL QueryAgent (Wave 1 item 1.6). Empty ->
    # falls back to postgres_url (sanitizer-only enforcement) with a warning.
    postgres_readonly_url: str = ""
    # Fernet key for RuntimeConfig secret-category rows (TRK-303 part 4/7).
    # Bootstrap-only — same tier as postgres_url: env/Bitwarden, NEVER itself
    # DB-overridable (that would be circular: the key that decrypts DB rows
    # can't live in a DB row). Empty means no secret overrides resolve yet;
    # _load_runtime_overrides skips secret rows rather than raising.
    runtime_config_encryption_key: str = ""
    redis_url: str = "redis://localhost:6379/0"
    # SQLAlchemy pool sizing for the shared primary engine (TRK-092). Defaults are
    # UNCHANGED from the former hardcoded literals in db/session.py — this only
    # makes them tunable. Right-sizing for concurrent sweep fan-out + dashboard/chat
    # load is a follow-up ops decision (needs real pool-utilization data), not a
    # value to guess here.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    # Dashboard LLM cost guardrails (TRK-090 / TRK-091). Both are Redis-backed,
    # per-user, and process-count-invariant; both are STARTING POINTS to tune with
    # real usage data, not load-tested capacity numbers.
    #   * chat_rate_limit_per_hour — max POST /chat + /custom-view calls per user
    #     per rolling hour. 30/hr ≈ one every 2 min; comfortably above interactive
    #     human use, well below a runaway loop.
    #   * daily_token_budget_per_user — cumulative (approx) LLM tokens a single user
    #     may drive through chat+custom-view per calendar day before further calls
    #     are refused. 500k ≈ ~250 interactions/day at a ~2k-token average — a
    #     denial-of-wallet backstop, not an everyday ceiling. Token counts are
    #     approximated (~4 chars/token over prompt+streamed output) since the
    #     streaming paths don't surface exact usage_metadata and AgentDecisionLog
    #     carries no per-user column to SUM.
    chat_rate_limit_per_hour: int = 30
    daily_token_budget_per_user: int = 500_000

    # Infrastructure APIs
    gitlab_url: str = ""
    gitlab_token: str = ""
    gitlab_ssl_verify: bool = True
    octopus_url: str = ""
    octopus_api_key: str = ""
    octopus_ssl_verify: bool = True
    # SaaS/API-key inventory agent (GitLab #103) — metadata-only collector.
    # Empty by default: collect() is a clean no-op until an operator points
    # this at a real SaaS admin/management API.
    saas_admin_url: str = ""
    saas_admin_token: str = ""
    saas_vendor: str = ""
    # Octopus deep-capture tunables (F-036): previously read via getattr() with
    # hardcoded fallbacks, so a .env override was silently ignored.
    octopus_history_days: int = 365  # time-box tasks/events to the last N days
    octopus_events_cap: int = 50000  # hard cap on octopus_events rows per run
    octopus_fetch_workers: int = 8  # bounded read concurrency for per-project GETs
    # Octopus audit-event deep-capture. DEFAULT OFF: on the deployment target,
    # event Details embed cardholder-shaped data (Visa-IIN PANs in track/`epan`
    # fields from pipeline test logs) that the DLP callback correctly fail-closes
    # on — so ingesting the event stream is both unnecessary and a PCI-scope risk.
    # Leave off unless the event stream is known PAN-free. See
    # docs/READONLY-MODEL.md "PCI/DLP in action" and TRK-100.
    octopus_collect_events: bool = False
    rapid7_url: str = ""
    rapid7_username: str = "api_key_auth"
    rapid7_api_key: str = ""
    rapid7_ssl_verify: bool = True
    # vuln_queue feeder cap (F-036): per run, fetch vulns for at most the top-N
    # assets by risk_score. Previously getattr()-only, so .env was a no-op.
    rapid7_vuln_asset_cap: int = 750
    # Coverage rotation (GitLab #173 / #188 Bug 2): of the asset cap above,
    # reserve this many slots for a round-robin slice over the assets ranked
    # BELOW the guaranteed top (cap - slots), advancing the window each run —
    # so a host outside the top-N by risk still gets its vuln_queue rows
    # refreshed at least once every ceil(R/slots) runs instead of never.
    # 0 disables rotation (pure top-cap, the pre-fix behavior). No effect at
    # all when the with-vulns asset count fits under the cap.
    rapid7_vuln_rotation_slots: int = 150
    # vuln-detail enrichment bound (MR7): of the distinct Rapid7 vuln SLUG ids
    # found across the queued assets' findings, enrich at most this many — taken
    # top-N PRIORITIZED by severity (critical/severe first). Each enriched slug is
    # 1 vuln-detail call + its solutions; a module-level cache dedupes solution
    # fetches. Logged: distinct found vs enriched vs skipped (no silent truncation).
    rapid7_vuln_detail_cap: int = 600
    # GitLab issue #102: Okta IdP read-only API — powers IdentityAgent's
    # principal/group-membership audit. okta_url is the org base, e.g.
    # "https://example.okta.com" (no trailing /api/v1). Empty => agent raises
    # CollectorSkipped (same "unconfigured, gracefully skip" convention as
    # vsphere_host/octopus_url/rapid7_url).
    okta_url: str = ""
    okta_api_token: str = ""
    okta_ssl_verify: bool = True

    # -------------------------------------------------------------------
    # Backup verification (GitLab issue #96) — pluggable/generic collector.
    # Empty backup_backend means "unconfigured" -> BackupAgent.collect()
    # raises CollectorSkipped and degrades cleanly (no live Veeam/Bacula/
    # cloud-snapshot target exists in this dev environment). Set to
    # "veeam" | "bacula" | "cloud_snapshot" to select which backend's
    # response-field mapping tools/backup.py's collector uses.
    # -------------------------------------------------------------------
    backup_backend: str = ""
    backup_api_url: str = ""
    backup_api_key: str = ""
    backup_ssl_verify: bool = True
    # A job is flagged "overdue" (no recent successful backup) once
    # last_success_at is older than this many days (or was never recorded).
    backup_overdue_days: int = 2
    # A job's restore/DR test is flagged "overdue" once last_restore_test_at
    # is older than this many days (or has never been recorded).
    backup_restore_test_overdue_days: int = 90

    # TRK-109: per-run LLM fan-out caps for the two reasoner collectors. Both
    # agents previously made one sequential LLM call per work item with no bound
    # (coverage: one per uncovered domain; remediation: one per open config-drift
    # without a proposal — 12k+ on the live dataset), so collect() always hit the
    # 600s wall-clock guard and the domains never completed a run. Each run now
    # processes at most this many LLM drafts and logs the deferred remainder
    # (no silent truncation); progress persists per item, so successive runs
    # drain the backlog.
    coverage_gap_llm_cap: int = 5
    remediation_draft_cap: int = 25
    vsphere_host: str = ""
    vsphere_user: str = ""
    vsphere_password: str = ""
    vsphere_ssl_verify: bool = True
    vsphere_hosts: str = ""  # comma-separated vCenter hostnames; overrides vsphere_host when set
    vsphere_connect_timeout: int = 30  # seconds before aborting a vCenter connection
    # Two-speed vSphere metrics: pulse (15 min) refreshes quickStats; perf history uses QueryPerf.
    # QueryPerf is off by default — it adds real API load to vCenter.
    vsphere_collect_perf_history: bool = False
    vsphere_perf_interval_id: int = 300  # seconds; 300 = 5-min rollup samples
    vsphere_perf_max_samples: int = 12  # samples to fetch (12 × 5 min = last hour)
    vsphere_perf_batch_size: int = 25  # VMs per QueryPerf call
    # Pulse-first rollout gate: when False (default) the scheduled 6h FULL vSphere
    # inventory job is NOT registered — only the 15-min pulse runs automatically.
    # A manual dispatch("vsphere", scope="all") still works on demand. Flip to True
    # to re-enable scheduled full inventory (no code change).
    vsphere_full_inventory_enabled: bool = False
    # Append-only vsphere_*_metrics rows older than this are pruned after each
    # collection so the time-series tables stay bounded.
    vsphere_metrics_retention_days: int = 30
    # Load balancer / reverse proxy / CDN collection (GitLab issue #100).
    # Every one of these APIs is naturally GET-only (F5 iControl REST, nginx
    # Plus API, HAProxy stats page, Cloudflare API) — see
    # infra_brain.tools.loadbalancer / infra_brain.agents.loadbalancer. Each
    # vendor is independently optional: an empty host/URL means that vendor is
    # skipped cleanly rather than raising.
    lb_enabled: bool = False
    # F5 BIG-IP iControl REST (read-only "Guest"/read-only role recommended).
    f5_host: str = ""
    f5_username: str = ""
    f5_password: str = ""
    f5_ssl_verify: bool = True
    # nginx Plus API (read-only endpoint; nginx Plus exposes no auth by
    # default — restrict via network ACL, not this collector).
    nginx_plus_url: str = ""
    # HAProxy stats page (CSV export via `;csv` — read-only regardless of
    # auth since the stats page itself has no write actions).
    haproxy_stats_url: str = ""
    haproxy_stats_username: str = ""
    haproxy_stats_password: str = ""
    # Cloudflare API (scoped read-only API token — Zone:Read + Load
    # Balancing:Read; never a Global API Key).
    cloudflare_api_token: str = ""
    cloudflare_zone_ids: str = ""  # comma-separated; empty -> discover via /zones

    ansible_inventory_path: str = ""
    # Comma-separated exact hostnames LinuxAgent must never record as
    # linux_host resources even though they answer Ansible's default "all"
    # target -- e.g. DNS aliases/CNAMEs (an inventory "adopted_dns"-style
    # group) that resolve back to a collector host itself. See
    # agents/linux.py's collect() for where this is applied.
    linux_exclude_hosts: str = ""

    # Ansible inventory reconciliation — GitLab repo is the source of truth.
    # When a discovered host is missing from the canonical inventory, the
    # InventoryReconcileAgent opens an MR that ADDS it (never deletes/modifies).
    inventory_source_project_id: int = 0  # GitLab project id of the inventory repo
    inventory_branch: str = "main"
    inventory_file_path: str = ""  # canonical inventory file; empty → auto-discover
    inventory_default_group: str = "discovered"  # group for hosts with no mapping
    inventory_group_map: str = ""  # optional "domain:group,domain:group" routing
    # domains to reconcile — "vuln" is Rapid7 assets (r7_asset type; the domain
    # string is literally "vuln", not "rapid7" — see agents/vuln.py)
    inventory_match_domains: str = "linux,windows,vsphere,cloud,vuln"
    # Some domains multiplex several Resource.type values under one domain and
    # only some of those types are actual hosts (e.g. Octopus emits
    # octopus_server/octopus_environment/octopus_project alongside the real
    # octopus_machine hosts; vSphere emits vsphere_vcenter/vsphere_permission/
    # vsphere_license/vsphere_alarm alongside real vsphere_vm/vsphere_host
    # hosts; the "cloud" domain emits vpc/security_group alongside real
    # ec2_instance hosts). Domains not listed here keep the old behavior (no
    # type filter) because they are host-only today (linux -> linux_host,
    # windows -> windows_host). Format: "domain:type1|type2,domain:type1|type2".
    inventory_host_types: str = (
        "octopus:octopus_machine,vsphere:vsphere_vm|vsphere_host,vuln:r7_asset,cloud:ec2_instance"
    )

    # MR-J item 3: curated host purpose/VLAN map — a hand-maintained mapping of
    # ~55 hosts' purpose (and, where known, VLAN/subnet), now seeded as a
    # dedicated curated file, host_purpose_map.yml, at the ROOT of the
    # fleet-inventory repo (a dedicated fleet-inventory GitLab project). 0 (default) → HostPurposeMap
    # sync is idle, matching every other optional GitLab-sourced collector's
    # "unset id = off" convention; set to 6 to enable.
    host_purpose_map_project_id: int = 0
    host_purpose_map_branch: str = "main"
    # Path within the fleet-inventory repo (project 6). The map lives at the
    # repo root as a plain top-level `host_purpose_map:` mapping document — no
    # longer the old assumed system_inventory.yml play path.
    host_purpose_map_file_path: str = "host_purpose_map.yml"

    # Remediation — approved fix proposals open an MR to this GitLab project.
    remediation_project_id: int = 0  # 0 → execution is a no-op (proposal only)
    remediation_branch: str = "main"

    # Container registry scanning (GitLab issue #101) — registry-agnostic OCI
    # Distribution API v2 (Docker Hub/GitLab CR/Harbor/ECR/GHCR/ACR all
    # implement the same read-only endpoints). Comma-separated base URLs;
    # empty -> ContainerRegistryAgent is idle (CollectorSkipped).
    container_registries: str = ""
    container_registry_token: str = ""  # optional shared bearer token (anon-pull if empty)
    container_registry_ssl_verify: bool = True
    container_registry_repo_cap: int = 200  # repos enumerated per registry per run
    container_registry_tag_cap: int = 20  # tags enumerated per repo per run

    # Home-lab monitoring/observability collectors (convergence-plan "develop
    # integrations... building new collectors" ask). Every URL empty ->
    # CollectorSkipped; each is a read-only HTTP client against the named
    # service's own API, no writes.
    homelab_services_manifest_path: str = ""  # "" -> HomelabServicesAgent's bundled default
    prometheus_url: str = ""
    prometheus_ssl_verify: bool = True
    grafana_url: str = ""
    grafana_api_token: str = ""  # "" -> GrafanaAgent still runs /api/health only (anon-accessible)
    alertmanager_url: str = ""
    alertmanager_token: str = ""
    uptime_kuma_url: str = ""
    uptime_kuma_status_page_slug: str = "default"  # unconfirmed real slug — see agent docstring
    wazuh_url: str = ""
    wazuh_username: str = ""
    wazuh_password: str = ""
    wazuh_ssl_verify: bool = True
    wazuh_agents_cap: int = 500
    wazuh_alerts_window_hours: int = 24
    wazuh_alerts_cap: int = 200
    # Wazuh Indexer (OpenSearch) — alert SEARCH lives here, not on the Manager
    # API. Wazuh 4.x moved it when it adopted the OpenSearch stack, so the
    # Manager's port 55000 has no /alerts route at all and the old collector
    # 404'd on every single run. agents/wazuh.py's module docstring called this
    # out and prescribed exactly this fix. Empty url => alerts are skipped
    # (agent inventory still collects), matching every other optional
    # integration in this file.
    wazuh_indexer_url: str = ""
    wazuh_indexer_username: str = ""
    wazuh_indexer_password: str = ""
    wazuh_indexer_ssl_verify: bool = True
    # Index pattern holding alert documents. Overridable because a deployment
    # can rotate/rename these (wazuh-alerts-4.x-*, a custom ILM alias, ...).
    wazuh_indexer_alerts_index: str = "wazuh-alerts-*"

    # P5.1 (convergence plan): the infra-ops plugin's hooks are stdlib-only
    # urllib clients with a hard ~1s fail-open timeout (D9) — FastMCP's
    # stateful streamable-http transport (session handshake + SSE framing)
    # can't be satisfied in that budget, so hooks reach record_observation/
    # record_client_state/record_governance_event through a small plain-REST
    # channel instead (api/routers/internal_state.py) gated by this single
    # shared-secret bearer token. Empty = fail-closed like ops_webhook_token,
    # unless infra_brain_dev is set (see webhooks.py's _verify_token pattern).
    internal_service_token: str = ""

    # P4.4a residual gap (b): opt-in hard-deny for the TRK-247 direct-
    # invocation shape (an MCP mutation tool called in-process, bypassing the
    # real HTTP+auth transport entirely — e.g. docker exec ... python -c ...).
    # Default False preserves the existing documented behavior (detect-and-
    # stamp, never block — a legitimate ops/debug path). Flip to True in a
    # deployment that wants a harder boundary. See mcp_server.py's
    # _mutations_enabled()/_mutation_disabled_response().
    infra_brain_mcp_deny_direct_invocation: bool = False

    # P6.1/P6.2 (convergence plan Phase 6, D10): the headless runner's
    # standing tasks (drift-triage, self-review, EOL report) call
    # create_gitlab_issue/record_governance_event/record_client_state/
    # propose_instinct over the REAL MCP HTTP transport (runner/mcp_client.py,
    # a fastmcp.Client — not an in-process call like chat/agent.py's
    # propose_host_purpose_update) with its OWN scoped key, so those actions
    # attribute as "mcp:headless-runner" and are independently revocable,
    # rather than stamping "direct:unattributed" like an in-process call
    # would. Default URL is loopback, NOT the mcp service's compose DNS name
    # ("mcp") — the scheduler container runs with network_mode: host (see
    # docker-compose.yml's scheduler service comment), so it is not on the
    # "backend" compose network at all and cannot resolve compose service
    # names; it reaches mcp's "8002:8002" host publish the same way it
    # reaches Postgres/Redis via their own loopback publishes. Empty token =
    # standing tasks are a clean no-op (no jobs registered), matching this
    # repo's "empty = off" convention for optional integrations.
    headless_runner_mcp_url: str = "http://127.0.0.1:8002/mcp"
    headless_runner_mcp_token: str = ""

    # GitLab #108: approved compliance_rule_gap proposals open an MR editing
    # rules/enforcement/compliance.yml in this GitLab project (normally the
    # infra-brain project itself, since that's where the file lives) —
    # separate from remediation_project_id because config-drift/vuln/EOL
    # remediation notes and compliance-rule YAML changes are not the same
    # repo. 0 (default) → execution is a no-op (proposal only), same
    # unset-id convention as every other optional GitLab write target.
    compliance_rules_project_id: int = 0
    compliance_rules_branch: str = "main"

    # Network devices (read-only SNMP — requires the 'net' extra: pysnmp)
    snmp_targets: str = ""  # comma-separated host/IP list (empty → NetAgent idle)
    snmp_community: str = ""  # read-only community string
    snmp_port: int = 161
    snmp_timeout_seconds: int = 2
    snmp_retries: int = 1

    # Network discovery agent (netdiscovery) — active shadow-IT & threat discovery.
    # Safety dial: passive (DNS+DB only) | polite (DEFAULT, ICMP+top-100 TCP) |
    #              normal (top-1000 TCP) | aggressive (full TCP range + intensive -sV).
    netdiscovery_aggressiveness: str = "polite"
    # Comma-separated CIDR blocks that must NEVER be probed (release valve for CDE).
    netdiscovery_exclude_cidrs: str = ""
    # Tier enablement — both default enabled; set to "false" to disable.
    netdiscovery_enable_ping_sweep: bool = True  # Tier 1: host sweep
    netdiscovery_enable_port_scan: bool = True  # Tier 2: service/OS scan
    # Rate controls (applied at all active aggressiveness levels).
    # TRK-280/TRK-281: bumped 50 -> 100 pkt/s. Docker-bridge routes (172.x.0.0/16)
    # never reach this rate cap — they're filtered out by
    # netdiscovery_max_subnet_hosts before any nmap call — so this only ever
    # throttles a genuinely reachable subnet (e.g. the real 192.0.2.0/24 LAN
    # exposed by MR !264's network_mode: host change). 100 pkt/s is still a
    # conservative ICMP/ARP -sn ping-sweep rate for a trusted internal LAN.
    netdiscovery_max_rate: int = 100  # packets/sec cap (conservative default)
    # --- Tier 1 scope + budget guards (TRK-239) -------------------------------
    # `ip route` inside the app container reports the Docker bridge network
    # (e.g. 172.26.0.0/16 — 65534 addresses) as a "reachable subnet".  At
    # NETDISCOVERY_MAX_RATE=50 pkt/s that single route cannot be swept inside
    # COLLECT_TIMEOUT_SECONDS, and it is not an in-scope network anyway.  Routes
    # with more than this many addresses are skipped (recorded as a Tier 1
    # error, not a whole-run failure).  1024 == a /22, matching the Tier 0 PTR
    # sweep's own lightweight ceiling.
    netdiscovery_max_subnet_hosts: int = 1024
    # Optional positive allowlist.  When non-empty, only routed subnets that
    # fall inside one of these CIDRs are swept; everything else is ignored
    # (silently — an out-of-scope route is not an error).  Empty (default) →
    # every route that passes the size guard is swept.
    netdiscovery_include_cidrs: str = ""
    # Per-subnet nmap wall-clock cap for the Tier 1 sweep.  One slow subnet
    # times out on its own and is recorded as an error; its siblings still run.
    # TRK-280/TRK-281: bumped 120 -> 300. A 254-host -sn ping sweep at
    # -T3/--max-rate 100 against a real, newly-host-network-reachable /24
    # (e.g. 192.0.2.0/24 per MR !264) was live-timing-out inside 120s; oversized
    # routes (Docker bridges) never reach this timeout at all — they're
    # filtered out by netdiscovery_max_subnet_hosts before any nmap call — so
    # this only ever bounds a genuine LAN sweep, not a bridge probe.
    netdiscovery_subnet_timeout_seconds: int = 300
    # Whole-Tier-1 wall-clock budget.  Must stay comfortably below
    # COLLECT_TIMEOUT_SECONDS so partial results are returned and persisted
    # instead of being destroyed by the outer _call_with_timeout guard.
    netdiscovery_tier1_budget_seconds: int = 480
    # Whole-Tier-2 wall-clock budget, same rationale as Tier 1's (TRK-280/281
    # follow-up): once Tier 1 started finding real hosts on a previously-
    # unreachable LAN, Tier 2 had far more to service-scan and started
    # blowing the outer _call_with_timeout guard, discarding the entire run's
    # results (Tier 0/1 included). Split across the fragile/normal scan
    # groups so one group's timeout doesn't cost the other's results.
    netdiscovery_tier2_budget_seconds: int = 480
    # Hosts per nmap invocation within a Tier 2 scan group. Live-measured
    # 2026-07-30 against real LAN hosts at the "polite" profile (-T2
    # --max-rate 50 --scan-delay 10ms, 23 ports, -sV): ~27s/host, so a
    # 20-host chunk (~550s) still exceeds the whole-group budget in one shot.
    # Sized so several chunks can complete within netdiscovery_tier2_budget_
    # seconds instead of one oversized chunk consuming the whole budget with
    # nothing to show for it. Full coverage of a ~150-host LAN still is NOT
    # achieved in one cycle at this throttle — see TRK-280 for the tracked
    # capacity follow-up (accept partial/rotating coverage vs. raise the
    # profile for trusted internal LANs vs. some other design choice).
    netdiscovery_tier2_chunk_size: int = 3
    # Fragile-device protection — ICMP/single-port only regardless of aggressiveness.
    netdiscovery_fragile_cidrs: str = ""  # comma-separated CIDRs
    netdiscovery_fragile_hosts: str = ""  # comma-separated IPs/hostnames
    # DNS resolver for the passive tier (empty → system default).
    netdiscovery_dns_resolver: str = ""
    # Dangerous-port watchlist (comma-separated port numbers).
    # Defaults: telnet/23, SMBv1/445, RDP/3389, VNC/5900, Telnet-alt/2323.
    netdiscovery_dangerous_ports: str = "23,445,3389,5900,2323"
    # Suspicious-signature watchlist (comma-separated keywords matched against
    # service name + banner; triggers is_suspicious flag + threat level bump).
    netdiscovery_suspicious_signatures: str = (
        "teamviewer,anydesk,cobalt strike,meterpreter,empire,metasploit,"
        "ngrok,frp,chisel,rathole,ncat,netcat"
    )

    # -----------------------------------------------------------------------------
    # DNS zone/record monitoring (GitLab issue #95) — strictly opt-in.
    # Comma-separated authoritative zone names to monitor, e.g.
    # "example.com,corp.internal". Empty (default) -> DnsAgent raises
    # CollectorSkipped every run, same "unconfigured -> graceful empty" pattern
    # as rapid7_url/vsphere_host.
    dns_zones: str = ""
    # Resolver to query (empty -> system default resolver via dnspython).
    dns_resolver_host: str = ""
    # Per-query wall-clock cap (SOA lookup and AXFR attempt each use this).
    dns_query_timeout_seconds: int = 10
    # -----------------------------------------------------------------------------
    # DNS allow-list gate (GitLab issue #95 residual 1) — mirrors netdiscovery's
    # _gate_nmap_targets boundary-gate pattern (config.py:325-345). Unlike
    # netdiscovery_exclude_cidrs/include_cidrs (IP ranges, exclude-by-default),
    # this is a positive allow-list: DNS zones are a much smaller, more
    # deliberate set than IP ranges, so "explicitly allowed" is the safer
    # default shape here. Comma-separated zone names. Empty (default) -> no
    # restriction, every zone in dns_zones is queried — backward-compatible
    # with pre-gate behavior.
    dns_allowed_zones: str = ""
    # -----------------------------------------------------------------------------

    # Notifications
    jira_url: str = ""
    jira_token: str = ""
    jira_project_key: str = "INFRA"
    jira_user_email: str = ""
    confluence_url: str = ""
    confluence_token: str = ""
    confluence_space_key: str = "INFRA"
    confluence_user_email: str = ""

    # -----------------------------------------------------------------------------
    # RAG knowledge store — strictly opt-in
    # -----------------------------------------------------------------------------
    # Master flag. Default OFF keeps the ConfluenceIngestAgent and the
    # search_knowledge retrieval tools inert (no-op / empty results) so the
    # rest of the system is byte-identical to today's behavior when disabled.
    rag_enabled: bool = False
    # Embedding provider: "" (default) auto-resolves — explicit override wins,
    # else mirror llm_provider when it can embed (bedrock/openai), else bedrock
    # (Anthropic has no embeddings API). Valid explicit values: bedrock, openai.
    embedding_provider: str = ""
    # Bedrock embedding model id (default provider). Titan v2 natively emits
    # 1024 dims, matching EMBEDDING_DIM below.
    embedding_model: str = "amazon.titan-embed-text-v2:0"
    # OpenAI embedding model (alternative provider). text-embedding-3-small
    # supports a 1024-dim output. Custom gateways get truncated+renormalized
    # to EMBEDDING_DIM client-side (see embeddings.py) rather than via the
    # OpenAI `dimensions` param, which some gateways mistranslate/reject.
    embedding_openai_model: str = "text-embedding-3-small"
    # Optional: point EMBEDDINGS at a different OpenAI-compatible gateway
    # than chat's OPENAI_BASE_URL -- e.g. chat on a cloud router but
    # embeddings on a local Ollama instance to avoid a rate-limited
    # upstream free tier some embedding-serving gateways proxy. Empty (the
    # default) falls back to OPENAI_BASE_URL, preserving prior behavior.
    embedding_openai_base_url: str = ""
    # Seconds to sleep between embed_documents() batches when using a custom
    # gateway (see embeddings.py _MAX_TEXTS_PER_EMBED_BATCH). Default 0 (off)
    # -- a local/unlimited endpoint has no need for this, and defaulting it
    # on made a real Ollama ingestion run needlessly ~20-40x slower before
    # being caught. Set >0 only for a gateway proxying a rate-limited
    # upstream (e.g. 65 for omniroute's Gemini-backed embeddings, which cap
    # at 100 requests/minute -- verified live).
    embedding_batch_pacing_seconds: float = 0
    # Vector dimension. MUST match the chosen model AND the document_chunks
    # embedding column. Changing it after indexing requires a new migration +
    # full re-embed. Cross-checked against db.models._EMBED_DIM by a test.
    embedding_dim: int = 1024
    # Comma-separated Confluence space keys to index. Empty falls back to the
    # single confluence_space_key.
    confluence_rag_spaces: str = ""
    # Default number of chunks returned by search_knowledge.
    rag_top_k: int = 5
    # TRK-122 R3: minimum cosine similarity a retrieved chunk must clear to be
    # returned by search_knowledge. Default 0.0 = OFF (no floor) so behavior is
    # byte-identical to pre-TRK-122 pure top-k retrieval. The real calibrated
    # value is set live per an empirical verification plan — a CONFIG change,
    # not a code change. Do not hardcode a "real" threshold here.
    rag_min_similarity: float = 0.0
    # TRK-297 R6: fuse a PostgreSQL full-text (tsvector/ts_rank) ranking with the
    # pgvector cosine ranking via Reciprocal Rank Fusion in search_knowledge.
    # Default ON, but subordinate to rag_enabled — with RAG off (the default)
    # this changes nothing, because search_knowledge is already inert. It is a
    # config flag rather than a hardcoded constant so a ranking regression can be
    # reverted to pure cosine by an operator, without a code deploy. Requires
    # migration 2a7f1c9e4d30 (document_chunks.text_tsv); if the column is missing
    # search_knowledge logs and falls back to cosine-only rather than failing.
    rag_hybrid_enabled: bool = True
    # TRK-122 R4: asymmetric-retrieval query instruction prefix. mxbai-embed-large
    # requires QUERIES (never documents) prefixed with e.g. "Represent this
    # sentence for searching relevant passages: " for correct retrieval; neither
    # the Ollama gateway nor OpenAIEmbeddings adds it. Empty = OFF (no prefix),
    # so non-asymmetric models (Titan) are unaffected. Set via env alongside
    # EMBEDDING_OPENAI_MODEL when the deployed embedding model needs it.
    embedding_query_prefix: str = ""
    # TRK-122 R1: filesystem root under which LocalDocsAgent ingests curated
    # repo documentation into the RAG store. Empty = clean no-op (the agent is
    # inert, no globbing, no writes), matching every other unconfigured
    # collector. In the deployed image this points at the COPYed docs/ tree.
    repo_docs_root: str = ""
    # RAG chunk-size budget (chars) the ingest chunkers target per chunk. The
    # breadcrumb/header prefix is subtracted from this, so prefix + body stays
    # within the value. Default 1000 (was a hardcoded 1200) leaves real margin
    # under mxbai-embed-large's 512-token window given WordPiece tokenizes
    # ~1.1-1.3x heavier than the cl100k estimate. Like ``embedding_dim``,
    # changing the embedding model (different context window) requires revisiting
    # this value — an over-window chunk is SILENTLY truncated by the Ollama
    # gateway, corrupting the embedding with no error.
    rag_chunk_size: int = 1000
    # The embedding model's hard token-window ceiling (mxbai-embed-large = 512).
    # Documents the constraint ``rag_chunk_size`` is sized against; revisit both
    # together when swapping the embedding model.
    embedding_max_tokens: int = 512
    # Personal-wiki ingestion (Hermes wiki -> RAG, opt-in domain). This is a
    # SEPARATE opt-in from rag_enabled on purpose: rag_enabled alone must never
    # be sufficient to start indexing personal content. Both this flag AND
    # rag_enabled AND personal_wiki_root must be set for PersonalWikiAgent to do
    # anything; any one unset keeps it a clean no-op. Default OFF — this is real
    # personal data (a separate person AI system's wiki), so the ingestion agent
    # requires an explicit, deliberate flip, never an automatic one.
    personal_wiki_ingest_enabled: bool = False
    # Filesystem root PersonalWikiAgent walks (its entities/, wiki/, and raw/
    # subdirectories; runtime/ is always skipped in code — cron-regenerated
    # live-state scratch data, not durable knowledge). Empty = clean no-op,
    # matching repo_docs_root's pattern.
    personal_wiki_root: str = ""

    # OB-1: generic outbound ops-alert webhook — collection-health alerts,
    # the scheduler dead-man switch, and agent-anomaly alerts (deny-verdict
    # spikes, runaway recursion loops) are all POSTed here as JSON when
    # configured. Empty = clean no-op (matches the Jira/Confluence
    # "unconfigured" pattern in NotificationAgent); alerts still log at
    # ERROR level either way.
    ops_webhook_url: str = ""
    ops_webhook_token: str = ""  # optional bearer token for the ops webhook

    # --- TRK-135 / TRK-329: in-app ops-alert RECEIVER -----------------------
    # Bearer token for the INBOUND receiver endpoint POST /webhooks/ops-alert
    # (webhooks.py). This is the destination half of the pair above: the two
    # producers (tools/ops_webhook.py's send_ops_alert and the standalone
    # docker/deadman-prober container) have had complete delivery wiring for
    # months, pointed at nothing, which is how an 8-day collector outage went
    # unnoticed. Setting OPS_WEBHOOK_URL=http://app:8000/webhooks/ops-alert
    # plus OPS_WEBHOOK_TOKEN == this value gives that pipeline a real, zero-
    # external-dependency destination whose alerts land in the dashboard.
    #
    # Deliberately a SEPARATE field from ops_webhook_token: that one is what we
    # SEND (it belongs to whatever third-party sink is configured); this one is
    # what we ACCEPT. They only coincide when the app is pointed at itself, and
    # collapsing them would mean repointing OPS_WEBHOOK_URL at PagerDuty
    # silently handed PagerDuty's token to our own inbound endpoint as well.
    #
    # Empty = the receiver is OFF and answers 503, never 200 and never
    # unauthenticated — an unconfigured receiver must not be an open one. It
    # does NOT follow the webhook_auth_required/infra_brain_dev dev-mode
    # convention of the other webhook secrets for that reason (see
    # webhooks.py::_verify_ops_alert_bearer).
    ops_webhook_receiver_token: str = ""

    # GitLab #111: chatops bot (inbound Slack/Teams -> read-only chat agent).
    # Empty = feature dark (routes 403 fail-closed, same convention as the
    # other webhook secrets). All three must be populated from Bitwarden
    # before this is live in any real Slack/Teams workspace.
    chatops_enabled: bool = False
    slack_signing_secret: str = ""  # HMAC-SHA256 request-signing secret (Slack app config)
    slack_bot_token: str = ""  # xoxb-... bearer token used to POST chat.postMessage replies
    teams_outgoing_webhook_secret: str = ""  # base64 HMAC key from the Teams "Outgoing Webhook" bot
    # lc-safety-reviewer finding: the HMAC signature only proves "this came
    # from our Slack app / Teams bot" -- it says nothing about WHICH human is
    # asking, unlike the dashboard chat path which requires a real
    # UIUser session (dashboard_auth.require_session). Without a scope
    # allowlist, any member of the Slack workspace (including a DM-only
    # guest) gets the full read-only agent surface, and can trigger a real
    # GitLab MR write via propose_host_purpose_update. Comma-separated;
    # empty = deny everything (fail closed, same "must be explicitly
    # configured" convention as the secrets above) -- there is no "allow all"
    # default, by design.
    chatops_allowed_slack_team_ids: str = ""
    chatops_allowed_teams_tenant_ids: str = ""
    # TRK-241 (GitLab #110): after this many consecutive unhealthy
    # check_collection_health() passes for the same domain, the alert string
    # is escalated (prefixed "ESCALATED"). Alert-only — no auto-retry/
    # auto-remediation is triggered by crossing this threshold.
    collection_health_escalation_streak: int = 3

    # Automation
    context7_api_key: str = ""

    # API operational tunables
    api_page_size: int = 100
    api_timeout_seconds: int = 30

    # Sweep graph (Phase 2 orchestration v2.1). Strictly opt-in — disabled
    # behavior (default) must stay byte-identical to the legacy per-domain
    # collector cron + dispatch() paths. Flip to True only after verifying
    # the sweep graph (graph.py) end-to-end in a non-prod environment.
    sweep_graph_enabled: bool = False
    # Cron expression for the single scheduled full sweep job, only
    # registered when sweep_graph_enabled=True. Default: every 4 hours.
    sweep_graph_schedule: str = "0 */4 * * *"

    # RootCause LLM reasoner (Phase 3 Task 2). Strictly opt-in — disabled
    # (default) keeps RootCauseAgent's deterministic timeline correlation
    # byte-identical to today's behavior.
    rootcause_llm_enabled: bool = False
    # Per-run cap on how many unnoted drift events get a full LLM reasoning
    # loop; events beyond the cap (and any event whose LLM path raises) fall
    # back to the deterministic path so a drift storm cannot fan out into
    # hundreds of model calls.
    rootcause_llm_max_events_per_run: int = 20

    # Compliance gap-finder (Phase 3 Task 3). Strictly opt-in — disabled
    # (default) keeps ComplianceAgent's 4 deterministic rules-as-code as the
    # only thing that ever runs. When enabled, an LLM-assisted method runs at
    # the end of collect() to propose NEW rule gaps (never modifies the
    # existing rules); it writes inert ProposedAction rows (action_type
    # "compliance_rule_gap") that the remediation executor's config_fix-only
    # filter never picks up — propose-only.
    compliance_gap_finder_enabled: bool = False

    # Remediation interrupt mini-graph (Phase 3 Task 4). Strictly opt-in —
    # disabled (default) keeps today's poll-only flow byte-identical: drafting
    # creates ProposedAction rows and the daily _execute_approved() poll
    # executes approved ones. When enabled, drafting ALSO starts one durable
    # interrupt graph per action (remediation_graph.py) and approve/reject
    # resumes it immediately; the poll stays registered in BOTH modes as the
    # safety net (spec §2.7 — no approval is ever stranded).
    remediation_interrupt_enabled: bool = False

    # Capacity forecast reasoner (GitLab #99). Strictly opt-in — disabled
    # (default) means CapacityForecastAgent.collect() is a no-op; there is no
    # prior "always-on" behavior to preserve since this is a brand-new agent,
    # same convention as sweep_graph_enabled. When enabled, a deterministic
    # (no-LLM) linear regression runs over the existing
    # vsphere_datastore_metrics time-series to project "days until
    # capacity_forecast_used_pct_threshold% used" per datastore, writing
    # results as Instinct rows (promoted_by="capacity_forecast") — no new
    # table, no new graph edge, matching the issue's own minimal design.
    capacity_forecast_enabled: bool = False
    # Used-percent threshold each datastore's trend is projected against.
    capacity_forecast_used_pct_threshold: float = 90.0

    # TRK-120: lightweight LLM cost/runaway-spend safety net for the AGENT
    # reasoner lane (chat/custom-view already has per-user guards, TRK-090/091 —
    # this closes the gap for scheduled reasoner agents). STRICTLY opt-in
    # (default OFF), matching the reasoner-tier flag convention above. When
    # enabled, LLMAgent.reason() stops making further model calls once a run's
    # cumulative usage_metadata.total_tokens across the run first reaches
    # llm_run_token_ceiling, degrading gracefully to the deterministic/template
    # fallback each caller already has rather than crashing the run.
    llm_run_token_ceiling_enabled: bool = False
    llm_run_token_ceiling: int = 100_000

    # T6 (rev14): per-tool-result character budget for the AGENT ReAct lane
    # (LLMAgent.reason -> _gate_tools). A ReAct loop re-reads the whole
    # transcript every turn, so one oversized tool result is charged again on
    # every remaining turn. Measured on a real DiscoveryAgent run
    # (agent_decision_log): prompt size climbed 17,275 -> 27,159 tokens over
    # five iterations on unbounded gitlab_projects_tool / gitlab_file_tool
    # output, and the run then died on the recursion limit with no answer.
    # This bounds each result before it enters the transcript; the chat lane has
    # had the equivalent bound for some time (chat/tools.py
    # _truncate_tool_result). Applies ONLY to the model transcript — direct
    # tool.invoke()/_run_tool callers and every deterministic collector still
    # receive full, unmodified output, so no truncated value can reach the
    # database or a drift comparison. Raise it if agents are being starved of
    # context; lower it to cut cost. <= 0 falls back to the 8000-char default
    # rather than disabling the bound (unbounded is the bug this fixes).
    # .env: LLM_TOOL_RESULT_MAX_CHARS.
    llm_tool_result_max_chars: int = 8000

    # TRK-182 follow-up: the "applied instinct" confidence gate was hardcoded
    # to 0.7 in two independent places (fleet.py roster count and the
    # dashboard integration-proposals UI). Exposed here as a single Settings
    # field so both surfaces (and any future ones) read the same value
    # instead of drifting apart.
    integration_confidence_gate: float = 0.70

    # Knowledge-graph stale-edge decay (TRK-089). Two settings:
    #
    # is_same_as_decay_days formalizes the IS_SAME_AS decay window that
    # GraphMaintenanceAgent._decay_confidence already read via getattr but was
    # never actually defined here (it always fell back to the module default of
    # 14). Defining it as 14 preserves today's behavior exactly.
    is_same_as_decay_days: int = 14
    # graph_edge_decay_enabled — STRICTLY opt-in. Default False keeps decay
    # behavior byte-identical to today: ONLY IS_SAME_AS edges decay/expire. When
    # True, the heuristic/evidence-derived edge types (VULNERABLE_TO,
    # AFFECTED_BY, TRIGGERED_BY, DEPLOYED_VIA, HAS_SOFTWARE, RUNS_EOL)
    # additionally decay per their configured per-type windows once the evidence
    # that asserted them stops being re-observed. This gating matters because the
    # feature newly *removes* data that previously persisted forever — a bug here
    # could silently delete real relationships — so it is off by default, the
    # same caution applied to the reasoner-tier flags above.
    graph_edge_decay_enabled: bool = False

    # F18: embedded scheduler switch. Default False — the dedicated scheduler
    # service (compose `scheduler` / k8s/scheduler.yaml, replicas:1) is the ONLY
    # scheduler by default. When False the FastAPI process registers NO
    # APScheduler jobs and never starts the in-app scheduler, so unlocked jobs
    # (collection-health alerts, retention prune, drift catch-up, sweep
    # reconciler/reasoner/notification) fire exactly once — not once per API
    # replica (agent-core runs replicas:2 + an HPA). The dead-man heartbeat CHECK
    # loop in main.py runs independently of this flag. Set to True only for a
    # single-process all-in-one deployment that has NO dedicated scheduler.
    embedded_scheduler_enabled: bool = False

    # F20: a collection_run stuck in status="in_progress" longer than this many
    # hours is assumed orphaned (process OOM/SIGKILL died mid-run) and is reaped
    # to status="failed" by the hourly collection-health job. Fresh in-progress
    # runs (younger than this) and terminal runs are never touched.
    collection_run_stale_after_hours: int = 6

    # Collector tunables
    collect_timeout_seconds: int = 300  # per-agent collect() wall-clock limit
    # Comma-separated domain names whose collection should be skipped without
    # raising a runtime error (e.g. "vsphere,windows" while a credential is
    # rotated or a connector is under maintenance).  Matching is case-sensitive
    # and must equal ETLConnector.domain exactly.  BaseAgent.run() raises
    # CollectorSkipped for listed domains so the run is recorded as
    # status="skipped", not status="failed" (F-022).
    collection_disabled_domains: str = ""
    # Comma-separated domain names to turn back ON despite their AgentSpec
    # declaring ``retired=True`` (etl/spec.py). Retirement is a standing
    # decision that an upstream system does not exist in this environment; this
    # is the escape hatch that reverses it WITHOUT a code change to a critical
    # file. The override only ever turns a collector ON — nothing here can
    # retire a live domain (use collection_disabled_domains for a static skip,
    # or the dispatchable__<domain> runtime row for a live pause). Matching is
    # case-sensitive and must equal ETLConnector.domain exactly. See
    # ``etl.spec.retired_domains()``.
    collection_revived_domains: str = ""
    # Ansible fact-gathering tunables (F-017). The facts timeout must stay below
    # collect_timeout_seconds so the subprocess dies (with a clear message)
    # before BaseAgent.run()'s thread-pool guard fires.
    ansible_facts_timeout_seconds: int = 270
    ansible_ping_timeout_seconds: int = 60
    ansible_ping_scope_enabled: bool = True
    # Parallelism + per-host SSH connect timeout for ansible runs. Default forks
    # (5) against a large fleet (e.g. 175 hosts) serializes into >60s of connect
    # attempts — every unreachable host stalls a fork on the default 10s connect
    # timeout — so the ping preflight blew its ansible_ping_timeout_seconds. A
    # higher fork count + a short connect timeout keeps a fleet-wide ping/gather
    # inside the window. Read-only: parallelism/timeout only, no module change.
    ansible_forks: int = 30
    ansible_connect_timeout_seconds: int = 8
    # SSH args exported as ANSIBLE_SSH_ARGS for every (read-only) ansible run.
    # Re-enables legacy host-key/kex algorithms so negotiation succeeds against
    # old fleet servers that only offer ssh-rsa/ssh-dss (modern OpenSSH rejects
    # them: "no matching host key type"). Security note: this widens accepted
    # algorithms for the ansible connection only; set to "" to disable if the
    # fleet is all-modern. Auth still requires a valid, authorized key.
    ansible_ssh_args: str = (
        "-o ControlMaster=auto -o ControlPersist=60s "
        "-o HostKeyAlgorithms=+ssh-rsa "
        "-o PubkeyAcceptedKeyTypes=+ssh-rsa "
        "-o KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1"
    )
    # WinRM credentials for WindowsAgent (F-021). Empty ansible_win_password →
    # WindowsAgent raises CollectorSkipped (no-op, not a failure). The password
    # is injected into the subprocess env only — never placed in argv.
    ansible_win_user: str = "Administrator"
    ansible_win_password: str = ""
    iac_max_projects: int = 100  # max GitLab projects IaCAgent will scan per run
    # Comma-separated GitLab group IDs to scan for IaC (e.g. "35,15,27,63").
    # When set, only projects inside those groups are scanned — this reaches repos
    # the token can see but isn't a direct member of. When empty, falls back to
    # all projects visible to the token.
    iac_group_ids: str = ""

    # Security
    dlp_fail_closed: bool = True
    scan_readonly_enforce: bool = True
    integration_approval_required: bool = True

    # Zones
    default_zone: str = "corpor"

    # Kubernetes
    k8s_cluster_name: str = ""  # agents/k8s.py: cluster label for multi-cluster upserts

    # Script execution (sandboxed, read-only)
    scripts_enabled: bool = False
    script_timeout_seconds: int = 30

    # Dashboard auth + seeding
    ui_cookie_secret: str = ""  # signs session cookies; empty = dev-mode (no login required)
    admin_username: str = "admin"
    admin_password: str = ""  # empty = no admin user seeded

    # Script library — GitLab-backed script store (Task F1)
    script_library_project_id: int = 0
    script_library_branch: str = "main"

    # Cloud / AWS
    aws_enabled: bool = False

    # Operational
    infra_ops_observe: bool = True

    # Webhook secrets (empty = dev mode, allow all unless webhook_auth_required=True)
    webhook_gitlab_secret: str = ""
    webhook_octopus_secret: str = ""
    webhook_ansible_secret: str = ""
    webhook_generic_secret: str = ""
    # TRK-242 (GitLab #113): shared secret for the incident-management ack-in
    # webhook (POST /webhooks/incident/ack) — same fail-closed _verify_token
    # convention as the other webhook secrets above.
    webhook_incident_secret: str = ""
    # C4: when True and a webhook secret is unset, _verify_token fails closed (403)
    # instead of allowing the request through.  Default True fails closed in prod;
    # dev must set INFRA_BRAIN_DEV=1 to keep the open path (see infra_brain_dev).
    webhook_auth_required: bool = True

    # Explicit dev switch. The ONLY thing that opens dev-mode auth paths (dashboard
    # open-auth, webhook empty-secret allow). Historically "dev-mode" meant "a secret
    # happened to be unset", which silently opened prod. Now dev is opt-in and loud.
    infra_brain_dev: bool = False

    # F23: deployment environment guard. "deployed" | "development". Default is
    # "development" so bare `python -m` runs, the test suite, and the local compose
    # stack (which opts into dev-mode auth via INFRA_BRAIN_DEV=1) all boot without
    # extra config. Hardened deployments (today: the dev deployment on
    # deploy-host-01 — there is no production environment yet) MUST set
    # ENVIRONMENT=deployed explicitly (see k8s/configmap.yaml and the compose
    # deploy overlay). When is_hardened_environment(environment) AND
    # infra_brain_dev is truthy, create_app() and the MCP server REFUSE to boot —
    # the dev master kill-switch (which disables dashboard/webhook/MCP auth) must
    # never be active on a deployed stack. "production" is kept as a legacy alias
    # of "deployed" so an older host-side .env can never silently weaken the guard.
    environment: str = "development"

    # Controls the Secure attribute on session cookies. Default True = safe for HTTPS.
    # Set COOKIE_SECURE=false for HTTP-only lab/dev deployments where Secure cookies
    # would be silently dropped by the browser and logins would never persist.
    cookie_secure: bool = True

    # TRK-095: comma-separated proxy IPs (or CIDR networks) whose X-Forwarded-For
    # header is trusted for deriving the client IP used in the login-lockout key.
    # SAFE DEFAULT = "" = trust NO proxy: X-Forwarded-For is ignored entirely and
    # the raw immediate-peer IP (request.client.host) is used, so a client cannot
    # spoof its attributed IP by injecting the header. Set this ONLY to the address(es)
    # of the reverse proxy/load-balancer that actually fronts this deployment (mirrors
    # uvicorn ProxyHeadersMiddleware's forwarded_allow_ips). When the immediate peer
    # is in this set, X-Forwarded-For is honored and trusted proxies are peeled off
    # the right to recover the real client IP. See dashboard_auth._lockout_client_ip.
    # Requires the fronting proxy to COLLAPSE/APPEND into a single X-Forwarded-For
    # chain (e.g. nginx `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`)
    # so the left-to-right hop order is correct for attribution.
    trusted_proxy_ips: str = ""

    # TRK-095 (residual): comma-separated list of Host header values the app will
    # serve. Wired into Starlette's TrustedHostMiddleware by the FastAPI app
    # factory (main.create_app) to reject requests carrying a spoofed/mismatched
    # Host header (DNS-rebinding / Host-header injection defense).
    # OPT-IN, SAFE DEFAULT = "" = ["*"] = accept ANY Host header — byte-for-byte the
    # pre-TRK-095 behavior, so an existing deployment is never broken by upgrading.
    # A deny-by-default host list would break deployments whose real allowed-hosts
    # set is unknown here, so — exactly like trusted_proxy_ips above (default =
    # trust-none) — hardening is enabled explicitly via config. Set this to the
    # deployment's public hostname(s), e.g. "infra-brain.corp.example,localhost".
    # Parse via parse_trusted_hosts(); entries are stripped and blanks dropped.
    trusted_hosts: str = ""

    # --- BEGIN secrets-manager inventory (GitLab issue #98) -------------------
    # METADATA-ONLY polling of an EXTERNAL secrets manager (Vault / Bitwarden
    # Secrets Manager). Distinct from infra-brain's OWN secret loading in
    # secrets.py — nothing here is used to fetch a secret infra-brain consumes,
    # and the collector (agents/secrets_inventory.py) is structurally incapable
    # of calling a value-returning endpoint.
    # Empty backend (the default) = feature off; the collector records
    # status="skipped" rather than failing.
    secrets_inventory_backend: str = ""  # "" | "vault" | "bitwarden" | "aws"
    secrets_inventory_url: str = ""
    # Read token for the LIST/metadata endpoints only. Grant it a policy with
    # metadata read + list capability and NOTHING on the data/value path.
    secrets_inventory_token: str = ""
    # Vault: comma-separated KV-v2 mount/path prefixes to enumerate, e.g.
    # "kv/apps,kv/platform". Bitwarden: comma-separated organization ids.
    secrets_inventory_paths: str = ""
    secrets_inventory_ssl_verify: bool = True
    # Hard cap on secrets enumerated per run (bounded fan-out, like
    # rapid7_vuln_asset_cap) — never silently unbounded.
    secrets_inventory_max_secrets: int = 2000
    # --- END secrets-manager inventory ---------------------------------------

    # --- BEGIN MCP interactive-tool wall-clock ceiling (GitLab #159/#165) -----
    # An MCP tool call is INTERACTIVE: a client (Claude, an IDE, a chat UI) is
    # blocked waiting on it, and every such client has its own transport
    # timeout. Before this setting existed, `query_nl` -> QueryAgent.query() ->
    # LLMAgent.reason() had NO wall-clock bound at all: it bypasses
    # ETLConnector.run()'s `_call_with_timeout` guard entirely, so the only
    # bound was per-CALL (llm_request_timeout_seconds, above) multiplied by
    # max_iters model turns multiplied by REASON_MAX_ATTEMPTS retries — a worst
    # case of ~2880s. The client's transport timeout always fired first, with
    # zero error surfaced to anyone: issue #165's silent hang.
    # This is the TOTAL wall-clock budget handed to reason(deadline_seconds=...)
    # for an MCP-initiated reasoning call. reason() checks it between model
    # turns/retries and returns a clear timeout error of its own instead of
    # being killed from outside with no diagnostic signal. Keep it comfortably
    # below the calling client's transport timeout. 0 disables (unbounded,
    # pre-#165 behavior) — not recommended.
    # .env: MCP_TOOL_TIMEOUT_SECONDS.
    # GitLab #172: a negative value silently disabled the deadline too (the
    # `> 0` check in agents/query.py evaluates False), but only 0 is
    # documented as the intentional "unbounded" opt-out above -- reject
    # negative values at startup instead of letting them behave like 0 by
    # accident.
    mcp_tool_timeout_seconds: int = Field(default=60, ge=0)
    # --- END MCP interactive-tool wall-clock ceiling --------------------------

    @field_validator("infra_brain_dev", "cookie_secure", mode="before")
    @classmethod
    def _coerce_empty_string_bool(cls, v: object) -> object:
        """Coerce empty-string and None to False for bool fields.

        docker-compose.yml uses ``${VAR:-}`` (POSIX default-empty) so that when a
        variable is unset in the host environment Docker Compose injects ``VAR=``
        (empty string) into the container.  Pydantic v2 cannot parse ``""`` as bool
        and raises a ValidationError that crash-loops uvicorn.  Treating empty/None
        as False restores the intended "unset → off" semantics while still allowing
        "1"/"true"/"0"/"false" to reach normal bool coercion.
        """
        if v is None or v == "":
            return False
        return v

    # Data retention (F-030). PCI DSS 10.7 requires >= 1 year of audit-trail
    # history; 400 days gives a margin. Enforced by the daily prune job
    # (infra_brain/retention.py, scheduled 04:30 UTC). See docs/RETENTION-POLICY.md.
    retention_prune_enabled: bool = True
    retention_audit_log_days: int = 400
    retention_agent_action_log_days: int = 400
    retention_snapshots_days: int = 90
    retention_drift_events_days: int = 180
    retention_collection_runs_days: int = 180
    # Checkpoint-row retention (F-030 / TRK-069). LangGraph-owned tables
    # (checkpoints/checkpoint_writes/checkpoint_blobs) have no created_at
    # column; age is the newest checkpoint->>'ts' per thread_id. Threads with
    # a pending/approved ProposedAction are never pruned regardless of age.
    retention_checkpoints_days: int = 30

    # LangSmith / observability
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "infra-brain"
    # TRK-042/OB-7: no cloud default — an empty endpoint means "tracing not
    # configured," and init_tracing() (observability.py) refuses to enable
    # tracing against an empty endpoint instead of silently egressing to the
    # public LangSmith cloud. Set this explicitly (e.g. a self-hosted
    # LangSmith/Langfuse-compatible endpoint) to opt back in.
    langsmith_endpoint: str = ""

    # Langfuse (Phase 4 Task 2: app-side tracing wiring, OB-7). Strictly
    # opt-in — disabled (default) means maybe_langfuse_handler() never
    # imports the langfuse package and both callback chains stay unchanged.
    langfuse_enabled: bool = False
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # --- M-4 additive block: notification-sweep emission cap (TRK) ---------
    # notification.py's notify_all() used to loop every open DriftEvent /
    # ComplianceViolation with zero bound, issuing one Jira HTTP call + one
    # commit PER ROW — a drift flood could attempt tens of thousands of
    # webhook/Jira calls in a single sweep. This caps how many notifications
    # ONE sweep may emit per sink; any remainder is left `open` (picked up by
    # the next sweep, never silently dropped) and the truncation is recorded
    # as an explicit audit-trail warning row, not silently swallowed.
    notification_max_per_sweep: int = 1000

    @field_validator("postgres_url")
    @classmethod
    def _require_postgres_url(cls, v: str) -> str:
        """Fail loud on an unset DSN instead of falling back to localhost.

        An empty POSTGRES_URL almost always means the env_file/DATABASE_URL wiring
        broke (the exact condition that let the migrate service run against an
        empty localhost DB and ship a drifted schema green). Aborting here turns
        that silent divergence into an immediate, obvious startup failure.

        Tests run with no .env (conftest disables it) and must still construct
        Settings, so they supply a DSN via the POSTGRES_URL env var (see
        tests/conftest.py, which sets a SQLite test DSN). This keeps the single
        source of truth intact — the value still flows through this one field.
        """
        if not v or not v.strip():
            raise ValueError(
                "POSTGRES_URL is required and must not be empty. Set it in the "
                "environment / .env (the migrate, app, scheduler, and mcp services "
                "all read it). There is no localhost fallback — an unset DSN would "
                "silently target the wrong database and ship schema drift green."
            )
        return v


# F23: ENVIRONMENT values that mean "hardened deployment" — dev-mode auth bypasses
# must refuse to boot. "deployed" is the canonical value; "production" is a legacy
# alias (the pre-2026-07-17 name, before the deploy-host-01 stack was correctly
# relabeled as the dev deployment) kept so an older host-side .env can never
# silently weaken the guard.
HARDENED_ENVIRONMENTS = frozenset({"deployed", "production"})


def is_hardened_environment(value: str) -> bool:
    return value.strip().lower() in HARDENED_ENVIRONMENTS


def parse_trusted_hosts(raw: str | None) -> list[str]:
    """Parse the comma-separated ``trusted_hosts`` setting into an allow-list.

    OPT-IN, SAFE DEFAULT: an unset/empty/whitespace-only value yields ``["*"]``,
    which TrustedHostMiddleware treats as "accept any Host header" — preserving
    the pre-TRK-095 behavior so upgrading never breaks an existing deployment.
    When set, entries are split on commas, stripped, and blank entries dropped
    (mirroring how collection_disabled_domains / trusted_proxy_ips are handled).
    """
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    return parts or ["*"]


# ---------------------------------------------------------------------------
# TRK-303 part 2/7: RuntimeConfig-aware settings resolver
#
# get_settings() used to be a bare @lru_cache over Settings(). It is now a
# TTL-polled resolver that layers admin-editable ``runtime_config`` DB rows on
# top of the env/default Settings. The public contract is unchanged:
#   * same signature, ``get_settings() -> Settings``
#   * ``get_settings.cache_clear()`` still exists and still forces a re-read
#     (secrets.py calls it right after loading Bitwarden secrets into os.environ)
#
# IMPORT-CYCLE / RECURSION NOTE (verified before writing this): every engine
# helper in ``db/session.py`` (``get_engine``, ``get_readonly_engine``,
# ``_pg_pool_kwargs``) calls ``get_settings()`` internally. The override fetch
# below therefore CANNOT reuse them — doing so would recurse infinitely. It
# builds a standalone, short-lived engine from a raw env-only ``Settings()``
# instance and disposes of it per refresh. ``db.models`` does not import this
# module, so the deferred import inside the fetch is cycle-free.
#
# This layer is strictly optional: any failure (table not yet migrated, DB
# unreachable, malformed row) degrades to the plain env/default Settings. It
# must never be able to hard-fail application startup.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Non-overridable settings (the runtime-config denylist).
#
# The runtime_config layer is an ADMIN CONVENIENCE. It must never be a path to
# weaken infra-brain's core read-only / auth guarantees (CLAUDE.md "Core
# guarantee", docs/READONLY-MODEL.md): a DB row must not be able to turn the
# read-only boundary into warn-only, put DLP into fail-open, disable webhook or
# dashboard auth, flip the deployed-environment guard, or repoint the DB the
# resolver itself reads from.
#
# Enforced in TWO independent places, deliberately:
#   1. here, in _load_runtime_overrides() — a row that reaches the table by ANY
#      route (direct psql, restored dump, a future writer) is ignored; and
#   2. in api/routers/runtime_config.py's PUT handler — a denylisted key is
#      rejected with 403 before it is ever persisted.
# Neither layer is allowed to be the only one: (1) alone would let junk rows
# accumulate silently, (2) alone would be bypassed by anything that is not the
# REST API.
#
# Membership rule — a field belongs here if it EITHER gates a safety / auth /
# read-only check, OR the resolver itself depends on it to build its own DB
# connection (a self-referential override could otherwise point the resolver at
# a DB that supplies its own next set of overrides).
_NON_OVERRIDABLE: frozenset[str] = frozenset(
    {
        # --- read-only + DLP + write-approval boundary --------------------
        "scan_readonly_enforce",  # False => read-only violations only warn
        "dlp_fail_closed",  # False => DLP findings only warn
        "integration_approval_required",  # False => sanctioned writes skip approval
        "scripts_enabled",  # True => enables the script-execution surface
        # --- the resolver's own bootstrap ---------------------------------
        "postgres_url",  # the DB these very rows are read from
        "postgres_readonly_url",  # SELECT-only role for the SQL agent
        "runtime_config_encryption_key",  # key that decrypts secret rows
        "redis_url",  # backs session-revocation + login-lockout state
        # --- environment / dev-mode kill switches -------------------------
        "environment",  # deployed-vs-development hardening guard
        "infra_brain_dev",  # master dev switch: opens dashboard/webhook/MCP auth
        # --- webhook auth --------------------------------------------------
        "webhook_auth_required",
        "webhook_gitlab_secret",
        "webhook_octopus_secret",
        "webhook_ansible_secret",
        "webhook_generic_secret",
        "webhook_incident_secret",
        # TRK-135/TRK-329: gates the inbound ops-alert receiver's bearer check.
        # An overridable value here would let a runtime_config row silently
        # rotate (or blank) the token that authenticates the only alert
        # destination this deployment has.
        "ops_webhook_receiver_token",
        # --- dashboard auth ------------------------------------------------
        "ui_cookie_secret",  # empty => dev-mode, no login required
        "admin_username",
        "admin_password",
        "cookie_secure",
        "trusted_proxy_ips",  # X-Forwarded-For spoofing / lockout attribution
        "trusted_hosts",  # Host-header / DNS-rebinding defense
        # --- chatops request authentication --------------------------------
        "slack_signing_secret",
        "slack_bot_token",
        "teams_outgoing_webhook_secret",
        "chatops_allowed_slack_team_ids",
        "chatops_allowed_teams_tenant_ids",
        # --- netdiscovery/nmap gate (the one subprocess surface that cannot
        # be made structurally read-only — audited by convention instead, see
        # CLAUDE.md's "Core guarantee" section and _gate_nmap_targets in
        # agents/netdiscovery.py). These are the gate's own inputs: emptying
        # exclude_cidrs or widening include/fragile removes the fence around
        # protected ranges within one 15s TTL, no redeploy required. ---------
        "netdiscovery_exclude_cidrs",
        "netdiscovery_include_cidrs",
        "netdiscovery_fragile_cidrs",
        "netdiscovery_fragile_hosts",
        "netdiscovery_enable_port_scan",
        # --- audit trail retention (Layer 3 of the read-only model) --------
        "retention_audit_log_days",
    }
)


def runtime_config_encryption_key(base: "Settings | None" = None) -> str:
    """Return the Fernet key for ``runtime_config`` secret rows.

    ALWAYS sourced from env/defaults, never from the resolved (DB-layered)
    Settings — both the encrypt side (the REST PUT handler) and the decrypt
    side (``_load_runtime_overrides``) go through this one function so they
    cannot drift into a split-brain where one uses a DB-supplied key and the
    other does not. ``runtime_config_encryption_key`` is additionally in
    ``_NON_OVERRIDABLE``; this function does not *rely* on that — it is
    explicit about the sourcing rather than depending on the denylist to make
    the two sides coincidentally agree.

    ``base`` lets a caller that already holds an env-only ``Settings`` reuse it
    instead of paying for a second identical construction.
    """
    return (base if base is not None else Settings()).runtime_config_encryption_key


_RUNTIME_CONFIG_TTL_SECONDS = 15.0
# Both bounds matter. connect_timeout caps how long establishing the TCP/auth
# handshake can take; statement_timeout caps the QUERY once the connection is
# accepted. Without the second one a DB that accepts connections but then hangs
# would block the refreshing caller indefinitely — and get_settings() is called
# synchronously from ~130 sites, many inside async FastAPI handlers, so an
# unbounded stall there freezes the whole event loop, not just one request.
_RUNTIME_CONFIG_CONNECT_TIMEOUT_S = 3
_RUNTIME_CONFIG_STATEMENT_TIMEOUT_MS = 3000

# runtime_config key prefixes that are known NOT to be Settings fields — see the
# resolver loop below. Kept as literals rather than imported from their owning
# module (runtime_flags.DISPATCHABLE_KEY_PREFIX) because that module imports
# db/session.py, which imports this one — the import back would be circular.
_NON_SETTINGS_KEY_PREFIXES = ("dispatchable__",)

# Cache and its timestamp live in ONE tuple so a reader always gets a consistent
# pair with a single atomic attribute read — no lock, no torn (new value, old
# timestamp) combination.
_settings_cache: tuple[Settings, float] | None = None
# Re-entrant: _clear_settings_cache() takes the same lock, and secrets.py may
# call cache_clear() from a context that already holds it. A plain Lock would
# deadlock there.
_settings_lock = threading.RLock()
# Re-entrancy guard: if anything reached from the override fetch ever calls
# get_settings() again, serve the env-only Settings instead of recursing.
_resolving = threading.local()
# Log-level throttle: warn loudly the FIRST time the override layer breaks (and
# again after any recovery), then drop to debug so a permanently-absent table
# cannot spam the log every TTL window.
_overrides_failing = False
# TypeAdapters are moderately expensive to build and this runs on the request
# path; one per field, built once.
_type_adapters: dict[str, TypeAdapter] = {}


def _validated_override(key: str, raw: str) -> object:
    """Validate ``raw`` against the *real* annotation of ``Settings.<key>``.

    Deliberately ignores the row's self-declared ``value_type``: that column is
    admin-supplied metadata, not a guarantee. ``model_copy(update=...)`` performs
    NO validation, so trusting ``value_type`` would let a single mistyped row put
    a ``str`` on an ``int`` field process-wide — surfacing as a TypeError
    somewhere unrelated up to a TTL later, with nothing pointing back at config.
    The field annotation is the only authority. Raises on mismatch.
    """
    adapter = _type_adapters.get(key)
    if adapter is None:
        adapter = TypeAdapter(Settings.model_fields[key].annotation)
        _type_adapters[key] = adapter
    return adapter.validate_python(raw)


def _load_runtime_overrides(base: "Settings | None" = None) -> dict[str, object]:
    """Read ``runtime_config`` rows and validate each against its Settings field.

    ``base`` is the caller's already-constructed env-only Settings, passed in to
    avoid building a second identical one per refresh.

    Returns ``{}`` — never raises — if the table does not exist yet
    (pre-migration), the DB is unreachable, or the rows are unusable.
    """
    global _overrides_failing
    try:
        import sqlalchemy
        from sqlalchemy.orm import Session as _Session

        from infra_brain.db.models import RuntimeConfig

        # Raw env-only instance: never the cached/resolved one (that is what we
        # are in the middle of computing).
        if base is None:
            base = Settings()
        url = base.postgres_url
        connect_args: dict[str, object] = {}
        if url.startswith("postgres"):
            connect_args["connect_timeout"] = _RUNTIME_CONFIG_CONNECT_TIMEOUT_S
            connect_args["options"] = f"-c statement_timeout={_RUNTIME_CONFIG_STATEMENT_TIMEOUT_MS}"
        elif url.startswith("sqlite"):
            # sqlite has no statement_timeout; this bounds lock-wait instead.
            connect_args["timeout"] = _RUNTIME_CONFIG_CONNECT_TIMEOUT_S
        eng = sqlalchemy.create_engine(url, pool_pre_ping=False, connect_args=connect_args)
        try:
            with _Session(eng) as session:
                rows = [
                    (r.key, r.value, r.is_secret, r.encrypted_value, r.value_type)
                    for r in session.query(RuntimeConfig).all()
                ]
        finally:
            eng.dispose()
    except Exception as exc:  # noqa: BLE001 — optional layer, must degrade quietly
        if _overrides_failing:
            log.debug("runtime_config overrides still unavailable: %s", exc)
        else:
            _overrides_failing = True
            log.warning(
                "runtime_config overrides unavailable — falling back to env/defaults: %s", exc
            )
        return {}
    _overrides_failing = False

    fields = Settings.model_fields
    out: dict[str, object] = {}
    for key, value, is_secret, encrypted_value, value_type in rows:
        # Denylist FIRST — before decryption, before the field-existence check.
        # This is the layer that catches a row inserted by anything other than
        # the REST API (direct psql, a restored dump). See _NON_OVERRIDABLE.
        if key in _NON_OVERRIDABLE:
            log.warning(
                "runtime_config key %r is non-overridable (safety/auth/bootstrap "
                "setting) — ignored; the env/default value stands",
                key,
            )
            continue
        if is_secret:
            if not runtime_config_encryption_key(base) or encrypted_value is None:
                # No key configured yet, or row has no ciphertext — skip
                # quietly rather than crash the whole resolve.
                continue
            try:
                from cryptography.fernet import Fernet, InvalidToken

                raw = (
                    Fernet(runtime_config_encryption_key(base).encode())
                    .decrypt(encrypted_value)
                    .decode()
                )
            except InvalidToken:
                # Wrong/rotated key — skip this override rather than raising.
                log.warning("runtime_config key %r failed decryption — ignored", key)
                continue
        else:
            raw = value
            if raw is None:
                continue
        # An unknown key would be grafted onto Settings as a bogus attribute.
        if key not in fields:
            # Some runtime_config keys are deliberately NOT Settings fields —
            # they are dynamic per-domain rows read directly by their owning
            # module (dispatchable__<domain>, read by runtime_flags.py). They
            # are not typos, and warning on each of them every TTL poll (~every
            # 15s, per worker, forever) drowns out the genuine typo detection
            # this warning exists for. Skip them silently instead.
            if not key.startswith(_NON_SETTINGS_KEY_PREFIXES):
                log.warning("runtime_config key %r is not a Settings field — ignored", key)
            continue
        try:
            out[key] = _validated_override(key, raw)
        except Exception as exc:  # noqa: BLE001 — a bad row must not break config
            if is_secret:
                # NEVER log `raw` here — for a secret row it is decrypted
                # plaintext, and this branch fires precisely when the value is
                # malformed (a mistyped/pasted credential is the common case).
                # The exception is withheld too: pydantic's ValidationError
                # embeds the offending input in its own message, so logging
                # `exc` would leak the plaintext just as surely as logging
                # `raw`. Key + declared value_type + exception CLASS is enough
                # for an operator to find and re-enter the row.
                log.warning(
                    "runtime_config secret key %r (declared value_type %r) does not "
                    "validate against its Settings field type — ignored (%s; value "
                    "and error detail withheld: secret row)",
                    key,
                    value_type,
                    type(exc).__name__,
                )
            else:
                log.warning(
                    "runtime_config key %r value %r does not validate against its Settings "
                    "field type — ignored: %s",
                    key,
                    raw,
                    exc,
                )
    return out


def get_settings() -> Settings:
    global _settings_cache

    if getattr(_resolving, "active", False):
        # Re-entered from inside the override fetch — env-only, no recursion.
        return Settings()

    snapshot = _settings_cache
    if snapshot is not None and (time.monotonic() - snapshot[1]) < _RUNTIME_CONFIG_TTL_SECONDS:
        return snapshot[0]

    # Serve-stale-while-revalidating. Only the ONE caller that wins the lock pays
    # the DB round-trip; everyone else immediately gets the previous value rather
    # than queueing behind blocking I/O. This is what keeps an expired TTL from
    # stalling every in-flight request in the process (see the timeout note
    # above). A cold cache has nothing stale to serve, so it must block.
    if not _settings_lock.acquire(blocking=snapshot is None):
        return snapshot[0]
    try:
        snapshot = _settings_cache
        if snapshot is not None and (time.monotonic() - snapshot[1]) < _RUNTIME_CONFIG_TTL_SECONDS:
            return snapshot[0]

        base = Settings()
        _resolving.active = True
        try:
            overrides = _load_runtime_overrides(base)
        finally:
            _resolving.active = False

        resolved = base.model_copy(update=overrides) if overrides else base
        _settings_cache = (resolved, time.monotonic())
        return resolved
    finally:
        _settings_lock.release()


def _clear_settings_cache() -> None:
    """Drop the resolved-settings cache so the next call re-reads env + DB.

    Named ``cache_clear`` on the function object for backwards compatibility —
    every existing ``get_settings.cache_clear()`` call site (secrets.py, the
    test suite's autouse fixtures) keeps working unchanged.
    """
    global _settings_cache
    with _settings_lock:
        _settings_cache = None


get_settings.cache_clear = _clear_settings_cache
