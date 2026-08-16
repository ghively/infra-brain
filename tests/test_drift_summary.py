"""Tests for the human-readable drift summariser (``infra_brain.drift_taxonomy``).

The maintainer's complaint that motivated this module, verbatim: *"I don't
understand what drifted or why they are flagged as drift."* The Drift page
rendered ``drift_type`` / ``field`` / ``old_value`` / ``new_value`` raw, so a
row read ``config_drift / agent_name / {'v': 'vmi-example-deploy'} / {'v': 'node_a'}``
— which tells an operator nothing without archaeology.

Every ``drift_type``/payload shape asserted below was inventoried from the two
authoritative sources, not guessed:

1. **The live database** (``drift_events``, 990 rows at time of writing) —
   ``config_drift`` (963), ``new_listening_port`` (26),
   ``potential_secret_in_iac`` (1), across the ``wazuh`` / ``homelab_services``
   / ``cicd`` / ``local_docs`` / ``linux`` / ``iac`` resource domains.
2. **Every writer in ``src/``** that constructs a ``DriftEvent(...)`` — the
   live table only exercises 3 of the 12 types the code can emit, so the
   remaining shapes are read off their writers:

   ===============================  ==========================================
   drift_type                       writer
   ===============================  ==========================================
   config_drift                     agents/drift.py::_detect_for_resources
   new_listening_port               agents/linux.py::_check_port_drift
   new_windows_service              agents/windows.py::_emit_service_drift
   service_stopped                  agents/windows.py::_emit_service_drift
   threat_escalation                agents/netdiscovery.py
   shadow_it_discovered             agents/netdiscovery.py
   dangerous_service_discovered     agents/netdiscovery.py::_upsert_service_item
   suspicious_service_discovered    agents/netdiscovery.py::_upsert_service_item
   potential_secret_in_iac          agents/iac.py
   identity_conflict                agents/host_reconcile.py (2 distinct shapes)
   identity_conflict_suffix_variant agents/host_reconcile.py
   identity_conflict_distinct_object agents/host_reconcile.py
   state_drift                      LEGACY — no writer; bulk-resolved by
                                    agents/drift.py::detect_state_drift
   (arbitrary)                      mcp_server.py::seed_drift_event
   ===============================  ==========================================

The load-bearing guarantees, per the task brief: each real shape produces its
exact sentence; an unknown/weird shape falls back honestly without crashing.
"""

from __future__ import annotations

import pytest

from infra_brain.drift_taxonomy import (
    DRIFT_RULE_EXPLANATIONS,
    describe_drift_rule,
    render_drift_value,
    summarize_drift,
)

# ---------------------------------------------------------------------------
# config_drift — 963/990 live rows. DriftDetector's signature payload shape is
# ``{"v": <scalar>}`` on BOTH sides (see agents/drift.py, which itself relies
# on that shape being unique to it for the TRK-256 auto-resolve check).
# ---------------------------------------------------------------------------


def test_config_drift_scalar_change_reads_as_a_sentence():
    """The single most common live shape: wazuh/agent_name, 424 rows."""
    assert (
        summarize_drift(
            "config_drift",
            "agent_name",
            {"v": "vmi-example-deploy"},
            {"v": "node_a"},
            hostname="sshd: authentication success.",
        )
        == "agent_name changed from vmi-example-deploy to node_a on sshd: authentication success."
    )


def test_config_drift_without_a_hostname_omits_the_trailing_clause():
    assert (
        summarize_drift("config_drift", "rule_level", {"v": 7}, {"v": 3})
        == "rule_level changed from 7 to 3"
    )


def test_config_drift_http_status_is_phrased_as_an_http_response():
    """homelab_services/http_status — a bare "200 -> 525" is meaningless to
    an operator who doesn't know the field is an HTTP status code."""
    assert (
        summarize_drift("config_drift", "http_status", {"v": 200}, {"v": 525}, hostname="outline")
        == "outline started returning HTTP 525 (was HTTP 200)"
    )


