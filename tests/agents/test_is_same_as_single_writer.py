"""Task 4.6, then P5 — how eight IS_SAME_AS writers became ONE, then ZERO.

Task 4.6 (original): eight sources emitted IS_SAME_AS into
resource_relationships — HostReconcileAgent (the intended sole writer),
GraphMaintenanceAgent (gap-fill + link-new), WindowsAgent, VulnAgent,
VsphereAgent, OctopusAgent, NetDiscoveryAgent, LinuxAgent. Each of the seven
non-owners is driven below with a seeded cross-source-match scenario that would
previously have produced an identity edge, and asserted silent.

P5 (2026-08-12): the LAST writer went too. ``host_reconcile``'s two emission
passes are deleted rather than re-anchored onto ``graph_edges``, because
re-anchoring would have made a second writer of the resolver-owned ``SAME_AS``
type with different thresholds and a different authority model.
``graph_phase3.resolve_entities`` is now the sole identity writer, in the store
the review queue and the human-decision gates actually read. Section 8 below is
that final step, inverted from "the owner still emits" to "nobody does"; the
coverage proof that made it safe is
``tests/test_p5_issameas_resolver_coverage.py``.

GraphMaintenanceAgent's decay pass still operates on existing identity edges
(now in ``graph_edges``) — maintenance on what someone else wrote was never
writing, and is explicitly retained.

Each per-agent test mocks ``emit_edges_batch`` at the agent's module path
(the codebase's own established pattern — see
``tests/agents/test_vuln.py::test_vulnerable_to_skips_self_loop_when_vuln_has_no_resource``
for the precedent) because the real upsert SQL is PostgreSQL-only (ON CONFLICT
+ jsonb cast) and these tests run on an in-memory SQLite engine.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from infra_brain.db.models import (
    CollectionRun,
    GraphEdge,
    GraphNode,
    LinuxHost,
    OctopusMachine,
    R7Asset,
    Resource,
    VsphereVm,
    WindowsPatchState,
)
from sqlalchemy import inspect as _sqla_inspect

from infra_brain.db.relationships import RelationshipType


def _legacy_store_absent(session) -> None:
    """P5 replacement for ``s.query(ResourceRelationship).count() == 0``.

    The pre-drop assertion counted rows in the legacy table. Post-drop the
    honest form of the same claim is stronger: the table does not exist AT ALL
    in the schema the agents just ran against (``create_all`` no longer builds
    it — the model left ``Base.metadata`` with the drop), so no writer could
    have produced a row even in principle.
    """
    assert "resource_relationships" not in _sqla_inspect(session.get_bind()).get_table_names()


def _no_is_same_as(mock_emit) -> None:
    """Assert none of the edge dicts passed to emit_edges_batch are IS_SAME_AS.

    Tolerates emit_edges_batch not being called at all (the strongest case —
    the whole edge-building path for IS_SAME_AS is gone) as well as being
    called with a batch that contains only non-IS_SAME_AS edge types (the
    "other edges preserved" case).
    """
    for call in mock_emit.call_args_list:
        _session_arg, edges = call.args[0], call.args[1]
        for edge in edges:
            rel_type = edge["rel_type"]
            rel_value = rel_type.value if hasattr(rel_type, "value") else rel_type
            assert rel_value != RelationshipType.IS_SAME_AS.value, (
                f"non-owner agent emitted an IS_SAME_AS edge: {edge}"
            )


# ---------------------------------------------------------------------------
# 1. WindowsAgent — _emit_windows_edges (IS_SAME_AS-only) must be removed
# ---------------------------------------------------------------------------


def test_windows_agent_has_no_identity_edge_method():
    """_emit_windows_edges was the sole IS_SAME_AS emission site in windows.py
    and emitted nothing else — it must be removed entirely, not neutered."""
    from infra_brain.agents.windows import WindowsAgent

    assert not hasattr(WindowsAgent, "_emit_windows_edges")


def test_windows_agent_module_no_longer_imports_emit_edges_batch():
    """windows.py had no other edge type to emit — after removing the sole
    IS_SAME_AS emission site, the module must not still import
    emit_edges_batch/RelationshipType (would be dead code / orphaned import)."""
    import infra_brain.agents.windows as windows_module

    assert not hasattr(windows_module, "emit_edges_batch")
    assert not hasattr(windows_module, "RelationshipType")


# ---------------------------------------------------------------------------
# 2. LinuxAgent — _emit_linux_edges must no longer emit IS_SAME_AS
# ---------------------------------------------------------------------------


def test_linux_agent_has_no_identity_edge_method():
    """_emit_linux_edges was the sole IS_SAME_AS emission site in linux.py
    and emitted nothing else — it must be removed entirely, not neutered."""
    from infra_brain.agents.linux import LinuxAgent

    assert not hasattr(LinuxAgent, "_emit_linux_edges")


def test_linux_agent_module_no_longer_imports_emit_edges_batch():
    """linux.py had no other edge type to emit — after removing the sole
    IS_SAME_AS emission site, the module must not still import
    emit_edges_batch/RelationshipType (would be dead code / orphaned import)."""
    import infra_brain.agents.linux as linux_module

    assert not hasattr(linux_module, "emit_edges_batch")
    assert not hasattr(linux_module, "RelationshipType")


# ---------------------------------------------------------------------------
# 3. VsphereAgent — _emit_identity_edges (IS_SAME_AS-only) must be removed
# ---------------------------------------------------------------------------


def test_vsphere_agent_has_no_identity_edge_method():
    """_emit_identity_edges was the sole IS_SAME_AS emission site in vsphere.py
    and emitted nothing else — it must be removed entirely, not neutered."""
    from infra_brain.agents.vsphere import VsphereAgent

    assert not hasattr(VsphereAgent, "_emit_identity_edges")


def test_vsphere_agent_write_inventory_emits_no_is_same_as(sqlite_engine):
    """_write_inventory must still emit topology edges (RUNS_ON/MEMBER_OF) but
    never IS_SAME_AS, even when a matching R7Asset exists for the VM's guest
    hostname (the exact scenario that used to produce an identity edge)."""
    from infra_brain.agents.vsphere import VsphereAgent

    r7_rid = uuid.uuid4()
    with Session(sqlite_engine) as s:
        r7_resource = Resource(
            id=r7_rid, domain="vuln", type="r7_asset", name="vm01", source="rapid7"
        )
        s.add(r7_resource)
        s.flush()
        s.add(R7Asset(resource_id=r7_rid, r7_asset_id=3, hostname="vm01", ip="10.0.0.5"))
        s.commit()

    import infra_brain.agents.vsphere as vsphere_module

    agent = VsphereAgent.__new__(VsphereAgent)
    agent.settings = MagicMock()
    agent.callbacks = []

    items = [
        {
            "type": "vsphere_vm",
            "name": "vm01",
            "data": {
                "vcenter": "vc1",
                "moref": "vm-1",
                "guest_hostname": "vm01",
                "ip_address": "10.0.0.5",
                "power_state": "poweredOn",
            },
        },
        {
            "type": "vsphere_host",
            "name": "esxi01",
            "data": {"vcenter": "vc1", "moref": "host-1", "connection_state": "connected"},
        },
    ]

    @contextmanager
    def _get_session():
        with Session(sqlite_engine) as s:
            yield s

    with patch("infra_brain.agents.vsphere.get_session", _get_session):
        agent._write_inventory(items)

    # P5 (rev11-T5-B): this used to patch ``vsphere.emit_edges_batch`` and
    # assert no IS_SAME_AS dict reached it. vsphere.py no longer emits ANY
    # legacy edge — the topology build went with the store — so the claim is
    # now the STRONGER one the module-level tests above make for windows,
    # linux and netdiscovery: the name is not even importable here, and the
    # inventory write really did happen.
    assert not hasattr(vsphere_module, "emit_edges_batch")
    assert not hasattr(vsphere_module, "RelationshipType")
    with Session(sqlite_engine) as s:
        assert s.query(VsphereVm).count() == 1
        _legacy_store_absent(s)


# ---------------------------------------------------------------------------
# 4 + 5. OctopusAgent / VulnAgent — the two behavioural tests that stood here
#        are replaced by a structural one. P5 deleted BOTH emitters outright.
# ---------------------------------------------------------------------------
#
# ``test_octopus_agent_emits_no_is_same_as`` seeded an OctopusMachine and a
# hostname-matching R7Asset, ran ``OctopusAgent._emit_graph_edges`` with a
# patched ``emit_edges_batch``, and asserted no IS_SAME_AS came out — the exact
# scenario that used to mint an identity edge. ``test_vuln_agent_emits_no_is_same_as``
# did the same for ``VulnAgent._write_graph_edges`` against a hostname-matching
# VsphereVm.
#
# P5 deleted both methods with the ``resource_relationships`` store they were
# the only writers into (epitaphs in agents/octopus.py and agents/vuln.py:
# both collectors are retired, zero live rows ever). Neither method can be
# invoked and neither module imports ``emit_edges_batch`` any more, so the
# behavioural tests cannot run — and the property they defended is now
# guaranteed by construction rather than by assertion.
#
# The single-writer claim itself is NOT weakened; it is strengthened. "These two
# collectors do not emit IS_SAME_AS" is now the stronger "these two collectors
# do not emit ANY edge into the legacy store", which the test below pins
# structurally. HostReconcileAgent remains the sole IS_SAME_AS writer (Task
# 4.6), and the other emission sites in this file (vsphere, netdiscovery, and
# the rest) keep their behavioural coverage unchanged.


def test_octopus_and_vuln_have_no_legacy_edge_writer_at_all():
    """The strongest form of "these two never write IS_SAME_AS".

    Both collectors' whole edge-emission methods are gone, and neither module
    reaches the legacy emit functions from any code path. A future IS_SAME_AS
    emission from either would require re-introducing a writer, which this
    catches regardless of which relationship type it carried.
    """
    import inspect

    from infra_brain.agents import octopus as octopus_mod
    from infra_brain.agents import vuln as vuln_mod

    assert not hasattr(octopus_mod.OctopusAgent, "_emit_graph_edges")
    assert not hasattr(vuln_mod.VulnAgent, "_write_graph_edges")

    for mod in (octopus_mod, vuln_mod):
        # The names are not even imported any more.
        assert not hasattr(mod, "emit_edges_batch"), f"{mod.__name__} re-imported emit_edges_batch"
        assert not hasattr(mod, "emit_edge"), f"{mod.__name__} re-imported emit_edge"

        code = "\n".join(
            ln for ln in inspect.getsource(mod).splitlines() if not ln.lstrip().startswith("#")
        )
        for banned in ("emit_edge(", "emit_edges_batch("):
            assert banned not in code, f"{mod.__name__} must not call {banned}"
        assert "RelationshipType.IS_SAME_AS" not in code


# ---------------------------------------------------------------------------
# 6. NetDiscoveryAgent — the "correlate with known inventory" IS_SAME_AS
#    emission site must be removed.
# ---------------------------------------------------------------------------


def test_netdiscovery_agent_emits_no_is_same_as(sqlite_engine):
    """_persist must not emit IS_SAME_AS even for an is_known=True host that
    matches an R7Asset by IP (the exact scenario that used to produce an
    identity edge)."""
    from infra_brain.agents.netdiscovery import NetDiscoveryAgent

    run_id = uuid.uuid4()
    r7_rid = uuid.uuid4()

    with Session(sqlite_engine) as s:
        s.add(
            CollectionRun(
                id=run_id, domain="netdiscovery", trigger_type="test", status="in_progress"
            )
        )
        s.add(
            Resource(id=r7_rid, domain="vuln", type="r7_asset", name="10.0.0.42", source="rapid7")
        )
        s.flush()
        s.add(R7Asset(resource_id=r7_rid, r7_asset_id=6, hostname="known-host", ip="10.0.0.42"))
        s.commit()

    agent = NetDiscoveryAgent.__new__(NetDiscoveryAgent)
    agent.settings = MagicMock()
    agent.settings.default_zone = "corpor"
    agent.callbacks = []

    classified = {
        "10.0.0.42": {
            "ip": "10.0.0.42",
            "responded": True,
            "mac": "aa:bb:cc:dd:ee:ff",
            "mac_vendor": "Test",
            "hostname": "known-host",
            "is_fragile": False,
            "is_known": True,
            "is_shadow_it": False,
            "threat_level": "none",
            "discovery_tier": "tier1",
        }
    }

    @contextmanager
    def _get_session():
        with Session(sqlite_engine) as s:
            yield s

    # netdiscovery.py no longer imports emit_edges_batch at all — its sole
    # IS_SAME_AS emission site (the "correlate with known inventory" block)
    # was removed and nothing else in _persist emits graph edges.
    import infra_brain.agents.netdiscovery as netdiscovery_module

    assert not hasattr(netdiscovery_module, "emit_edges_batch")
    assert not hasattr(netdiscovery_module, "RelationshipType")

    with patch("infra_brain.agents.netdiscovery.get_session", _get_session):
        hosts_written, _services_written = agent._persist(classified, [], run_id, frozenset(), [])

    assert hosts_written == 1


# ---------------------------------------------------------------------------
# 7. GraphMaintenanceAgent — gap-fill + link-new IS_SAME_AS emission must be
#    removed; decay (a maintenance pass over EXISTING edges) must be kept.
# ---------------------------------------------------------------------------


def test_graph_maintenance_fill_cross_domain_gaps_emits_no_is_same_as(sqlite_engine):
    """_fill_cross_domain_gaps must not emit IS_SAME_AS even when a matching
    OctopusMachine + LinuxHost pair exists for the same hostname (the exact
    scenario that used to produce a gap-fill identity edge)."""
    from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent

    mach_rid = uuid.uuid4()
    linux_rid = uuid.uuid4()

    with Session(sqlite_engine) as s:
        s.add_all(
            [
                Resource(
                    id=mach_rid,
                    domain="octopus",
                    type="octopus_machine",
                    name="node01",
                    source="octopus",
                ),
                Resource(
                    id=linux_rid, domain="linux", type="linux_host", name="node01", source="ansible"
                ),
            ]
        )
        s.flush()
        s.add(
            OctopusMachine(
                resource_id=mach_rid, octopus_id="Machines-2", name="node01", status="Online"
            )
        )
        s.add(LinuxHost(resource_id=linux_rid, distro="ubuntu", kernel="5.15", arch="x86_64"))
        s.commit()

    import infra_brain.agents.graph_maintenance as gm_module

    agent = GraphMaintenanceAgent.__new__(GraphMaintenanceAgent)
    agent.settings = MagicMock()
    agent.callbacks = []

    # P5 (rev11-T5-B): graph_maintenance no longer imports the legacy emitter,
    # so there is nothing to patch and nothing to inspect for IS_SAME_AS dicts.
    # The stub must still be a no-op that writes no legacy row.
    with Session(sqlite_engine) as s:
        assert agent._fill_cross_domain_gaps(s) == {"filled": 0}
        _legacy_store_absent(s)
    assert not hasattr(gm_module, "emit_edges_batch")


def test_graph_maintenance_link_new_resources_emits_no_is_same_as(sqlite_engine):
    """_link_new_resources must not emit IS_SAME_AS for a newly-created
    Resource that matches an existing R7Asset by hostname (the exact
    scenario that used to produce a link-new identity edge)."""
    from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent

    new_rid = uuid.uuid4()
    r7_rid = uuid.uuid4()
    now = datetime.now(timezone.utc)

    with Session(sqlite_engine) as s:
        # "New" resource: last_seen inside the 2h window, no outgoing edges.
        s.add(
            Resource(
                id=new_rid,
                domain="linux",
                type="linux_host",
                name="fresh01",
                source="ansible",
                last_seen=now,
            )
        )
        s.add(Resource(id=r7_rid, domain="vuln", type="r7_asset", name="fresh01", source="rapid7"))
        s.flush()
        s.add(R7Asset(resource_id=r7_rid, r7_asset_id=7, hostname="fresh01"))
        s.commit()

    import infra_brain.agents.graph_maintenance as gm_module

    agent = GraphMaintenanceAgent.__new__(GraphMaintenanceAgent)
    agent.settings = MagicMock()
    agent.callbacks = []

    # P5 (rev11-T5-B): see the note on the gap-fill test above.
    with Session(sqlite_engine) as s:
        assert agent._link_new_resources(s) == {"linked": 0}
        _legacy_store_absent(s)
    assert not hasattr(gm_module, "emit_edges_batch")


# ---------------------------------------------------------------------------
# 7b. Stale-edge decay, now over ``graph_edges`` (rev10/T3 store move) and
#     still flag-gated per TRK-089: graph_edge_decay_enabled=False (default)
#     MUST leave the inference-backed edge types untouched and keep identity
#     decay byte-identical; =True decays them on their own windows with the
#     identical math. Two exemptions are enforced, not implied:
#     authority='human' and method='declared' are never touched.
# ---------------------------------------------------------------------------


def _make_maint_agent(*, decay_enabled):
    """GraphMaintenanceAgent with decay-relevant settings pinned to real values
    (MagicMock returns truthy Mocks for both the int window and the bool flag,
    so both must be set explicitly — see TRK-089)."""
    from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent

    agent = GraphMaintenanceAgent.__new__(GraphMaintenanceAgent)
    agent.settings = MagicMock()
    agent.settings.is_same_as_decay_days = 14
    agent.settings.graph_edge_decay_enabled = decay_enabled
    agent.callbacks = []
    return agent


def _seed_graph_edge(
    engine,
    edge_type,
    *,
    confidence,
    age_days,
    method="deterministic_match",
    authority="auto",
):
    """Seed two graph_nodes + one active graph_edge aged by ``recorded_at``.

    Returns the edge id. ``recorded_at`` is what decay reads (see the
    within-window test for why it is not ``valid_from``).
    """
    stamp = datetime.now(timezone.utc) - timedelta(days=age_days)
    eid = uuid.uuid4()
    with Session(engine) as s:
        src = GraphNode(
            id=uuid.uuid4(),
            node_type="VsphereVM",
            natural_key=f"vc1:{eid}",
            name="src",
            source="vsphere",
        )
        tgt = GraphNode(
            id=uuid.uuid4(),
            node_type="R7Asset",
            natural_key=f"r7:{eid}",
            name="tgt",
            source="rapid7",
        )
        s.add_all([src, tgt])
        s.flush()
        s.add(
            GraphEdge(
                id=eid,
                source_id=src.id,
                target_id=tgt.id,
                edge_type=edge_type,
                method=method,
                confidence=Decimal(confidence),
                source="test",
                authority=authority,
                valid_from=stamp,
                recorded_at=stamp,
                evidence={},
            )
        )
        s.commit()
    return eid


def test_graph_maintenance_decay_still_ages_existing_same_as_edges(sqlite_engine):
    """Decay maintenance on already-existing identity edges, now in ``graph_edges``.

    STORE MOVE (rev10/T3). This used to walk ``resource_relationships``
    IS_SAME_AS rows and had THREE outcome tiers: decay in place, then
    ``status='archived'`` below 0.50 (the KG-6 fix, which replaced an outright
    hard delete), then hard delete below 0.05 to bound archived-row growth.

    ``graph_edges`` has no ``status`` column and one absolute rule — edges are
    retired by stamping ``valid_to``, never DELETEd — so the third tier is gone
    on purpose: a retired bitemporal row IS the reviewable history the archive
    tier was invented to provide, and deleting it would destroy exactly what
    motivated moving to this store. Two tiers now: decay, then retire.

    The identity edges themselves are unaffected as a CLAIM — since P5 there is
    exactly ONE identity writer anywhere, ``graph_phase3``, and this pass only
    ages what it wrote. Ageing someone else's claim was never writing one, which
    is why decay survived every round of writer consolidation.
    """
    agent = _make_maint_agent(decay_enabled=False)

    # Edge 1: stale, confidence high enough to survive one pass.
    keep = _seed_graph_edge(sqlite_engine, "SAME_AS", confidence="0.950", age_days=30)
    # Edge 2: stale AND low enough that one *0.90 pass crosses the 0.500 floor.
    retire = _seed_graph_edge(sqlite_engine, "SAME_AS", confidence="0.520", age_days=30)

    with Session(sqlite_engine) as s:
        result = agent._decay_confidence(s)
        s.commit()

    assert result["decayed"] == 1
    assert result["retired"] == 1
    assert result["removed"] == 1  # alias of retired (legacy stat name)

    with Session(sqlite_engine) as s:
        e1 = s.get(GraphEdge, keep)
        assert e1.confidence == Decimal("0.855")  # 0.950 * 0.90
        assert e1.valid_to is None

        e2 = s.get(GraphEdge, retire)
        assert e2 is not None, "graph_edges is bitemporal — decay must never DELETE"
        assert e2.valid_to is not None, "below the floor the edge must be RETIRED"
        assert e2.confidence == Decimal("0.468")  # 0.520 * 0.90
        assert e2.evidence["retired_reason"] == "decayed_below_floor"

    # No legacy-store row was written or removed by the pass.
    with Session(sqlite_engine) as s:
        _legacy_store_absent(s)


def test_decay_never_touches_a_human_authority_edge(sqlite_engine):
    """The authority exemption, enforced not merely implied.

    Edge-authority spec §7.3: decay never touches structural or human-confirmed
    edges. A named person's assertion does not erode because a machine stopped
    re-deriving it; retiring one is ``retract_same_as``'s job.
    """
    eid = _seed_graph_edge(
        sqlite_engine,
        "SAME_AS",
        confidence="1.000",
        age_days=400,
        method="declared",
        authority="human",
    )
    agent = _make_maint_agent(decay_enabled=True)

    with Session(sqlite_engine) as s:
        result = agent._decay_confidence(s)
        s.commit()

    assert result == {"decayed": 0, "removed": 0, "retired": 0}
    with Session(sqlite_engine) as s:
        edge = s.get(GraphEdge, eid)
        assert edge.confidence == Decimal("1.000")
        assert edge.valid_to is None


def test_decay_never_touches_a_structural_declared_edge(sqlite_engine):
    """The structural exemption. The legacy pass got this by OMISSION from
    ``_HEURISTIC_DECAY_DAYS``; here it is BOTH an allow-list
    (``_GRAPH_EDGE_DECAY_DAYS``) and an explicit ``method != 'declared'``
    filter, because a free-string ``edge_type`` vocabulary makes "forgot to
    exclude it" too cheap a mistake."""
    hosted = _seed_graph_edge(
        sqlite_engine, "HOSTED_ON", confidence="1.000", age_days=400, method="declared"
    )
    runs = _seed_graph_edge(
        sqlite_engine, "RUNS_ON", confidence="1.000", age_days=400, method="declared"
    )
    agent = _make_maint_agent(decay_enabled=True)

    with Session(sqlite_engine) as s:
        result = agent._decay_confidence(s)
        s.commit()

    assert result == {"decayed": 0, "removed": 0, "retired": 0}
    with Session(sqlite_engine) as s:
        for eid in (hosted, runs):
            edge = s.get(GraphEdge, eid)
            assert edge.confidence == Decimal("1.000")
            assert edge.valid_to is None


