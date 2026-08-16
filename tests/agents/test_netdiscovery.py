"""Tests for NetDiscoveryAgent.

Coverage per spec §12:
  - Reachable-scope discovery (mocked routes) → expected subnet set
  - Exclusion guard: in / out / malformed → fails-closed
  - Aggressiveness → nmap flag mapping
  - Fragile-device downgrade (always ICMP/single-port, never port/version scan)
  - DNS tier (mocked resolver; PTR/forward; resolver restriction honored)
  - nmap XML parsing: -sn and service/OS fixtures → host/service rows; MAC OUI
  - Classification: known/shadow-IT/threat + dedup
  - Watchlist: dangerous-port + suspicious-signature flag + drift
  - Idempotent upsert: re-run no duplicates; last_seen advances
  - Run-status accuracy: forced tool failure → run failed with error_message
"""

import logging
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.netdiscovery import NetDiscoveryAgent, _threat_rank
from infra_brain.db.models import (
    CollectionRun,
    DriftEvent,
    NetDiscoveryHost,
    NetDiscoveryService,
    Resource,
)

from tests.support.pg import make_engine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_settings(**overrides):
    defaults = dict(
        netdiscovery_aggressiveness="polite",
        netdiscovery_exclude_cidrs="",
        netdiscovery_enable_ping_sweep=True,
        netdiscovery_enable_port_scan=True,
        netdiscovery_max_rate=50,
        netdiscovery_max_subnet_hosts=1024,
        netdiscovery_include_cidrs="",
        netdiscovery_subnet_timeout_seconds=120,
        netdiscovery_tier1_budget_seconds=480,
        netdiscovery_scan_delay=10,
        netdiscovery_port_scan_window="",
        netdiscovery_fragile_cidrs="",
        netdiscovery_fragile_hosts="",
        netdiscovery_dns_resolver="",
        netdiscovery_dangerous_ports="23,445,3389,5900,2323",
        netdiscovery_suspicious_signatures=(
            "teamviewer,anydesk,cobalt strike,meterpreter,empire,"
            "metasploit,ngrok,frp,chisel,rathole,ncat,netcat"
        ),
        default_zone="corpor",
        collect_timeout_seconds=300,
        collection_disabled_domains="",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_agent(settings=None) -> NetDiscoveryAgent:
    agent = NetDiscoveryAgent.__new__(NetDiscoveryAgent)
    agent.settings = settings if settings is not None else _make_settings()
    agent.callbacks = []
    return agent


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def session_factory(engine):
    @contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    return _get_session


@pytest.fixture(autouse=True)
def _patch_shared_base_session(session_factory):
    """M-1: point ``infra_brain.etl.base``'s ``get_session`` at the test engine too.

    NetDiscoveryAgent.run()'s run-row finalize now goes through the shared
    ``ETLConnector._finalize_run`` / ``run_row_guard`` helpers (so the SEC-2
    DSN scrub and the TRK-106 finalize-on-BaseException are enforced in one
    place rather than re-implemented per override). Those helpers resolve
    ``get_session`` from ``infra_brain.etl.base``, so a test that patches only
    ``infra_brain.agents.netdiscovery.get_session`` would let the finalize
    escape to the real engine. Autouse because every run()-level test in this
    module hits that path; it binds to the same per-test ``engine`` fixture the
    explicit patches use, so both references see one database.
    """
    with patch("infra_brain.etl.base.get_session", session_factory):
        yield


@pytest.fixture
def agent():
    return _make_agent()


# ---------------------------------------------------------------------------
# Nmap XML fixtures
# ---------------------------------------------------------------------------

_NMAP_SN_XML = dedent("""
<?xml version="1.0" encoding="UTF-8"?>
<nmaprun>
  <host>
    <status state="up"/>
    <address addrtype="ipv4" addr="10.0.0.1"/>
    <address addrtype="mac" addr="00:11:22:33:44:55" vendor="Acme Corp"/>
    <hostnames><hostname name="router.local"/></hostnames>
  </host>
  <host>
    <status state="up"/>
    <address addrtype="ipv4" addr="10.0.0.2"/>
  </host>
  <host>
    <status state="down"/>
    <address addrtype="ipv4" addr="10.0.0.3"/>
  </host>
</nmaprun>
""").strip()

_NMAP_SERVICE_XML = dedent("""
<?xml version="1.0" encoding="UTF-8"?>
<nmaprun>
  <host>
    <status state="up"/>
    <address addrtype="ipv4" addr="10.0.0.1"/>
    <ports>
      <port portid="22" protocol="tcp">
        <state state="open"/>
        <service name="ssh" product="OpenSSH" version="8.9"/>
      </port>
      <port portid="3389" protocol="tcp">
        <state state="open"/>
        <service name="ms-wbt-server" product="Microsoft Terminal Services"/>
      </port>
      <port portid="80" protocol="tcp">
        <state state="closed"/>
        <service name="http"/>
      </port>
    </ports>
    <os><osmatch name="Linux 5.15"/></os>
  </host>
</nmaprun>
""").strip()


# ---------------------------------------------------------------------------
# 1. Reachable scope discovery
# ---------------------------------------------------------------------------


def test_reachable_subnets_parses_ip_route_output(agent):
    """Mocked ``ip route`` output → correct subnet list (excludes loopback/default)."""
    fake_output = dedent("""
        default via 10.0.0.1 dev eth0 proto dhcp
        10.0.0.0/24 dev eth0 proto kernel scope link src 10.0.0.50
        172.16.10.0/24 dev eth1 proto kernel scope link src 172.16.10.1
        127.0.0.0/8 dev lo proto kernel scope link src 127.0.0.1
        169.254.0.0/16 dev eth0 proto kernel scope link src 169.254.1.1
    """)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_output)
        subnets = agent._reachable_subnets()

    # Only routable, non-loopback, non-link-local
    assert "10.0.0.0/24" in subnets
    assert "172.16.10.0/24" in subnets
    assert not any("127" in s for s in subnets)
    assert not any("169.254" in s for s in subnets)


def test_reachable_subnets_excludes_docker_bridge_interfaces(agent):
    """Container-internal bridge routes (docker0/br-*/veth*/podman*/cni*) must
    never reach the sweep target list, regardless of subnet size.

    Regression guard: TRK-239's oversized-subnet guard in ``_sweep_targets()``
    turned every Docker bridge /16 into a permanent per-run error (streak
    121, confirmed live) instead of silently excluding routes that were never
    real LAN segments in the first place. Filtering by interface name here
    means bridge routes never reach that guard at all, while a genuinely
    oversized REAL route (a non-bridge interface) still hits it as designed.
    """
    fake_output = dedent("""
        default via 10.0.0.1 dev eth0 proto dhcp
        10.0.0.0/24 dev eth0 proto kernel scope link src 10.0.0.50
        172.17.0.0/16 dev docker0 proto kernel scope link src 172.17.0.1
        172.18.0.0/16 dev br-abc123def456 proto kernel scope link src 172.18.0.1
        172.19.0.0/16 dev veth1a2b3c4 proto kernel scope link src 172.19.0.1
        172.20.0.0/16 dev podman0 proto kernel scope link src 172.20.0.1
        172.21.0.0/16 dev cni-podman0 proto kernel scope link src 172.21.0.1
    """)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_output)
        subnets = agent._reachable_subnets()

    assert subnets == ["10.0.0.0/24"]


