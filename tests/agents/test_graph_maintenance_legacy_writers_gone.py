"""P5 / rev11-T5-B — the SURGICAL legacy-edge writers are gone.

P4 pointed every reader at ``graph_edges``; P5 removes the writers so the
legacy ``resource_relationships`` table can be dropped. rev11-T5-A took the
files whose edge emission was a whole method; THIS file pins the surgical half
— five methods where the legacy edge write was *interleaved* with work that
must survive:

===========================================================================
file / method                              extracted            kept
===========================================================================
agents/vsphere.py::_write_inventory        RUNS_ON/MEMBER_OF/   the 7 vSphere
                                           IN_DATACENTER        detail tables
agents/container_registry.py               PULLED_FROM,         container_images
  ::_write_container_registry_details      HAS_VULNERABILITY_   rows + the
                                           SCAN                 registry Resource
agents/loadbalancer.py::_write_lb_details  ROUTES_TO,           lb_instances /
                                           MEMBER_OF_POOL,      lb_pools /
                                           TERMINATES_TLS_FOR   lb_pool_members /
                                                                lb_virtual_servers
agents/pki.py::_write_pki_registry         CHAINS_TO, HAS_CRL,  CA rows, the
                                           HAS_OCSP_RESPONDER   declared-node
                                                                certificate rows,
                                                                and the CRL/OCSP
                                                                probe STATUS
agents/graph_maintenance.py                the whole typed-     prune / decay /
                                           relationship +       reconcile on
                                           convergence-node +   graph_edges, the
                                           vSphere-topology     declarative engine
                                           derivation           and the resolver
===========================================================================

Two load-bearing claims:

(a) **No call site remains.** A source scan of the five files finds no
    ``emit_edge``/``emit_edges_batch`` call, and none of the five modules
    still carries the imported name. A rowcount assertion alone would pass
    on a fixture that merely failed to reach the write.

(b) **graph_maintenance still does its job.** ``collect(scope="all")`` over a
    population that used to produce VULNERABLE_TO / DEPLOYS_TO / RUNS_EOL /
    MEMBER_OF / vSphere-topology edges writes ZERO legacy rows while the
    declarative engine, the entity resolver and the two Phase-2 emitters all
    still fire, and prune/decay/reconcile still report on ``graph_edges``. A
    change that made the pass "clean" by skipping the engine would satisfy a
    rowcount test and destroy the graph.

EPITAPHS — FOUR TEST FILES DELETED WHOLE, because every test in them drove code
that no longer exists. Recorded here rather than left to `git log`, so a reader
wondering "was that coverage replaced or just dropped?" gets an answer:

  tests/agents/test_graph_convergence_nodes.py        (2,076 lines, 103 tests)
      Every TRK-103 convergence block: the CVE/vendor/subnet/os_version/
      vmdk/pool/site/inventory-group/team/vlan/k8s/linux-service nodes and
      their edges, plus the vSphere fork bridge and ghost retirement. All of
      it drove ``_populate_convergence_nodes``. NOT REPLACED, deliberately —
      the nodes were edge anchors nothing else read (proved by grep before
      deletion, see the "3b." epitaph), so there is no surviving behaviour to
      re-point the tests at. The two facts that DO survive (ansible group
      membership, EOL product) are covered by their own detail-table tests.

  tests/agents/test_graph_vsphere_topology.py         (348 lines)
      ``_populate_vsphere_topology`` and its metadata fallback. Retired
      domain, zero live rows, method deleted.

  tests/agents/test_graph_maintenance_gating.py       (398 lines)
      TRK-117 Phase-2 freshness gating. The gate existed only to decide
      whether a typed-relationship block had to re-derive; with no blocks
      left there is nothing to gate and the four gate methods are deleted.

  tests/agents/test_graph_maintenance_backfill.py     (784 lines)
      DEPLOYED_VIA / TRIGGERED_DEPLOYMENT / PROVISIONS / RUNS_EOL derivation
      + a "DEFINED_IN is no longer derived here" pair. One claim in it
      survived its file — "the declaration that replaced DEFINED_IN is still
      registered" — and is already asserted twice over, by
      ``tests/test_declared_vs_emitted_edges.py::
      test_migrated_types_are_actually_declared_somewhere`` and by
      ``tests/agents/test_iac_defined_in_graph.py::
      test_iac_declares_defined_in_for_every_file_classification``.
"""

import re
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