def test_decay_flag_off_leaves_stale_inferred_role_edges_untouched(sqlite_engine):
    """REGRESSION (TRK-089's rule, ported): with graph_edge_decay_enabled=False,
    the inference-backed types never expire. Only SAME_AS decays unconditionally.

    The type under test moved with the store: the legacy flag gated
    VULNERABLE_TO / AFFECTED_BY / TRIGGERED_BY / DEPLOYED_VIA / HAS_SOFTWARE /
    RUNS_EOL in ``resource_relationships``; the graph_edges equivalents are
    ``graph_role_tagging``'s HAS_ROLE / IN_ENVIRONMENT, which are likewise
    re-asserted every pass from an INFERENCE (hostname pattern, annotation,
    FQDN suffix) rather than from an FK.
    """
    eid = _seed_graph_edge(
        sqlite_engine,
        "HAS_ROLE",
        confidence="0.850",
        age_days=90,
        method="inferred_from_hostname_pattern",
    )
    agent = _make_maint_agent(decay_enabled=False)

    with Session(sqlite_engine) as s:
        result = agent._decay_confidence(s)
        s.commit()

    assert result == {"decayed": 0, "removed": 0, "retired": 0}
    with Session(sqlite_engine) as s:
        edge = s.get(GraphEdge, eid)
        assert edge.confidence == Decimal("0.850")
        assert edge.valid_to is None