def test_reachable_subnets_returns_empty_on_failure(agent):
    """If ``ip route`` fails, returns empty list without raising."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("ip not found")
        subnets = agent._reachable_subnets()
    assert subnets == []


def test_reachable_subnets_missing_ip_binary_logs_warning(agent, caplog):
    """A missing ``ip`` binary must be loud (warning), not indistinguishable from no routes.

    Regression guard for TRK-232: the app image shipped without iproute2, so
    ``subprocess.run(["ip", "route"])`` raised FileNotFoundError, was swallowed at
    debug level, and net discovery silently collected nothing beyond Tier 0.
    """
    with caplog.at_level(logging.WARNING, logger="infra_brain.agents.netdiscovery"):
        with patch("subprocess.run", side_effect=FileNotFoundError("ip not found")):
            subnets = agent._reachable_subnets()

    assert subnets == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING when the ip binary is missing"
    msg = " ".join(r.getMessage() for r in warnings)
    assert "iproute2" in msg
    assert "reachable subnets" in msg


def test_reachable_subnets_other_errors_stay_quiet(agent, caplog):
    """Non-FileNotFoundError failures keep the previous debug-level behaviour."""
    with caplog.at_level(logging.DEBUG, logger="infra_brain.agents.netdiscovery"):
        with patch("subprocess.run", side_effect=OSError("boom")):
            subnets = agent._reachable_subnets()

    assert subnets == []
    # No "missing binary" warning — that path is reserved for FileNotFoundError.
    assert not [
        r for r in caplog.records if r.levelno >= logging.WARNING and "iproute2" in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# 1b. Tier 1 sweep scope + budget (TRK-239)
# ---------------------------------------------------------------------------


def test_sweep_targets_skips_oversized_subnet(agent):
    """The Docker bridge /16 `ip route` reports must not become an nmap target.

    Regression guard for TRK-239: TRK-232 installed iproute2, so
    ``_reachable_subnets()`` started returning the container's own bridge
    network (172.26.0.0/16 — 65534 addresses).  Sweeping it at --max-rate 50
    cannot finish inside COLLECT_TIMEOUT_SECONDS, and every scheduled run
    failed with resources_found=0.

    The oversized route is excluded from ``targets`` — the point of the guard —
    but is NOT reported through ``errors``. Refusing a 65k-address sweep is the
    cap working as designed, and BaseAgent maps errors-with-no-data to
    status="failed"; on a host whose routes are ALL oversized that produced a
    permanent "failed" (644 in one week) no action could clear. The refusal
    stays visible via a WARNING log per subnet.
    """
    targets, errors = agent._sweep_targets(["172.26.0.0/16", "10.0.0.0/24"])

    assert targets == ["10.0.0.0/24"]
    assert errors == []


def test_sweep_targets_keeps_subnets_within_the_size_cap(agent):
    targets, errors = agent._sweep_targets(["10.0.0.0/24", "198.51.100.0/25"])
    assert targets == ["10.0.0.0/24", "198.51.100.0/25"]
    assert errors == []


def test_sweep_targets_allowlist_filters_silently(agent):
    """An out-of-scope route is dropped without being reported as an error."""
    a = _make_agent(_make_settings(netdiscovery_include_cidrs="10.0.0.0/8"))
    targets, errors = a._sweep_targets(["10.1.2.0/24", "198.51.100.22/24"])
    assert targets == ["10.1.2.0/24"]
    assert errors == []


def test_sweep_targets_allowlist_narrows_wide_route_to_intersection(agent):
    """A narrow allowlist entry must genuinely narrow a wider discovered route.

    Regression guard for the intersection-vs-overlap bug: previously the code
    only checked ``net.overlaps(inc)`` and then appended the original (wide)
    route unmodified, so a narrow allowlist entry against a wide discovered
    supernet did nothing to shrink scope. The sweep target must become the
    allowlist entry itself, not the original /8.
    """
    a = _make_agent(
        _make_settings(
            netdiscovery_include_cidrs="10.5.0.0/16",
            netdiscovery_max_subnet_hosts=70000,
        )
    )
    targets, errors = a._sweep_targets(["10.0.0.0/8"])
    assert targets == ["10.5.0.0/16"]
    assert errors == []


def test_sweep_targets_allowlist_keeps_narrower_discovered_route_unchanged(agent):
    """If the discovered route is already narrower than the allowlist entry,
    the discovered route itself is the sweep target — no narrowing needed."""
    a = _make_agent(
        _make_settings(
            netdiscovery_include_cidrs="10.0.0.0/8",
            netdiscovery_max_subnet_hosts=70000,
        )
    )
    targets, errors = a._sweep_targets(["10.5.0.0/16"])
    assert targets == ["10.5.0.0/16"]
    assert errors == []


def test_sweep_targets_allowlist_multiple_entries_against_one_wide_route(agent):
    """Multiple allowlist entries can each narrow the same wide discovered route.

    Each match should produce its own (non-duplicated) narrowed target.
    """
    a = _make_agent(
        _make_settings(
            netdiscovery_include_cidrs="10.90.10.0/16,10.2.0.0/16",
            netdiscovery_max_subnet_hosts=70000,
        )
    )
    targets, errors = a._sweep_targets(["10.0.0.0/8"])
    # "10.90.10.0/16" has host bits set; ip_network(strict=False) normalizes it
    # to "10.90.0.0/16" (the scrub pass in 2831d9b rewrote the CIDRs here and
    # left an un-normalized, un-sorted expectation this assert could never meet).
    assert sorted(targets) == ["10.2.0.0/16", "10.90.0.0/16"]
    assert errors == []


def test_sweep_targets_reports_unparseable_subnet(agent):
    targets, errors = agent._sweep_targets(["not-a-network"])
    assert targets == []
    assert len(errors) == 1
    assert "not-a-network" in errors[0]


def test_tier1_sweep_issues_one_bounded_nmap_call_per_subnet(agent):
    """Chunked sweep: one bounded nmap invocation per subnet, not one giant call."""
    calls: list[tuple[list[str], int]] = []

    def fake_run_nmap(cmd, timeout=600):
        calls.append((cmd, timeout))
        return _NMAP_SN_XML

    with patch.object(agent, "_run_nmap", side_effect=fake_run_nmap):
        results, errors = agent._tier1_host_sweep(
            ["10.0.0.0/24", "192.0.2.0/24"], "/usr/bin/nmap", "polite"
        )

    assert len(calls) == 2
    assert calls[0][0][-1] == "10.0.0.0/24"
    assert calls[1][0][-1] == "192.0.2.0/24"
    # Each call carries its own per-subnet timeout, not the 600s default.
    assert all(timeout == 120 for _, timeout in calls)
    assert errors == []
    assert "10.0.0.1" in results


def test_tier1_sweep_polite_uses_t3_not_t2(agent):
    """TRK-280/TRK-281: polite's Tier 1 host sweep moved -T2 -> -T3.

    Regression guard distinct from test_aggressiveness_polite_flags, which
    covers the unrelated Tier 2 `_PROFILE_FLAGS["polite"]` (service/OS scan)
    constant — that one intentionally still carries -T2 and is untouched by
    this change.
    """
    calls: list[list[str]] = []

    def fake_run_nmap(cmd, timeout=600):
        calls.append(cmd)
        return _NMAP_SN_XML

    with patch.object(agent, "_run_nmap", side_effect=fake_run_nmap):
        agent._tier1_host_sweep(["10.0.0.0/24"], "/usr/bin/nmap", "polite")

    assert len(calls) == 1
    assert "-T3" in calls[0]
    assert "-T2" not in calls[0]
    assert "--max-rate" in calls[0]


def test_tier1_sweep_uses_tuned_timeout_and_rate_for_real_reachable_subnet(agent):
    """TRK-280/TRK-281: the 254-host 192.0.2.0/24 corporate LAN (real, host-network-
    reachable per MR !264) must use the tuned per-subnet timeout and --max-rate
    from settings, not the old 120s/50pkt-s starved values that were live-timing-out.

    Docker-bridge routes never reach this call at all — _sweep_targets() filters
    them out via netdiscovery_max_subnet_hosts before any nmap invocation — so
    every subnet handed to _tier1_host_sweep is already a genuine in-scope target,
    and a single tuned timeout/rate setting is sufficient (no separate "is this a
    real subnet" branch is needed inside the nmap-invocation code itself).
    """
    a = _make_agent(
        _make_settings(
            netdiscovery_subnet_timeout_seconds=300,
            netdiscovery_max_rate=100,
        )
    )
    calls: list[tuple[list[str], int]] = []

    def fake_run_nmap(cmd, timeout=600):
        calls.append((cmd, timeout))
        return _NMAP_SN_XML

    with patch.object(a, "_run_nmap", side_effect=fake_run_nmap):
        results, errors = a._tier1_host_sweep(["192.0.2.0/24"], "/usr/bin/nmap", "polite")

    assert len(calls) == 1
    cmd, timeout = calls[0]
    assert timeout == 300
    assert "--max-rate" in cmd
    assert cmd[cmd.index("--max-rate") + 1] == "100"
    assert cmd[-1] == "192.0.2.0/24"
    assert errors == []
    assert "10.0.0.1" in results


def test_tier1_sweep_partial_result_when_one_subnet_fails(agent):
    """F-007: a failing subnet is recorded, the others still produce hosts."""

    def fake_run_nmap(cmd, timeout=600):
        if cmd[-1] == "192.0.2.0/24":
            raise RuntimeError("nmap timed out: subnet unreachable")
        return _NMAP_SN_XML

    with patch.object(agent, "_run_nmap", side_effect=fake_run_nmap):
        results, errors = agent._tier1_host_sweep(
            ["10.0.0.0/24", "192.0.2.0/24"], "/usr/bin/nmap", "polite"
        )

    assert "10.0.0.1" in results  # partial result survived
    assert len(errors) == 1
    assert "192.0.2.0/24" in errors[0]
    assert "unreachable" in errors[0]


def test_tier1_sweep_stops_at_the_phase_budget(agent):
    """The whole-phase budget stops the loop before the outer 600s guard fires."""
    a = _make_agent(_make_settings(netdiscovery_tier1_budget_seconds=100))
    ticks = iter([0, 0, 150, 150, 150, 150])

    def fake_run_nmap(cmd, timeout=600):
        return _NMAP_SN_XML

    with (
        patch.object(a, "_run_nmap", side_effect=fake_run_nmap) as mock_nmap,
        patch("infra_brain.agents.netdiscovery.time.monotonic", side_effect=lambda: next(ticks)),
    ):
        results, errors = a._tier1_host_sweep(
            ["10.0.0.0/24", "192.0.2.0/24", "10.0.2.0/24"], "/usr/bin/nmap", "polite"
        )

    assert mock_nmap.call_count == 1  # first subnet swept, then budget exhausted
    assert "10.0.0.1" in results  # partial results retained
    assert any("budget" in e for e in errors)
    assert any("192.0.2.0/24" in e for e in errors)


def test_tier1_sweep_budget_exhaustion_skip_list_correct_with_duplicate_targets(agent):
    """A duplicate subnet string in ``targets`` (e.g. two interfaces/VRF routing
    to the same normalized CIDR from ``ip route``) must not corrupt the 'not
    swept' skip list reported at budget exhaustion. The skip list must reflect
    the current loop position, not the first occurrence of that subnet string
    anywhere in ``targets`` — otherwise an already-swept duplicate is wrongly
    reported as skipped, and a distinct subnet that actually WAS swept can be
    wrongly reported as skipped too.
    """
    a = _make_agent(_make_settings(netdiscovery_tier1_budget_seconds=100))
    ticks = iter([0, 0, 50, 150, 150, 150])

    def fake_run_nmap(cmd, timeout=600):
        return _NMAP_SN_XML

    with (
        patch.object(a, "_run_nmap", side_effect=fake_run_nmap) as mock_nmap,
        patch("infra_brain.agents.netdiscovery.time.monotonic", side_effect=lambda: next(ticks)),
    ):
        results, errors = a._tier1_host_sweep(
            ["10.0.0.0/24", "192.0.2.0/24", "10.0.0.0/24"], "/usr/bin/nmap", "polite"
        )

    assert mock_nmap.call_count == 2  # first two targets swept before budget ran out
    budget_error = next(e for e in errors if "budget" in e)
    # The already-swept "192.0.2.0/24" must not appear in the skip list — only
    # the unswept third entry (the duplicate "10.0.0.0/24") should.
    assert "192.0.2.0/24" not in budget_error
    assert "10.0.0.0/24" in budget_error


def test_tier1_sweep_no_targets_returns_empty(agent):
    with patch.object(agent, "_run_nmap") as mock_nmap:
        results, errors = agent._tier1_host_sweep([], "/usr/bin/nmap", "polite")
    mock_nmap.assert_not_called()
    assert results == {}
    assert errors == []


# ---------------------------------------------------------------------------
# 2. Exclusion guard (_should_probe)
# ---------------------------------------------------------------------------


def test_should_probe_allows_non_excluded_ip():
    agent = _make_agent(_make_settings(netdiscovery_exclude_cidrs="198.51.100.23/24"))
    assert agent._should_probe("10.0.0.1") is True


def test_should_probe_denies_ip_in_excluded_cidr():
    agent = _make_agent(_make_settings(netdiscovery_exclude_cidrs="198.51.100.23/24"))
    assert agent._should_probe("198.51.100.24") is False


def test_should_probe_denies_exact_match():
    agent = _make_agent(_make_settings(netdiscovery_exclude_cidrs="10.10.10.5/32"))
    assert agent._should_probe("10.10.10.5") is False


def test_should_probe_fails_closed_on_malformed_cidr():
    """A malformed CIDR in the exclude list → fail-closed (deny the probe)."""
    agent = _make_agent(_make_settings(netdiscovery_exclude_cidrs="not-a-cidr,10.0.0.0/8"))
    # The malformed entry causes _should_probe to return False for safety.
    assert agent._should_probe("10.0.0.1") is False


def test_should_probe_denies_empty_ip():
    assert _make_agent()._should_probe("") is False


def test_should_probe_denies_unparseable_ip():
    assert _make_agent()._should_probe("not-an-ip") is False


def test_should_probe_denies_whitespace_only():
    assert _make_agent()._should_probe("   ") is False


# ---------------------------------------------------------------------------
# 3. Aggressiveness → nmap flag mapping
# ---------------------------------------------------------------------------


def test_aggressiveness_polite_flags(agent):
    """polite profile includes -T2 and --max-rate."""
    from infra_brain.agents.netdiscovery import _PROFILE_FLAGS

    flags = _PROFILE_FLAGS["polite"]
    assert "-T2" in flags
    assert "--max-rate" in flags
    assert "-O" not in flags
    assert "--version-intensity" in flags
    assert flags[flags.index("--version-intensity") + 1] == "0"


def test_aggressiveness_normal_flags():
    from infra_brain.agents.netdiscovery import _PROFILE_FLAGS

    flags = _PROFILE_FLAGS["normal"]
    assert "-T3" in flags
    assert "-sV" in flags
    assert "-O" not in flags


def test_aggressiveness_aggressive_flags():
    from infra_brain.agents.netdiscovery import _PROFILE_FLAGS

    flags = _PROFILE_FLAGS["aggressive"]
    assert "-T4" in flags
    assert "-O" in flags
    assert "-p" in flags
    # Full port range
    assert "1-65535" in flags


def test_aggressiveness_passive_no_flags():
    from infra_brain.agents.netdiscovery import _PROFILE_FLAGS

    assert _PROFILE_FLAGS["passive"] == []


# ---------------------------------------------------------------------------
# 4. Fragile-device downgrade
# ---------------------------------------------------------------------------


def test_fragile_detection_by_host_list():
    agent = _make_agent(_make_settings(netdiscovery_fragile_hosts="10.0.0.10,10.0.0.20"))
    assert agent._is_fragile("10.0.0.10") is True
    assert agent._is_fragile("10.0.0.99") is False


def test_fragile_detection_by_cidr():
    agent = _make_agent(_make_settings(netdiscovery_fragile_cidrs="172.30.0.0/24"))
    assert agent._is_fragile("172.30.0.5") is True
    assert agent._is_fragile("172.30.1.5") is False


def test_fragile_detection_by_mac_oui():
    """A MAC OUI matching the iDRAC list → fragile, regardless of IP."""
    agent = _make_agent()
    # 00:18:8B is the Dell iDRAC OUI in _FRAGILE_OUI_PREFIXES
    assert agent._is_fragile("10.0.0.5", mac="00:18:8B:AA:BB:CC") is True
    assert agent._is_fragile("10.0.0.5", mac="AA:BB:CC:DD:EE:FF") is False


def test_tier2_scan_gates_fragile_hosts_to_fragile_flags():
    """TRK-048: _FRAGILE_FLAGS must actually gate the nmap command for a
    fragile-tagged host — the reduced flag set is used instead of the
    configured aggressiveness profile, and aggressive-only flags (-O,
    --version-intensity) never reach a fragile host's command line.
    """
    from infra_brain.agents.netdiscovery import _FRAGILE_FLAGS

    agent = _make_agent(_make_settings(netdiscovery_fragile_hosts="10.0.0.10"))
    live_hosts = {
        "10.0.0.10": {"mac": None},  # fragile via host list
        "10.0.0.20": {"mac": None},  # normal host
    }

    commands: list[list[str]] = []

    def fake_run_nmap(cmd: list[str], timeout: int = 600) -> str:
        commands.append(cmd)
        return ""

    with patch.object(agent, "_run_nmap", side_effect=fake_run_nmap):
        agent._tier2_service_scan(live_hosts, "nmap", "aggressive")

    assert len(commands) == 2
    fragile_cmd = next(c for c in commands if "10.0.0.10" in c)
    normal_cmd = next(c for c in commands if "10.0.0.20" in c)

    assert "10.0.0.20" not in fragile_cmd
    assert "10.0.0.10" not in normal_cmd

    for flag in _FRAGILE_FLAGS:
        assert flag in fragile_cmd
    # Aggressive-profile-only flags must not leak into the fragile command.
    assert "-O" not in fragile_cmd
    assert "--version-intensity" not in fragile_cmd

    # The normal host still gets the full aggressive profile.
    assert "-O" in normal_cmd


def test_tier2_scan_partial_result_when_one_group_fails():
    """TRK-280/281 follow-up: a failing scan group is recorded, the other
    group's rows still survive — mirrors Tier 1's F-007 per-subnet resilience.
    """
    agent = _make_agent(_make_settings(netdiscovery_fragile_hosts="10.0.0.10"))
    live_hosts = {
        "10.0.0.10": {"mac": None},  # fragile
        "10.0.0.20": {"mac": None},  # normal
    }

    def fake_run_nmap(cmd, timeout=600):
        if "10.0.0.10" in cmd:
            raise RuntimeError("nmap timed out: fragile group unreachable")
        return _NMAP_SERVICE_XML

    with patch.object(agent, "_run_nmap", side_effect=fake_run_nmap):
        rows, errors = agent._tier2_service_scan(live_hosts, "nmap", "aggressive")

    assert len(errors) == 1
    assert "fragile" in errors[0]
    assert "unreachable" in errors[0]
    assert rows  # the normal group's rows survived the fragile group's failure


def test_tier2_scan_stops_at_the_phase_budget():
    """The whole-phase budget stops further groups before the outer 600s guard fires."""
    agent = _make_agent(
        _make_settings(
            netdiscovery_fragile_hosts="10.0.0.10", netdiscovery_tier2_budget_seconds=100
        )
    )
    live_hosts = {
        "10.0.0.10": {"mac": None},  # fragile — scanned first
        "10.0.0.20": {"mac": None},  # normal — budget exhausted before this group
    }
    ticks = iter([0, 0, 150, 150])

    def fake_run_nmap(cmd, timeout=600):
        return _NMAP_SERVICE_XML

    with (
        patch.object(agent, "_run_nmap", side_effect=fake_run_nmap) as mock_nmap,
        patch("infra_brain.agents.netdiscovery.time.monotonic", side_effect=lambda: next(ticks)),
    ):
        rows, errors = agent._tier2_service_scan(live_hosts, "nmap", "aggressive")

    assert mock_nmap.call_count == 1  # fragile group scanned, then budget exhausted
    assert any("budget" in e for e in errors)
    assert any("normal" in e for e in errors)


def test_tier2_scan_no_hosts_returns_empty():
    agent = _make_agent()
    with patch.object(agent, "_run_nmap") as mock_nmap:
        rows, errors = agent._tier2_service_scan({}, "nmap", "polite")
    mock_nmap.assert_not_called()
    assert rows == []
    assert errors == []


def test_tier2_scan_chunks_large_groups_and_survives_one_chunk_failing():
    """Live-verified 2026-07-30: one invocation across ~109 real hosts
    exceeded the whole-group budget and contributed zero rows. Chunking
    (netdiscovery_tier2_chunk_size) must split a large normal-host group into
    multiple nmap invocations, and one chunk's failure must not cost the
    other chunks' rows — same discipline as Tier 1's per-subnet resilience.
    """
    agent = _make_agent(_make_settings(netdiscovery_tier2_chunk_size=2))
    live_hosts = {f"10.0.0.{i}": {"mac": None} for i in range(1, 6)}  # 5 normal hosts

    calls: list[list[str]] = []

    def fake_run_nmap(cmd, timeout=600):
        calls.append(cmd)
        if "10.0.0.3" in cmd:  # second chunk fails
            raise RuntimeError("nmap timed out: chunk unreachable")
        return _NMAP_SERVICE_XML

    with patch.object(agent, "_run_nmap", side_effect=fake_run_nmap):
        rows, errors = agent._tier2_service_scan(live_hosts, "nmap", "aggressive")

    assert len(calls) == 3  # 5 hosts / chunk_size=2 -> 3 chunks (2, 2, 1)
    assert len(errors) == 1
    assert "chunk 2/3" in errors[0]
    assert rows  # the two surviving chunks' rows are still present


def test_fragile_flag_appears_in_sn_parse(agent):
    """Fragile OUI in nmap XML → is_fragile=True in the parsed result."""
    xml = dedent("""
    <?xml version="1.0"?>
    <nmaprun>
      <host>
        <status state="up"/>
        <address addrtype="ipv4" addr="10.0.0.1"/>
        <address addrtype="mac" addr="00:18:8B:AA:BB:CC" vendor="Dell iDRAC"/>
      </host>
    </nmaprun>
    """).strip()
    result = agent._parse_sn_xml(xml)
    assert "10.0.0.1" in result
    assert result["10.0.0.1"]["is_fragile"] is True


# ---------------------------------------------------------------------------
# 5. DNS tier (mocked resolver)
# ---------------------------------------------------------------------------


def test_ptr_sweep_returns_resolved_ips(agent):
    """PTR sweep over a small subnet returns {ip: hostname} for hosts that
    reverse-resolve — the resolved hostname is kept, not discarded."""
    resolved = {}

    def fake_gethostbyaddr(ip):
        if ip == "10.0.0.1":
            return ("router.local", [], [ip])
        raise OSError("no reverse")

    with patch("socket.gethostbyaddr", side_effect=fake_gethostbyaddr):
        resolved = agent._ptr_sweep("10.0.0.0/30", "")

    assert resolved["10.0.0.1"] == "router.local"
    assert "10.0.0.2" not in resolved  # didn't resolve


def test_ptr_sweep_skips_large_subnets(agent):
    """/16 subnet exceeds 1022-host limit → no PTR sweep performed."""
    with patch("socket.gethostbyaddr") as mock_ptr:
        result = agent._ptr_sweep("10.0.0.0/16", "")
    mock_ptr.assert_not_called()
    assert result == {}


def test_tier0_passive_respects_exclusion(engine, session_factory):
    """IPs from R7Asset that fall in the excluded CIDR are filtered out."""
    from infra_brain.db.models import R7Asset

    agent = _make_agent(_make_settings(netdiscovery_exclude_cidrs="10.99.0.0/24"))

    with Session(engine) as s:
        res = Resource(
            id=uuid.uuid4(),
            domain="r7",
            type="r7_asset",
            name="test-asset",
            source="test",
            zone="corpor",
            last_seen=datetime.now(UTC),
        )
        s.add(res)
        s.flush()
        s.add(
            R7Asset(
                id=uuid.uuid4(),
                resource_id=res.id,
                r7_asset_id=1001,
                hostname="host.local",
                ip="10.99.0.5",
                os_family="Linux",
                risk_score=0,
                vuln_total=0,
            )
        )
        s.commit()

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(agent, "_ptr_sweep", return_value={}),
        patch.object(agent, "_tailscale_peers", return_value={}),
    ):
        result = agent._tier0_passive()

    # 10.99.0.5 is in the excluded CIDR → filtered
    assert "10.99.0.5" not in result


def test_tier0_passive_includes_hostname_from_source_join(engine, session_factory):
    """TRK-273 cheap win: _tier0_passive must not discard hostname info it
    already has in hand from the rows it queries (R7Asset.hostname here) —
    it should surface it instead of returning {"ip": ip} with nothing else."""
    from infra_brain.db.models import R7Asset

    agent = _make_agent(_make_settings())

    with Session(engine) as s:
        res = Resource(
            id=uuid.uuid4(),
            domain="r7",
            type="r7_asset",
            name="test-asset",
            source="test",
            zone="corpor",
            last_seen=datetime.now(UTC),
        )
        s.add(res)
        s.flush()
        s.add(
            R7Asset(
                id=uuid.uuid4(),
                resource_id=res.id,
                r7_asset_id=1002,
                hostname="webapp01.corp.local",
                ip="10.5.0.9",
                os_family="Linux",
                risk_score=0,
                vuln_total=0,
            )
        )
        s.commit()

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(agent, "_ptr_sweep", return_value={}),
        patch.object(agent, "_tailscale_peers", return_value={}),
    ):
        result = agent._tier0_passive()

    assert result["10.5.0.9"] == "webapp01.corp.local"


def test_ptr_sweep_hostname_reaches_persisted_host(engine, session_factory):
    """TRK-273 cheap win: a hostname resolved by _ptr_sweep must flow through
    _tier0_passive -> run()'s live_hosts dict -> the persisted NetDiscoveryHost
    row, instead of being resolved and then thrown away."""
    agent = _make_agent(_make_settings(netdiscovery_aggressiveness="passive"))

    def fake_gethostbyaddr(ip):
        if ip == "10.7.0.2":
            return ("printer07.corp.local", [], [ip])
        raise OSError("no reverse")

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(agent, "_reachable_subnets", return_value=["10.7.0.0/30"]),
        patch.object(agent, "_tailscale_peers", return_value={}),
        patch("socket.gethostbyaddr", side_effect=fake_gethostbyaddr),
    ):
        result = agent.run(trigger_type="test")

    assert result.status == "completed"

    with Session(engine) as s:
        host = s.query(NetDiscoveryHost).filter_by(ip="10.7.0.2").first()
        assert host is not None
        assert host.hostname == "printer07.corp.local"


# ---------------------------------------------------------------------------
# 6. nmap XML parsing
# ---------------------------------------------------------------------------


def test_parse_sn_xml_returns_up_hosts(agent):
    """Only 'up' hosts appear in the result; 'down' hosts are skipped."""
    result = agent._parse_sn_xml(_NMAP_SN_XML)
    assert "10.0.0.1" in result
    assert "10.0.0.2" in result
    assert "10.0.0.3" not in result  # state=down


def test_parse_sn_xml_captures_mac_and_vendor(agent):
    result = agent._parse_sn_xml(_NMAP_SN_XML)
    assert result["10.0.0.1"]["mac"] == "00:11:22:33:44:55"
    assert result["10.0.0.1"]["mac_vendor"] == "Acme Corp"


def test_parse_sn_xml_captures_hostname(agent):
    result = agent._parse_sn_xml(_NMAP_SN_XML)
    assert result["10.0.0.1"]["hostname"] == "router.local"


def test_parse_sn_xml_handles_empty_input(agent):
    assert agent._parse_sn_xml("") == {}


def test_parse_sn_xml_handles_malformed_xml(agent):
    """Malformed XML returns {} without raising."""
    result = agent._parse_sn_xml("this is not xml<")
    assert result == {}


def test_parse_service_xml_returns_open_ports(agent):
    """Only open ports appear; closed/filtered ports are skipped."""
    rows = agent._parse_service_xml(_NMAP_SERVICE_XML, "polite")
    ports = [(r["port"], r["proto"]) for r in rows]
    assert (22, "tcp") in ports
    assert (3389, "tcp") in ports
    assert (80, "tcp") not in ports  # state=closed


def test_parse_service_xml_captures_banner(agent):
    rows = agent._parse_service_xml(_NMAP_SERVICE_XML, "polite")
    ssh_row = next(r for r in rows if r["port"] == 22)
    assert "OpenSSH" in (ssh_row["banner"] or "")


def test_parse_service_xml_captures_os_guess(agent):
    rows = agent._parse_service_xml(_NMAP_SERVICE_XML, "normal")
    assert any("Linux" in (r.get("os_guess") or "") for r in rows)


def test_parse_service_xml_handles_empty_input(agent):
    assert agent._parse_service_xml("", "polite") == []


def test_parse_service_xml_excludes_filtered_host(agent):
    """Hosts that fail _should_probe are excluded even if nmap returned them."""
    xml = dedent("""
    <?xml version="1.0"?>
    <nmaprun>
      <host>
        <status state="up"/>
        <address addrtype="ipv4" addr="10.99.0.1"/>
        <ports><port portid="22" protocol="tcp"><state state="open"/></port></ports>
      </host>
    </nmaprun>
    """).strip()
    agent2 = _make_agent(_make_settings(netdiscovery_exclude_cidrs="10.99.0.0/24"))
    rows = agent2._parse_service_xml(xml, "polite")
    assert rows == []


# ---------------------------------------------------------------------------
# 7. Classification
# ---------------------------------------------------------------------------


def test_classify_known_host_not_shadow_it(agent):
    live = {"10.0.0.1": {"ip": "10.0.0.1", "responded": True}}
    known = {"10.0.0.1"}
    result = agent._classify_hosts(live, known)
    assert result["10.0.0.1"]["is_known"] is True
    assert result["10.0.0.1"]["is_shadow_it"] is False


def test_classify_unknown_responded_host_is_shadow_it(agent):
    live = {"10.0.0.50": {"ip": "10.0.0.50", "responded": True}}
    known: set[str] = set()
    result = agent._classify_hosts(live, known)
    assert result["10.0.0.50"]["is_shadow_it"] is True
    assert result["10.0.0.50"]["threat_level"] in ("low", "med", "high")


def test_classify_unreachable_host_not_shadow_it(agent):
    """A host that didn't respond (passive only) is NOT shadow-IT."""
    live = {"10.0.0.50": {"ip": "10.0.0.50", "responded": False}}
    known: set[str] = set()
    result = agent._classify_hosts(live, known)
    assert result["10.0.0.50"]["is_shadow_it"] is False


def test_classify_threat_level_escalation(agent):
    """Unknown + no MAC vendor → threat_level at least 'med'."""
    live = {"10.0.0.50": {"ip": "10.0.0.50", "responded": True, "mac_vendor": None}}
    known: set[str] = set()
    result = agent._classify_hosts(live, known)
    assert _threat_rank(result["10.0.0.50"]["threat_level"]) >= _threat_rank("med")


# ---------------------------------------------------------------------------
# 8. Watchlist matches
# ---------------------------------------------------------------------------


def test_dangerous_port_sets_flag(agent):
    is_dangerous, _ = agent._classify_service(
        3389,
        "ms-wbt-server",
        None,
        agent._parse_dangerous_ports(),
        agent._parse_suspicious_signatures(),
    )
    assert is_dangerous is True


def test_non_dangerous_port_no_flag(agent):
    is_dangerous, _ = agent._classify_service(
        443,
        "https",
        "nginx",
        agent._parse_dangerous_ports(),
        agent._parse_suspicious_signatures(),
    )
    assert is_dangerous is False


def test_suspicious_signature_in_service_name(agent):
    _, is_suspicious = agent._classify_service(
        4444,
        "anydesk",
        None,
        agent._parse_dangerous_ports(),
        agent._parse_suspicious_signatures(),
    )
    assert is_suspicious is True


def test_suspicious_signature_in_banner(agent):
    _, is_suspicious = agent._classify_service(
        9999,
        "unknown",
        "TeamViewer Host",
        agent._parse_dangerous_ports(),
        agent._parse_suspicious_signatures(),
    )
    assert is_suspicious is True


def test_no_watchlist_match(agent):
    is_dangerous, is_suspicious = agent._classify_service(
        443,
        "https",
        "nginx/1.24",
        agent._parse_dangerous_ports(),
        agent._parse_suspicious_signatures(),
    )
    assert is_dangerous is False
    assert is_suspicious is False


# ---------------------------------------------------------------------------
# 9. Idempotent upsert
# ---------------------------------------------------------------------------


def test_idempotent_upsert_no_duplicates(engine, session_factory):
    """Running persist twice for the same IP must not create duplicate rows."""
    agent = _make_agent()
    run_id = uuid.uuid4()

    classified = {
        "10.0.0.1": {
            "ip": "10.0.0.1",
            "responded": True,
            "mac": "00:11:22:33:44:55",
            "mac_vendor": "Test",
            "hostname": "host.local",
            "is_fragile": False,
            "is_known": True,
            "is_shadow_it": False,
            "threat_level": "none",
            "discovery_tier": "tier1",
        }
    }

    # Seed a CollectionRun so the FK resolves.
    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id, domain="netdiscovery", trigger_type="test", status="in_progress"
            )
        )
        s.commit()

    with patch("infra_brain.agents.netdiscovery.get_session", session_factory):
        agent._persist(classified, [], run_id, frozenset(), [])
        agent._persist(classified, [], run_id, frozenset(), [])

    with Session(engine) as s:
        count = s.query(NetDiscoveryHost).filter_by(ip="10.0.0.1").count()
        assert count == 1


