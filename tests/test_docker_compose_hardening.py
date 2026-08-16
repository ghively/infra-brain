"""Structural assertions on docker/docker-compose*.yml (DEPLOY-1/3/4, OB-2, network
segmentation). Parses the real YAML the same way the existing k8s-yaml-lint CI job
parses k8s/*.yaml — a regression test against config drift, not app code."""

from pathlib import Path

import yaml

COMPOSE_DIR = Path(__file__).resolve().parents[1] / "docker"
BASE = yaml.safe_load((COMPOSE_DIR / "docker-compose.yml").read_text())

LONG_RUNNING = {"postgres", "redis", "n8n", "app", "scheduler", "mcp"}
ONE_SHOT = {"migrate", "seed"}
# Phase 4 OB-1: deadman-prober is long-running but uses `restart: always`
# (not `unless-stopped`) and has no compose healthcheck of its own — it IS
# the healthcheck for app/scheduler, so it's tracked separately from
# LONG_RUNNING rather than folded in and weakening those assertions.
ALWAYS_RESTART = {"deadman-prober"}
ALL_SERVICES = LONG_RUNNING | ONE_SHOT | ALWAYS_RESTART


def test_no_obsolete_version_key_in_any_compose_file():
    for name in ["docker-compose.yml", "docker-compose.deploy.yml", "docker-compose.dev.yml"]:
        text = (COMPOSE_DIR / name).read_text()
        doc = yaml.safe_load(text)
        assert "version" not in doc, f"{name} still has the obsolete top-level `version:` key"


def test_every_service_has_resource_limits():
    for name in ALL_SERVICES:
        svc = BASE["services"][name]
        assert "mem_limit" in svc, f"{name} missing mem_limit"
        assert "cpus" in svc, f"{name} missing cpus"


def test_every_long_running_service_has_restart_policy():
    for name in LONG_RUNNING:
        svc = BASE["services"][name]
        assert svc.get("restart") == "unless-stopped", f"{name} missing restart policy"


def test_one_shot_services_have_no_restart_policy():
    for name in ONE_SHOT:
        svc = BASE["services"][name]
        assert "restart" not in svc, f"{name} is one-shot and must not auto-restart"


def test_every_long_running_service_has_healthcheck():
    for name in LONG_RUNNING:
        svc = BASE["services"][name]
        assert "healthcheck" in svc, f"{name} missing healthcheck"


def test_every_long_running_service_has_log_caps():
    for name in LONG_RUNNING:
        svc = BASE["services"][name]
        logging_cfg = svc.get("logging", {})
        assert logging_cfg.get("driver") == "json-file", f"{name} missing json-file log driver"
        opts = logging_cfg.get("options", {})
        assert "max-size" in opts and "max-file" in opts, f"{name} missing log size/file caps"


def test_n8n_is_network_segmented_away_from_backend_services():
    n8n_nets = set(BASE["services"]["n8n"].get("networks", []))
    backend_nets = set(BASE["services"]["postgres"].get("networks", []))
    assert n8n_nets, "n8n must be explicitly assigned a network"
    assert backend_nets, "postgres must be explicitly assigned a network"
    assert not (n8n_nets & backend_nets), "n8n must not share a network with postgres/backend"


def test_top_level_networks_block_defines_backend_and_n8n_nets():
    nets = BASE.get("networks", {})
    assert "backend" in nets
    assert "n8n_net" in nets


def test_deadman_prober_has_log_caps_and_restart_always():
    svc = BASE["services"]["deadman-prober"]
    assert svc.get("restart") == "always"
    logging_cfg = svc.get("logging", {})
    assert logging_cfg.get("driver") == "json-file"
    opts = logging_cfg.get("options", {})
    assert "max-size" in opts and "max-file" in opts


def test_deadman_prober_env_plumbing():
    svc = BASE["services"]["deadman-prober"]
    env = svc.get("environment", {})
    assert "APP_URL" in env
    assert "OPS_WEBHOOK_URL" in env
    assert "OPS_WEBHOOK_TOKEN" in env


def test_deadman_prober_depends_on_app():
    svc = BASE["services"]["deadman-prober"]
    assert "app" in svc.get("depends_on", [])


# ---------------------------------------------------------------------------
# Langfuse stack (docker/langfuse/docker-compose.yml) — separate compose
# project; same hardening bar (resource limits, log caps, pinned tags) minus
# the main stack's healthcheck/restart-policy conventions (Langfuse's own
# images define their own readiness semantics).
# ---------------------------------------------------------------------------

LANGFUSE_DIR = Path(__file__).resolve().parents[1] / "docker" / "langfuse"
LANGFUSE = yaml.safe_load((LANGFUSE_DIR / "docker-compose.yml").read_text())
LANGFUSE_SERVICES = [
    "langfuse-postgres",
    "langfuse-clickhouse",
    "langfuse-redis",
    "langfuse-minio",
    "langfuse-web",
    "langfuse-worker",
]


def test_langfuse_stack_has_six_services():
    assert set(LANGFUSE["services"].keys()) == set(LANGFUSE_SERVICES)