def test_config_drift_content_hash_is_abbreviated_not_dumped():
    """local_docs/content_hash — two 64-char hex digests side by side are pure
    noise; what the operator needs is "the file changed"."""
    assert (
        summarize_drift(
            "config_drift",
            "content_hash",
            {"v": "44bf41f70a33089c1b1a2d389f5f97514e744350efccd8fde3f042a0da2c7db4"},
            {"v": "a0f1421db28b2f39acd47c4a9494faa09fefe49ae46d5e60ae455961cc9863c2"},
            hostname="docs/PATTERNS.md",
        )
        == "docs/PATTERNS.md content changed (hash 44bf41f7… → a0f1421d…)"
    )


def test_config_drift_added_key_has_no_old_value():
    """DeepDiff ``dictionary_item_added`` writes old_value={"v": None}."""
    assert (
        summarize_drift("config_drift", "tags.env", {"v": None}, {"v": "prod"}, hostname="web01")
        == "tags.env was added on web01 (now prod)"
    )


def test_config_drift_removed_key_has_no_new_value():
    """DeepDiff ``dictionary_item_removed`` writes new_value={"v": None}."""
    assert (
        summarize_drift("config_drift", "tags.env", {"v": "prod"}, {"v": None}, hostname="web01")
        == "tags.env was removed on web01 (was prod)"
    )


def test_config_drift_with_both_sides_empty_still_says_something_true():
    assert (
        summarize_drift("config_drift", "tags.env", {"v": None}, {"v": None}, hostname="web01")
        == "tags.env changed on web01"
    )


def test_config_drift_long_value_is_truncated_not_dumped():
    """local_docs/title carries full markdown headings (150+ chars live)."""
    long_title = "Session Handoff — " + ("x" * 200)
    out = summarize_drift(
        "config_drift", "title", {"v": "old"}, {"v": long_title}, hostname="docs/HANDOFF.md"
    )
    assert len(out) < 220, out
    assert out.endswith("…") or "…" in out


def test_config_drift_nested_dotted_field_path_is_preserved():
    """``_field_from_path`` renders nested paths as ``a.b[1].c``."""
    assert (
        summarize_drift("config_drift", "nested.list[1].a", {"v": 1}, {"v": 2}, hostname="web01")
        == "nested.list[1].a changed from 1 to 2 on web01"
    )


# ---------------------------------------------------------------------------
# new_listening_port — 26 live rows (linux domain).
# agents/linux.py: old_value=None, new_value={"port": <int>}
# ---------------------------------------------------------------------------


def test_new_listening_port_matches_the_maintainers_own_example():
    assert (
        summarize_drift("new_listening_port", "port", None, {"port": 8443}, hostname="node_a")
        == "node_a started listening on port 8443 (was not listening)"
    )


def test_new_listening_port_from_the_live_row():
    assert (
        summarize_drift("new_listening_port", "port", None, {"port": 34709}, hostname="media_host")
        == "media_host started listening on port 34709 (was not listening)"
    )


def test_new_listening_port_with_a_missing_port_key_does_not_crash():
    out = summarize_drift("new_listening_port", "port", None, {}, hostname="media_host")
    assert isinstance(out, str) and out


# ---------------------------------------------------------------------------
# Windows service drift — agents/windows.py::_emit_service_drift
# ---------------------------------------------------------------------------


def test_new_windows_service():
    assert (
        summarize_drift(
            "new_windows_service",
            "service_name",
            None,
            {"service_name": "Spooler"},
            hostname="win-app-01",
        )
        == "new Windows service Spooler appeared on win-app-01 (was not installed)"
    )


def test_service_stopped_reads_the_state_pair_and_the_name_out_of_the_field():
    """The service name lives in ``field`` as ``service:<name>``, not in the
    payload — the payload carries only the state pair."""
    assert (
        summarize_drift(
            "service_stopped",
            "service:Spooler",
            {"state": "Running"},
            {"state": "Stopped"},
            hostname="win-app-01",
        )
        == "Windows service Spooler is no longer running on win-app-01 (Running → Stopped)"
    )


# ---------------------------------------------------------------------------
# netdiscovery — agents/netdiscovery.py
# ---------------------------------------------------------------------------