def test_decay_flag_off_still_ages_same_as_identically(sqlite_engine):
    """REGRESSION: with the flag False a stale SAME_AS still decays by *0.90 —
    identity decay is unconditional and its window still comes from
    ``settings.is_same_as_decay_days``, exactly as before the store move."""
    eid = _seed_graph_edge(sqlite_engine, "SAME_AS", confidence="0.950", age_days=30)
    agent = _make_maint_agent(decay_enabled=False)

    with Session(sqlite_engine) as s:
        result = agent._decay_confidence(s)
        s.commit()

    assert result["decayed"] == 1
    assert result["retired"] == 0
    with Session(sqlite_engine) as s:
        assert s.get(GraphEdge, eid).confidence == Decimal("0.855")


def test_decay_flag_on_decays_stale_inferred_edges_both_tiers(sqlite_engine):
    """NEW BEHAVIOR: with the flag True, inference-backed edges past their 45d
    window decay by *0.90 and exercise both surviving tiers — kept-active and
    retired — using identical math to SAME_AS."""
    keep = _seed_graph_edge(
        sqlite_engine,
        "IN_ENVIRONMENT",
        confidence="0.900",
        age_days=60,
        method="inferred_from_fqdn_suffix",
    )
    retire = _seed_graph_edge(
        sqlite_engine,
        "HAS_ROLE",
        confidence="0.520",
        age_days=60,
        method="inferred_from_annotation",
    )
    agent = _make_maint_agent(decay_enabled=True)

    with Session(sqlite_engine) as s:
        result = agent._decay_confidence(s)
        s.commit()

    assert result["decayed"] == 1
    assert result["retired"] == 1

    with Session(sqlite_engine) as s:
        e1 = s.get(GraphEdge, keep)
        assert e1.confidence == Decimal("0.810")  # 0.900 * 0.90
        assert e1.valid_to is None

        e2 = s.get(GraphEdge, retire)
        assert e2 is not None, "retired, never deleted"
        assert e2.valid_to is not None
        assert e2.confidence == Decimal("0.468")