def test_persist_update_branch_merges_resource_metadata(engine, session_factory):
    """Second scan of the same host must MERGE Resource.metadata_, not overwrite it.

    Regression guard for Task 4.4 review finding CRITICAL 2: the update branch
    previously did ``resource.metadata_ = {...}`` directly (a full overwrite).
    Both scans must route through ``upsert_resource`` so a key present only in
    the first scan (e.g. ``mac_vendor``) survives a second scan that introduces
    a new key (e.g. a changed ``hostname``) but omits the old one's value.
    """
    agent = _make_agent()
    run_id = uuid.uuid4()

    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id, domain="netdiscovery", trigger_type="test", status="in_progress"
            )
        )
        s.commit()

    first_scan = {
        "10.0.0.1": {
            "ip": "10.0.0.1",
            "responded": True,
            "mac": "00:11:22:33:44:55",
            "mac_vendor": "Acme Corp",
            "hostname": None,
            "is_fragile": False,
            "is_known": True,
            "is_shadow_it": False,
            "threat_level": "none",
        }
    }
    with patch("infra_brain.agents.netdiscovery.get_session", session_factory):
        agent._persist(first_scan, [], run_id, frozenset(), [])

    with Session(engine) as s:
        resource = (
            s.query(Resource)
            .filter_by(domain="netdiscovery", type="discovered_host", name="10.0.0.1")
            .one()
        )
        assert resource.metadata_["mac_vendor"] == "Acme Corp"

    # Second scan: hostname now resolves, but mac/mac_vendor info.get() would
    # be None-ish absent info re-supplying it — simulate a scan that only
    # updates hostname (a realistic re-scan where a new field is learned).
    second_scan = {
        "10.0.0.1": {
            "ip": "10.0.0.1",
            "responded": True,
            "mac": "00:11:22:33:44:55",
            "mac_vendor": "Acme Corp",
            "hostname": "host.local",
            "is_fragile": False,
            "is_known": True,
            "is_shadow_it": False,
            "threat_level": "none",
        }
    }
    with patch("infra_brain.agents.netdiscovery.get_session", session_factory):
        agent._persist(second_scan, [], run_id, frozenset(), [])

    with Session(engine) as s:
        resource = (
            s.query(Resource)
            .filter_by(domain="netdiscovery", type="discovered_host", name="10.0.0.1")
            .one()
        )
        # New key present.
        assert resource.metadata_["hostname"] == "host.local"
        # Original key from the first scan is PRESERVED (merge, not overwrite).
        assert resource.metadata_["mac_vendor"] == "Acme Corp"
        assert resource.metadata_["mac"] == "00:11:22:33:44:55"