def test_threat_escalation():
    assert (
        summarize_drift(
            "threat_escalation",
            "threat_level",
            {"threat_level": "low"},
            {"threat_level": "high"},
            hostname="198.51.100.14",
        )
        == "threat level for 198.51.100.14 escalated from low to high"
    )


def test_shadow_it_discovered_with_a_resolved_hostname():
    assert (
        summarize_drift(
            "shadow_it_discovered",
            "ip",
            None,
            {"ip": "198.51.100.15", "hostname": "unknown-box"},
            hostname="198.51.100.15",
        )
        == "shadow-IT host discovered at 198.51.100.15 (unknown-box) — responding on "
        "the network but not in inventory"
    )


def test_shadow_it_discovered_without_a_resolved_hostname():
    """``info.get("hostname")`` is frequently None on an unresponsive host."""
    assert (
        summarize_drift(
            "shadow_it_discovered",
            "ip",
            None,
            {"ip": "198.51.100.15", "hostname": None},
            hostname="198.51.100.15",
        )
        == "shadow-IT host discovered at 198.51.100.15 — responding on the network "
        "but not in inventory"
    )


def test_dangerous_service_discovered():
    assert (
        summarize_drift(
            "dangerous_service_discovered",
            "port/tcp/23",
            None,
            {"port": 23, "proto": "tcp", "service": "telnet", "banner": "Welcome"},
            hostname="198.51.100.14",
        )
        == "dangerous service telnet found on port 23/tcp on 198.51.100.14"
    )


def test_suspicious_service_discovered_with_no_service_name():
    """nmap frequently returns an empty ``service`` for an unknown port."""
    assert (
        summarize_drift(
            "suspicious_service_discovered",
            "port/tcp/4444",
            None,
            {"port": 4444, "proto": "tcp", "service": "", "banner": "x"},
            hostname="198.51.100.14",
        )
        == "suspicious service found on port 4444/tcp on 198.51.100.14"
    )


# ---------------------------------------------------------------------------
# potential_secret_in_iac — 1 live row. agents/iac.py
# ---------------------------------------------------------------------------


def test_potential_secret_in_iac_from_the_live_row():
    assert (
        summarize_drift(
            "potential_secret_in_iac",
            "file_path",
            None,
            {
                "file_path": "inventory/host_vars/cloudflare_tunnel_b.yml",
                "secret_type": "token",
                "confidence_tier": "literal",
            },
            hostname="homelab-ansible/inventory/host_vars/cloudflare_tunnel_b.yml",
        )
        == "possible token committed in IaC file "
        "inventory/host_vars/cloudflare_tunnel_b.yml (literal confidence)"
    )


def test_potential_secret_summary_never_leaks_a_value():
    """agents/iac.py deliberately never records the matched literal. The
    summariser must not reintroduce one from any stray payload key."""
    out = summarize_drift(
        "potential_secret_in_iac",
        "file_path",
        None,
        {
            "file_path": "group_vars/all.yml",
            "secret_type": "password",
            "confidence_tier": "literal_high",
            "value": "hunter2-SHOULD-NEVER-APPEAR",
        },
        hostname="repo/group_vars/all.yml",
    )
    assert "hunter2" not in out


# ---------------------------------------------------------------------------
# host_reconcile identity conflicts — TWO structurally different payloads under
# (partly) the same drift_type. See agents/host_reconcile.py.
# ---------------------------------------------------------------------------


def test_identity_conflict_ip_shape():
    """``_upsert_ip_conflict_event``: field="ip_address",
    old/new = {"ip": ..., "source": ...}."""
    assert (
        summarize_drift(
            "identity_conflict",
            "ip_address",
            {"ip": "10.0.0.5", "source": "vsphere"},
            {"ip": "10.0.0.9", "source": "linux"},
            hostname="web01",
        )
        == "web01 has conflicting IP addresses across sources: vsphere reports "
        "10.0.0.5, linux reports 10.0.0.9"
    )


