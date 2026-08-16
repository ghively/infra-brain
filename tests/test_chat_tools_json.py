"""Tests for dual-output format and new tools in infra_brain.chat.tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

from tests.support.pg import make_engine


def test_query_resources_json_format():
    """query_resources with format='json' returns a valid JSON list."""
    rows = [
        {
            "hostname": "srv01",
            "domain": "linux",
            "resource_type": "host",
            "zone": "corp",
            "last_seen_at": "2026-06-01",
        }
    ]
    with patch("infra_brain.chat.tools._rows", return_value=rows):
        from infra_brain.chat.tools import query_resources

        result = query_resources.invoke({"domain": "linux", "format": "json"})
    data = json.loads(result)
    assert isinstance(data, list)
    assert data[0]["hostname"] == "srv01"


def test_query_resources_text_unchanged():
    """query_resources without format param returns a plain text table (not JSON)."""
    rows = [
        {
            "hostname": "srv01",
            "domain": "linux",
            "resource_type": "host",
            "zone": "corp",
            "last_seen_at": "2026-06-01",
        }
    ]
    with patch("infra_brain.chat.tools._rows", return_value=rows):
        from infra_brain.chat.tools import query_resources

        result = query_resources.invoke({"domain": "linux"})
    assert isinstance(result, str)
    assert not result.startswith("[")  # not JSON


def test_query_resources_diversifies_across_domains_when_unfiltered():
    """GitLab #206: with no domain filter, a single hyperactive domain (e.g.
    netdiscovery re-sweeping every 15 min) must not monopolize every slot in
    the result just because its rows have the most recent last_seen values.
    Exercises the real raw SQL against an in-memory sqlite engine (not a
    mocked _rows) so the fix is actually proven, not merely asserted.
    """
    import uuid
    from sqlalchemy.orm import Session

    from infra_brain.db.models import Resource

    engine = make_engine()

    with Session(engine) as s:
        # netdiscovery: 10 rows, all more recently seen than anything else.
        for i in range(10):
            s.add(
                Resource(
                    id=uuid.uuid4(),
                    domain="netdiscovery",
                    type="host",
                    name=f"nd-host-{i}",
                    source="test",
                    zone="lan",
                    last_seen=datetime(2026, 8, 2, 12, i, tzinfo=UTC),
                )
            )
        # Three other domains, one older row each.
        for i, domain in enumerate(["linux", "windows", "vsphere"]):
            s.add(
                Resource(
                    id=uuid.uuid4(),
                    domain=domain,
                    type="host",
                    name=f"{domain}-host",
                    source="test",
                    zone="lan",
                    last_seen=datetime(2026, 8, 1, 0, i, tzinfo=UTC),
                )
            )
        s.commit()

    with patch("infra_brain.chat.tools.get_engine", return_value=engine):
        from infra_brain.chat.tools import query_resources

        result = query_resources.invoke({"limit": 4, "format": "json"})

    rows = json.loads(result)
    assert len(rows) == 4
    domains_seen = {r["domain"] for r in rows}
    # Before the fix this was {"netdiscovery"} — 4/4 rows from one domain.
    assert domains_seen == {"netdiscovery", "linux", "windows", "vsphere"}


def test_query_resources_domain_filter_behaviour_unchanged():
    """With an explicit domain filter, ordering is still plain last_seen DESC
    -- the diversification logic is a no-op for a single-domain result set."""
    import uuid
    from sqlalchemy.orm import Session

    from infra_brain.db.models import Resource

    engine = make_engine()

    with Session(engine) as s:
        for i in range(3):
            s.add(
                Resource(
                    id=uuid.uuid4(),
                    domain="linux",
                    type="host",
                    name=f"linux-host-{i}",
                    source="test",
                    zone="lan",
                    last_seen=datetime(2026, 8, 2, 12, i, tzinfo=UTC),
                )
            )
        s.commit()

    with patch("infra_brain.chat.tools.get_engine", return_value=engine):
        from infra_brain.chat.tools import query_resources

        result = query_resources.invoke({"domain": "linux", "limit": 3, "format": "json"})

    rows = json.loads(result)
    assert [r["hostname"] for r in rows] == ["linux-host-2", "linux-host-1", "linux-host-0"]


def test_query_drift_events_json_format():
    """query_drift_events with format='json' returns a valid JSON list."""
    rows = [
        {
            "id": "abc",
            "domain": "linux",
            "hostname": "srv01",
            "field_name": "kernel_version",
            "old_value": "5.4",
            "new_value": "5.15",
            "detected_at": "2026-06-20",
            "status": "open",
            "jira_ticket": None,
        }
    ]
    with patch("infra_brain.chat.tools._rows", return_value=rows):
        from infra_brain.chat.tools import query_drift_events

        result = query_drift_events.invoke({"format": "json"})
    data = json.loads(result)
    assert isinstance(data, list)
    assert data[0]["domain"] == "linux"


def test_query_collection_runs_json_format():
    """query_collection_runs with format='json' returns a valid JSON list."""
    rows = [
        {
            "domain": "linux",
            "trigger_type": "scheduled",
            "status": "success",
            "records_collected": 10,
            "started_at": "2026-06-20",
            "duration_seconds": 45.0,
        }
    ]
    with patch("infra_brain.chat.tools._rows", return_value=rows):
        from infra_brain.chat.tools import query_collection_runs

        result = query_collection_runs.invoke({"format": "json"})
    data = json.loads(result)
    assert isinstance(data, list)
    assert data[0]["domain"] == "linux"


def test_query_fleet_health_json_format():
    """query_fleet_health with format='json' returns a valid JSON dict."""
    rows = [
        {"open_drift": 3, "open_cves": 1, "eol_resources": 2, "last_successful_run": "2026-06-21"}
    ]
    with patch("infra_brain.chat.tools._rows", return_value=rows):
        from infra_brain.chat.tools import query_fleet_health

        result = query_fleet_health.invoke({"format": "json"})
    data = json.loads(result)
    assert isinstance(data, (list, dict))
    if isinstance(data, dict):
        assert "open_drift" in data
    else:
        assert "open_drift" in data[0]


def test_query_linux_detail_hostname_not_found():
    """query_linux_detail returns an error string when the host does not exist."""
    with patch("infra_brain.chat.tools._rows", return_value=[]):
        from infra_brain.chat.tools import query_linux_detail

        result = query_linux_detail.invoke({"hostname": "nonexistent-host"})
    assert "not found" in result.lower() or "error" in result.lower()


def test_query_drift_trend_returns_data():
    """query_drift_trend returns a non-empty string."""
    rows = [{"date": "2026-06-20", "domain": "linux", "count": 5}]
    with patch("infra_brain.chat.tools._rows", return_value=rows):
        from infra_brain.chat.tools import query_drift_trend

        result = query_drift_trend.invoke({"days": 30})
    assert isinstance(result, str)
    assert len(result) > 0
    assert "linux" in result


# ---------------------------------------------------------------------------
# Phase 3 Task 1: query_compliance / query_resource_neighborhood
# ---------------------------------------------------------------------------


def test_query_compliance_returns_rows():
    rows = [
        {
            "rule": "no-root-ssh",
            "severity": "high",
            "host": "web-01",
            "detail": "PermitRootLogin yes",
            "status": "open",
            "detected_at": "2026-06-20",
        }
    ]
    with patch("infra_brain.chat.tools._rows", return_value=rows) as mock_rows:
        from infra_brain.chat.tools import query_compliance

        result = query_compliance.invoke({"host": "web-01"})
    assert "no-root-ssh" in result
    assert "web-01" in result
    # host filter reached the SQL params
    _, params = mock_rows.call_args[0]
    assert params["host"] == "web-01"


def test_query_compliance_no_rows():
    with patch("infra_brain.chat.tools._rows", return_value=[]):
        from infra_brain.chat.tools import query_compliance

        result = query_compliance.invoke({})
    assert "no compliance violations" in result.lower()


def test_query_compliance_defaults_status_open():
    """Default status filter is 'open' unless explicitly overridden."""
    with patch("infra_brain.chat.tools._rows", return_value=[]) as mock_rows:
        from infra_brain.chat.tools import query_compliance

        query_compliance.invoke({})
    _, params = mock_rows.call_args[0]
    assert params["status"] == "open"


# ---------------------------------------------------------------------------
# query_resource_neighborhood — graph-first P5
#
# These used to patch ``chat.tools.get_neighborhood`` and assert on the shape
# of a mocked legacy-store dict. That function is gone, and mocking its
# replacement would prove nothing about whether the tool reads the right table.
# The behavioural coverage moved to tests/test_chat_graph_kg_only.py, which
# seeds BOTH stores with contradictory data and pins "zero reads of
# resource_relationships" with a statement counter.
#
# What stays here is the part that belongs with the other chat-tool shape
# tests: the miss path and the honesty of the tool's own docstring, which is
# the only thing the LLM ever sees about what the graph can and cannot answer.
# ---------------------------------------------------------------------------


def test_query_resource_neighborhood_unknown_entity_is_a_clear_miss():
    from infra_brain.chat.tools import query_resource_neighborhood

    with patch("infra_brain.chat.tools.kg_neighborhood_payload") as payload:
        payload.return_value = {
            "found": False,
            "reason": "'ghost-01' not found in the knowledge graph",
        }
        result = query_resource_neighborhood.invoke({"resource_name": "ghost-01"})
    assert "not found" in result.lower()


def test_query_resource_neighborhood_clamps_depth_and_limit():
    """An LLM tool call has no 422 channel, so out-of-range args are clamped
    rather than rejected — but they must actually BE clamped, not passed
    through into an unbounded walk."""
    from infra_brain.chat import tools as chat_tools

    captured = {}

    def _fake_resolve(session, ident, limit=10):
        captured["ident"] = ident
        return []

    with (
        patch.object(chat_tools.graph_kg, "resolve_roots", _fake_resolve),
        patch("infra_brain.chat.tools.get_session"),
    ):
        out = chat_tools.kg_neighborhood_payload("anything", depth=99, limit=9999)
    assert out["found"] is False
    assert captured["ident"] == "anything"


def test_query_resource_neighborhood_docstring_states_the_containment_gap():
    """The KG store holds no containment edges. If the tool's description does
    not say so, the model will read an empty edge list as "nothing runs here" —
    which is the single most likely wrong answer this re-point can produce."""
    from infra_brain.chat.tools import query_resource_neighborhood

    doc = (query_resource_neighborhood.description or "").lower()
    assert "containment" in doc
    assert "query_linux_detail" in doc


def test_bound_graph_tool_docstring_states_the_containment_gap():
    from infra_brain.chat.agent import query_graph_neighborhood

    doc = (query_graph_neighborhood.description or "").lower()
    assert "containment" in doc
    assert "query_linux_detail" in doc


# ---------------------------------------------------------------------------
# #81: query_vulnerabilities / query_eol_status chat tools
# ---------------------------------------------------------------------------


def test_query_vulnerabilities_returns_rows():
    rows = [
        {
            "cve_id": "CVE-2026-1234",
            "host": "web-01",
            "severity": "critical",
            "sla_due": "2026-08-01",
            "status": "open",
        }
    ]
    with patch("infra_brain.chat.tools._rows", return_value=rows) as mock_rows:
        from infra_brain.chat.tools import query_vulnerabilities

        result = query_vulnerabilities.invoke({"host": "web-01"})
    assert "CVE-2026-1234" in result
    assert "web-01" in result
    _, params = mock_rows.call_args[0]
    assert params["host"] == "web-01"


def test_query_vulnerabilities_no_rows():
    with patch("infra_brain.chat.tools._rows", return_value=[]):
        from infra_brain.chat.tools import query_vulnerabilities

        result = query_vulnerabilities.invoke({})
    assert "no vulnerabilities" in result.lower()


def test_query_vulnerabilities_defaults_status_open():
    """Default status filter is 'open' unless explicitly overridden."""
    with patch("infra_brain.chat.tools._rows", return_value=[]) as mock_rows:
        from infra_brain.chat.tools import query_vulnerabilities

        query_vulnerabilities.invoke({})
    _, params = mock_rows.call_args[0]
    assert params["status"] == "open"


def test_query_eol_status_returns_rows():
    rows = [
        {
            "asset_name": "legacy-app-01",
            "resource_name": "srv02",
            "eol_date": "2026-09-01",
            "pci_risk_score": 8,
        }
    ]
    with patch("infra_brain.chat.tools._rows", return_value=rows) as mock_rows:
        from infra_brain.chat.tools import query_eol_status

        result = query_eol_status.invoke({"host": "srv02"})
    assert "legacy-app-01" in result
    assert "srv02" in result
    _, params = mock_rows.call_args[0]
    assert params["host"] == "srv02"


def test_query_eol_status_no_rows():
    with patch("infra_brain.chat.tools._rows", return_value=[]):
        from infra_brain.chat.tools import query_eol_status

        result = query_eol_status.invoke({})
    assert "no eol registry entries" in result.lower()


def test_query_eol_status_forwards_days_until_eol():
    with patch("infra_brain.chat.tools._rows", return_value=[]) as mock_rows:
        from infra_brain.chat.tools import query_eol_status

        query_eol_status.invoke({"days_until_eol": 30})
    _, params = mock_rows.call_args[0]
    assert params["cutoff_days"] == 30


# ---------------------------------------------------------------------------
# TRK-067: search_knowledge RAG chat tool
# ---------------------------------------------------------------------------


_KB_HITS = [
    {
        "title": "Backup Runbook",
        "space": "OPS",
        "url": "https://wiki.example.com/OPS/backup-runbook",
        "chunk_index": 0,
        "text": "To restore from the nightly backup, run the restore script on the standby node.",
        "source": "confluence",
    }
]


def test_search_knowledge_text_format():
    """Text output contains the page title and URL of each hit."""
    with patch("infra_brain.embeddings.search_knowledge", return_value=_KB_HITS):
        from infra_brain.chat.tools import search_knowledge

        result = search_knowledge.invoke({"query": "backup runbook", "limit": 3})
    assert isinstance(result, str)
    assert "Backup Runbook" in result
    assert "https://wiki.example.com/OPS/backup-runbook" in result


def test_search_knowledge_json_format():
    """format='json' returns JSON parseable back to the hit list."""
    with patch("infra_brain.embeddings.search_knowledge", return_value=_KB_HITS):
        from infra_brain.chat.tools import search_knowledge

        result = search_knowledge.invoke({"query": "backup runbook", "limit": 3, "format": "json"})
    data = json.loads(result)
    assert isinstance(data, list)
    assert data[0]["title"] == "Backup Runbook"


def test_search_knowledge_empty_or_disabled():
    """Empty result (RAG disabled / nothing indexed) returns the note; no DB needed."""
    with patch("infra_brain.embeddings.search_knowledge", return_value=[]):
        from infra_brain.chat.tools import search_knowledge

        result = search_knowledge.invoke({"query": "anything"})
    assert "no knowledge-base results" in result.lower()


# ---------------------------------------------------------------------------
# TRK-122 R2: numbered, citable grounding-pack format
# ---------------------------------------------------------------------------


def test_search_knowledge_numbered_citable_format_with_similarity():
    """Each hit gets a [N] header with title + similarity, plus a citation nudge."""
    hits = [
        {
            "title": "Backup Runbook",
            "space": "OPS",
            "url": "https://wiki.example.com/OPS/backup-runbook",
            "chunk_index": 0,
            "text": "Restore from the nightly backup on the standby node.",
            "source": "confluence",
            "similarity": 0.8123,
        }
    ]
    with patch("infra_brain.embeddings.search_knowledge", return_value=hits):
        from infra_brain.chat.tools import search_knowledge

        result = search_knowledge.invoke({"query": "restore backup"})
    assert "[1]" in result
    assert '"Backup Runbook"' in result
    assert "similarity 0.8123" in result
    assert "[Source 1:" in result  # citation instruction present
    assert "say so rather than guessing" in result


def test_search_knowledge_does_not_truncate_chunk_text():
    """TRK-122 R2(a): the old [:400] slice is gone — full chunk text is returned."""
    long_text = "SENTINEL " + ("x " * 400) + "TAILMARKER"  # ~1200 chars, > old 400 cap
    hits = [
        {
            "title": "Long Doc",
            "space": "docs",
            "url": "",
            "chunk_index": 0,
            "text": long_text,
            "source": "local_docs",
            "similarity": 0.5,
        }
    ]
    with patch("infra_brain.embeddings.search_knowledge", return_value=hits):
        from infra_brain.chat.tools import search_knowledge

        result = search_knowledge.invoke({"query": "long"})
    assert "SENTINEL" in result
    assert "TAILMARKER" in result  # end of a >400-char chunk survives


# ---------------------------------------------------------------------------
# R-20 gate regression coverage (TRK-027, fixed at llm_base.py:98-105 /
# callbacks/boundary.py's enforce_tool_gate): the DLP/read-only gate must scan
# BOTH positional and keyword args for every tool call, including the two new
# tools added in this task. These drive enforce_tool_gate directly (the real
# fail-closed call site) rather than re-testing DLP internals.
# ---------------------------------------------------------------------------


def test_gate_scans_positional_args_for_query_compliance():
    from infra_brain.callbacks.boundary import enforce_tool_gate

    with (
        patch("infra_brain.callbacks.readonly.get_settings") as ro_settings,
        patch("infra_brain.callbacks.dlp.get_settings") as dlp_settings,
    ):
        ro_settings.return_value.scan_readonly_enforce = False
        dlp_settings.return_value.dlp_fail_closed = False
        # Positional-only args (no kwargs) must still be scanned/forwarded —
        # the pre-R-20 bug dropped positional args whenever kwargs were absent
        # too (``kwargs or {"args": args}``), collapsing to an empty dict.
        enforce_tool_gate(
            "query_compliance", {"args": ["web-01", "no-root-ssh"], "kwargs": {}}, agent_name="test"
        )


def test_gate_scans_positional_args_for_query_resource_neighborhood():
    from infra_brain.callbacks.boundary import enforce_tool_gate

    with (
        patch("infra_brain.callbacks.readonly.get_settings") as ro_settings,
        patch("infra_brain.callbacks.dlp.get_settings") as dlp_settings,
    ):
        ro_settings.return_value.scan_readonly_enforce = False
        dlp_settings.return_value.dlp_fail_closed = False
        enforce_tool_gate(
            "query_resource_neighborhood",
            {"args": ["web-01", 2], "kwargs": {}},
            agent_name="test",
        )
