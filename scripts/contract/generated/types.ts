// AUTO-GENERATED — do not edit by hand.
// Source: infra_brain.api.schemas (Pydantic v2 model_json_schema)
// Regenerate: uv run python scripts/contract/generate.py
// Task 5.6 — Phase 5 contract rework.

export interface ActivityOut {
  agent: string;
  args_summary: string;
  domain?: string;
  error?: string;
  latency_ms: number;
  status?: string;
  tool: string;
  ts: string;
  verdict: string;
}

export interface ActivityPageOut {
  items: ActivityOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface AgentConfigOut {
  domain: string;
  last_error?: string | null;
  last_run_at?: string | null;
  last_status?: string | null;
  paused?: boolean;
  ready: boolean;
  requirements: AgentConfigRequirement[];
}

export interface AgentConfigPageOut {
  items: AgentConfigOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface AgentConfigRequirement {
  configured: boolean;
  current_value?: string | null;
  key: string;
  label: string;
}

export interface AgentOut {
  desc?: string;
  domain: string;
  kind: string;
  last_run: string;
  name: string;
  output: string;
  schedule: string;
  status: string;
  tools?: string[];
}

export interface AgentRosterPageOut {
  items: AgentOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface AnsibleInventoryGroupOut {
  file_path?: string;
  hosts?: AnsibleInventoryHostOut[];
  name: string;
  project_name?: string;
}

export interface AnsibleInventoryHostOut {
  name: string;
}

export interface AnsiblePlaybookPlayOut {
  file_path?: string;
  hosts?: unknown[];
  name?: string;
  play_index?: number;
  project_name?: string;
}

export interface AnsibleStructureOut {
  groups?: AnsibleInventoryGroupOut[];
  plays?: AnsiblePlaybookPlayOut[];
}

export interface AuditOut {
  agent: string;
  allowed: boolean;
  category: string;
  ihash: string;
  ohash: string;
  reason: string;
  tool: string;
  ts: string;
}

export interface AuditPageOut {
  items: AuditOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface BulkSeedErrorOut {
  error: string;
  hostname?: string | null;
  index: number;
}

export interface BulkSeedOut {
  created: number;
  errors?: BulkSeedErrorOut[];
  updated: number;
}

export interface CiScheduleOut {
  active?: boolean | null;
  created_at?: string | null;
  cron?: string;
  description?: string;
  project_id: number;
  ref?: string;
  schedule_id: number;
}

export interface CiSchedulesPageOut {
  items?: CiScheduleOut[];
  limit?: number;
  offset?: number;
  total?: number;
}

export interface CloudNetOverviewOut {
  cloud_resources?: CloudResourceOut[];
  k8s_deployments?: K8sDeploymentOut[];
  k8s_nodes?: K8sNodeOut[];
  k8s_pods?: K8sPodOut[];
  net_devices?: NetDeviceOut[];
  summary?: CloudNetSummaryOut;
}

export interface CloudNetSummaryOut {
  cloud_resource_count?: number;
  k8s_deployment_count?: number;
  k8s_node_count?: number;
  k8s_pod_count?: number;
  net_device_count?: number;
}

export interface CloudResourceOut {
  cloud_id: string;
  cloud_type: string;
  name: string;
  provider: string;
  region?: string;
  state?: string;
}

export interface CollectionRunPageOut {
  items: RunOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface ComplianceOut {
  detail: string;
  detected_at: string;
  host: string;
  rule: string;
  severity: string;
  status: string;
}

export interface CompliancePageOut {
  items: ComplianceOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface ComposeServiceOut {
  file_path?: string;
  image?: string;
  ports?: unknown[];
  project_name?: string;
  service_name: string;
}

export interface ComposeServicesPageOut {
  items?: ComposeServiceOut[];
  limit?: number;
  offset?: number;
  total?: number;
}

export interface CountsOut {
  applied_instincts?: number;
  compliance_open?: number;
  compliance_resolved?: number;
  critical_cves?: number;
  eol_overdue?: number;
  eol_total?: number;
  invrec_proposed?: number;
  invrec_total?: number;
  open_cves?: number;
  open_drift?: number;
  severe_cves?: number;
  total_resources?: number;
}

export interface CustomViewOut {
  created_at: string;
  id: string;
  is_public: boolean;
  openui_lang: string;
  prompt: string;
  share_token: string;
  share_url: string;
  title: string;
}

export interface CustomViewPageOut {
  items: CustomViewOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface CveAffectedHostOut {
  hostname?: string;
  kb_id?: string;
  last_updated?: string | null;
  resource_id?: string;
  sla?: string;
  status?: string;
}

export interface CveDetailOut {
  affected_host_count?: number;
  affected_hosts?: CveAffectedHostOut[];
  categories?: unknown[];
  cve_id: string;
  cvss?: number;
  cvss_v2?: number;
  cvss_vector?: string;
  denial_of_service?: boolean;
  exploits?: number;
  fix_available?: boolean;
  malware_kits?: number;
  pci_fail?: boolean;
  pci_status?: string;
  published?: string | null;
  r7_vuln_ids?: string[];
  risk_score?: number;
  severity?: string;
  sla_deadline?: string | null;
  sla_overdue_count?: number;
  solutions?: CveSolutionOut[];
  title?: string;
}

export interface CveListItemOut {
  affected_hosts?: number;
  cve_id: string;
  cvss?: number;
  exploits?: number;
  fix_available?: boolean;
  pci_fail?: boolean;
  risk_score?: number;
  severity?: string;
  title?: string;
}

export interface CveListOut {
  by_severity?: Record<string, unknown>;
  items?: CveListItemOut[];
  limit?: number;
  offset?: number;
  total?: number;
}

export interface CveSolutionOut {
  estimate?: string;
  solution_type?: string;
  steps?: string;
  summary?: string;
}

export interface DecisionOut {
  agent: string;
  decision_summary: string;
  domain: string;
  iteration: number;
  reasoning_text: string;
  run_id: string;
  tools_chosen: string[];
  ts: string;
}

export interface DecisionPageOut {
  items: DecisionOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface DocumentChunkPreviewOut {
  chunk_index: number;
  text_preview?: string;
  token_count?: number | null;
}

export interface DocumentDetailOut {
  chunk_count?: number;
  chunks?: DocumentChunkPreviewOut[];
  content_hash?: string;
  external_id?: string;
  id: string;
  indexed_at?: string | null;
  last_updated?: string | null;
  sensitivity?: string;
  source?: string;
  source_version?: number | null;
  space?: string;
  status?: string;
  title: string;
  url?: string;
}

export interface DocumentOut {
  chunk_count?: number;
  id: string;
  indexed_at?: string | null;
  last_updated?: string | null;
  sensitivity?: string;
  source?: string;
  space?: string;
  status?: string;
  title: string;
  url?: string;
}

export interface DocumentsPageOut {
  items?: DocumentOut[];
  limit?: number;
  offset?: number;
  total?: number;
}

export interface DriftOut {
  detected_at: string;
  domain: string;
  drift_type: string;
  field_name: string;
  hostname: string;
  id: string;
  jira_ticket?: string;
  new_display?: string;
  new_value: string;
  old_display?: string;
  old_value: string;
  root_cause?: string;
  rule?: string;
  status: string;
  summary?: string;
}

export interface DriftPageOut {
  items: DriftOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface DriftTrendOut {
  days: number;
  domain_filter: string;
  points: DriftTrendPoint[];
  total: number;
}

export interface DriftTrendPoint {
  count: number;
  date: string;
  domain: string;
}

export interface EolMigrationOut {
  id: string;
  migration_path: string;
}

export interface EolOut {
  asset: string;
  eol: string;
  host: string;
  id: string;
  migration: string;
  pci_risk_score?: number | null;
  status: string;
}

export interface EolPageOut {
  items: EolOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface FleetAssetOut {
  assessed?: boolean;
  asset_type?: string;
  config_count?: number;
  hostname: string;
  id: string;
  ip?: string;
  os?: string;
  os_product?: string;
  os_vendor?: string;
  os_version?: string;
  r7_asset_id: number;
  risk_band?: string;
  risk_score?: number;
  vuln_critical?: number;
  vuln_exploits?: number;
  vuln_moderate?: number;
  vuln_severe?: number;
  vuln_total?: number;
}

export interface FleetAssetsOut {
  items?: FleetAssetOut[];
  limit?: number;
  offset?: number;
  summary?: FleetSummaryOut;
  total?: number;
}

export interface FleetOsCountOut {
  count: number;
  os_product: string;
}

export interface FleetRiskBandOut {
  band: string;
  count: number;
}

export interface FleetSummaryOut {
  assessed_assets?: number;
  by_os?: FleetOsCountOut[];
  by_risk_band?: FleetRiskBandOut[];
  total_assets?: number;
  total_critical?: number;
  total_severe?: number;
}

export interface GeneratedScriptPageOut {
  items: ScriptOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface HealthItem {
  detail: string;
  name: string;
  status: string;
}

export interface HealthOut {
  postgres?: string | null;
  redis?: string | null;
  status: string;
}

export interface HealthzOut {
  status: string;
}

export interface HostCertificateOut {
  days_until_expiry?: number | null;
  is_expired?: boolean;
  issuer?: string | null;
  not_after?: string | null;
  not_before?: string | null;
  store: string;
  subject?: string | null;
  thumbprint?: string;
}

export interface HostFirewallRuleOut {
  action?: string | null;
  chain?: string | null;
  rule_text: string;
  source: string;
  table_name?: string | null;
}

export interface HostIdentityOut {
  fqdn?: string | null;
  id: string;
  ip_addresses?: string[];
  last_reconciled?: string | null;
  linux_resource_id?: string | null;
  observed?: boolean;
  octopus_machine_status?: string | null;
  octopus_resource_id?: string | null;
  os_family?: string | null;
  patch_status?: string | null;
  r7_resource_id?: string | null;
  retired_at?: string | null;
  risk_score?: number | null;
  short_hostname: string;
  vsphere_power_state?: string | null;
  vsphere_resource_id?: string | null;
  vuln_count?: number | null;
  windows_resource_id?: string | null;
}

export interface HostPostureOut {
  certificates?: HostCertificateOut[];
  firewall_rules?: HostFirewallRuleOut[];
  hostname: string;
  local_group_members?: WindowsLocalGroupMemberOut[];
  local_users?: WindowsLocalUserOut[];
  resource_id: string;
  security_posture?: HostSecurityPostureOut | null;
  shares?: HostShareOut[];
}

export interface HostSecurityPostureOut {
  apparmor_status?: string | null;
  av_enabled?: boolean | null;
  av_product?: string | null;
  av_signature_date?: string | null;
  firewall_enabled?: boolean | null;
  firewall_service?: string | null;
  rdp_enabled?: boolean | null;
  selinux_mode?: string | null;
  ssh_password_auth?: boolean | null;
  ssh_permit_root_login?: boolean | null;
  ssh_pubkey_auth?: boolean | null;
  uac_enabled?: boolean | null;
}

export interface HostShareOut {
  name: string;
  path?: string | null;
  permissions?: Record<string, unknown>[];
  share_type: string;
}

export interface HostVulnHeaderOut {
  hostname: string;
  risk_score?: number;
  vuln_critical?: number;
  vuln_moderate?: number;
  vuln_severe?: number;
}

export interface HostVulnItemOut {
  cve_id: string;
  cvss_v3?: number;
  exploits?: number;
  fix_available?: boolean;
  kb_id?: string;
  last_updated?: string | null;
  pci_fail?: boolean;
  r7_vuln_id?: string;
  severity?: string;
  sla?: string;
  sla_due?: string | null;
  solution_summary?: string;
  status?: string;
  title?: string;
}

export interface HostVulnsOut {
  header?: HostVulnHeaderOut;
  items?: HostVulnItemOut[];
  limit?: number;
  offset?: number;
  total?: number;
}

export interface HostsPageOut {
  items: unknown[];
  limit: number;
  offset: number;
  total: number;
}

export interface IacOverviewOut {
  projects?: IacProjectOut[];
  summary?: IacSummaryOut;
}

export interface IacPipelineRunOut {
  created_at?: string | null;
  duration?: number | null;
  pipeline_id: number;
  ref?: string;
  source?: string;
  status?: string;
  web_url?: string;
}

export interface IacProjectOut {
  archived?: boolean;
  default_branch?: string;
  file_count?: number;
  files_by_type?: Record<string, unknown>;
  gitlab_project_id: number;
  last_activity_at?: string | null;
  last_pipeline_ref?: string;
  last_pipeline_status?: string;
  name: string;
  path_with_namespace?: string;
  recent_pipelines?: IacPipelineRunOut[];
  visibility?: string;
}

export interface IacSummaryOut {
  file_count?: number;
  files_by_type?: Record<string, unknown>;
  pipeline_run_count?: number;
  project_count?: number;
}

export interface InstinctOut {
  applied?: boolean;
  citation?: string;
  confidence: number;
  domain: string;
  pattern: string;
  promoted_at: string;
  promoted_by?: string;
  zone: string;
}

export interface InstinctPageOut {
  items: InstinctOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface IntegrationProposalPageOut {
  items: ProposalOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface InventoryReconcileOut {
  detected_at: string;
  domain: string;
  host: string;
  mr_url: string;
  status: string;
  target_group: string;
}

export interface InventoryReconcilePageOut {
  items: InventoryReconcileOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface K8sDeploymentOut {
  available?: number | null;
  cluster?: string;
  name: string;
  namespace: string;
  ready?: number | null;
  replicas?: number | null;
}

export interface K8sManifestResourceOut {
  api_version?: string;
  file_path?: string;
  kind: string;
  name: string;
  namespace?: string;
  project_name?: string;
}

export interface K8sManifestResourcesPageOut {
  items?: K8sManifestResourceOut[];
  limit?: number;
  offset?: number;
  total?: number;
}

export interface K8sNodeOut {
  arch?: string;
  cluster?: string;
  kubelet_version?: string;
  name: string;
  roles?: unknown[];
  status?: string;
}

export interface K8sPodOut {
  cluster?: string;
  name: string;
  namespace: string;
  node_name?: string;
  phase?: string;
}

export interface KV {
  k: string;
  v: string;
}

export interface LLMAgentStatsOut {
  agent: string;
  completed: number;
  domain: string;
  last_run_at?: string | null;
  narrated_turns: number;
  peak_call_tokens: number;
  recursion_limit: number;
  runs: number;
  silent_turns: number;
  tokens_billed: number;
  tool_calls: number;
  truncated: number;
  turns: number;
}

export interface LLMFlagOut {
  effect: string;
  enabled: boolean;
  name: string;
}

export interface LLMOutcomeCountsOut {
  completed?: number;
  recursion_limit?: number;
  truncated?: number;
  unknown?: number;
}

export interface LLMRunDetailOut {
  agent: string;
  distinct_tools: number;
  domain: string;
  ended_at: string;
  max_tool_repeat: number;
  narrated_turns: number;
  outcome: string;
  outcome_reason: string;
  peak_call_tokens: number;
  run_id: string;
  silent_turns: number;
  started_at: string;
  steps: LLMStepOut[];
  token_metric?: string;
  tokens_billed: number;
  tool_calls: number;
  turns: number;
}

export interface LLMRunOut {
  agent: string;
  distinct_tools: number;
  domain: string;
  ended_at: string;
  max_tool_repeat: number;
  narrated_turns: number;
  outcome: string;
  peak_call_tokens: number;
  run_id: string;
  silent_turns: number;
  started_at: string;
  tokens_billed: number;
  tool_calls: number;
  turns: number;
}

export interface LLMRunPageOut {
  items: LLMRunOut[];
  limit: number;
  offset: number;
  token_metric?: string;
  total: number;
}

export interface LLMStepOut {
  call_tokens?: number | null;
  iteration: number;
  reasoning_state: string;
  reasoning_text: string;
  tool_repeats: Record<string, unknown>;
  tools_chosen: string[];
  ts: string;
}

export interface LLMSummaryOut {
  by_agent: LLMAgentStatsOut[];
  flags: LLMFlagOut[];
  generated_at: string;
  model: string;
  narrated_turns: number;
  outcomes: LLMOutcomeCountsOut;
  peak_call_tokens: number;
  provider: string;
  rows_scanned: number;
  runs: number;
  scan_cap: number;
  silent_turns: number;
  since: string;
  token_ceiling: number;
  token_ceiling_enabled: boolean;
  token_metric?: string;
  tokens_billed: number;
  tool_calls: number;
  top_tools: LLMToolUseOut[];
  truncated_scan: boolean;
  turns: number;
  window_hours: number;
}

export interface LLMToolUseOut {
  calls: number;
  max_in_one_iteration: number;
  tool: string;
}

export interface LinuxCronOut {
  command: string;
  owner: string;
  schedule: string;
}

export interface LinuxDetailOut {
  arch?: string | null;
  crons?: LinuxCronOut[];
  distro?: string | null;
  hostname: string;
  kernel?: string | null;
  mounts?: LinuxMountOut[];
  nics?: LinuxNicOut[];
  packages?: LinuxPackageOut[];
  pending_updates?: LinuxPendingUpdateOut[];
  ports?: LinuxPortOut[];
  resource_id: string;
  services?: LinuxServiceOut[];
  users?: LinuxUserOut[];
}

export interface LinuxMountOut {
  device?: string | null;
  fstype?: string | null;
  mount: string;
  size_available_gb?: number | null;
  size_total_gb?: number | null;
}

export interface LinuxNicOut {
  ipv4?: string | null;
  ipv6?: string | null;
  mac?: string | null;
  name: string;
  speed_mbps?: number | null;
}

export interface LinuxPackageOut {
  installed_at?: string | null;
  manager: string;
  name: string;
  version: string;
}

export interface LinuxPendingUpdateOut {
  available_version?: string | null;
  current_version?: string | null;
  manager?: string | null;
  package: string;
  security?: boolean;
}

export interface LinuxPortOut {
  port: number;
  process?: string | null;
  proto: string;
  state: string;
}

export interface LinuxServiceOut {
  enabled: boolean;
  last_checked: string;
  name: string;
  state: string;
}

export interface LinuxUserOut {
  last_login?: string | null;
  shell: string;
  sudo: boolean;
  username: string;
}

export interface LoginOut {
  authenticated: boolean;
  dev_mode: boolean;
  username?: string | null;
}

export interface LogoutOut {
  authenticated: boolean;
}

export interface McpKeyOut {
  allowed_tools_count: number;
  created_at: string;
  created_by: string;
  expired?: boolean;
  expires_at?: string | null;
  id: string;
  last_used_at?: string | null;
  name: string;
  revoked: boolean;
}

export interface McpKeyPageOut {
  items: McpKeyOut[];
  total: number;
}

export interface McpToolCatalogOut {
  groups?: Record<string, unknown>;
  mutation: string[];
  readonly: string[];
}

export interface MeOut {
  authenticated: boolean;
  dev_mode: boolean;
  name?: string | null;
  role?: string | null;
  username?: string | null;
}

export interface NetDeviceOut {
  contact?: string;
  ip: string;
  location?: string;
  name: string;
  sysname?: string;
}

export interface NetDiscoveryHostOut {
  discovery_tier?: string;
  first_seen?: string | null;
  hostname?: string;
  id: string;
  ip: string;
  is_fragile?: boolean;
  is_known?: boolean;
  is_shadow_it?: boolean;
  last_seen?: string | null;
  mac?: string;
  mac_vendor?: string;
  resource_id?: string | null;
  responded?: boolean;
  services?: NetDiscoveryServiceOut[];
  threat_level?: string;
  zone?: string;
}

export interface NetDiscoveryHostsPageOut {
  items?: NetDiscoveryHostOut[];
  limit?: number;
  offset?: number;
  summary?: NetDiscoverySummaryOut;
  total?: number;
}

export interface NetDiscoveryServiceOut {
  banner?: string;
  fingerprint?: string;
  is_dangerous?: boolean;
  is_suspicious?: boolean;
  last_seen?: string | null;
  port: number;
  proto: string;
  service?: string;
}

export interface NetDiscoverySummaryOut {
  by_threat_level?: Record<string, unknown>;
  known_count?: number;
  shadow_it_count?: number;
  total_hosts?: number;
}

export interface NotificationOut {
  confluence_url?: string | null;
  created: string;
  domain: string;
  jira_url?: string | null;
  status: string;
  target: string;
  title: string;
  type: string;
}

export interface NotificationPageOut {
  items: NotificationOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface ObservationOut {
  agent: string;
  count: number;
  domain: string;
  last_seen: string;
  pattern: string;
  tool: string;
}

export interface ObservationPageOut {
  items: ObservationOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface OpsAlertReceivedOut {
  category: string;
  messages: number;
  received: boolean;
}

export interface PageEnvelope {
  items: unknown[];
  limit: number;
  offset: number;
  total: number;
}

export interface ProposalOut {
  confidence: number;
  endpoint: string;
  id: string;
  proposed_at: string;
  source: string;
  status: string;
  type: string;
}

export interface R7AssetAddressOut {
  ip: string;
  mac?: string;
}

export interface R7AssetConfigOut {
  name: string;
  value?: string;
}

export interface R7AssetDetailOut {
  addresses?: R7AssetAddressOut[];
  configs?: R7AssetConfigOut[];
  hostname?: string;
  id: string;
  users?: R7AssetUserOut[];
}

export interface R7AssetUserOut {
  full_name?: string;
  username: string;
}

export interface R7SiteOut {
  asset_count?: number;
  importance?: string;
  last_scan_time?: string | null;
  name: string;
  r7_site_id: number;
  risk_score?: number;
  site_type?: string;
}

export interface R7SitesPageOut {
  items?: R7SiteOut[];
  limit?: number;
  offset?: number;
  total?: number;
}

export interface R7TagOut {
  color?: string;
  name: string;
  r7_tag_id: number;
  source?: string;
  tag_type?: string;
}

export interface R7TagsPageOut {
  items?: R7TagOut[];
  limit?: number;
  offset?: number;
  total?: number;
}

export interface RemediationOut {
  action_type: string;
  agent: string;
  approved_by: string;
  confidence: number;
  created_at: string;
  id: string;
  result_url: string;
  status: string;
  target: string;
}

export interface RemediationPageOut {
  items: RemediationOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface RemediationRollupOut {
  items: RemediationRollupRowOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface RemediationRollupRowOut {
  action_type: string;
  count: number;
  field: string | null;
  sample_targets: string[];
}

export interface ResourceOut {
  domain: string;
  drift_count?: number;
  hostname: string;
  id: string;
  last_seen_at: string;
  meta?: KV[];
  resource_type: string;
  status: string;
  zone: string;
}

export interface ResourceOwnershipOut {
  criticality_tier?: string | null;
  on_call_rotation?: string | null;
  owner_team?: string | null;
  resource_id: string;
  source?: string | null;
  updated_at?: string | null;
}

export interface ResourcePageOut {
  items: ResourceOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface RunOut {
  detail_rows_written?: number;
  domain: string;
  duration_seconds: number;
  error_message?: string | null;
  records_collected: number;
  started_at: string;
  status: string;
  trigger_type: string;
}

export interface RuntimeConfigOut {
  category: string;
  is_secret: boolean;
  key: string;
  updated_at: string;
  updated_by: string;
  value: string | null;
  value_type: string;
}

export interface RuntimeConfigPageOut {
  items: RuntimeConfigOut[];
}

export interface ScanOut {
  domain: string;
  endpoint: string;
  last_run?: string | null;
  last_success?: string | null;
  method: string;
  next_run: string;
  schedule: string;
  status: string;
}

export interface ScanPointItemOut {
  domain: string;
  id: string;
  last_run?: string | null;
  method?: string | null;
  schedule?: string | null;
}

export interface ScanPointPageOut {
  items: ScanOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface ScriptOut {
  created_at?: string | null;
  created_by_agent: string;
  domain?: string;
  git_path?: string;
  language: string;
  last_returncode: number;
  last_run_at: string | null;
  name: string;
  purpose: string;
  run_count: number;
}

export interface SeedResourceOut {
  created: boolean;
  hostname: string;
  resource_id: string;
}

export interface SettingCatalogEntry {
  db_row?: boolean;
  default?: string | null;
  degraded?: boolean;
  description: string;
  editable?: boolean;
  env_var: string;
  group: string;
  key: string;
  locked_reason?: string | null;
  managed_in?: string | null;
  override_ignored_reason?: string | null;
  secret?: boolean;
  secret_reason?: string | null;
  secret_state?: string | null;
  shadowed_value?: string | null;
  source: string;
  type: string;
  value?: string | null;
}

export interface SettingGroup {
  group: string;
  rows: SettingRow[];
}

export interface SettingRow {
  k: string;
  on?: boolean | null;
  type: string;
  v?: string | null;
}

export interface SettingsCatalogPageOut {
  groups: string[];
  items: SettingCatalogEntry[];
  total: number;
}

export interface SettingsPageOut {
  items: SettingGroup[];
  limit: number;
  offset: number;
  total: number;
}

export interface SnapshotOut {
  label: string;
  ts: string;
}

export interface SnapshotPageOut {
  items: SnapshotOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface SoftwareAggRowOut {
  host_count?: number;
  product: string;
  vendor?: string;
  version?: string;
}

export interface SoftwareDetailRowOut {
  hostname?: string;
  id: string;
  product: string;
  r7_asset_id: number;
  software_type?: string;
  vendor?: string;
  version?: string;
}

export interface SoftwareInventoryOut {
  items?: SoftwareAggRowOut | SoftwareDetailRowOut[];
  limit?: number;
  offset?: number;
  summary?: SoftwareSummaryOut;
  total?: number;
  view?: string;
}

export interface SoftwareSummaryOut {
  hosts_covered?: number;
  total_records?: number;
  unique_products?: number;
}

export interface SweepAcceptedOut {
  accepted: boolean;
  domain: string;
}

export interface SweepDetailOut {
  domains: SweepDomainOut[];
  duration_seconds?: number | null;
  finished_at?: string | null;
  max_iters_hit_count?: number;
  started_at?: string | null;
  status_counts: SweepStatusCountsOut;
  sweep_id: string;
}

export interface SweepDomainOut {
  domain: string;
  duration_seconds?: number | null;
  error_message?: string | null;
  finished_at?: string | null;
  max_iters_hits?: number;
  records_collected?: number;
  started_at?: string | null;
  status: string;
  tier: string;
}

export interface SweepListOut {
  items: SweepSummaryOut[];
  limit: number;
  total: number;
}

export interface SweepStatusCountsOut {
  completed?: number;
  failed?: number;
  in_progress?: number;
  interrupt_pending?: number;
  partial?: number;
  retry_exhausted?: number;
  skipped?: number;
}

export interface SweepSummaryOut {
  domain_count: number;
  duration_seconds?: number | null;
  finished_at?: string | null;
  max_iters_hit_count?: number;
  started_at?: string | null;
  status_counts: SweepStatusCountsOut;
  sweep_id: string;
}

export interface SystemHealthPageOut {
  items: HealthItem[];
  limit: number;
  offset: number;
  total: number;
}

export interface TerraformResourceOut {
  file_path?: string;
  project_name?: string;
  resource_name: string;
  resource_type: string;
}

export interface TerraformResourcesPageOut {
  items?: TerraformResourceOut[];
  limit?: number;
  offset?: number;
  total?: number;
}

export interface UiSettingsPageOut {
  items: SettingRow[];
  limit: number;
  offset: number;
  total: number;
}

export interface VersionOut {
  environment: string;
  version: string;
}

export interface VsphereAlarmOut {
  acknowledged?: boolean | null;
  alarm_name?: string | null;
  entity_name?: string | null;
  entity_type?: string | null;
  overall_status?: string | null;
  triggered_at?: string | null;
}

export interface VsphereClusterOut {
  datacenter_name?: string;
  drs_enabled?: boolean | null;
  ha_enabled?: boolean | null;
  name: string;
  num_hosts?: number | null;
  overall_status?: string;
  total_memory_gb?: number | null;
}

export interface VsphereDatacenterOut {
  cluster_count?: number;
  host_count?: number;
  name: string;
  vm_count?: number;
}

export interface VsphereDatastoreOut {
  accessible?: boolean | null;
  capacity_gb?: number | null;
  datastore_type?: string;
  free_gb?: number | null;
  name: string;
  used_pct?: number | null;
}

export interface VsphereHostOut {
  cluster_name?: string;
  connection_state?: string;
  in_maintenance_mode?: boolean | null;
  memory_gb?: number | null;
  model?: string;
  name: string;
  overall_status?: string;
  power_state?: string;
  vendor?: string;
  version?: string;
  vm_count?: number | null;
}

export interface VsphereLicenseOut {
  edition_key?: string | null;
  expiration?: string | null;
  name?: string | null;
  total?: number | null;
  used?: number | null;
}

export interface VsphereNetworkOut {
  accessible?: boolean | null;
  host_count?: number | null;
  name: string;
  network_kind: string;
  num_ports?: number | null;
  vm_count?: number | null;
}

export interface VsphereOverviewOut {
  clusters?: VsphereClusterOut[];
  datacenters?: VsphereDatacenterOut[];
  datastores?: VsphereDatastoreOut[];
  hosts?: VsphereHostOut[];
  summary?: VsphereSummaryOut;
  vms?: VsphereVmOut[];
}

export interface VspherePermissionOut {
  entity?: string | null;
  is_group?: boolean | null;
  principal?: string | null;
  propagate?: boolean | null;
  role_name?: string | null;
}

export interface VsphereResourcePoolOut {
  cpu_limit?: number | null;
  cpu_reservation?: number | null;
  memory_limit?: number | null;
  memory_reservation?: number | null;
  name: string;
  vm_count?: number | null;
}

export interface VsphereSecondaryOut {
  alarms?: VsphereAlarmOut[];
  licenses?: VsphereLicenseOut[];
  networks?: VsphereNetworkOut[];
  permissions?: VspherePermissionOut[];
  resource_pools?: VsphereResourcePoolOut[];
  snapshots?: VsphereSnapshotOut[];
  summary?: VsphereSecondarySummaryOut;
  vm_disks?: VsphereVmDiskOut[];
}

export interface VsphereSecondarySummaryOut {
  alarm_count?: number;
  license_count?: number;
  network_count?: number;
  permission_count?: number;
  resource_pool_count?: number;
  snapshot_count?: number;
  stale_snapshot_count?: number;
  vm_disk_count?: number;
}

export interface VsphereSnapshotOut {
  age_days?: number | null;
  created_at?: string | null;
  is_current?: boolean | null;
  name?: string | null;
  state?: string | null;
  vm_name?: string;
}

export interface VsphereSummaryOut {
  cluster_count?: number;
  datacenter_count?: number;
  datastore_count?: number;
  host_count?: number;
  latest_metric_at?: string | null;
  network_count?: number;
  template_count?: number;
  total_capacity_gb?: number;
  total_free_gb?: number;
  vm_count?: number;
}

export interface VsphereVmDiskOut {
  backing_type?: string | null;
  capacity_gb?: number | null;
  datastore_name?: string | null;
  label?: string | null;
  thin_provisioned?: boolean | null;
  vm_name?: string;
}

export interface VsphereVmOut {
  esxi_host?: string;
  guest_full_name?: string;
  ip_address?: string;
  memory_mb?: number | null;
  name: string;
  num_cpu?: number | null;
  overall_status?: string;
  power_state?: string;
  tools_status?: string;
}

export interface VulnOut {
  cve: string;
  cvss?: number;
  exploits?: number;
  host: string;
  pci_fail?: boolean;
  pkg: string;
  priority?: number;
  severity: string;
  sla: string;
  status: string;
}

export interface VulnPageOut {
  items: VulnOut[];
  limit: number;
  offset: number;
  total: number;
}

export interface WebhookCloudAcceptedOut {
  accepted: boolean;
  domain: string;
  provider: string;
}

export interface WebhookDeliveryOut {
  attempt_count: number;
  category: string;
  created_at: string;
  dedup_key?: string | null;
  delivered_at?: string | null;
  domain?: string | null;
  id: string;
  last_error?: string | null;
  max_attempts: number;
  next_attempt_at?: string | null;
  status: string;
  subscription_id: string;
}

export interface WebhookDeliveryPageOut {
  items: WebhookDeliveryOut[];
  total: number;
}

export interface WebhookSubscriptionOut {
  active: boolean;
  created_at: string;
  created_by?: string;
  description?: string;
  domain_filter?: string | null;
  event_pattern: string;
  has_secret: boolean;
  id: string;
  name: string;
  target_url: string;
  updated_at: string;
}

export interface WebhookSubscriptionPageOut {
  items: WebhookSubscriptionOut[];
  total: number;
}

export interface WindowsLocalGroupMemberOut {
  group_name: string;
  member_name: string;
}

export interface WindowsLocalUserOut {
  enabled?: boolean | null;
  is_admin?: boolean;
  last_logon?: string | null;
  password_never_expires?: boolean | null;
  password_required?: boolean | null;
  username: string;
}

export interface WindowsPatchOut {
  hostname: string;
  kb_list: string[];
  last_patched?: string | null;
  pending_count: number;
  services?: WindowsServiceOut[];
  winrm_status: string;
}

export interface WindowsServiceOut {
  name: string;
  path?: string | null;
  start_type?: string | null;
  state?: string | null;
}