def test_identity_conflict_suffix_variant_shape():
    assert (
        summarize_drift(
            "identity_conflict_suffix_variant",
            "resource_id",
            {
                "short_hostname": "web01",
                "source": "vsphere",
                "kept_resource_id": "aaa",
                "kept_hostname": "web01",
            },
            {
                "short_hostname": "web01",
                "source": "vsphere",
                "dropped_resource_id": "bbb",
                "dropped_hostname": "web01.corp.example.com",
                "conflict_class": "suffix_variant",
            },
            hostname="web01",
        )
        == "web01 matched two different vsphere records — kept web01, dropped "
        "web01.corp.example.com (same host spelled two ways)"
    )


def test_identity_conflict_distinct_object_shape():
    assert (
        summarize_drift(
            "identity_conflict_distinct_object",
            "resource_id",
            {
                "short_hostname": "db02",
                "source": "vsphere",
                "kept_resource_id": "aaa",
                "kept_hostname": "db02",
            },
            {
                "short_hostname": "db02",
                "source": "vsphere",
                "dropped_resource_id": "bbb",
                "dropped_hostname": "db02",
                "conflict_class": "distinct_object",
            },
            hostname="db02",
        )
        == "db02 matched two different vsphere records — kept db02, dropped db02 "
        "(genuinely different objects — a clone, replica or restore)"
    )


def test_identity_conflict_unclassified_hostname_shape():
    """The flat ``identity_conflict`` type on the hostname-collision payload
    (conflict_class="unclassified") — distinct from the ip_address shape above
    even though the drift_type string is identical."""
    assert (
        summarize_drift(
            "identity_conflict",
            "resource_id",
            {
                "short_hostname": "app03",
                "source": "linux",
                "kept_resource_id": "aaa",
                "kept_hostname": "app03",
            },
            {
                "short_hostname": "app03",
                "source": "linux",
                "dropped_resource_id": "bbb",
                "dropped_hostname": "app03.dmz",
                "conflict_class": "unclassified",
            },
            hostname="app03",
        )
        == "app03 matched two different linux records — kept app03, dropped "
        "app03.dmz (collision could not be classified)"
    )


# ---------------------------------------------------------------------------
# state_drift — LEGACY. agents/drift.py::detect_state_drift no longer writes
# these and bulk-resolves any that remain open on every pass (GitLab #137).
# The summariser must still explain a historical row an operator can surface
# via the "All" status tab.
# ---------------------------------------------------------------------------


def test_legacy_state_drift_presence_is_explained_as_legacy():
    out = summarize_drift("state_drift", "presence", None, None, hostname="old-host-07")
    assert (
        out == "old-host-07 was absent from its domain's last collection run "
        "(legacy retirement bookkeeping — no longer written)"
    )


# ---------------------------------------------------------------------------
# The MCP seeding shape — mcp_server.py::seed_drift_event wraps under "value"
# and attaches severity/source/note, under a CALLER-CHOSEN drift_type.
# ---------------------------------------------------------------------------


def test_seeded_event_value_wrapper_is_unwrapped():
    assert (
        summarize_drift(
            "config_drift",
            "os_version",
            {"value": "22.04"},
            {"value": "24.04", "severity": "medium", "source": "manual", "note": "n"},
            hostname="web01",
        )
        == "os_version changed from 22.04 to 24.04 on web01"
    )


# ---------------------------------------------------------------------------
# Honest generic fallback — an unknown drift_type or a payload shape nobody
# anticipated must produce "field X changed" plus the raw values, and must
# NEVER raise.
# ---------------------------------------------------------------------------


def test_unknown_drift_type_falls_back_to_field_changed_plus_raw_values():
    assert (
        summarize_drift(
            "some_future_drift_type", "widget_count", {"n": 1}, {"n": 2}, hostname="web01"
        )
        == 'widget_count changed on web01: {"n": 1} → {"n": 2}'
    )


def test_unknown_drift_type_with_only_a_new_value():
    assert (
        summarize_drift("some_future_drift_type", "thing", None, {"a": 1}, hostname="web01")
        == 'thing recorded on web01: {"a": 1}'
    )


def test_unknown_drift_type_with_no_values_at_all():
    assert (
        summarize_drift("some_future_drift_type", "thing", None, None, hostname="web01")
        == "thing changed on web01 (some_future_drift_type)"
    )