def test_persist_service_update_branch_merges_resource_metadata(engine, session_factory):
    """Second scan of the same service must MERGE Resource.metadata_, not overwrite.

    Regression guard for Task 4.4 review finding CRITICAL 2 (service-row write):
    the update branch previously only touched ``last_seen`` and never refreshed
    ``metadata_`` at all. It must now route through ``upsert_resource`` so the
    service metadata is merged/refreshed consistently across scans.
    """
    agent = _make_agent()
    run_id = uuid.uuid4()

    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id, domain="netdiscovery", trigger_type="test", status="in_progress"
            )
        )
        s.commit()

    classified = {
        "10.0.0.1": {
            "ip": "10.0.0.1",
            "responded": True,
            "is_fragile": False,
            "is_known": True,
            "is_shadow_it": False,
            "threat_level": "none",
        }
    }

    first_services = [
        {"ip": "10.0.0.1", "port": 22, "proto": "tcp", "service": "ssh", "banner": None}
    ]
    with patch("infra_brain.agents.netdiscovery.get_session", session_factory):
        agent._persist(classified, first_services, run_id, frozenset(), [])

    with Session(engine) as s:
        svc_resource = (
            s.query(Resource)
            .filter_by(domain="netdiscovery", type="discovered_service", name="10.0.0.1:22/tcp")
            .one()
        )
        assert svc_resource.metadata_["service"] == "ssh"
        assert svc_resource.metadata_.get("banner") is None

    second_services = [
        {
            "ip": "10.0.0.1",
            "port": 22,
            "proto": "tcp",
            "service": "ssh",
            "banner": "OpenSSH_9.0",
        }
    ]
    with patch("infra_brain.agents.netdiscovery.get_session", session_factory):
        agent._persist(classified, second_services, run_id, frozenset(), [])

    with Session(engine) as s:
        svc_resource = (
            s.query(Resource)
            .filter_by(domain="netdiscovery", type="discovered_service", name="10.0.0.1:22/tcp")
            .one()
        )
        # Refreshed key.
        assert svc_resource.metadata_["banner"] == "OpenSSH_9.0"
        # Key from the first scan still present (merge policy of upsert_resource).
        assert svc_resource.metadata_["service"] == "ssh"


