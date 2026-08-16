"""Wave 1.5a — MCP lockdown guards (F-025)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

import infra_brain.mcp_server as mcp_server

_REPO_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE_FILES = (
    _REPO_ROOT / "docker" / "docker-compose.yml",
    _REPO_ROOT / "docker" / "docker-compose.dev.yml",
)


def test_mutating_tool_disabled_without_flag(monkeypatch):
    """seed_resource must refuse when INFRA_BRAIN_MCP_ENABLE_MUTATIONS is unset."""
    monkeypatch.delenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", raising=False)
    result = mcp_server.seed_resource(hostname="h1", domain="linux")
    assert "error" in result
    assert "disabled" in result["error"]


def test_mutating_tool_enabled_with_flag(monkeypatch):
    """With the flag set, the guard passes and the tool proceeds far enough that the
    disabled-error is NOT returned (it may still error on the DB, which is fine here)."""
    monkeypatch.setenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", "1")
    # trigger_collection is the cheapest to drive: it only makes an httpx call.
    # We assert it is not blocked by the mutation gate.
    result = mcp_server.trigger_collection(domain="cicd")
    assert not (
        isinstance(result, dict)
        and result.get("error", "").startswith("mutating MCP tools are disabled")
    )


def test_query_tool_not_gated():
    """_mutations_enabled must not affect the presence of read-only query tools."""
    assert hasattr(mcp_server, "query_resources")
    assert hasattr(mcp_server, "get_collection_health")


def test_rce_tools_removed():
    """deploy_agent / set_secret / update_config / get_agent_logs must be gone."""
    for name in ("deploy_agent", "set_secret", "update_config", "get_agent_logs"):
        assert not hasattr(mcp_server, name), f"{name} must be removed (RCE surface)"


def test_mcp_port_binding_is_off_loopback_for_lan_reach():
    """Auth-overhaul cutover: the MCP port is reachable across LAN subnets, so it
    must NOT be pinned to 127.0.0.1 anymore. Access control is now per-key auth
    (mcp_auth), not loopback binding. Assert the base compose publishes container
    port 8002 on a non-loopback mapping."""
    config = yaml.safe_load(_COMPOSE_FILES[0].read_text())
    ports = config.get("services", {}).get("mcp", {}).get("ports", [])
    matched = False
    for entry in ports:
        mapping = entry if isinstance(entry, str) else str(entry)
        if mapping.rsplit(":", 1)[-1] != "8002":
            continue
        matched = True
        assert not mapping.startswith("127.0.0.1:"), (
            f"MCP port mapping '{mapping}' is still loopback-only; the auth "
            f"overhaul makes it LAN-reachable via per-key auth (spec Part 1)."
        )
    assert matched, "no MCP port mapping for container port 8002 found"


def test_global_mcp_token_env_removed_from_compose():
    """INFRA_BRAIN_MCP_TOKEN is replaced entirely (not kept as a fallback)."""
    config = yaml.safe_load(_COMPOSE_FILES[0].read_text())
    env = config.get("services", {}).get("mcp", {}).get("environment", {}) or {}
    keys = env.keys() if isinstance(env, dict) else [e.split("=", 1)[0] for e in env]
    assert "INFRA_BRAIN_MCP_TOKEN" not in keys


# ── TRK-136: promote_instinct server-side validation ──────────────────────────
# The mutation gate (INFRA_BRAIN_MCP_ENABLE_MUTATIONS) is the primary guard and
# is exercised above. These tests cover the additional, independent validation
# layer added on top of it: non-empty citation and a bounded confidence — all
# enforced BEFORE any Instinct row is constructed. ``approved_by`` is no
# longer server-side-validated as a required non-empty string because it is
# no longer caller-controlled at all — see the unforgeable-attribution tests
# below.


def _mock_session():
    """A minimal mock DB session usable as a context manager for get_session()."""
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    return session


def test_promote_instinct_rejects_empty_citation(monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", "1")
    result = mcp_server.promote_instinct(
        pattern="p", domain="linux", citation="   ", approved_by="operator"
    )
    assert "error" in result
    assert "citation" in result["error"]


def test_promote_instinct_attribution_is_server_derived_not_caller_supplied(monkeypatch):
    """Regression test: promote_instinct must NEVER write the raw
    ``approved_by`` argument straight to ``Instinct.promoted_by`` — that was
    the unforgeable-attribution bug. A caller passing an arbitrary identity
    string (impersonating another human/agent) must have that value appear
    only as a quoted ``(says: ...)`` label, never as the identity prefix
    itself, exactly like ``approve_proposal``/``promote_instinct_v2``/
    ``record_environment_note`` etc. already behave elsewhere in this file.
    """
    monkeypatch.setenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", "1")
    session = _mock_session()
    with (
        patch("infra_brain.mcp_server.get_session", return_value=session),
        patch(
            "infra_brain.mcp_server._caller_identity",
            return_value="mcp:real-key-name",
        ),
    ):
        result = mcp_server.promote_instinct(
            pattern="p",
            domain="linux",
            citation="doc-123",
            approved_by="totally-not-operator",
            confidence=0.9,
        )
    assert "error" not in result
    assert session.add.call_count == 1
    instinct = session.add.call_args_list[0].args[0]
    # The server-derived identity must lead, and the caller-supplied string
    # must appear only as a quoted claim appended after it -- never as (or
    # in place of) the identity itself.
    assert instinct.promoted_by.startswith("mcp:real-key-name")
    assert "totally-not-operator" not in instinct.promoted_by.split("(says:")[0]


def test_promote_instinct_accepts_missing_approved_by(monkeypatch):
    """approved_by is now an optional label, not a required identity string —
    omitting it must not error, since attribution comes from the
    authenticated key regardless."""
    monkeypatch.setenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", "1")
    session = _mock_session()
    with patch("infra_brain.mcp_server.get_session", return_value=session):
        result = mcp_server.promote_instinct(pattern="p", domain="linux", citation="doc-123")
    assert "error" not in result
    instinct = session.add.call_args_list[0].args[0]
    assert instinct.promoted_by  # non-empty: falls back to the identity sentinel


def test_promote_instinct_rejects_confidence_at_or_below_zero(monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", "1")
    result = mcp_server.promote_instinct(
        pattern="p",
        domain="linux",
        citation="doc-123",
        approved_by="operator",
        confidence=0,
    )
    assert "error" in result
    assert "confidence" in result["error"]


def test_promote_instinct_rejects_confidence_above_one(monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", "1")
    result = mcp_server.promote_instinct(
        pattern="p",
        domain="linux",
        citation="doc-123",
        approved_by="operator",
        confidence=1.5,
    )
    assert "error" in result
    assert "confidence" in result["error"]


def test_promote_instinct_accepts_valid_input_when_mutations_enabled(monkeypatch):
    monkeypatch.setenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", "1")
    session = _mock_session()
    with patch("infra_brain.mcp_server.get_session", return_value=session):
        result = mcp_server.promote_instinct(
            pattern="always patch CVE-X within 48h",
            domain="linux",
            citation="doc-123",
            approved_by="operator",
            confidence=0.9,
        )
    assert "error" not in result
    assert result["domain"] == "linux"
    assert result["confidence"] == 0.9
    assert session.add.call_count == 1
    added_types = {type(c.args[0]).__name__ for c in session.add.call_args_list}
    assert added_types == {"Instinct"}
    assert session.commit.call_count == 1


def test_promote_instinct_still_gated_by_mutation_flag_even_with_valid_input(
    monkeypatch,
):
    """Valid citation/approved_by/confidence must NOT bypass the primary gate."""
    monkeypatch.delenv("INFRA_BRAIN_MCP_ENABLE_MUTATIONS", raising=False)
    result = mcp_server.promote_instinct(
        pattern="p", domain="linux", citation="doc-123", approved_by="operator"
    )
    assert "error" in result
    assert "disabled" in result["error"]
