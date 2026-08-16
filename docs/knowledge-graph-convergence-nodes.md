# Knowledge-Graph Completion — Convergence Nodes & Missing Edges

**Status:** design backlog (2026-07-13). Ranked items 1–7 IMPLEMENTED (code-only)
2026-07-13 on branch `fix/collector-failures-2026-07-13` — see
`agents/graph_maintenance.py::_populate_convergence_nodes` and
`tests/agents/test_graph_convergence_nodes.py`. Live backfill happens on the next
scheduled `graph_maintenance` pass after merge.
See TRK-102 (graph audit) and TRK-103 (this catalog). Source: Fable5 live-DB analysis.

## Implementation status (2026-07-13, code-only)

DONE (emitters in `_populate_convergence_nodes`, idempotent, unit-tested):
- **CVE node** (`vuln/cve`): new edges `COVERS` (r7_vulnerability→cve),
  `FIXES` (r7_solution→cve, via r7_vuln_solutions⋈r7_vuln_cves), and host
  `VULNERABLE_TO` cve (reuses the existing type toward the new node).
- **SoftwareTitle** (`software/software_title`): host `HAS_SOFTWARE` title,
  `properties.grain="title"` to distinguish from the existing name@version edge.
- **UserAccount** (`identity/user_account`): host `HAS_ACCOUNT` user from
  r7_asset_users; octopus/vsphere principals converge onto the node (no host FK
  → node-only, no edge).
- **Subnet** (`network/subnet`): host `IN_SUBNET` /24 from r7_asset_addresses +
  net_discovery_hosts + vsphere_vms IPs (IPv6/unparseable skipped).
- **OSVersion** (`os/os_version`): host `RUNS_OS`; plus host `RUNS_EOL` when the
  OS label matches an `eol_registry.asset_name`.
- **ComplianceRule** (`compliance/rule`): violation shadow `INSTANCE_OF` rule.
  **DriftField** (`drift/field`): drift shadow `ON_FIELD` field.
- **OctopusRole** (`octopus/role`): machine `HAS_ROLE` role.

New `RelationshipType`s added + registered in `RELATIONSHIP_PROPS`: `COVERS`,
`FIXES`, `HAS_ACCOUNT`, `IN_SUBNET`, `RUNS_OS`, `INSTANCE_OF`, `ON_FIELD`,
`HAS_ROLE`. `relationship_type` is a plain `String(64)` column (not a native PG
enum), so **no migration** was required. Item 8's "dead emitters"
(TAGGED_AS / DEPLOYED_VIA / DEPENDS_ON / ANSIBLE_MANAGES) already have live
emitters in this repo (the audit doc predates them) — the CI guard confirms none
are dead.

TODO / DEFERRED (not attempted in this tranche):
- **Duplicate/forked vSphere node-typing MERGE** (`vm` vs `vsphere_vm`, etc.,
  TRK-102 CRITICAL-1). A data migration needing its own careful pass; the
  convergence work above is non-vSphere-node-typed so it does not depend on it.
- **Blocked-tier nodes** (item 10: NetworkPort, Certificates/CA, Windows
  LocalGroups/users, KB, Kernel/distro) — source tables empty; blocked on
  collectors landing data, not schema.
- **Item 9 single-column promotions** (vendor, tools/tentacle version, NTP/
  syslog, license, R7Site, container image, …) — lower connectivity/effort;
  left for a follow-up tranche.

## Implementation status (2026-07-15, code-only)

Branch `feat/kg-convergence-nodes-2026-07-15`. Live-DB counts verified 2026-07-15
on `deploy-host-01`.

### Item 9 single-column promotions — NOW BUILT

Each is a new convergence `Resource` node + edge type emitted in
`agents/graph_maintenance.py::_populate_convergence_nodes`, tested in
`tests/agents/test_graph_convergence_nodes.py`. New `RelationshipType` values
(**no migration** — `relationship_type` is `String(64)`):

- **RUNS_TOOLS_VERSION** — VsphereVm → `vsphere/vmware_tools_version` (847 rows / 28 distinct).
- **RUNS_TENTACLE_VERSION** — OctopusMachine → `octopus/tentacle_version` (388 / 10).
- **RUNS_IMAGE** — ComposeService (via `IacFile.resource_id`) → `container/container_image`
  (26 / 22; IaC path only — live-K8s pod/deployment images remain blocked, JSONB-only).