import infra_brain.agents.container_registry as container_registry_mod
import infra_brain.agents.graph_maintenance as graph_maintenance_mod
import infra_brain.agents.loadbalancer as loadbalancer_mod
import infra_brain.agents.pki as pki_mod
import infra_brain.agents.vsphere as vsphere_mod
from infra_brain.agents.graph_maintenance import GraphMaintenanceAgent
from infra_brain.db.models import (
    ZONE_CORPORATE,
    AnsibleInventoryGroup,
    AnsibleInventoryHost,
    EolRegistry,
    IacFile,
    OctopusEnvironment,
    OctopusMachine,
    R7VulnCve,
    R7Vulnerability,
    Resource,
    VsphereHost,
    VsphereVm,
    VulnQueueItem,
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
# (a) source-level proof
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[2] / "src" / "infra_brain"

#: The five surgical files. Each keeps its method; only the edge write leaves.
SURGICAL_FILES = (
    "agents/vsphere.py",
    "agents/container_registry.py",
    "agents/loadbalancer.py",
    "agents/pki.py",
    "agents/graph_maintenance.py",
)

_EMIT_CALL = re.compile(r"\bemit_edges?(?:_batch)?\s*\(")

SURGICAL_MODULES = {
    "agents/vsphere.py": vsphere_mod,
    "agents/container_registry.py": container_registry_mod,
    "agents/loadbalancer.py": loadbalancer_mod,
    "agents/pki.py": pki_mod,
    "agents/graph_maintenance.py": graph_maintenance_mod,
}


@pytest.mark.parametrize("relpath", SURGICAL_FILES)
def test_no_legacy_emit_call_site_remains(relpath):
    """grep proof: not one ``emit_edge(...)`` / ``emit_edges_batch(...)`` call."""
    text = (_SRC / relpath).read_text(encoding="utf-8")
    hits = [
        f"{relpath}:{n}: {line.strip()}"
        for n, line in enumerate(text.splitlines(), start=1)
        if _EMIT_CALL.search(line)
    ]
    assert hits == [], "legacy resource_relationships emitter still called:\n" + "\n".join(hits)


@pytest.mark.parametrize("relpath", SURGICAL_FILES)
def test_module_no_longer_imports_the_legacy_emitter(relpath):
    """Stronger than the grep: the NAME must be gone from the module too, so a
    future edit cannot reach the legacy store without re-adding the import."""
    mod = SURGICAL_MODULES[relpath]
    leftovers = [n for n in ("emit_edge", "emit_edges_batch") if hasattr(mod, n)]
    assert leftovers == [], f"{relpath} still imports {leftovers}"


# ---------------------------------------------------------------------------
# (b) graph_maintenance still does its job
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    eng = make_engine()

    @contextmanager
    def _get_session():
        with Session(eng) as s:
            yield s

    return eng, _get_session


def _make_agent() -> GraphMaintenanceAgent:
    agent = GraphMaintenanceAgent.__new__(GraphMaintenanceAgent)
    agent.settings = MagicMock()
    agent.settings.is_same_as_decay_days = 14
    agent.settings.graph_edge_decay_enabled = True
    agent.settings.default_zone = ZONE_CORPORATE
    agent.callbacks = []
    agent.domain = "graph_maintenance"
    agent._maint_errors = []
    return agent


def _resource(s, domain, rtype, name, metadata=None) -> Resource:
    r = Resource(
        id=uuid.uuid4(),
        domain=domain,
        type=rtype,
        name=name,
        source="test",
        zone=ZONE_CORPORATE,
        metadata_=metadata,
    )
    s.add(r)
    s.flush()
    return r


def _seed_derivable_population(eng) -> None:
    """Seed exactly the shapes the DELETED derivations used to turn into edges.

    Every one of these produced a ``resource_relationships`` row on this base:
    VULNERABLE_TO (vuln queue ⋈ r7), DEPLOYS_TO (octopus), RUNS_EOL (eol
    registry), MEMBER_OF (ansible inventory group), and the vSphere RUNS_ON
    topology. If any still does, claim (b) fails loudly with the type names.
    """
    with Session(eng) as s:
        host = _resource(s, "linux", "linux_host", "web01")
        vuln_res = _resource(s, "vuln", "vulnerability", "rhel-cve-2024-1")

        s.add(
            VulnQueueItem(
                id=uuid.uuid4(),
                resource_id=host.id,
                cve_id="CVE-2024-0001",
                severity="high",
            )
        )
        s.add(R7VulnCve(id=uuid.uuid4(), r7_vuln_id="rhel-1", cve_id="CVE-2024-0001"))
        s.add(
            R7Vulnerability(
                id=uuid.uuid4(),
                r7_vuln_id="rhel-1",
                title="t",
                resource_id=vuln_res.id,
            )
        )

        # DEPLOYS_TO
        m_res = _resource(s, "octopus", "octopus_machine", "octo-m1")
        e_res = _resource(s, "octopus", "octopus_environment", "Production")
        s.add(
            OctopusMachine(
                id=uuid.uuid4(),
                octopus_id="Machines-1",
                name="octo-m1",
                status="Online",
                resource_id=m_res.id,
                environment_ids=["Environments-1"],
                roles=["web"],
            )
        )
        s.add(
            OctopusEnvironment(
                id=uuid.uuid4(),
                octopus_id="Environments-1",
                name="Production",
                resource_id=e_res.id,
            )
        )

        # RUNS_EOL
        s.add(
            EolRegistry(
                id=uuid.uuid4(),
                asset_name="Ubuntu 18.04",
                resource_id=host.id,
                eol_date=date(2023, 5, 31),
            )
        )

        # MEMBER_OF (ansible inventory group)
        iac = IacFile(
            id=uuid.uuid4(),
            gitlab_project_id=1,
            path="inventories/prod/hosts",
            file_type="inventory",
            ref="main",
        )
        s.add(iac)
        s.flush()
        grp = AnsibleInventoryGroup(id=uuid.uuid4(), iac_file_id=iac.id, name="webservers")
        s.add(grp)
        s.flush()
        s.add(AnsibleInventoryHost(id=uuid.uuid4(), group_id=grp.id, name="web01"))

        # vSphere topology
        vm_res = _resource(s, "vsphere", "vsphere_vm", "vm-a")
        esx_res = _resource(s, "vsphere", "vsphere_host", "esx01")
        s.add(
            VsphereVm(
                id=uuid.uuid4(),
                vcenter="vc01",
                name="vm-a",
                moref="vm-1",
                resource_id=vm_res.id,
                esxi_host="esx01",
            )
        )
        s.add(
            VsphereHost(
                id=uuid.uuid4(),
                vcenter="vc01",
                name="esx01",
                moref="host-1",
                resource_id=esx_res.id,
            )
        )
        s.commit()


class TestFullPassWritesNothingLegacyAndStillBuildsTheGraph:
    def test_collect_writes_zero_legacy_rows_and_still_runs_engine_and_resolver(self, db):
        eng, get_session = db
        _seed_derivable_population(eng)

        with Session(eng) as s:
            _legacy_store_absent(s)  # fixture cannot even hold legacy rows

        agent = _make_agent()
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
            _legacy_store_absent(s)  # P5 removed every writer AND the store

        stats = upsert.call_args.kwargs["metadata"]
        # The graph_edges maintenance passes still ran and reported.
        assert stats["pruned"] == 0
        assert stats["decayed"] == 0
        assert stats["contradictory_edges_removed"] == 0
        assert stats["_derivation_version"] == 5, (
            "removing the entire legacy derivation is the largest change to what "
            "this pass derives since gating was added — it must bump the version"
        )

    def test_quick_scope_also_writes_nothing_legacy(self, db):
        eng, get_session = db
        _seed_derivable_population(eng)

        agent = _make_agent()
        with (
            patch("infra_brain.agents.graph_maintenance.get_session", get_session),
            patch("infra_brain.api._seeding.upsert_resource"),
        ):
            agent.collect(scope="quick")

        with Session(eng) as s:
            _legacy_store_absent(s)

    def test_no_convergence_resource_rows_are_minted_any_more(self, db):
        """The convergence nodes existed ONLY to anchor the deleted edges.

        Nothing in ``src/`` reads ``vuln/cve``, ``network/subnet``,
        ``ansible/inventory_group``, … (the sole reader of
        ``identity/user_account`` was identity.py's IS_PRINCIPAL_FOR emitter,
        deleted in the sibling rev11-T5-A wave), so they go with their edges
        rather than accumulating as rows no query reaches.
        """
        eng, get_session = db
        _seed_derivable_population(eng)
        with Session(eng) as s:
            before = s.query(Resource).count()

        agent = _make_agent()
        with (
            patch("infra_brain.agents.graph_maintenance.get_session", get_session),
            patch(
                "infra_brain.agents.graph_maintenance.graph_engine.emit_all",
                return_value=({}, []),
            ),
            patch(
                "infra_brain.agents.graph_maintenance.graph_phase3.resolve_entities",
                return_value={},
            ),
            patch(
                "infra_brain.agents.graph_maintenance.graph_phase2.emit_all",
                return_value=({}, []),
            ),
            patch(
                "infra_brain.agents.graph_maintenance.graph_role_tagging.emit_all",
                return_value=({}, []),
            ),
            patch("infra_brain.api._seeding.upsert_resource"),
        ):
            agent.collect(scope="all")

        with Session(eng) as s:
            after = s.query(Resource).count()
            minted = (
                s.query(Resource.domain, Resource.type, Resource.name)
                .filter(Resource.source == "graph_maintenance")
                .all()
            )
        assert after == before, f"pass minted {after - before} Resource row(s): {minted}"