@pytest.mark.parametrize(
    "old_value,new_value",
    [
        (None, None),
        ("a bare string, not a dict", "another bare string"),
        ([1, 2, 3], [4, 5, 6]),
        (12345, 67890),
        (True, False),
        ({"v": {"deeply": {"nested": ["structure", 1, None]}}}, {"v": []}),
        ({"v": "x" * 5000}, {"v": "y" * 5000}),
        ({}, {}),
        ({"v": float("nan")}, {"v": float("inf")}),
    ],
)
@pytest.mark.parametrize(
    "drift_type",
    [
        "config_drift",
        "new_listening_port",
        "new_windows_service",
        "service_stopped",
        "threat_escalation",
        "shadow_it_discovered",
        "dangerous_service_discovered",
        "suspicious_service_discovered",
        "potential_secret_in_iac",
        "identity_conflict",
        "identity_conflict_suffix_variant",
        "identity_conflict_distinct_object",
        "state_drift",
        "totally_unknown",
        "",
    ],
)
def test_never_crashes_on_any_payload_for_any_known_type(drift_type, old_value, new_value):
    """Cartesian product of every known drift_type against deliberately wrong
    payloads. Every cell must return a non-empty, bounded string."""
    out = summarize_drift(drift_type, "some_field", old_value, new_value, hostname="h")
    assert isinstance(out, str)
    assert out.strip()
    assert len(out) <= 400, f"summary must stay one readable sentence, got {len(out)}"


def test_never_crashes_on_hostile_inputs():
    """None field, None drift_type, an object whose __str__ raises."""

    class Explodes:
        def __repr__(self):  # pragma: no cover - exercised via the call below
            raise RuntimeError("boom")

        __str__ = __repr__

    assert summarize_drift(None, None, None, None)  # type: ignore[arg-type]
    assert summarize_drift("config_drift", "f", {"v": Explodes()}, {"v": Explodes()})


# ---------------------------------------------------------------------------
# "Why is this flagged" — the static rule map.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "drift_type",
    [
        "config_drift",
        "new_listening_port",
        "new_windows_service",
        "service_stopped",
        "threat_escalation",
        "shadow_it_discovered",
        "dangerous_service_discovered",
        "suspicious_service_discovered",
        "potential_secret_in_iac",
        "identity_conflict",
        "identity_conflict_suffix_variant",
        "identity_conflict_distinct_object",
        "state_drift",
    ],
)
def test_every_writable_drift_type_has_a_rule_explanation(drift_type):
    """Every drift_type any writer in src/ can emit must be in the map — this
    test is the guard that keeps the map in step with the collectors."""
    assert drift_type in DRIFT_RULE_EXPLANATIONS
    explanation = describe_drift_rule(drift_type)
    assert explanation and explanation == DRIFT_RULE_EXPLANATIONS[drift_type]


def test_rule_explanations_are_one_line_each():
    for drift_type, text in DRIFT_RULE_EXPLANATIONS.items():
        assert "\n" not in text, drift_type
        assert text.strip() == text, drift_type


def test_unknown_drift_type_gets_an_honest_rule_fallback():
    out = describe_drift_rule("some_future_drift_type")
    assert "some_future_drift_type" in out
    assert out  # never empty


def test_describe_drift_rule_tolerates_none():
    assert describe_drift_rule(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# render_drift_value — powers the drawer's before/after diff. Unwraps the
# writer-specific envelope so the drawer shows ``vmi-example-deploy`` rather than
# ``{'v': 'vmi-example-deploy'}``.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"v": "vmi-example-deploy"}, "vmi-example-deploy"),
        ({"v": 200}, "200"),
        ({"v": None}, ""),
        ({"value": "22.04"}, "22.04"),
        (None, ""),
        ({"port": 8443}, '{"port": 8443}'),
        ({"state": "Running"}, '{"state": "Running"}'),
        ("bare string", "bare string"),
        (42, "42"),
    ],
)
def test_render_drift_value(raw, expected):
    assert render_drift_value(raw) == expected


def test_render_drift_value_never_crashes_on_unserialisable_input():
    class Weird:
        pass

    assert isinstance(render_drift_value({"v": Weird()}), str)
    assert isinstance(render_drift_value(Weird()), str)
