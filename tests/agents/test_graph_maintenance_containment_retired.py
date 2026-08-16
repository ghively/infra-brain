"""T3 / rev10 (+ P5 / rev11-T5-B) — GraphMaintenanceAgent writes no legacy edge.

P5 UPDATE. rev10/T3 froze the legacy store here by retiring the CONTAINMENT
derivations while the genuine ones kept deriving; rev11-T5-B deleted the rest,
because ``resource_relationships`` is being dropped. Two consequences for this
file, both recorded rather than quietly applied:

  * every assertion that drove ``_populate_typed_relationships`` directly now
    drives ``collect()`` instead — the method is gone, the claim is not;
  * ``TestGenuineEmittersSurvive`` is DELETED. Its whole point was that a
    genuine type (MADE_BY) must keep deriving through a CONTAINMENT retirement.
    P5 is not a containment retirement — it removes the store — so the claim it
    guarded is no longer true and cannot be rewritten into something that is.
    The genuine types it protected are accounted for individually in the "3b."
    epitaph in ``agents/graph_maintenance.py``; the two GENUINE-over-LIVE-DATA
    losses (MEMBER_OF, RUNS_EOL) are named there as accepted losses pending
    TRK-359.


Three load-bearing claims, one test class each:

(a) **A full maintenance pass writes ZERO ``resource_relationships`` rows.**
    Seeded against a fixture shaped exactly like the live containment
    population (drift events, compliance violations, linux ports/crons/
    mounts/pending-updates/users, host certificates/shares/firewall rules/
    security posture, windows + rapid7 software, octopus roles/variables/
    teams, CI schedules, vSphere snapshots/disks, MAC vendors) — every one
    of these used to become an edge, and none of them may any more.

(b) **Decay / prune / contradiction-reconcile act on ``graph_edges``** and
    honour the authority model: an ``authority='human'`` edge is never
    decayed, retired or pruned, and a structural (``method='declared'``)
    edge never decays. Retirement stamps ``valid_to`` — this store never
    DELETEs.

(c) **The containment FACTS are still collected.** The derivation was
    deleted, not the collection: every detail row seeded in (a) is still
    present, unmodified, after the pass. That is the §3.1 point — the facts
    stay fully queryable in their detail tables.

See ``docs/decisions/2026-08-11-graph-first-architecture.md`` §3.1 and the
P3 migration ``alembic/versions/8965b6329b94_*`` whose ``CONTAINMENT_TYPES``
frozenset is the authority for which types are containment.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent
from infra_brain.db.models import (
    ZONE_CORPORATE,
    CiSchedule,
    ComplianceViolation,
    DriftEvent,
    GitlabProject,
    GraphEdge,
    GraphNode,
    HostCertificate,
    HostFirewallRule,
    HostSecurityPosture,
    HostShare,
    LinuxCron,
    LinuxHost,
    LinuxMount,
    LinuxNic,
    LinuxPendingUpdate,
    LinuxPort,
    LinuxUser,
    OctopusMachine,
    OctopusMachineRole,
    OctopusProject,
    OctopusVariable,
    R7Asset,
    R7AssetUser,
    R7Software,
    Resource,
    VsphereSnapshot,
    VsphereVm,
    VsphereVmDisk,
    WindowsSoftware,
)



from tests.support.pg import make_engine


def _legacy_store_absent(session) -> None:
    """P5 integration: the honest form of "wrote zero legacy rows".

    These tests predate the fold that removed the ORM model from
    ``Base.metadata``; they counted rows to prove no writer fired. Post-drop
    the claim is structural — the table does not exist in the schema this
    test just ran the agent against, so no writer COULD have produced a row.
    """
    from sqlalchemy import inspect as _sqla_inspect

    assert "resource_relationships" not in _sqla_inspect(session.get_bind()).get_table_names()


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _make_agent(decay_enabled: bool = True) -> GraphMaintenanceAgent:
    agent = GraphMaintenanceAgent.__new__(GraphMaintenanceAgent)
    agent.settings = MagicMock()
    agent.settings.is_same_as_decay_days = 14
    agent.settings.graph_edge_decay_enabled = decay_enabled
    agent.callbacks = []
    agent._maint_errors = []
    return agent


def _resource(session, domain, rtype, name, metadata=None) -> Resource:
    r = Resource(
        id=uuid.uuid4(),
        domain=domain,
        type=rtype,
        name=name,
        source="test",
        zone=ZONE_CORPORATE,
        metadata_=metadata,
    )
    session.add(r)
    session.flush()
    return r


def _node(session, node_type, natural_key, *, resource_id=None, source="test") -> GraphNode:
    n = GraphNode(
        id=uuid.uuid4(),
        node_type=node_type,
        natural_key=natural_key,
        name=natural_key,
        source=source,
        resource_id=resource_id,
    )
    session.add(n)
    session.flush()
    return n


def _edge(
    session,
    src: GraphNode,
    tgt: GraphNode,
    edge_type: str,
    *,
    method: str = "deterministic_match",
    confidence: str = "0.990",
    authority: str = "auto",
    age_days: int = 0,
    emitter: str = "test",
) -> GraphEdge:
    stamp = datetime.now(UTC) - timedelta(days=age_days)
    e = GraphEdge(
        id=uuid.uuid4(),
        source_id=src.id,
        target_id=tgt.id,
        edge_type=edge_type,
        method=method,
        confidence=Decimal(confidence),
        source=emitter,
        authority=authority,
        valid_from=stamp,
        recorded_at=stamp,
        evidence={},
    )
    session.add(e)
    session.flush()
    return e


@pytest.fixture()
def db():
    eng = make_engine()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    return eng, _get_session


# ---------------------------------------------------------------------------
# The live-shaped containment fixture
# ---------------------------------------------------------------------------

#: (table, expected row count) — asserted intact after the pass by claim (c).
CONTAINMENT_DETAIL_TABLES = (
    (DriftEvent, 2),
    (ComplianceViolation, 1),
    (LinuxPort, 2),
    (LinuxCron, 1),
    (LinuxMount, 1),
    (LinuxPendingUpdate, 1),
    (LinuxUser, 1),
    (LinuxNic, 1),
    (HostCertificate, 1),
    (HostShare, 1),
    (HostFirewallRule, 1),
    (HostSecurityPosture, 1),
    (WindowsSoftware, 1),
    (R7Software, 1),
    (R7AssetUser, 1),
    (OctopusMachineRole, 1),
    (OctopusVariable, 1),
    (CiSchedule, 1),
    (VsphereSnapshot, 1),
    (VsphereVmDisk, 1),
)


def _seed_containment_fixture(eng) -> None:
    """Seed one row of every source a containment derivation used to read."""
    with Session(eng) as s:
        host = _resource(s, "linux", "host", "app-01")
        win = _resource(s, "windows", "host", "win-01")
        r7host = _resource(s, "rapid7", "host", "r7-01")
        vmres = _resource(s, "vsphere", "vsphere_vm", "vm-01")
        proj = _resource(s, "cicd", "gitlab_project", "grp/proj", {"project_id": 7})
        octo_machine = _resource(s, "octopus", "octopus_machine", "octo-m1")
        octo_project = _resource(s, "octopus", "octopus_project", "octo-p1")

        lh = LinuxHost(
            id=uuid.uuid4(),
            resource_id=host.id,
            distro="Ubuntu 22.04",
            kernel="5.15.0",
            arch="x86_64",
        )
        s.add(lh)
        s.flush()

        # drift + compliance (HAS_DRIFT / ON_FIELD / HAS_VIOLATION / RELATED_TO)
        s.add(DriftEvent(resource_id=host.id, drift_type="config", field="packages", status="open"))
        s.add(DriftEvent(resource_id=host.id, drift_type="config", field="ports", status="open"))
        s.add(
            ComplianceViolation(
                resource_id=host.id,
                rule="PCI-DSS-6.2",
                host="app-01",
                severity="high",
                detail="x",
                status="open",
            )
        )

        # linux OS internals (EXPOSES_PORT / HAS_CRON / HAS_MOUNT /
        # HAS_PENDING_UPDATE / HAS_ACCOUNT / HAS_VENDOR)
        s.add(LinuxPort(host_id=lh.id, port=443, proto="tcp", process="nginx", state="LISTEN"))
        s.add(LinuxPort(host_id=lh.id, port=22, proto="tcp", process="sshd", state="LISTEN"))
        s.add(
            LinuxCron(host_id=lh.id, owner="root", schedule="0 * * * *", command="/usr/bin/backup")
        )
        s.add(LinuxMount(host_id=lh.id, mount="/var", device="/dev/sda1", fstype="ext4"))
        s.add(
            LinuxPendingUpdate(
                host_id=lh.id,
                package="openssl",
                current_version="3.0.1",
                available_version="3.0.2",
            )
        )
        s.add(LinuxUser(host_id=lh.id, username="deploy", shell="/bin/bash"))
        s.add(LinuxNic(host_id=lh.id, name="eth0", mac="00:50:56:AA:BB:CC"))

        # host-scoped security detail (HAS_CERTIFICATE / HAS_SHARE /
        # HAS_FIREWALL_RULE / HAS_SECURITY_POSTURE)
        s.add(
            HostCertificate(
                resource_id=host.id,
                store="LocalMachine\\My",
                thumbprint="AABBCC",
                subject="CN=app-01",
                issuer="CN=corp-ca",
            )
        )
        s.add(HostShare(resource_id=host.id, name="data", share_type="nfs", path="/srv/data"))
        s.add(
            HostFirewallRule(
                resource_id=host.id,
                chain="INPUT",
                rule_text="-p tcp --dport 443 -j ACCEPT",
                action="ACCEPT",
                source="iptables",
            )
        )
        s.add(HostSecurityPosture(resource_id=host.id, firewall_enabled=True))

        # software inventory (HAS_SOFTWARE both grains / TAGGED_AS / AFFECTED_BY)
        # No `publisher=` for the same reason as R7Software.vendor below: the
        # publisher column feeds the KEPT software_title MADE_BY vendor edge.
        s.add(WindowsSoftware(resource_id=win.id, name="7-Zip", version="22.0"))
        r7a = R7Asset(id=uuid.uuid4(), resource_id=r7host.id, r7_asset_id=1)
        s.add(r7a)
        s.flush()
        # NOTE: deliberately no `vendor=`. R7Software.vendor is the source for
        # the software_title MADE_BY vendor edge, which is a GENUINE two-entity
        # relationship and is deliberately KEPT — see TestGenuineEmittersSurvive
        # below, which seeds it on purpose. This fixture is containment-only, so
        # every edge it can still produce is a regression.
        s.add(R7Software(asset_id=r7a.id, r7_asset_id=1, product="openssl", version="3.0.2"))
        s.add(R7AssetUser(asset_id=r7a.id, username="svc_scan"))

        # octopus / cicd containment (HAS_ROLE / HAS_VARIABLE / HAS_SCHEDULE)
        s.add(OctopusMachineRole(role_name="web"))
        s.add(
            OctopusMachine(
                resource_id=octo_machine.id,
                octopus_id="Machines-1",
                name="octo-m1",
                status="Online",
                roles=["web"],
            )
        )
        s.add(
            OctopusProject(
                resource_id=octo_project.id,
                octopus_id="Projects-1",
                name="octo-p1",
                slug="octo-p1",
            )
        )
        s.add(OctopusVariable(owner_type="project", owner_octopus_id="Projects-1", name="ApiKey"))
        s.add(GitlabProject(resource_id=proj.id, gitlab_project_id=7, name="proj"))
        s.add(
            CiSchedule(
                resource_id=proj.id,
                project_id=7,
                schedule_id=1,
                description="nightly",
                cron="0 2 * * *",
            )
        )

        # vSphere containment (HAS_SNAPSHOT / HAS_DISK)
        vm = VsphereVm(
            id=uuid.uuid4(), resource_id=vmres.id, vcenter="vc1", moref="vm-1", name="vm-01"
        )
        s.add(vm)
        s.flush()
        # snapshot_id / disk_key are INTEGER columns. SQLite accepts a string
        # there and PostgreSQL rejects it (InvalidTextRepresentation) — caught
        # by the agent-orm-check gate, which is exactly its purpose.
        s.add(
            VsphereSnapshot(
                vcenter="vc1",
                vm_moref="vm-1",
                vm_name="vm-01",
                snapshot_id=1,
                name="pre-patch",
            )
        )
        s.add(
            VsphereVmDisk(
                vcenter="vc1",
                vm_moref="vm-1",
                vm_name="vm-01",
                disk_key=2000,
                label="Hard disk 1",
                datastore_name="ds1",
                capacity_gb=40,
            )
        )

        s.commit()


# ---------------------------------------------------------------------------
# (a) A full pass writes ZERO resource_relationships rows
# ---------------------------------------------------------------------------


class TestWholeCollectPass:
    """End-to-end ``collect(scope="all")``, not just the deriver.

    The retirement must leave the 2-hourly pass DOING ITS JOB, not merely doing
    less: the declarative engine and entity resolution are what actually build
    the graph now, and a change that made the pass fast by skipping them would
    satisfy every other test in this file.
    """

    def test_collect_still_runs_the_engine_and_the_resolver_and_writes_no_legacy_rows(self, db):
        eng, get_session = db
        _seed_containment_fixture(eng)
        with Session(eng) as s:
            _legacy_store_absent(s)

        agent = _make_agent()
        agent.domain = "graph_maintenance"
        agent.settings.default_zone = ZONE_CORPORATE

        with (
            patch("infra_brain.agents.graph_maintenance.get_session", get_session),
            patch(
                "infra_brain.agents.graph_maintenance.graph_engine.emit_all",
                return_value=({}, []),
            ) as engine_emit,
            patch(
                "infra_brain.agents.graph_maintenance.graph_phase3.resolve_entities",
                return_value={},
            ) as resolve,
            patch(
                "infra_brain.agents.graph_maintenance.graph_phase2.emit_all",
                return_value=({}, []),
            ) as phase2_emit,
            patch(
                "infra_brain.agents.graph_maintenance.graph_role_tagging.emit_all",
                return_value=({}, []),
            ) as role_emit,
            patch("infra_brain.api._seeding.upsert_resource") as upsert,
        ):
            outcome = agent.collect(scope="all")

        assert engine_emit.call_count == 1, "the DECLARATIVE engine must still run"
        assert resolve.call_count == 1, "entity resolution must still run"
        assert phase2_emit.call_count == 1
        assert role_emit.call_count == 1
        assert outcome.count_override == 1

        with Session(eng) as s:
            # Not merely frozen any more — dropped.
            _legacy_store_absent(s)

        # The three maintenance passes ran and reported on graph_edges.
        stats = upsert.call_args.kwargs["metadata"]
        assert stats["pruned"] == 0
        assert stats["decayed"] == 0
        assert stats["contradictory_edges_removed"] == 0
        assert stats["_derivation_version"] == 5, (
            "a change this large to what the pass derives must be distinguishable "
            "in the health history"
        )


class TestNoLegacyStoreWrites:
    """P5: the two tests here used to call ``_populate_typed_relationships``
    and assert (1) no rows and (2) ``emit_edges_batch`` never fired. The method
    is deleted, so both are re-expressed against what remains — which is a
    STRONGER pair of claims, because they now cover the entire pass rather than
    one method inside it."""

    def test_full_pass_writes_zero_resource_relationship_rows(self, db):
        eng, get_session = db
        _seed_containment_fixture(eng)

        with Session(eng) as s:
            _legacy_store_absent(s)  # fixture cannot even hold legacy rows

        agent = _make_agent()
        agent.domain = "graph_maintenance"
        agent.settings.default_zone = ZONE_CORPORATE
        with (
            patch("infra_brain.agents.graph_maintenance.get_session", get_session),
            patch("infra_brain.api._seeding.upsert_resource"),
        ):
            agent.collect(scope="all")

        with Session(eng) as s:
            # No writers left anywhere — nor a table for them to write to.
            _legacy_store_absent(s)

    def test_module_cannot_reach_the_legacy_store_at_all(self, db):
        """Stronger than a rowcount, and stronger than the old mock-count test:
        the write helper is not merely uncalled, it is not importable from the
        module, so no future edit reaches the legacy store without re-adding an
        import a reviewer will see."""
        import infra_brain.agents.graph_maintenance as gm

        assert not hasattr(gm, "emit_edges_batch")
        assert not hasattr(gm, "emit_edge")
        assert not hasattr(gm, "RelationshipType")
        assert not hasattr(gm, "ResourceRelationship")


# ``TestGenuineEmittersSurvive`` was DELETED here (P5, rev11-T5-B). It asserted
# that ``software_title MADE_BY vendor`` — a genuine two-entity relationship,
# deliberately absent from the P3 migration's CONTAINMENT_TYPES — kept deriving
# through the rev10/T3 containment retirement, so an over-eager deletion pass
# would fail loudly instead of quietly emptying the graph. P5 IS the deletion
# pass, and it is a deliberate one: the store is going, so MADE_BY stops being
# derived along with everything else. Its input is untouched
# (``r7_software.vendor``, one row per asset/product), and the disposition for
# it and every other type is recorded in the "3b." epitaph in
# ``agents/graph_maintenance.py``.


# ---------------------------------------------------------------------------
# (c) The containment FACTS are still there — derivation deleted, not collection
# ---------------------------------------------------------------------------


class TestContainmentFactsSurvive:
    def test_detail_tables_intact_after_a_full_pass(self, db):
        eng, get_session = db
        _seed_containment_fixture(eng)

        agent = _make_agent()
        agent.domain = "graph_maintenance"
        agent.settings.default_zone = ZONE_CORPORATE
        with (
            patch("infra_brain.agents.graph_maintenance.get_session", get_session),
            patch("infra_brain.api._seeding.upsert_resource"),
        ):
            agent.collect(scope="all")

        with Session(eng) as s:
            for model, expected in CONTAINMENT_DETAIL_TABLES:
                assert s.query(model).count() == expected, (
                    f"{model.__tablename__} lost rows — the DERIVATION was retired, "
                    "the collection was not"
                )
            # spot-check the values, not just the counts
            port = s.query(LinuxPort).filter_by(port=443).one()
            assert port.proto == "tcp" and port.process == "nginx"
            cert = s.query(HostCertificate).one()
            assert cert.thumbprint == "AABBCC"


# ---------------------------------------------------------------------------
# (b) decay / prune / reconcile now act on graph_edges, honouring authority
# ---------------------------------------------------------------------------


class TestDecayOnGraphEdges:
    def _stale_pair(self, s, label: str):
        a = _node(s, "VsphereVM", f"vc1:{label}-a")
        b = _node(s, "R7Asset", f"{label}-b")
        return a, b

    def test_stale_auto_same_as_decays_in_graph_edges(self, db):
        eng, get_session = db
        with Session(eng) as s:
            a, b = self._stale_pair(s, "decay")
            e = _edge(s, a, b, "SAME_AS", confidence="0.800", age_days=40)
            eid = e.id
            s.commit()

        agent = _make_agent()
        with get_session() as s:
            counts = agent._decay_confidence(s)
            s.commit()

        with Session(eng) as s:
            edge = s.get(GraphEdge, eid)
            assert edge.confidence < Decimal("0.800"), "stale auto SAME_AS must decay"
            assert edge.valid_to is None, "still above the floor — must stay active"
            _legacy_store_absent(s)
        assert counts["decayed"] >= 1

    def test_human_authority_edge_is_never_decayed(self, db):
        eng, get_session = db
        with Session(eng) as s:
            a, b = self._stale_pair(s, "human")
            e = _edge(
                s,
                a,
                b,
                "SAME_AS",
                method="declared",
                confidence="1.000",
                authority="human",
                age_days=400,
            )
            eid = e.id
            s.commit()

        agent = _make_agent()
        with get_session() as s:
            agent._decay_confidence(s)
            s.commit()

        with Session(eng) as s:
            edge = s.get(GraphEdge, eid)
            assert edge.confidence == Decimal("1.000"), "human edge must not decay"
            assert edge.valid_to is None, "human edge must not be retired by decay"
            assert edge.authority == "human"

    def test_structural_declared_edge_is_never_decayed(self, db):
        eng, get_session = db
        with Session(eng) as s:
            a, b = self._stale_pair(s, "structural")
            e = _edge(s, a, b, "HOSTED_ON", method="declared", confidence="1.000", age_days=400)
            eid = e.id
            s.commit()

        agent = _make_agent()
        with get_session() as s:
            agent._decay_confidence(s)
            s.commit()

        with Session(eng) as s:
            edge = s.get(GraphEdge, eid)
            assert edge.confidence == Decimal("1.000")
            assert edge.valid_to is None

    def test_below_floor_is_retired_not_deleted(self, db):
        eng, get_session = db
        with Session(eng) as s:
            a, b = self._stale_pair(s, "floor")
            e = _edge(s, a, b, "SAME_AS", confidence="0.500", age_days=90)
            eid = e.id
            s.commit()

        agent = _make_agent()
        with get_session() as s:
            counts = agent._decay_confidence(s)
            s.commit()

        with Session(eng) as s:
            edge = s.get(GraphEdge, eid)
            assert edge is not None, "graph_edges is bitemporal — decay must never DELETE"
            assert edge.valid_to is not None, "below the floor the edge must be RETIRED"
        assert counts["retired"] >= 1


class TestPruneOnGraphEdges:
    def test_edge_whose_declared_node_lost_its_resource_is_retired(self, db):
        eng, get_session = db
        with Session(eng) as s:
            # "GitlabProject" is declared by cicd's AgentSpec with
            # resource_backed=True, so a NULL resource_id can only mean the FK's
            # ON DELETE SET NULL fired.
            orphan = _node(s, "GitlabProject", "orphan", resource_id=None, source="cicd")
            live = _node(s, "AnsiblePlaybook", "live", resource_id=None, source="iac")
            e = _edge(s, orphan, live, "DEFINED_IN", method="declared", confidence="1.000")
            eid = e.id
            s.commit()

        agent = _make_agent()
        with get_session() as s:
            result = agent._prune_stale_edges(s)
            s.commit()

        with Session(eng) as s:
            edge = s.get(GraphEdge, eid)
            assert edge is not None, "prune must retire, never DELETE"
            assert edge.valid_to is not None
        assert result["pruned"] >= 1

    def test_prune_never_touches_a_human_edge(self, db):
        eng, get_session = db
        with Session(eng) as s:
            orphan = _node(s, "GitlabProject", "orphan-h", resource_id=None, source="cicd")
            other = _node(s, "GitlabProject", "orphan-h2", resource_id=None, source="cicd")
            e = _edge(
                s,
                orphan,
                other,
                "SAME_AS",
                method="declared",
                confidence="1.000",
                authority="human",
            )
            eid = e.id
            s.commit()

        agent = _make_agent()
        with get_session() as s:
            agent._prune_stale_edges(s)
            s.commit()

        with Session(eng) as s:
            assert s.get(GraphEdge, eid).valid_to is None, "human edge is never pruned"


class TestContradictionReconcileOnGraphEdges:
    def test_auto_same_as_contradicted_by_human_veto_is_retired(self, db):
        eng, get_session = db
        with Session(eng) as s:
            a = _node(s, "VsphereVM", "vc1:contra-a")
            b = _node(s, "R7Asset", "contra-b")
            auto = _edge(s, a, b, "SAME_AS", confidence="0.800")
            _edge(
                s,
                b,
                a,
                "NOT_SAME_AS",
                method="declared",
                confidence="1.000",
                authority="human",
            )
            auto_id = auto.id
            s.commit()

        agent = _make_agent()
        with get_session() as s:
            result = agent._reconcile_contradictory_edges(s)
            s.commit()

        with Session(eng) as s:
            assert s.get(GraphEdge, auto_id).valid_to is not None, (
                "an auto SAME_AS may not coexist with an active human NOT_SAME_AS"
            )
        assert result["removed"] >= 1

    def test_human_not_same_as_is_never_retired(self, db):
        eng, get_session = db
        with Session(eng) as s:
            a = _node(s, "VsphereVM", "vc1:keep-a")
            b = _node(s, "R7Asset", "keep-b")
            veto = _edge(
                s,
                a,
                b,
                "NOT_SAME_AS",
                method="declared",
                confidence="1.000",
                authority="human",
            )
            vid = veto.id
            s.commit()

        agent = _make_agent()
        with get_session() as s:
            agent._reconcile_contradictory_edges(s)
            s.commit()

        with Session(eng) as s:
            assert s.get(GraphEdge, vid).valid_to is None