def test_langfuse_every_service_has_resource_limits():
    for name in LANGFUSE_SERVICES:
        svc = LANGFUSE["services"][name]
        assert "mem_limit" in svc, f"{name} missing mem_limit"
        assert "cpus" in svc, f"{name} missing cpus"


def test_langfuse_clickhouse_honors_mem_floor():
    svc = LANGFUSE["services"]["langfuse-clickhouse"]
    mem = str(svc["mem_limit"]).lower().rstrip("b")
    assert mem.endswith("g"), f"clickhouse mem_limit {svc['mem_limit']!r} must be expressed in GiB"
    value = float(mem[:-1])
    assert value >= 8, f"clickhouse mem_limit {svc['mem_limit']!r} must honor the ~8GiB floor"


def test_langfuse_every_service_has_log_caps():
    for name in LANGFUSE_SERVICES:
        svc = LANGFUSE["services"][name]
        logging_cfg = svc.get("logging", {})
        assert logging_cfg.get("driver") == "json-file", f"{name} missing json-file log driver"
        opts = logging_cfg.get("options", {})
        assert "max-size" in opts and "max-file" in opts, f"{name} missing log size/file caps"


def test_langfuse_images_are_pinned_not_latest():
    for name in LANGFUSE_SERVICES:
        image = LANGFUSE["services"][name]["image"]
        assert ":latest" not in image, f"{name} must not use a floating :latest tag"
        assert ":" in image, f"{name} image must carry an explicit tag"


def test_langfuse_no_obsolete_version_key():
    assert "version" not in LANGFUSE


def test_langfuse_telemetry_disabled_on_web_and_worker():
    for name in ("langfuse-web", "langfuse-worker"):
        env = LANGFUSE["services"][name]["environment"]
        assert env.get("TELEMETRY_ENABLED") in ("false", False, "${TELEMETRY_ENABLED:-false}")


def test_langfuse_named_volumes_are_prefixed():
    for vol_name, vol_def in LANGFUSE.get("volumes", {}).items():
        assert vol_def["name"].startswith("infra-brain-langfuse_"), (
            f"volume {vol_name} not under the infra-brain-langfuse_ prefix"
        )


def test_langfuse_web_joins_external_observability_network():
    nets = LANGFUSE.get("networks", {})
    assert "observability" in nets
    assert nets["observability"].get("external") is True
    web_nets = set(LANGFUSE["services"]["langfuse-web"].get("networks", []))
    assert "observability" in web_nets


# ---------------------------------------------------------------------------
# GitLab #174 — NetDiscoveryAgent's Tier 1 host sweep needs CAP_NET_RAW to do
# an ARP-based scan (mac/mac_vendor come back null otherwise); the scheduler
# container runs nmap as non-root (`USER appuser` in the Dockerfile), so the
# capability is granted via a `setcap` file capability on the nmap binary.
# Docker's DEFAULT capability set already includes NET_RAW in the bounding
# set, so the file capability alone is sufficient -- no `cap_add:` is needed
# in compose, and none exists. These are regression guards against the fix
# (already shipped in docker/Dockerfile) silently regressing; they are
# static config assertions, not a substitute for live verification against
# the real running container.
# ---------------------------------------------------------------------------

DOCKERFILE_TEXT = (COMPOSE_DIR / "Dockerfile").read_text()


def test_nmap_has_cap_net_raw_setcap_in_dockerfile():
    assert "setcap cap_net_raw+ep" in DOCKERFILE_TEXT
    setcap_line = next(
        line for line in DOCKERFILE_TEXT.splitlines() if "setcap cap_net_raw+ep" in line
    )
    assert "/usr/bin/nmap" in setcap_line


def test_dockerfile_does_not_grant_cap_net_admin_to_nmap():
    """NET_ADMIN is NOT in Docker's default capability bounding set -- granting
    it to the unprivileged nmap binary (instead of NET_RAW, which IS in the
    default set) makes execve() fail outright without an explicit
    `cap_add:`, per the Dockerfile's own #174 comment. Guards against a
    well-intentioned but wrong "fix" that would actually break nmap.
    """
    assert "cap_net_admin+ep" not in DOCKERFILE_TEXT


def test_scheduler_service_does_not_drop_net_raw_capability():
    """The scheduler service (where NetDiscoveryAgent's nmap subprocess runs,
    per its network_mode: host below) must not cap_drop NET_RAW or ALL --
    doing so would remove NET_RAW from the bounding set and silently break
    the #174 setcap fix even though the Dockerfile line itself is untouched.
    """
    svc = BASE["services"]["scheduler"]
    dropped = {str(c).upper() for c in svc.get("cap_drop", [])}
    assert "NET_RAW" not in dropped
    assert "ALL" not in dropped


def test_scheduler_service_uses_host_networking_for_layer2_visibility():
    """netdiscovery's ARP-based Tier 1 sweep needs the host's real L2
    interfaces/routes -- the bridge network only exposes the Docker bridge
    subnet. Regression guard for the network_mode: host requirement
    documented at length on the scheduler service block itself."""
    svc = BASE["services"]["scheduler"]
    assert svc.get("network_mode") == "host"
