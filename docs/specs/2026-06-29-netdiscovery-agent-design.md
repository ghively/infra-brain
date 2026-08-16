# Spec — `netdiscovery`: Active Network, Shadow-IT & Threat Discovery Agent
Date: 2026-06-29 · Status: shipped · Repo: agents/infra-brain

## 1. Goal / mission
Continuously discover and document **every device and service reachable on the network**, track them over time, and surface **shadow IT and unauthorized / potentially malicious presence** — unmanaged machines, rogue devices, unsanctioned tools, attacker-associated services, and anything unaccounted-for. A security discovery + threat-hunting capability (supports PCI asset-inventory completeness and unauthorized-device detection). Aggressive coverage, but **non-disruptive by default**.

## 2. Non-goals
- Not a vulnerability scanner (Rapid7/`vuln` owns CVE scanning).
- Not a host-fact collector (`linux`/`windows` own SSH/WinRM enrichment of known hosts).
- No exploitation / active intrusion — discovery, fingerprinting, and detection only; never attacks or alters targets.

## 3. Hard constraints
- **Propose, never dispose:** records and proposes findings (e.g. inventory MRs); never changes target hosts.
- **Non-disruptive by default:** default settings must not crash fragile devices or trip aggressive IDS/IPS alerts. Intrusive techniques (OS fingerprinting, NSE scripts, UDP scans, fast timing) are OFF unless aggressiveness is explicitly raised.
- **Self-contained safety:** the infra-ops `scan-boundary-guard` hook does NOT run in this container; all guard logic lives in this agent.
- **Zone reality:** the HSA zone is air-gapped and unreachable — self-enforcing. The residual is the cardholder data environment (CDE): if a CDE segment is routable, active scanning into it is PCI-significant; the exclusion list is the release valve. (Accepted operating decision.)

## 4. Architecture
- New `NetDiscoveryAgent(BaseAgent)` in `src/infra_brain/agents/netdiscovery.py`, domain `netdiscovery`. Deterministic (NOT an LLMAgent).
- Registered in `supervisor.py` `AGENT_REGISTRY`; scheduled in `scheduler.py` `_DEFAULT_SCHEDULES`.
- `collect(self, scope)` resolves reachable scope, runs the enabled tiers at the configured aggressiveness, classifies findings (known / shadow-IT / threat), and persists via dedicated detail tables + generic `Resource` rows.

## 5. Scope & safety model
**Scope = reachability (no allowlist).** The agent enumerates the host's network interfaces and routing table to determine every directly-connected and routed network it can reach, and watches all of them. Intent: "if it can reach the network, it watches it."
- `NETDISCOVERY_EXCLUDE_CIDRS` (list, default empty) — the only never-probe carve-out (release valve for CDE / sensitive ranges).
- `_should_probe(ip) -> bool`: fail-closed guard before EVERY probe; False if IP is in an excluded CIDR or on parse error/ambiguity. Pure, exhaustively unit-tested.
- Reachable-scope discovery (interfaces/routes) unit-tested with mocked route tables.

**Tier enablement (active ships ON):** `NETDISCOVERY_ENABLE_PING_SWEEP` (default true), `NETDISCOVERY_ENABLE_PORT_SCAN` (default true) — present so scanning can be disabled if needed; enabled by default. Passive tier always on.

## 6. Aggressiveness profiles (the safety dial)
`NETDISCOVERY_AGGRESSIVENESS`: `passive` | `polite` (DEFAULT) | `normal` | `aggressive`. Maps to concrete nmap/probe settings:
- **passive** — Tier 0 only (DNS + DB correlation); no packets to target hosts.
- **polite (DEFAULT — "active but not intrusive")** — host discovery via ICMP + on-segment ARP + TCP-connect to a small common-port set; `nmap -T2` with `--max-rate` capped and `--scan-delay`; light service detection on ~top-100 TCP ports with `-sV --version-intensity 0`. **No OS fingerprint (-O), no NSE scripts, no UDP.**
- **normal** — top-1000 TCP ports, `-T3`, `-sV` default intensity, light OS detection.
- **aggressive** — full TCP range, `-T4`, intensive `-sV` + OS detection. Opt-in only.
Rate controls (apply at all active levels): `NETDISCOVERY_MAX_RATE` (packets/sec cap, conservative default), `NETDISCOVERY_SCAN_DELAY`, optional `NETDISCOVERY_PORT_SCAN_WINDOW` to bound heavy scans to a maintenance window.

