"""Batch J MCP tools — internal governance read-only query tools (issue #54)."""

import contextlib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from infra_brain import mcp_server
from infra_brain.db.models import (
    AgentActionLog,
    AgentConfigSetting,
    AgentDecisionLog,
    AuditLog,
    DriftEvent,
    Resource,
)

from tests.support.pg import make_engine


@pytest.fixture
def engine():
    eng = make_engine()
    return eng


@pytest.fixture
def patched_session(engine):
    @contextlib.contextmanager
    def _get_session():
        with Session(engine) as s:
            yield s

    with patch("infra_brain.mcp_server.get_session", _get_session):
        yield engine


def _seed(engine, *objs):
    with Session(engine) as s:
        s.add_all(objs)
        s.commit()


def test_get_audit_log_success(patched_session):
    _seed(
        patched_session,
        AuditLog(agent="linux", tool="ssh_facts", input_hash="a" * 64, allowed=True),
        AuditLog(
            agent="cicd",
            tool="gitlab_api",
            input_hash="b" * 64,
            allowed=False,
            denial_reason="boundary",
        ),
    )
    rows = mcp_server.get_audit_log()
    assert len(rows) == 2
    assert {r["agent"] for r in rows} == {"linux", "cicd"}
    # only hashes are exposed, never raw payloads.
    assert rows[0]["input_hash"] and "output_hash" in rows[0]


def test_get_audit_log_denied_filter(patched_session):
    _seed(
        patched_session,
        AuditLog(agent="linux", tool="ssh_facts", input_hash="a" * 64, allowed=True),
        AuditLog(
            agent="cicd",
            tool="gitlab_api",
            input_hash="b" * 64,
            allowed=False,
            denial_reason="boundary",
        ),
    )
    rows = mcp_server.get_audit_log(allowed=False)
    assert [r["agent"] for r in rows] == ["cicd"]
    assert rows[0]["denial_reason"] == "boundary"


def test_get_audit_log_empty(patched_session):
    assert mcp_server.get_audit_log() == []


def test_get_agent_activity_success(patched_session):
    _seed(
        patched_session,
        AgentActionLog(
            agent="linux", tool="ssh_facts", verdict="allow", status="ok", latency_ms=12.5
        ),
        AgentActionLog(
            agent="cicd", tool="gitlab_api", verdict="deny", status="error", error="blocked"
        ),
    )
    rows = mcp_server.get_agent_activity()
    assert len(rows) == 2
    assert {r["verdict"] for r in rows} == {"allow", "deny"}


def test_get_agent_activity_verdict_filter(patched_session):
    _seed(
        patched_session,
        AgentActionLog(agent="linux", tool="ssh_facts", verdict="allow", status="ok"),
        AgentActionLog(agent="cicd", tool="gitlab_api", verdict="deny", status="error"),
    )
    rows = mcp_server.get_agent_activity(verdict="deny")
    assert [r["agent"] for r in rows] == ["cicd"]


def test_get_agent_activity_empty(patched_session):
    assert mcp_server.get_agent_activity() == []


def test_get_agent_decisions_success(patched_session):
    _seed(
        patched_session,
        AgentDecisionLog(
            agent="rootcause",
            domain="linux",
            iteration=0,
            reasoning_text="considered pkg drift",
            tools_chosen=["get_drift"],
            decision_summary="open ticket",
        ),
    )
    rows = mcp_server.get_agent_decisions()
    assert len(rows) == 1
    assert rows[0]["agent"] == "rootcause"
    assert rows[0]["tools_chosen"] == ["get_drift"]


def test_get_agent_decisions_agent_filter(patched_session):
    _seed(
        patched_session,
        AgentDecisionLog(agent="rootcause", domain="linux", iteration=0, decision_summary="a"),
        AgentDecisionLog(agent="compliance", domain="cicd", iteration=0, decision_summary="b"),
    )
    rows = mcp_server.get_agent_decisions(agent="compliance")
    assert [r["agent"] for r in rows] == ["compliance"]