def test_idempotent_upsert_last_seen_advances(engine, session_factory):
    """Re-running persist advances ``last_seen`` on the host row."""
    import time

    agent = _make_agent()
    run_id = uuid.uuid4()
    classified = {
        "10.0.0.1": {
            "ip": "10.0.0.1",
            "responded": True,
            "is_fragile": False,
            "is_known": True,
            "is_shadow_it": False,
            "threat_level": "none",
        }
    }

    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id, domain="netdiscovery", trigger_type="test", status="in_progress"
            )
        )
        s.commit()

    with patch("infra_brain.agents.netdiscovery.get_session", session_factory):
        agent._persist(classified, [], run_id, frozenset(), [])

    with Session(engine) as s:
        first = s.query(NetDiscoveryHost).filter_by(ip="10.0.0.1").one()
        t1 = first.last_seen

    time.sleep(0.05)

    with patch("infra_brain.agents.netdiscovery.get_session", session_factory):
        agent._persist(classified, [], run_id, frozenset(), [])

    with Session(engine) as s:
        second = s.query(NetDiscoveryHost).filter_by(ip="10.0.0.1").one()
        assert second.last_seen >= t1


# ---------------------------------------------------------------------------
# 10. Service persistence with watchlist flags
# ---------------------------------------------------------------------------


def test_service_dangerous_flag_persisted(engine, session_factory):
    """A service on port 3389 must be persisted with is_dangerous=True."""
    agent = _make_agent()
    run_id = uuid.uuid4()

    classified = {
        "10.0.0.1": {
            "ip": "10.0.0.1",
            "responded": True,
            "is_fragile": False,
            "is_known": True,
            "is_shadow_it": False,
            "threat_level": "none",
        }
    }
    services = [{"ip": "10.0.0.1", "port": 3389, "proto": "tcp", "service": "rdp"}]

    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id, domain="netdiscovery", trigger_type="test", status="in_progress"
            )
        )
        s.commit()

    with patch("infra_brain.agents.netdiscovery.get_session", session_factory):
        agent._persist(
            classified,
            services,
            run_id,
            agent._parse_dangerous_ports(),
            agent._parse_suspicious_signatures(),
        )

    with Session(engine) as s:
        svc = s.query(NetDiscoveryService).filter_by(port=3389).first()
        assert svc is not None
        assert svc.is_dangerous is True


def test_shadow_it_drift_event_emitted(engine, session_factory):
    """A newly-discovered shadow-IT host must produce a drift event."""
    agent = _make_agent()
    run_id = uuid.uuid4()

    classified = {
        "10.0.0.99": {
            "ip": "10.0.0.99",
            "responded": True,
            "is_fragile": False,
            "is_known": False,
            "is_shadow_it": True,
            "threat_level": "low",
        }
    }

    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id, domain="netdiscovery", trigger_type="test", status="in_progress"
            )
        )
        s.commit()

    with patch("infra_brain.agents.netdiscovery.get_session", session_factory):
        agent._persist(classified, [], run_id, frozenset(), [])

    with Session(engine) as s:
        events = s.query(DriftEvent).filter_by(drift_type="shadow_it_discovered").all()
        assert len(events) >= 1
        assert events[0].new_value["ip"] == "10.0.0.99"


# ---------------------------------------------------------------------------
# 10b. Per-item SAVEPOINT isolation (TRK-132 regression guard)
# ---------------------------------------------------------------------------