- **RUNS_ESXI_BUILD** — VsphereHost → `vsphere/esxi_build` (17 / 1).
- **HAS_HARDWARE_MODEL** — VsphereHost → `hardware/hardware_model` (17 / 2).
- **HAS_CPU_MODEL** — VsphereHost → `hardware/cpu_model` (17 / 4).
- **HAS_HARDWARE_VENDOR** — VsphereHost → `hardware/hardware_vendor` (17 / 2;
  distinct from the software `MADE_BY` vendor node).
- **HAS_BIOS** — VsphereHost → `hardware/bios_version` (17 / 3).
- **SENDS_SYSLOG_TO** — VsphereHost → `network/syslog_target` (14 / 1).
- **SYNCS_TIME_WITH** — VsphereHost → `network/ntp_server` (JSONB list unnested;
  17 hosts non-null).
- **BACKED_BY_FILER** — VsphereDatastore → `network/nfs_filer` (12 / 2; datastore
  is the anchor, no host FK).

### Item 9 still blocked (schema, not data — do not build until schema gains a link)

- **vCenter Role** — `vsphere_permissions` has no `resource_id`/host FK
  (`role_name` present, 18 distinct, but no relational anchor).
- **R7Site** — no `r7_asset` → `r7_site` FK (6 site rows exist but cannot be
  linked to hosts).
- **AnsibleInventoryGroup** — item-8 name-join only (no host FK; out of the
  item-9 tranche).

### Part B — vSphere fork MERGE (TRK-102 CRITICAL-1): RESOLVED

Resolved via an additive `IS_SAME_AS` ghost→live bridge (**no schema migration**).
New method `_populate_vsphere_fork_bridge` in `graph_maintenance.py` emits
`IS_SAME_AS` edges from stale 2026-06-29 ghost nodes to live 2026-07-13 typed
nodes, matched by name per type pair:

- `vm` → `vsphere_vm` (841 / 827)
- `dvportgroup` → `vsphere_dvportgroup` (129 / 129)
- `datastore` → `vsphere_datastore` (33 / 33)
- `network` → `vsphere_portgroup` | `vsphere_dvportgroup` (name-matched across both)
- `esxi_host` → `vsphere_host` (17 / 17)
- `cluster` → `vsphere_cluster` (6 / 6)
- `datacenter` → `vsphere_datacenter` (1 / 1)

Read-only over existing `Resource` rows; respects the human-gating convention on
the live vSphere re-seed. The prior edge-routing fix (`_vsphere_name_to_id`,
commit 04a0800) already prevented live edges landing on ghosts; this bridge
additionally links the two node populations.

**Follow-up NOTE:** true ghost-node RETIREMENT (a `retired_at` column on
`Resource`) is deferred — it would require a generated Alembic migration + read-path
filters and is out of this tranche.

## Implementation status (2026-07-17, code-only)

Branch `feat/trk-103-104-convergence-node-tranche` (TRK-103/104). A further
buildable tranche — every block emitted in
`agents/graph_maintenance.py::_populate_convergence_nodes`, unit-tested in
`tests/agents/test_graph_convergence_nodes.py`, bounded emission per TRK-108
(the ~7.4k-edge Octopus VariableName block uses the `_EdgeBuffer`
flush-every-`chunk` accumulator; the rest are small bounded lists). No
migration (`relationship_type` is `String(64)`).

### NOW BUILT

- **VMDK → datastore** — `vsphere_vm_disks` → `vsphere/vmdk` node
  (key `{vcenter}:{vm_moref}:{disk_key}`). Edges: `vm HAS_DISK vmdk`
  (`properties.grain="vmdk"`, parent VM by exact `(vcenter, moref)` join) and
  `vmdk STORED_ON datastore` (datastore resolved via `_vsphere_name_to_id`).
  Reuses HAS_DISK / STORED_ON — no new enum.
- **VM Snapshot** — `vsphere_snapshots` → `vsphere/snapshot` node
  (key `{vcenter}:{vm_moref}:{snapshot_id}`). Edge `vm HAS_SNAPSHOT snapshot`.
  NEW enum member **HAS_SNAPSHOT**.
- **vCenter Role** — `vsphere_permissions.role_name` → `vsphere/role` node
  (key `role_name`). Edge `user_account HAS_ROLE role`, built in the same
  permission loop as GRANTED_ON (recorded even when the managed entity does not
  resolve). Reuses HAS_ROLE (counted separately as `HAS_ROLE_vcenter`). This
  UNBLOCKS the 2026-07-15 "vCenter Role — no relational anchor" note: the anchor
  is the permission principal's `user_account` node, not a host FK.