def test_get_agent_decisions_empty(patched_session):
    assert mcp_server.get_agent_decisions() == []


def test_get_agent_config_status_masks_secrets(patched_session):
    _seed(
        patched_session,
        AgentConfigSetting(key="rootcause_llm_enabled", value="true"),
        AgentConfigSetting(key="gitlab_api_token", value="glpat-abcd1234"),
    )
    rows = mcp_server.get_agent_config_status()
    by_key = {r["key"]: r["value"] for r in rows}
    assert by_key["rootcause_llm_enabled"] == "true"
    # a key containing "token" must be masked, never returned raw.
    assert "glpat-abcd1234" not in by_key["gitlab_api_token"]
    assert by_key["gitlab_api_token"].endswith("1234")


def test_get_agent_config_status_empty(patched_session):
    assert mcp_server.get_agent_config_status() == []


def test_get_settings_masks_secret_fields(patched_session):
    class _FakeSettings:
        def model_dump(self):
            return {
                "llm_provider": "anthropic",
                "anthropic_api_key": "sk-secret1234",
                "environment": "development",
            }

    with patch("infra_brain.config.get_settings", return_value=_FakeSettings()):
        out = mcp_server.get_settings()
    assert out["llm_provider"] == "anthropic"
    assert out["environment"] == "development"
    # "anthropic_api_key" contains "key" -> masked, never raw.
    assert "sk-secret1234" not in out["anthropic_api_key"]
    assert out["anthropic_api_key"].endswith("1234")


def test_get_settings_masks_driver_suffixed_dsn(patched_session):
    """A DSN whose scheme carries a SQLAlchemy driver suffix (postgresql+asyncpg://,
    postgresql+psycopg2://, postgresql+psycopg://) must still hit the DSN-fallback
    masking branch — the field name (postgres_url) doesn't hint at a secret, so
    only the DSN regex catches it. Plain `\\w` in that regex excluded `+` and
    leaked these raw credential-bearing DSNs over MCP."""
    raw_pw = "supersecretpw"
    for scheme in (
        "postgresql+asyncpg",
        "postgresql+psycopg2",
        "postgresql+psycopg",
        "postgresql",
    ):
        dsn = f"{scheme}://svc:{raw_pw}@db.internal:5432/infra"

        class _FakeSettings:
            def model_dump(self):
                return {"postgres_url": dsn, "environment": "development"}

        with patch("infra_brain.config.get_settings", return_value=_FakeSettings()):
            out = mcp_server.get_settings()
        assert raw_pw not in out["postgres_url"], f"{scheme} leaked raw credential"
        assert out["postgres_url"] != dsn, f"{scheme} was not masked at all"


def test_get_settings_returns_dict(patched_session):
    class _FakeSettings:
        def model_dump(self):
            return {"llm_provider": "anthropic"}

    with patch("infra_brain.config.get_settings", return_value=_FakeSettings()):
        out = mcp_server.get_settings()
    assert isinstance(out, dict)
    assert out["llm_provider"] == "anthropic"


# ── get_tool_catalog (GitLab #197 machine-readable allowed-actions catalog) ──


def test_get_tool_catalog_returns_full_shape():
    from infra_brain.mcp_auth import MUTATION_TOOL_NAMES, READONLY_TOOL_NAMES, TOOL_GROUPS

    out = mcp_server.get_tool_catalog()

    assert set(out) == {
        "version",
        "readonly_tools",
        "mutation_tools",
        "tool_groups",
        "mutations_globally_enabled",
    }
    assert out["readonly_tools"] == list(READONLY_TOOL_NAMES)
    assert out["mutation_tools"] == list(MUTATION_TOOL_NAMES)
    assert out["tool_groups"] == TOOL_GROUPS
    assert isinstance(out["mutations_globally_enabled"], bool)
    # The catalog tool names itself -- it must be listed among its own
    # read-only tools and grouped, or the catalog would omit its own existence.
    assert "get_tool_catalog" in out["readonly_tools"]
    assert "get_tool_catalog" in out["tool_groups"]["Internal governance"]