def test_persist_savepoint_isolates_bad_host_item(engine, session_factory, caplog):
    """One malformed host item must not sink the rest of the batch.

    Regression guard for TRK-132: ``_persist`` previously shared a single
    commit at the end of the run — one bad row raising partway through the
    loop poisoned the whole transaction (InFailedSqlTransaction cascade) and
    silently lost every other host write for that run. Each host item is now
    wrapped in its own SAVEPOINT (``session.begin_nested()``), so a bad
    middle item is logged and skipped while its siblings still persist.
    """
    agent = _make_agent()
    run_id = uuid.uuid4()

    good_info = {
        "responded": True,
        "is_fragile": False,
        "is_known": True,
        "is_shadow_it": False,
        "threat_level": "none",
    }
    # classified["10.0.0.2"] is malformed (None instead of a dict) — accessing
    # ``.get`` on it inside _upsert_host_item raises AttributeError, simulating
    # a corrupted/bad item landing in the middle of an otherwise-good batch.
    classified = {
        "10.0.0.1": dict(good_info),
        "10.0.0.2": None,
        "10.0.0.3": dict(good_info),
    }

    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id, domain="netdiscovery", trigger_type="test", status="in_progress"
            )
        )
        s.commit()

    with patch("infra_brain.agents.netdiscovery.get_session", session_factory):
        hosts_written, services_written = agent._persist(classified, [], run_id, frozenset(), [])

    # Only the 2 good hosts count as written; the bad one is skipped, not fatal.
    assert hosts_written == 2
    assert services_written == 0
    assert any(
        "skipping bad host item" in rec.message and "10.0.0.2" in rec.message
        for rec in caplog.records
    )

    with Session(engine) as s:
        ips = {row.ip for row in s.query(NetDiscoveryHost).all()}
        assert ips == {"10.0.0.1", "10.0.0.3"}


def test_persist_savepoint_isolates_bad_service_item(engine, session_factory, caplog):
    """One malformed service item must not sink the other services for the same host.

    Regression guard for TRK-132 — service half of the same fix. Each service
    item is wrapped in its own SAVEPOINT, independent from its sibling
    services (and from the host-level SAVEPOINT).
    """
    agent = _make_agent()
    run_id = uuid.uuid4()

    classified = {
        "10.0.0.1": {
            "ip": "10.0.0.1",
            "responded": True,
            "is_fragile": False,
            "is_known": True,
            "is_shadow_it": False,
            "threat_level": "none",
        }
    }
    # The middle service row is malformed — missing the required "port" key,
    # so ``svc["port"]`` raises KeyError inside _upsert_service_item.
    services = [
        {"ip": "10.0.0.1", "port": 22, "proto": "tcp", "service": "ssh"},
        {"ip": "10.0.0.1", "proto": "tcp", "service": "bad-row-missing-port"},
        {"ip": "10.0.0.1", "port": 443, "proto": "tcp", "service": "https"},
    ]

    with Session(engine) as s:
        s.add(
            CollectionRun(
                id=run_id, domain="netdiscovery", trigger_type="test", status="in_progress"
            )
        )
        s.commit()

    with patch("infra_brain.agents.netdiscovery.get_session", session_factory):
        hosts_written, services_written = agent._persist(
            classified, services, run_id, frozenset(), []
        )

    assert hosts_written == 1
    # Only the 2 good services count as written; the bad one is skipped.
    assert services_written == 2
    assert any("skipping bad service item" in rec.message for rec in caplog.records)

    with Session(engine) as s:
        ports = {row.port for row in s.query(NetDiscoveryService).all()}
        assert ports == {22, 443}


def test_run_surfaces_persist_skip_errors_as_partial(engine, session_factory):
    """M-2 (F-007): ``_persist``'s per-item SAVEPOINT loop (TRK-132) logs and
    skips a bad host item so its siblings still commit — but that per-item
    resilience swallowed the failure entirely: it never reached ``run()``'s
    ``errors`` list, so a run that dropped EVERY discovered host still
    reported "completed" with an empty ``errors`` list on both the returned
    ``CollectionResult`` and the persisted ``CollectionRun`` row. A dropped
    host write must downgrade the run per the R3 status mapping (errors +
    no data -> failed; here there IS other data, so "partial" is possible,
    but the only real invariant under test is: never "completed").
    """
    agent = _make_agent()

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(agent, "_tier0_passive", return_value={"10.0.0.1"}),
        patch.object(agent, "_reachable_subnets", return_value=[]),
        patch("shutil.which", return_value=None),
        patch.object(
            agent,
            "_classify_hosts",
            return_value={
                "10.0.0.1": {
                    "responded": True,
                    "is_fragile": False,
                    "is_known": True,
                    "is_shadow_it": False,
                    "threat_level": "none",
                }
            },
        ),
        patch.object(agent, "_load_known_ips", return_value=set()),
        patch.object(
            agent,
            "_upsert_host_item",
            side_effect=RuntimeError("simulated bad row"),
        ),
    ):
        result = agent.run(trigger_type="test")

    assert result.status != "completed", (
        "a run that dropped every discovered host write must never report 'completed'"
    )
    assert result.errors, "the dropped host item must be recorded in result.errors"
    assert any("10.0.0.1" in e for e in result.errors)

    with Session(engine) as s:
        run = s.query(CollectionRun).filter_by(domain="netdiscovery").first()
        assert run is not None
        assert run.status != "completed", (
            "the persisted CollectionRun row must also reflect the dropped work"
        )


# ---------------------------------------------------------------------------
# 11. Run-status accuracy (regression guard for spec §11 anti-pattern)
# ---------------------------------------------------------------------------


def test_run_honours_collection_disabled_domains(engine, session_factory):
    """AA-R-11/12: netdiscovery fully overrides run() and must still respect
    collection_disabled_domains, same as the standard ETLConnector.run() flow."""
    agent = _make_agent(_make_settings(collection_disabled_domains="netdiscovery,other"))

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(agent, "_tier0_passive") as mock_tier0,
    ):
        result = agent.run(trigger_type="test")

    mock_tier0.assert_not_called()
    assert result.status == "skipped"

    with Session(engine) as s:
        run = s.query(CollectionRun).filter_by(domain="netdiscovery").first()
        assert run is not None
        assert run.status == "skipped"
        assert "collection_disabled_domains" in (run.error_message or "")


def test_run_fails_with_error_message_on_tier0_failure(engine, session_factory):
    """A Tier 0 exception must mark the run FAILED with a non-empty error_message.

    This is the regression guard for the linux/windows 'except Exception: return []'
    masking anti-pattern.  A real failure must NEVER report status=completed/0.
    """
    agent = _make_agent()

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(
            agent,
            "_tier0_passive",
            side_effect=RuntimeError("DNS resolver exploded"),
        ),
    ):
        result = agent.run(trigger_type="test")

    assert result.status == "failed"
    assert result.resources_found == 0
    assert any("Tier 0" in e for e in result.errors)

    # Also verify the CollectionRun DB row was updated to failed.
    with Session(engine) as s:
        run = s.query(CollectionRun).filter_by(domain="netdiscovery").first()
        assert run is not None
        assert run.status == "failed"
        assert run.error_message is not None
        assert "DNS resolver exploded" in run.error_message


def test_run_fails_with_error_message_on_tier1_failure(engine, session_factory):
    """A Tier 1 exception marks run FAILED — not completed/0."""
    agent = _make_agent()

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(agent, "_tier0_passive", return_value=set()),
        patch.object(agent, "_reachable_subnets", return_value=["10.0.0.0/24"]),
        patch("shutil.which", return_value="/usr/bin/nmap"),
        patch.object(agent, "_tier1_host_sweep", side_effect=RuntimeError("nmap process crashed")),
    ):
        result = agent.run(trigger_type="test")

    assert result.status == "failed"
    assert any("Tier 1" in e for e in result.errors)


def test_run_keeps_tier0_data_when_tier1_subnet_is_out_of_bounds(engine, session_factory):
    """TRK-239 regression: an unsweepable subnet must NOT zero out the run.

    The live failure mode was: `ip route` reports only the Docker bridge /16,
    Tier 1 hands it to one 600s-bounded nmap call, the call times out, and the
    whole run is marked failed with resources_found=0 — discarding the 2560
    hosts Tier 0 had already harvested from the DB.  Now the oversized subnet
    is skipped and the Tier 0 data survives.

    The run reports "completed", not "partial": the size cap refusing a sweep
    is this agent working as designed, so it no longer files an entry in
    ``errors``. That reporting choice was itself causing a second outage — on a
    host where EVERY discovered route is oversized there is no Tier 1 data at
    all, so errors-with-no-data became status="failed" on all 644 runs in a
    week, unclearable without an env change. netdiscovery is in
    ZERO_OK_DOMAINS, so a legitimately empty sweep raises no alert; the refusal
    stays visible through the per-subnet WARNING log.
    """
    agent = _make_agent()

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(agent, "_tier0_passive", return_value={"10.0.0.1"}),
        patch.object(agent, "_reachable_subnets", return_value=["172.26.0.0/16"]),
        patch("shutil.which", return_value="/usr/bin/nmap"),
        patch.object(agent, "_run_nmap") as mock_nmap,
        patch.object(agent, "_load_known_ips", return_value=set()),
        patch.object(agent, "_persist", return_value=(7, 0)),
    ):
        result = agent.run(trigger_type="test")

    # The oversized subnet never reached nmap at all.
    mock_nmap.assert_not_called()
    # Tier 0's data survives — the whole point of the guard.
    assert result.resources_found == 7
    # ...and the refusal no longer poisons the run's status.
    assert result.status == "completed"
    assert result.errors == []

    with Session(engine) as s:
        run = s.query(CollectionRun).filter_by(domain="netdiscovery").first()
        assert run is not None
        assert run.status == "completed"
        assert run.resources_found == 7
        # The refused subnet is no longer recorded as a run error — see the
        # docstring above. It is still visible in the agent's WARNING log.
        assert not (run.error_message or "")


def test_run_populates_detail_rows_written(engine, session_factory):
    """GitLab #148: run() writes NetDiscoveryHost/NetDiscoveryService detail
    rows via _persist but — because it bypasses ETLConnector._write_details —
    never populated CollectionRun.detail_rows_written, which stayed 0 on every
    production run while the detail tables filled up."""
    agent = _make_agent()

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(agent, "_tier0_passive", return_value={"10.0.0.1"}),
        patch.object(agent, "_reachable_subnets", return_value=[]),
        patch("shutil.which", return_value="/usr/bin/nmap"),
        patch.object(agent, "_load_known_ips", return_value=set()),
        patch.object(agent, "_persist", return_value=(5, 3)),
    ):
        result = agent.run(trigger_type="test")

    assert result.status == "completed"
    assert result.resources_found == 8
    assert result.detail_rows_written == 8

    with Session(engine) as s:
        run = s.query(CollectionRun).filter_by(domain="netdiscovery").first()
        assert run is not None
        assert run.detail_rows_written == 8