- **AnsibleInventoryGroup** — `ansible_inventory_groups` ⋈
  `ansible_inventory_hosts` → `ansible/inventory_group` node
  (key `group.name.lower()`). Edge `host MEMBER_OF inventory_group` — REUSES
  MEMBER_OF (PART_OF stays DEFERRED, never emitted). Host resolved by the same
  hostname→Resource(domain in linux/windows) match ANSIBLE_MANAGES uses.
- **Octopus VariableName** — `octopus_variables.name` (METADATA ONLY — no value
  column, no secret) → `octopus/variable_name` node (key `name.lower()`). Edge
  `project HAS_VARIABLE variable_name` (owner_type=project, via
  `OctopusProject.octopus_id→resource_id`) and
  `library_variable_set HAS_VARIABLE variable_name` (owner_type=library,
  anchored on a `octopus/library_variable_set` convergence node keyed by
  owner_name — the set has no resource_id). NEW enum member **HAS_VARIABLE**.
  Largest block → `_EdgeBuffer` bounded emission.
- **OctopusTeam** — `octopus_teams` → `octopus/team` node (key `name` or
  `octopus_id`). `member_user_ids` → `OctopusUser.username` → identity
  `user_account` node, edge `user_account MEMBER_OF team` (reuses MEMBER_OF);
  `project_ids` → `OctopusProject.resource_id`, edge `team HAS_ACCESS project`.
  NEW enum member **HAS_ACCESS**.

New `RelationshipType`s: **HAS_SNAPSHOT**, **HAS_VARIABLE**, **HAS_ACCESS**
(all registered in `RELATIONSHIP_PROPS`; declared-vs-emitted + deferred-types CI
guards pass).

### SKIPPED (design decision / schema — deliberately not attempted)

- **ResourcePool IN_POOL** — no per-VM pool column (would need a schema/collector
  change).
- **R7Site** — no `r7_asset.site_id` FK (no relational anchor to a host).
- **project → library_variable_set DEPENDS_ON** — no column linking a project to
  the library variable sets it includes.

Branch `feat/trk-104-has-schedule-convergence-edge` — merged@`b16d41e` (MR !186).

### CI pipeline schedule — NOW BUILT

- **HAS_SCHEDULE** — GitLab project → CI pipeline schedule. A new
  `RelationshipType` value emitted in
  `agents/graph_maintenance.py::_populate_convergence_nodes`, tested in
  `tests/agents/test_graph_convergence_nodes.py`. Sourced from `ci_schedules`
  joined to `gitlab_projects` on `ci_schedules.project_id ==
  gitlab_projects.gitlab_project_id`; the edge runs from
  `GitlabProject.resource_id` to `CiSchedule.resource_id`. Both endpoints are
  already real, populated `Resource` rows — no convergence node is created. Edge
  properties carry the schedule's `cron`, `ref`, and `active` fields. **No schema
  change** (`relationship_type` is `String(64)`).

## The principle

Today the graph links resources to resources and to their *own* children (host → its
software rows). It is **not** yet a true knowledge graph because **shared values are
scalar columns / per-host rows, not nodes** — so you can't pivot on them
("click Adobe → all machines", "click a CVE → all affected hosts + package + patch",
"click a username → every host/permission/session it touches").

**Rule:** any attribute two+ resources can share becomes a first-class **convergence
node** that many resources link to. Bidirectional traversal already works
(`get_neighborhood`/`_WALK_SQL`, `db/relationships.py`); the gap is missing nodes/edges.

## Ranked build order (connectivity per unit effort — data already collected)