def test_get_tool_catalog_version_is_stable_and_change_sensitive():
    """version is a deterministic content hash: identical on repeated calls,
    and changes if the underlying catalog data changes (#197 -- so a headless
    consumer can detect a policy drift without diffing the whole payload)."""
    from infra_brain.mcp_auth import catalog_version

    v1 = mcp_server.get_tool_catalog()["version"]
    v2 = mcp_server.get_tool_catalog()["version"]
    assert v1 == v2
    assert v1 == catalog_version()

    with patch("infra_brain.mcp_auth.READONLY_TOOL_NAMES", ["only_one_tool"]):
        changed = catalog_version()
    assert changed != v1


# ── get_recent_changes (GitLab #131 correlation slice) ───────────────────────


def _seed_resource(session_engine, name: str, domain: str = "linux") -> "Resource":
    with Session(session_engine) as s:
        r = Resource(domain=domain, type="host", name=name, source="LinuxAgent")
        s.add(r)
        s.commit()
        s.refresh(r)
        return r


def test_get_recent_changes_with_genuine_recent_activity(patched_session):
    r = _seed_resource(patched_session, "web-01")
    with Session(patched_session) as s:
        s.add(
            DriftEvent(resource_id=r.id, drift_type="config", field="firewall.rule", status="open")
        )
        s.add(
            AgentActionLog(
                agent="linux",
                domain="linux",
                tool="ssh_facts",
                args_summary="host=web-01 collecting facts",
                verdict="allow",
                status="ok",
            )
        )
        s.commit()

    result = mcp_server.get_recent_changes("web-01")

    assert result["resource"] == "web-01"
    assert result["counts"]["drift_events"] == 1
    assert result["counts"]["agent_activity"] == 1
    assert result["drift_events"][0]["resource_name"] == "web-01"
    assert result["agent_activity"][0]["args_summary"] == "host=web-01 collecting facts"


def test_get_recent_changes_no_activity_in_window(patched_session):
    r = _seed_resource(patched_session, "quiet-host")
    stale = datetime.now(timezone.utc) - timedelta(hours=48)
    with Session(patched_session) as s:
        s.add(
            DriftEvent(
                resource_id=r.id,
                drift_type="config",
                field="old-field",
                status="open",
                detected_at=stale,
            )
        )
        s.add(
            AgentActionLog(
                agent="linux",
                domain="linux",
                tool="ssh_facts",
                args_summary="host=quiet-host collecting facts",
                verdict="allow",
                status="ok",
                ts=stale,
            )
        )
        s.commit()

    result = mcp_server.get_recent_changes("quiet-host", hours=24)

    assert result["counts"]["drift_events"] == 0
    assert result["counts"]["agent_activity"] == 0
    assert result["drift_events"] == []
    assert result["agent_activity"] == []


def test_get_recent_changes_excludes_graph_maintenance_by_default(patched_session):
    fleet = _seed_resource(patched_session, "graph-affinity-host", domain="linux")
    with Session(patched_session) as s:
        gm = Resource(
            domain="graph_maintenance",
            type="graph_maintenance_report",
            name="graph-affinity-host-health",
            source="GraphMaintenanceAgent",
        )
        s.add(gm)
        s.flush()
        s.add(
            DriftEvent(resource_id=fleet.id, drift_type="config", field="real-field", status="open")
        )
        s.add(
            DriftEvent(resource_id=gm.id, drift_type="config", field="timings.prune", status="open")
        )
        s.commit()

    default_result = mcp_server.get_recent_changes("graph-affinity-host")
    assert default_result["counts"]["drift_events"] == 1
    assert default_result["drift_events"][0]["resource_domain"] == "linux"

    included_result = mcp_server.get_recent_changes(
        "graph-affinity-host", include_graph_maintenance=True
    )
    assert included_result["counts"]["drift_events"] == 2
    assert {d["resource_domain"] for d in included_result["drift_events"]} == {
        "linux",
        "graph_maintenance",
    }