def test_run_skips_tier1_cleanly_when_nmap_absent(engine, session_factory):
    """Missing nmap binary → Tier 1 skipped cleanly; run may still complete."""
    agent = _make_agent()

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(agent, "_tier0_passive", return_value=set()),
        patch.object(agent, "_reachable_subnets", return_value=[]),
        patch(
            "shutil.which",
            return_value=None,  # nmap absent
        ),
        patch.object(agent, "_classify_hosts", return_value={}),
        patch.object(agent, "_load_known_ips", return_value=set()),
        patch.object(agent, "_persist", return_value=(0, 0)),
    ):
        result = agent.run(trigger_type="test")

    # Should not fail because of missing nmap — clean skip
    assert result.status == "completed"
    assert result.errors == []


def test_run_passive_aggressiveness_skips_active_tiers(engine, session_factory):
    """aggressiveness=passive must skip Tier 1 and Tier 2 entirely."""
    agent = _make_agent(_make_settings(netdiscovery_aggressiveness="passive"))

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(agent, "_tier0_passive", return_value=set()),
        patch.object(agent, "_tier1_host_sweep") as mock_t1,
        patch.object(agent, "_tier2_service_scan") as mock_t2,
        patch.object(agent, "_classify_hosts", return_value={}),
        patch.object(agent, "_load_known_ips", return_value=set()),
        patch.object(agent, "_persist", return_value=(0, 0)),
    ):
        agent.run(trigger_type="test")

    mock_t1.assert_not_called()
    mock_t2.assert_not_called()


def test_run_sets_discovery_tier_from_the_real_tier_pipeline(engine, session_factory):
    """Regression test: discovery_tier must reflect the tier that actually
    detected each host, not silently fall through to the NetDiscoveryHost
    column's ``"passive"`` default for every host regardless of how it was
    found (GitLab audit finding — _upsert_host_item's
    ``info.get("discovery_tier", "passive")`` always hit the default because
    nothing upstream ever set the key).

    Exercises the real Tier0 -> Tier1 -> Tier2 -> _classify_hosts pipeline
    inside run() with hand-mocked tier results (not a hand-built ``classified``
    dict passed straight to ``_persist``) so a regression to the
    never-set-it bug fails here even though it's invisible to persist-level
    tests that pre-populate discovery_tier by hand (e.g.
    test_idempotent_upsert_no_duplicates).
    """
    agent = _make_agent()

    # 10.0.0.1: passive-only — harvested from DB/PTR, never actively probed.
    # 10.0.0.2: live only via the Tier 1 ping sweep (never known to Tier 0).
    # 10.0.0.3: live via Tier 1 AND actively service-scanned in Tier 2.
    tier1_results = {
        "10.0.0.2": {"mac": None, "mac_vendor": None, "hostname": None},
        "10.0.0.3": {"mac": None, "mac_vendor": None, "hostname": None},
    }
    tier2_rows = [
        {
            "ip": "10.0.0.3",
            "port": 22,
            "proto": "tcp",
            "service": "ssh",
            "banner": None,
            "fingerprint": None,
            "os_guess": None,
        }
    ]

    captured = {}

    def _fake_persist(classified, service_rows, run_id, dangerous_ports, suspicious_sigs):
        captured["classified"] = classified
        return (len(classified), len(service_rows))

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(agent, "_tier0_passive", return_value={"10.0.0.1": None}),
        patch.object(agent, "_reachable_subnets", return_value=["10.0.0.0/24"]),
        patch("shutil.which", return_value="/usr/bin/nmap"),
        patch.object(agent, "_tier1_host_sweep", return_value=(tier1_results, [])),
        patch.object(agent, "_tier2_service_scan", return_value=(tier2_rows, [])),
        patch.object(agent, "_load_known_ips", return_value=set()),
        patch.object(agent, "_persist", side_effect=_fake_persist),
    ):
        result = agent.run(trigger_type="test")

    assert result.status == "completed"
    classified = captured["classified"]
    assert classified["10.0.0.1"]["discovery_tier"] == "passive"
    assert classified["10.0.0.2"]["discovery_tier"] == "tier1"
    assert classified["10.0.0.3"]["discovery_tier"] == "tier2"


# ---------------------------------------------------------------------------
# 11b. GitLab #178 — rotation prevents permanent tail starvation under budget cuts
#
# TRACKER.md verdict: "Tier-1/Tier-2 budget exhaustion, not a watchdog reap.
# Stable chunk order starves a fixed tail every run." Before the fix, both
# _tier1_host_sweep's subnet list and _tier2_service_scan's per-group host
# list were always walked in the exact same order; whichever entry sorted
# last was the one a budget cut always sacrificed, run after run. The fix
# rotates the starting position by a persisted run_ordinal (the domain's
# collection_runs count) via _rotate_for_run. These tests simulate several
# consecutive collection runs -- not just one run "looking fine" by luck --
# and assert the tail actually gets covered across them.
# ---------------------------------------------------------------------------


def test_rotate_for_run_cycles_through_every_starting_position():
    items = ["a", "b", "c", "d"]
    # run_ordinal=0 is a no-op (preserves the original stable order) -- this
    # is the default every existing direct caller/test relies on.
    assert NetDiscoveryAgent._rotate_for_run(items, 0) == items
    assert NetDiscoveryAgent._rotate_for_run(items, 1) == ["b", "c", "d", "a"]
    assert NetDiscoveryAgent._rotate_for_run(items, 2) == ["c", "d", "a", "b"]
    assert NetDiscoveryAgent._rotate_for_run(items, 3) == ["d", "a", "b", "c"]
    # Wraps around via modulo once the ordinal exceeds len(items).
    assert NetDiscoveryAgent._rotate_for_run(items, 4) == items
    assert NetDiscoveryAgent._rotate_for_run(items, 5) == ["b", "c", "d", "a"]


def test_rotate_for_run_handles_empty_list():
    assert NetDiscoveryAgent._rotate_for_run([], 3) == []


def test_tier1_sweep_rotation_prevents_permanent_tail_starvation(agent):
    """With a per-phase budget that only ever lets ONE subnet through, a
    stable (non-rotating) order would starve the same tail subnets on every
    run, forever. Simulate several consecutive runs -- each with the
    distinct, incrementing run_ordinal that agent.run() actually derives
    from the growing collection_runs count -- and confirm every subnet gets
    swept in at least one of them.
    """
    subnets = ["10.0.0.0/24", "10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
    a = _make_agent(_make_settings(netdiscovery_tier1_budget_seconds=100))

    swept_per_run: list[str] = []
    for run_ordinal in range(len(subnets)):
        calls: list[str] = []

        def fake_run_nmap(cmd, timeout=600):
            calls.append(cmd[-1])
            return _NMAP_SN_XML

        # Budget allows exactly one subnet through before the next elapsed
        # check reports exhaustion (same ticking pattern as
        # test_tier1_sweep_stops_at_the_phase_budget above).
        ticks = iter([0, 0, 150, 150, 150, 150, 150, 150])
        with (
            patch.object(a, "_run_nmap", side_effect=fake_run_nmap),
            patch(
                "infra_brain.agents.netdiscovery.time.monotonic",
                side_effect=lambda: next(ticks),
            ),
        ):
            a._tier1_host_sweep(subnets, "/usr/bin/nmap", "polite", run_ordinal=run_ordinal)

        assert len(calls) == 1
        swept_per_run.append(calls[0])

    # Every subnet was the one actually swept in at least one simulated run
    # -- i.e. no subnet is a permanently-starved tail.
    assert set(swept_per_run) == set(subnets)
    # run_ordinal=0 still reproduces the original stable order (first subnet
    # first), so the no-rotation case is unchanged.
    assert swept_per_run[0] == subnets[0]


def test_tier2_scan_rotation_prevents_permanent_tail_starvation():
    """Same starvation pattern as Tier 1's test above, but for Tier 2's
    per-group host chunks (one host per chunk here, to isolate the effect)."""
    ips = [f"10.0.0.{i}" for i in range(1, 5)]  # 4 hosts, chunk_size=1 -> 4 chunks
    agent = _make_agent(
        _make_settings(netdiscovery_tier2_chunk_size=1, netdiscovery_tier2_budget_seconds=100)
    )
    live_hosts = {ip: {"mac": None} for ip in ips}

    scanned_per_run: list[str] = []
    for run_ordinal in range(len(ips)):
        calls: list[list[str]] = []

        def fake_run_nmap(cmd, timeout=600):
            calls.append(cmd)
            return _NMAP_SERVICE_XML

        ticks = iter([0, 0, 150, 150, 150, 150, 150, 150])
        with (
            patch.object(agent, "_run_nmap", side_effect=fake_run_nmap),
            patch(
                "infra_brain.agents.netdiscovery.time.monotonic",
                side_effect=lambda: next(ticks),
            ),
        ):
            agent._tier2_service_scan(live_hosts, "nmap", "aggressive", run_ordinal=run_ordinal)

        assert len(calls) == 1
        scanned_ip = next(ip for ip in ips if ip in calls[0])
        scanned_per_run.append(scanned_ip)

    assert set(scanned_per_run) == set(ips)


def test_run_derives_an_incrementing_rotation_ordinal_across_runs(engine, session_factory):
    """Wiring check: run() must derive run_ordinal from the persisted
    collection_runs count for this domain (no new schema needed) and thread
    it through to _tier1_host_sweep, so consecutive scheduled runs actually
    rotate instead of every call silently defaulting to run_ordinal=0.
    """
    agent = _make_agent()
    seen_ordinals: list[int] = []

    def fake_tier1(subnets, nmap_path, aggressiveness, run_ordinal=0):
        seen_ordinals.append(run_ordinal)
        return {}, []

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(agent, "_tier0_passive", return_value=set()),
        patch.object(agent, "_reachable_subnets", return_value=["10.0.0.0/24"]),
        patch("shutil.which", return_value="/usr/bin/nmap"),
        patch.object(agent, "_tier1_host_sweep", side_effect=fake_tier1),
        patch.object(agent, "_load_known_ips", return_value=set()),
        patch.object(agent, "_persist", return_value=(0, 0)),
    ):
        agent.run(trigger_type="test")
        agent.run(trigger_type="test")

    assert seen_ordinals == [1, 2]