**Fragile-device protection (don't-break-infra, overrides the profile):** a fragile-class detector identifies printers, iDRAC/BMC, OT/SCADA, and embedded devices via MAC OUI, banner/port signature, or explicit `NETDISCOVERY_FRAGILE_CIDRS` / `NETDISCOVERY_FRAGILE_HOSTS`. Any host classed fragile is probed at **ICMP / single-port reachability only, regardless of aggressiveness** — never port/version/OS scanned. (iDRACs already exist in this environment.)

## 7. Discovery tiers
- **Tier 0 — Passive (always on):** harvest known IPs from `r7_asset`, `vsphere_vm`, `HostIdentity.ip_addresses`; enumerate reachable subnets; reverse/forward DNS via `NETDISCOVERY_DNS_RESOLVER`; PTR-sweep reachable ranges. Output: resolved names, candidate IPs, accounted-for baseline.
- **Tier 1 — Active host discovery (on by default):** host + ARP sweep across reachable subnets at the configured profile, each target through `_should_probe`. Output: live IPs + MAC (on-segment) + MAC OUI/vendor.
- **Tier 2 — Active service/OS scan (on by default):** service-version (+OS only at normal/aggressive) on live hosts, rate-limited, profile- and fragility-aware. Output: open ports, banners, OS guess.

## 8. Data model (dedicated relational tables — not flat JSON)
- `net_discovery_hosts`: id, ip, mac (nullable), mac_vendor (nullable), hostname (nullable), first_seen, last_seen, responded (bool), discovery_tier, is_fragile (bool), **is_known** (bool), **is_shadow_it** (bool), **threat_level** (none/low/med/high), resource_id (FK), zone.
- `net_discovery_services`: id, host_id (FK), port, proto, service, banner (nullable), fingerprint (nullable), **is_dangerous** (bool), **is_suspicious** (bool), last_seen.
- **Classification:** `is_known` = correlates to a sanctioned source (Ansible inventory, `r7_asset`, `vsphere_vm`, `OctopusMachine`, `HostIdentity`) via IS_SAME_AS / hostname+IP. `is_shadow_it` = live & reachable but matches no sanctioned source. `threat_level` raised by: unknown MAC OUI, unauthorized remote-access / attacker-associated services, known host in an unexpected segment, brand-new host since last sweep.
- **Watchlists (configurable):** `NETDISCOVERY_DANGEROUS_PORTS` (telnet/23, SMBv1/445, exposed RDP/3389, …) → `is_dangerous`; `NETDISCOVERY_SUSPICIOUS_SIGNATURES` (remote-access tools, C2-style ports/banners) → `is_suspicious` + threat bump.
- **DriftEvents:** new shadow-IT host; new dangerous/suspicious service; known host in unexpected segment; threat_level escalation.
- **Edges:** emit `IS_SAME_AS` to correlate/dedup against existing assets (reuse host-reconcile helpers).
- Generic `Resource` rows: type `discovered_host`/`discovered_service`, domain `netdiscovery` (so dashboard + `query_resources` surface them).

## 9. Integration
- `host_reconcile`: new source feeding `HostIdentity`.
- `inventory_reconcile`: confirmed known-class uninventoried hosts → add-only inventory MR proposals (shadow-IT hosts flagged, NOT auto-proposed).
- `DiscoveryAgent` (LLM): consumes netdiscovery findings as another correlation source.

## 10. Config reference (env vars)
| Var | Default | Purpose |
|---|---|---|
| NETDISCOVERY_AGGRESSIVENESS | polite | Safety dial: passive/polite/normal/aggressive |
| NETDISCOVERY_EXCLUDE_CIDRS | (empty) | Only never-probe carve-out (CDE/sensitive) |
| NETDISCOVERY_ENABLE_PING_SWEEP | true | Tier 1 host sweep |
| NETDISCOVERY_ENABLE_PORT_SCAN | true | Tier 2 service/OS scan |
| NETDISCOVERY_MAX_RATE | conservative | Packets/sec cap |
| NETDISCOVERY_SCAN_DELAY | conservative | Inter-probe delay |
| NETDISCOVERY_PORT_SCAN_WINDOW | (none) | Optional window for heavy scans |
| NETDISCOVERY_FRAGILE_CIDRS / _HOSTS | (empty) | Force ICMP/single-port only |
| NETDISCOVERY_DNS_RESOLVER | (system) | Resolver for DNS tier |
| NETDISCOVERY_DANGEROUS_PORTS | telnet,smbv1,rdp,… | Dangerous-service watchlist |
| NETDISCOVERY_SUSPICIOUS_SIGNATURES | (curated set) | Shadow-IT / attacker tooling signatures |

## 11. Error handling (explicitly avoiding the linux/windows anti-pattern)
- Per-tier `try/except` recording partial results and setting run status **accurately**: a scan/tool failure marks the run `failed` with `error_message` — NEVER `completed`/0. (Lesson from the linux/windows `except Exception: return []` data-loss bug.)
- Missing `nmap` binary or a disabled tier ⇒ clean `skipped` status with reason, not a crash, not a silent empty.
- Every exclusion/fragility downgrade and guard denial is logged (target + reason).

## 12. Testing
- Reachable-scope discovery: mocked interfaces/routes → expected subnet set.
- Exclusion guard: in/out/​malformed-fails-closed.
- Aggressiveness mapping: each profile → expected nmap flags/timing/port-set.
- Fragile-device downgrade: fragile-classed host is ICMP/single-port only even at aggressive.
- DNS tier: mocked resolver; PTR/forward; resolver restriction honored.
- nmap parsing: XML fixtures (`-sn`, service/OS) → host/service rows; MAC OUI mapping.
- Classification: known suppressed, shadow-IT flagged, threat escalation; dedup via IS_SAME_AS.
- Watchlists: dangerous-port & suspicious-signature matches set flags + drift.
- Idempotent upsert: re-run no duplicates; last_seen advances.
- **Run-status accuracy:** forced tool failure → run `failed` with error_message (regression guard for the P1 masking bug).

## 13. Dependencies & migrations
- Add `nmap` system package to the Docker image. DNS/CIDR/route enumeration via stdlib `socket`/`ipaddress` (+ small route/iface read; document approach).
- Alembic migration for `net_discovery_hosts` + `net_discovery_services` (idempotent, additive). Extend `db/schema_check.py` `_REQUIRED_COLUMNS` to assert the new tables/columns at startup — avoiding the `create_all` column-drift class noted in the empty-dashboard diagnostic.

## 14. Rollout
- Single phase: passive + active tiers ship enabled at `polite` aggressiveness. Deploy is activation. Watch first sweeps; raise aggressiveness or set a scan window only deliberately. Populate `NETDISCOVERY_EXCLUDE_CIDRS` / fragile lists before first run if any reachable range or device must be protected.

## 15. Open items
- Confirm corporate DNS resolver address.
- Curate initial `NETDISCOVERY_DANGEROUS_PORTS` + `NETDISCOVERY_SUSPICIOUS_SIGNATURES` and the fragile-device lists with the team.
- Confirm MAC capture (on-segment ARP) feasibility from the container's network position; if NATed off-segment, MAC/OUI signals degrade gracefully to IP-only.
- Decide whether any routable CDE segment goes into `NETDISCOVERY_EXCLUDE_CIDRS` before first run.