1. **CVE node** — `r7_vuln_cves.cve_id` + `vuln_queue.cve_id` (~1k). Edges: r7_vuln `COVERS` cve, host `VULNERABLE_TO` cve, solution `FIXES` cve, software `AFFECTED_BY` cve. Flagship: "click CVE → hosts + vulnerable package + patch."
2. **SoftwareTitle rollup** — `r7_software.product`: 14.5k titles / 338k rows (9,958 shared). Makes the 349k HAS_SOFTWARE edges clickable at "Adobe" grain (today only `name@version`).
3. **UserAccount** — `r7_asset_users.username` (6,804 distinct / 99,722 rows) + vsphere principals/sessions + octopus users. Biggest new cross-domain hub: "click jsmith → every host/permission/session."
4. **Subnet (/24)** — derive from netdiscovery(2,699)/r7/vsphere IPs → 252 subnets. Unites segments cross-source; also strengthens IS_SAME_AS.
5. **OSVersion** — `r7_assets.os_*` (133 fingerprints / 2,651) + vsphere guest_id. EOL/patch blast-radius; join EolRegistry for free RUNS_EOL fan-out.
6. **ComplianceRule (3) + DriftField (186)** — trivial; collapse 32k noisy nodes into clickable categories.
7. **OctopusRole** — `octopus_machine_roles` (171), fully collected. "click web-server role → machines + deploy steps."
8. **Wire dead emitters** — TAGGED_AS (tags already nodes, 252; PCI-scope tags like "Digital Card CDE"), DEPLOYED_VIA (gitlab→octopus), DEPENDS_ON, ANSIBLE_MANAGES + inventory-group promotion (45 groups). Enum+data exist; graph_maintenance wiring only.
9. **Single-column promotions** — Vendor/Publisher (1,006), VMware ToolsVersion (28), TentacleVersion (10), ESXi build, HW/CPU model, BIOS, NTP server, Syslog target, NFS filer, vCenter, License edition, vCenter Role (18), R7Site (6), ContainerImage (22).
10. **Blocked on collectors landing data** (schema homes already exist): NetworkPort (`net_discovery_services` empty despite 2,699 hosts), Certificates/CA, Windows LocalGroups/users, KB articles, Kernel/distro, VLAN/HostPurpose, MACVendor/OUI, LinuxPackage.

## Full catalog by domain

**A. Software/packages:** SoftwareTitle, SoftwareVersion(✅), Vendor, LinuxPackage(🔒), ContainerImage, OctopusPackage/Feed.
**B. Vuln/patch/EOL:** CVE, R7Vulnerability(✅), Solution(✅), KB(🔒), EOL product(✅ tiny).
**C. OS/platform:** OSVersion, OSFamily, ESXi build, Kernel(🔒), ToolsVersion, VM HW version, TentacleVersion, CPU/HW model, BIOS.
**D. Identity:** UserAccount, LocalGroup(🔒), vCenter Role, OctopusTeam.
**E. Network:** NetworkPort(🔒/compose➕), Subnet, VLAN, IPAddress, MACVendor(⚠️), vSphere Network(✅), NTPServer, SyslogTarget, NFS filer, DNS domain.
**F. Certs:** Certificate(🔒), CertIssuer/CA(🔒).
**G. vSphere topology/gov:** RUNS_ON/MEMBER_OF/IN_DATACENTER/STORED_ON/ATTACHED_TO(✅), VMDK→datastore, ResourcePool IN_POOL, Snapshot, License edition, AlarmDefinition(⚠️regrain), vCenter node.
**H. Deploy/CI:** DEPLOYED_TO/DEPLOYS_TO/MANAGES(✅), Environment(✅), OctopusRole, Release/Deployment, Feed, LibraryVariableSet(⚠️), VariableName, GitLab(✅)/DEPLOYED_VIA(⚠️), AnsibleInventoryGroup, CiSchedule.
**I. Compliance/drift/gov:** violation(✅)/HAS_VIOLATION(✅), drift(✅)/HAS_DRIFT(✅), ComplianceRule, DriftField, R7Site, Zone(defer), HostPurpose(🔒), Jira/Confluence.
**J. Cross-domain identity:** IS_SAME_AS(✅ 3,704; host_identities 8 sources) — extend to netdiscovery via IP/Subnet nodes.

## Warnings / prerequisites
- **Duplicate vSphere node typing** (`vm` 841 vs `vsphere_vm` 827, etc. — the forked-graph issue, TRK-102) must be merged/regrained BEFORE adding edges, or every convergence node doubles its edges.
- **Dedup/canonical-key policy** needed — `Resource` isn't unique-constrained on (domain,type,name); high-fan-in nodes ("Administrator", port 443) will dominate `get_neighborhood` walks (mind the max_nodes/max_edges caps).
- **CI guard:** new RelationshipType values must be added to `RELATIONSHIP_PROPS` / `tests/test_declared_vs_emitted_edges.py` or the declared-vs-emitted gate fails.
- `net_discovery_services` empty → the port pivot is blocked on the Tier-2 persist path, not schema.