def test_decay_flag_on_leaves_recent_inferred_edge_within_window(sqlite_engine):
    """WITHIN-WINDOW: with the flag True, an inferred edge whose ``recorded_at``
    is well inside its 45d window is left completely untouched.

    ``recorded_at`` (not ``valid_from``) is the freshness signal:
    ``upsert_edge`` stamps it on every same-authority refresh, which is the
    graph_edges equivalent of the legacy ``last_seen``. ``valid_from``
    deliberately keeps the edge's TRUE start across refreshes, so using it here
    would decay an edge that is being re-observed every two hours.
    """
    eid = _seed_graph_edge(
        sqlite_engine,
        "HAS_ROLE",
        confidence="0.850",
        age_days=5,
        method="inferred_from_hostname_pattern",
    )
    agent = _make_maint_agent(decay_enabled=True)

    with Session(sqlite_engine) as s:
        result = agent._decay_confidence(s)
        s.commit()

    assert result == {"decayed": 0, "removed": 0, "retired": 0}
    with Session(sqlite_engine) as s:
        assert s.get(GraphEdge, eid).confidence == Decimal("0.850")


# ---------------------------------------------------------------------------
# 8. HostReconcileAgent — was the last writer; P5 removed it too. Nothing
#    anywhere writes IS_SAME_AS into resource_relationships now.
# ---------------------------------------------------------------------------