# ---------------------------------------------------------------------------
# 12. Threat rank helper
# ---------------------------------------------------------------------------


def test_threat_rank_ordering():
    assert _threat_rank("none") < _threat_rank("low")
    assert _threat_rank("low") < _threat_rank("med")
    assert _threat_rank("med") < _threat_rank("high")
    assert _threat_rank("unknown") == 0  # defaults to none rank


# ---------------------------------------------------------------------------
# Item 1.5 (Wave 1) — subprocess boundary gate + audit
# ---------------------------------------------------------------------------


def test_gate_blocks_excluded_ip_before_subprocess(monkeypatch):
    """A gate-failing IP raises BEFORE subprocess.run. Fails today: no gate exists."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import pytest

    import infra_brain.agents.netdiscovery as mod

    agent = mod.NetDiscoveryAgent()
    agent.settings = SimpleNamespace(netdiscovery_exclude_cidrs="10.9.9.0/24")
    audit = MagicMock()
    monkeypatch.setattr(agent, "_audit_subprocess", audit)
    run_mock = MagicMock()
    monkeypatch.setattr(mod.subprocess, "run", run_mock)

    with pytest.raises(PermissionError, match="netdiscovery-gate"):
        agent._run_nmap(["nmap", "-sn", "-oX", "-", "10.9.9.5"])

    run_mock.assert_not_called()
    assert audit.call_args.kwargs["verdict"] == "deny"


def test_gate_blocks_overlapping_network_before_subprocess(monkeypatch):
    """A target NETWORK overlapping an excluded CIDR is denied pre-subprocess."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import pytest

    import infra_brain.agents.netdiscovery as mod

    agent = mod.NetDiscoveryAgent()
    agent.settings = SimpleNamespace(netdiscovery_exclude_cidrs="10.9.9.0/24")
    monkeypatch.setattr(agent, "_audit_subprocess", MagicMock())
    run_mock = MagicMock()
    monkeypatch.setattr(mod.subprocess, "run", run_mock)

    with pytest.raises(PermissionError, match="netdiscovery-gate"):
        agent._run_nmap(["nmap", "-sn", "-oX", "-", "10.9.0.0/16"])
    run_mock.assert_not_called()


def test_allowed_nmap_run_emits_allow_audit_row(monkeypatch):
    """Every allowed nmap invocation writes a verdict=allow audit row, then runs."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import infra_brain.agents.netdiscovery as mod

    agent = mod.NetDiscoveryAgent()
    agent.settings = SimpleNamespace(netdiscovery_exclude_cidrs="")
    audit = MagicMock()
    monkeypatch.setattr(agent, "_audit_subprocess", audit)
    run_mock = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="<xml/>", stderr=""))
    monkeypatch.setattr(mod.subprocess, "run", run_mock)

    out = agent._run_nmap(["nmap", "-sn", "-oX", "-", "192.0.2.0/28"])

    assert out == "<xml/>"
    run_mock.assert_called_once()
    assert audit.call_args.kwargs["verdict"] == "allow"
    assert audit.call_args.kwargs["tool"] == "nmap"


def test_ip_route_invocation_is_audited(monkeypatch):
    """_reachable_subnets writes an audit row for the ip-route subprocess."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import infra_brain.agents.netdiscovery as mod

    agent = mod.NetDiscoveryAgent()
    agent.settings = SimpleNamespace(netdiscovery_exclude_cidrs="")
    audit = MagicMock()
    monkeypatch.setattr(agent, "_audit_subprocess", audit)
    run_mock = MagicMock(
        return_value=SimpleNamespace(returncode=0, stdout="10.0.0.0/24 dev eth0\n", stderr="")
    )
    monkeypatch.setattr(mod.subprocess, "run", run_mock)

    subnets = agent._reachable_subnets()

    assert subnets == ["10.0.0.0/24"]
    assert audit.call_args.kwargs["tool"] == "ip_route"
    assert audit.call_args.kwargs["verdict"] == "allow"


# ---------------------------------------------------------------------------
# 12. Tailscale mesh-peer discovery (Tier 0 passive source)
# ---------------------------------------------------------------------------

_TAILSCALE_STATUS_JSON = """
{
  "Self": {
    "HostName": "ai_node",
    "DNSName": "ai_node.tailnet-example.ts.net.",
    "OS": "linux",
    "TailscaleIPs": ["203.0.113.19", "fd7a:115c:a1e0::1"],
    "Online": true
  },
  "Peer": {
    "node1": {
      "HostName": "amd_node",
      "DNSName": "amd_node.tailnet-example.ts.net.",
      "OS": "windows",
      "TailscaleIPs": ["203.0.113.11", "fd7a:115c:a1e0::2"],
      "Online": true
    },
    "node2": {
      "HostName": "hermes-sandbox",
      "DNSName": "hermes-sandbox.tailnet-example.ts.net.",
      "OS": "linux",
      "TailscaleIPs": ["203.0.113.16"],
      "Online": false
    },
    "node3": {
      "HostName": "ipv6-only-peer",
      "DNSName": "ipv6-only-peer.tailnet-example.ts.net.",
      "OS": "linux",
      "TailscaleIPs": ["fd7a:115c:a1e0::3"],
      "Online": true
    }
  }
}
"""


def _make_ts_agent(monkeypatch, which_result="/usr/bin/tailscale"):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import infra_brain.agents.netdiscovery as mod

    agent = mod.NetDiscoveryAgent()
    agent.settings = SimpleNamespace(netdiscovery_exclude_cidrs="")
    monkeypatch.setattr(agent, "_audit_subprocess", MagicMock())
    monkeypatch.setattr(mod.shutil, "which", lambda tool: which_result)
    return agent, mod


def test_tailscale_peers_parses_self_and_peers(monkeypatch):
    """Self plus every Peer entry is harvested; only the IPv4 address is kept."""
    from unittest.mock import MagicMock
    from types import SimpleNamespace

    agent, mod = _make_ts_agent(monkeypatch)
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        MagicMock(
            return_value=SimpleNamespace(returncode=0, stdout=_TAILSCALE_STATUS_JSON, stderr="")
        ),
    )

    peers = agent._tailscale_peers()

    assert peers["203.0.113.19"] == "ai_node"
    assert peers["203.0.113.11"] == "amd_node"
    assert peers["203.0.113.16"] == "hermes-sandbox"
    # IPv6-only peer has no IPv4 address to key on — skipped entirely.
    assert "ipv6-only-peer" not in peers.values()
    assert len(peers) == 3


def test_tailscale_peers_returns_empty_when_binary_absent(monkeypatch):
    """No tailscale on PATH — degrade to {} without touching subprocess."""
    from unittest.mock import MagicMock

    agent, mod = _make_ts_agent(monkeypatch, which_result=None)
    run_mock = MagicMock()
    monkeypatch.setattr(mod.subprocess, "run", run_mock)

    assert agent._tailscale_peers() == {}
    run_mock.assert_not_called()


def test_tailscale_peers_returns_empty_on_nonzero_exit(monkeypatch):
    """tailscaled not running / daemon error — degrade to {}, never raise."""
    from unittest.mock import MagicMock
    from types import SimpleNamespace

    agent, mod = _make_ts_agent(monkeypatch)
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        MagicMock(return_value=SimpleNamespace(returncode=1, stdout="", stderr="not running")),
    )

    assert agent._tailscale_peers() == {}


def test_tailscale_peers_returns_empty_on_malformed_json(monkeypatch):
    """Unparseable stdout degrades to {} rather than raising."""
    from unittest.mock import MagicMock
    from types import SimpleNamespace

    agent, mod = _make_ts_agent(monkeypatch)
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        MagicMock(return_value=SimpleNamespace(returncode=0, stdout="not json{", stderr="")),
    )

    assert agent._tailscale_peers() == {}


def test_tailscale_peers_returns_empty_on_timeout(monkeypatch):
    """A hung tailscaled degrades to {} rather than propagating TimeoutExpired."""
    import subprocess as real_subprocess
    from unittest.mock import MagicMock

    agent, mod = _make_ts_agent(monkeypatch)
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        MagicMock(side_effect=real_subprocess.TimeoutExpired(cmd="tailscale", timeout=10)),
    )

    assert agent._tailscale_peers() == {}


def test_tailscale_status_invocation_is_audited(monkeypatch):
    """Every tailscale status call writes a verdict=allow audit row (Item 1.5)."""
    from unittest.mock import MagicMock
    from types import SimpleNamespace

    agent, mod = _make_ts_agent(monkeypatch)
    audit = agent._audit_subprocess
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        MagicMock(
            return_value=SimpleNamespace(returncode=0, stdout=_TAILSCALE_STATUS_JSON, stderr="")
        ),
    )

    agent._tailscale_peers()

    assert audit.call_args.kwargs["tool"] == "tailscale_status"
    assert audit.call_args.kwargs["verdict"] == "allow"


def test_load_known_ips_includes_tailscale_peers(engine, session_factory):
    """Tailscale mesh peers are sanctioned — _load_known_ips must fold them in
    so they classify as is_known rather than shadow-IT (TRK: Tailscale subnet
    discovery — CGNAT peers can't be inventoried any other way)."""
    agent = _make_agent(_make_settings())

    with (
        patch("infra_brain.agents.netdiscovery.get_session", session_factory),
        patch.object(agent, "_tailscale_peers", return_value={"203.0.113.19": "ai_node"}),
    ):
        known = agent._load_known_ips()

    assert "203.0.113.19" in known