def test_nothing_writes_is_same_as_into_the_legacy_store_any_more(sqlite_engine):
    """End-to-end ZERO-writer assertion, on the fixture that used to prove ONE.

    Same seed as before P5 — R7Asset + VsphereVm + OctopusMachine + LinuxHost +
    WindowsPatchState all resolving to one short hostname, ZERO pre-existing
    resource_relationships rows — i.e. the maximal cross-source convergence, the
    input that made ``_emit_is_same_as_edges`` fire hardest. A full
    ``HostReconcileAgent.run()`` over it now writes no identity edge at all.

    What the run still does is unchanged and is the point: it builds the
    ``HostIdentity`` merge from the source domain tables directly (never from
    ``resource_relationships``), which is what ``graph_phase3`` reads back as
    ambiguity counter-evidence. The agent went from asserting identity to
    INFORMING the one thing that does.
    """
    from infra_brain.agents.host_reconcile import HostReconcileAgent

    r7_rid = uuid.uuid4()
    vm_rid = uuid.uuid4()
    mach_rid = uuid.uuid4()
    linux_rid = uuid.uuid4()
    win_rid = uuid.uuid4()

    with Session(sqlite_engine) as s:
        s.add_all(
            [
                Resource(
                    id=r7_rid, domain="vuln", type="r7_asset", name="multi01", source="rapid7"
                ),
                Resource(
                    id=vm_rid, domain="vsphere", type="vsphere_vm", name="multi01", source="vsphere"
                ),
                Resource(
                    id=mach_rid,
                    domain="octopus",
                    type="octopus_machine",
                    name="multi01",
                    source="octopus",
                ),
                Resource(
                    id=linux_rid,
                    domain="linux",
                    type="linux_host",
                    name="multi01",
                    source="ansible",
                ),
                Resource(
                    id=win_rid,
                    domain="windows",
                    type="windows_host",
                    name="multi01",
                    source="ansible",
                ),
            ]
        )
        s.flush()
        s.add(R7Asset(resource_id=r7_rid, r7_asset_id=8, hostname="multi01", ip="10.1.1.1"))
        s.add(
            VsphereVm(
                resource_id=vm_rid,
                vcenter="vc1",
                moref="vm-99",
                name="multi01",
                guest_hostname="multi01",
            )
        )
        s.add(
            OctopusMachine(
                resource_id=mach_rid, octopus_id="Machines-3", name="multi01", status="Online"
            )
        )
        s.add(LinuxHost(resource_id=linux_rid, distro="ubuntu", kernel="5.15", arch="x86_64"))
        s.add(WindowsPatchState(resource_id=win_rid, hostname="multi01"))
        s.commit()

    # Sanity: no relationships exist yet — proves whatever HostReconcile
    # produces below did not come from pre-existing collector edges.
    with Session(sqlite_engine) as s:
        _legacy_store_absent(s)

    agent = HostReconcileAgent.__new__(HostReconcileAgent)
    agent.settings = MagicMock()
    agent.callbacks = []

    @contextmanager
    def _get_session():
        with Session(sqlite_engine) as s:
            yield s

    # No emit_edges_batch patch: the module no longer imports it at all, which
    # is the structural half of this assertion — a deleted writer that still
    # held the writing primitive would be one edit away from coming back.
    import infra_brain.agents.host_reconcile as host_reconcile_module

    assert not hasattr(host_reconcile_module, "emit_edges_batch")
    assert not hasattr(host_reconcile_module, "RelationshipType")

    with (
        patch("infra_brain.agents.host_reconcile.get_session", _get_session),
        patch("infra_brain.etl.base.get_session", _get_session),
        patch("infra_brain.etl.base.build_callbacks", return_value=[]),
    ):
        result = agent.run(trigger_type="test", scope="all")

    assert result.status == "completed"

    with Session(sqlite_engine) as s:
        _legacy_store_absent(s)  # no legacy store, so no legacy identity edge —
        # graph_phase3.resolve_entities owns identity now, in graph_edges
    # ...and the merge it DOES do still happened, so the resolver's inputs are
    # intact rather than the agent having simply stopped working.
    from infra_brain.db.models import HostIdentity

    with Session(sqlite_engine) as s:
        identity = s.query(HostIdentity).filter_by(short_hostname="multi01").one()
        assert identity.r7_resource_id is not None
        assert identity.linux_resource_id is not None
