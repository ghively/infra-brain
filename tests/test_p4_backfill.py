"""Tests for the P4 backfill planner (migration ``c7a41f0b93de``).

Same shape as ``tests/test_p3_backfill.py`` — the migration's decisions live in
a pure, database-free planning layer because the interesting behaviour is
*which* legacy rows become *what kind of* edge, and that is a function of the
rows, not of Postgres.

The module is loaded by path and NOT registered in ``sys.modules``, EXACTLY the
way alembic loads it. That is a regression guard, not an oversight: ``@dataclass``
on a module carrying ``from __future__ import annotations`` resolves its string
annotations through ``sys.modules[cls.__module__]``, which is ``None`` for a
path-loaded module — so a future import in the migration would blow up during
``alembic upgrade`` and nowhere else. Registering it here would hide that.

WHAT IS DIFFERENT FROM P3, AND THEREFORE WHAT THIS FILE IS REALLY FOR
---------------------------------------------------------------------
P3 copied each legacy row's endpoints straight across. P4 cannot, for
``ANSIBLE_MANAGES``: the deriver stored ``gitlab_project → linux_host`` and the
declaration stores ``ansible_inventory_file → linux_host`` (TRK-354 Option A).
So the bulk of what follows exercises the RE-ANCHOR — that it finds the right
witness, that it refuses rather than guesses when there is none, that the
rename doubling collapses onto one edge, and that a retired *project* row never
closes an edge it is not an endpoint of.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "c7a41f0b93de_p4_backfill_last_declared_relationships.py"
)
_spec = importlib.util.spec_from_file_location("_p4_backfill_migration", _MIGRATION_PATH)
assert _spec is not None and _spec.loader is not None
p4 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p4)  # NB: intentionally not registered in sys.modules


#: The live shape's timestamps: the pre-rename sweep, the post-rename one, and
#: a hypothetical later engine pass.
T_OLD = datetime(2026, 8, 1, 6, 25, tzinfo=timezone.utc)
T_NEW = datetime(2026, 8, 11, 6, 20, tzinfo=timezone.utc)
T_RENAME = datetime(2026, 8, 11, 2, 6, tzinfo=timezone.utc)
T_ENGINE = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)

PROJECT_ID = 113


# ---------------------------------------------------------------------------
# Builders — the live rows, spelled the way the database spells them
# ---------------------------------------------------------------------------


def project(rid="r-proj-new", name="infra-brain/iac/homelab-ansible", retired=None):
    return p4.ResourceRow(
        id=rid,
        domain="cicd",
        type="gitlab_project",
        name=name,
        retired_at=retired,
        metadata={"project_id": PROJECT_ID, "default_branch": "main"},
    )


def inventory_file(rid="r-inv", name="homelab-ansible/inventory/inventory.yml", retired=None):
    return p4.ResourceRow(
        id=rid,
        domain="iac",
        type="ansible_inventory_file",
        name=name,
        retired_at=retired,
        metadata={
            "project_id": PROJECT_ID,
            "file_path": "inventory/inventory.yml",
            "ref": "main",
        },
    )


def host(rid="r-host", name="node_a", retired=None):
    return p4.ResourceRow(
        id=rid, domain="linux", type="linux_host", name=name, retired_at=retired, metadata={}
    )


def certificate(rid="r-cert", thumb="E1375F2F", issuer="CN = vmi-example-runner.contaboserver.net"):
    return p4.ResourceRow(
        id=rid,
        domain="security",
        type="certificate",
        name=thumb,
        retired_at=None,
        metadata={"thumbprint": thumb, "subject": issuer, "issuer": issuer},
    )


def ca(rid="r-ca", name="CN = vmi-example-runner.contaboserver.net"):
    return p4.ResourceRow(
        id=rid,
        domain="pki",
        type="certificate_authority",
        name=name,
        retired_at=None,
        metadata={"ca_type": "root", "issuer": name},
    )


def legacy(
    rid,
    rel_type,
    frm,
    to,
    *,
    created_at=T_OLD,
    last_seen=None,
    confidence=0.9,
    status="active",
    source="iac_collector",
):
    return p4.LegacyRow(
        id=rid,
        relationship_type=rel_type,
        source=source,
        confidence=confidence,
        status=status,
        created_at=created_at,
        last_seen=last_seen or created_at,
        from_resource=frm,
        to_resource=to,
    )


def witnesses(pairs):
    """``{(project_id, host_key): [inventory ResourceRow]}`` from a plain dict."""
    return {(PROJECT_ID, p4.host_match_key(hostname)): files for hostname, files in pairs.items()}


def run(
    rows,
    *,
    active_edges=None,
    existing_backfill=None,
    existing_nodes=None,
    project_ids=None,
    inv=None,
):
    return p4.plan_backfill(
        rows,
        active_edges=active_edges or {},
        existing_backfill=existing_backfill or set(),
        existing_nodes=existing_nodes if existing_nodes is not None else set(),
        project_id_by_resource=project_ids or {},
        inventory_witnesses=inv or {},
    )


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_the_allow_list_is_exactly_the_two_newly_declared_types():
    """P4 carries what P3 could not, and nothing else.

    The allow-list shape is P3's and the reason is unchanged: an edge type
    nothing declares has no maintainer, so backfilling it would mint
    ``authority='auto'`` rows nothing will ever refresh or retire.
    """
    assert p4.MIGRATABLE_TYPES == {"ANSIBLE_MANAGES", "ISSUED_BY"}
    assert p4.classify("ANSIBLE_MANAGES") == "migrate"
    assert p4.classify("ISSUED_BY") == "migrate"


def test_the_still_undeclared_types_are_counted_not_copied():
    """MEMBER_OF / TRIGGERED_BY / RUNS_EOL stay in the legacy store.

    All three were judged genuine-but-blocked in this pass (see
    docs/TRACKER.md); none has a declaration, so none has a maintainer.
    """
    for rel_type in ("MEMBER_OF", "TRIGGERED_BY", "RUNS_EOL"):
        assert p4.classify(rel_type) == p4.SKIPPED_UNDECLARED


def test_p3s_types_are_counted_separately_from_undeclared_ones():
    """A row P3 already carried is not a gap; the report must not read like one."""
    for rel_type in ("BELONGS_TO", "DEFINED_IN", "RUNS_IMAGE"):
        assert p4.classify(rel_type) == p4.SKIPPED_P3
    assert p4.classify("HAS_DRIFT") == p4.SKIPPED_CONTAINMENT


def test_every_legacy_row_lands_in_exactly_one_bucket():
    inv_file = inventory_file()
    rows = [
        legacy("a", "ANSIBLE_MANAGES", project(), host()),
        legacy("b", "ISSUED_BY", certificate(), ca()),
        legacy("c", "BELONGS_TO", inv_file, project()),
        legacy("d", "HAS_DRIFT", host(), host(rid="r-drift")),
        legacy("e", "MEMBER_OF", host(), host(rid="r-group")),
    ]
    plan = run(
        rows,
        project_ids={"r-proj-new": PROJECT_ID},
        inv=witnesses({"node_a": [inv_file]}),
    )
    assert plan.total() == len(rows)


# ---------------------------------------------------------------------------
# The re-anchor
# ---------------------------------------------------------------------------


def test_the_reanchored_edge_does_not_run_between_the_legacy_endpoints():
    """The whole reason P4 needed new machinery, asserted directly."""
    inv_file = inventory_file()
    plan = run(
        [legacy("a", "ANSIBLE_MANAGES", project(), host())],
        project_ids={"r-proj-new": PROJECT_ID},
        inv=witnesses({"node_a": [inv_file]}),
    )

    (edge,) = plan.edges
    assert edge.source_identity == ("AnsibleInventoryFile", inv_file.name)
    assert edge.target_identity == ("LinuxHost", "node_a")
    assert edge.edge_type == "ANSIBLE_MANAGES"
    # And the row says so, so a reader who greps the legacy id is not left
    # wondering why the ends disagree.
    assert edge.evidence["reanchored"]["legacy_from_name"] == "infra-brain/iac/homelab-ansible"
    assert edge.evidence["reanchored"]["resolved_from_name"] == inv_file.name


def test_a_legacy_row_with_no_inventory_witness_is_refused_not_guessed():
    """A plays-half-only assertion about a host no inventory lists.

    The re-anchored edge cannot make that assertion, so nothing is written and
    the row is counted. Writing it from some other inventory file in the same
    project would be inventing a fact.
    """
    plan = run(
        [legacy("a", "ANSIBLE_MANAGES", project(), host(name="orphan_host"))],
        project_ids={"r-proj-new": PROJECT_ID},
        inv=witnesses({"node_a": [inventory_file()]}),
    )

    assert plan.edges == []
    assert plan.counts["ANSIBLE_MANAGES"] == {p4.SKIPPED_NO_WITNESS: 1}


def test_a_retired_project_row_still_resolves_through_its_metadata_project_id():
    """THE bug the first scratch replay caught: half the population vanished.

    cicd's L-8b rename REPOINTED ``gitlab_projects.resource_id`` at the new
    ``resources`` row, so the retired pre-rename row — the one carrying the
    earlier ``created_at`` this backfill exists to recover — has no
    ``gitlab_projects`` row at all. Resolving only through that table skipped 7
    of the live 14 rows and dated every survivor from the rename.

    ``resources.metadata['project_id']`` is the SAME immutable key
    ``agents/iac.py``'s BELONGS_TO declaration joins on, chosen there for
    exactly this property.
    """
    inv_file = inventory_file()
    old_project = project(rid="r-proj-old", name="homelab-ansible", retired=T_RENAME)
    plan = run(
        [legacy("old", "ANSIBLE_MANAGES", old_project, host(), created_at=T_OLD)],
        project_ids={},  # the rename left this row out of gitlab_projects
        inv=witnesses({"node_a": [inv_file]}),
    )

    (edge,) = plan.edges
    assert edge.valid_from == T_OLD
    assert edge.source_identity == ("AnsibleInventoryFile", inv_file.name)


def test_a_project_endpoint_with_no_id_at_all_is_refused():
    """Neither route resolves → no descent, and the row is counted not guessed."""
    anonymous = p4.ResourceRow(
        id="r-nameless",
        domain="cicd",
        type="gitlab_project",
        name="mystery",
        retired_at=None,
        metadata={},
    )
    plan = run(
        [legacy("a", "ANSIBLE_MANAGES", anonymous, host())],
        project_ids={},
        inv=witnesses({"node_a": [inventory_file()]}),
    )
    assert plan.edges == []
    assert plan.counts["ANSIBLE_MANAGES"] == {p4.SKIPPED_NO_WITNESS: 1}


def test_the_project_id_is_compared_as_an_integer_not_a_string():
    """``iac_files.gitlab_project_id`` is an int column; JSON metadata is not.

    A str/int mismatch here would have looked exactly like "no witness" — a
    silent, fully-counted, entirely wrong skip.
    """
    assert (
        p4.project_id_for(
            p4.ResourceRow("r", "cicd", "gitlab_project", "p", None, {"project_id": "113"}), {}
        )
        == 113
    )
    assert (
        p4.project_id_for(
            p4.ResourceRow(
                "r", "cicd", "gitlab_project", "p", None, {"project_id": "not-a-number"}
            ),
            {},
        )
        is None
    )


def test_the_gitlab_projects_table_wins_when_it_has_an_answer():
    """The fallback is a fallback; the table is the authority when present."""
    resource = project()
    assert p4.project_id_for(resource, {"r-proj-new": 999}) == 999


def test_the_host_key_fold_matches_the_declared_normalizer():
    """The re-anchor must fold hostnames the way the EdgeSpec declares.

    ``key_normalizer="host"`` is ``graph_match_key``: lowercase, short-name,
    underscore folded to hyphen, placeholder names rejected. A migration that
    matched differently would resolve a different witness set than the live
    edge does.
    """
    assert p4.host_match_key("node-a") == p4.host_match_key("node_a") == "node-a"
    assert p4.host_match_key("node_a.corp.example.com.") == "node-a"
    assert p4.host_match_key("localhost") == ""
    assert p4.host_match_key("10.0.0.42") == "10.0.0.42", "an IP is a whole identity"


def test_the_fold_actually_bridges_the_two_spellings_end_to_end():
    inv_file = inventory_file()
    plan = run(
        # The linux_host resource spelled with a hyphen; the inventory witness
        # key is the FOLDED form ``host_match_key`` would compute from the raw
        # underscore-spelled inventory row — the case the fold exists for.
        [legacy("a", "ANSIBLE_MANAGES", project(), host(name="node-a"))],
        project_ids={"r-proj-new": PROJECT_ID},
        inv={(PROJECT_ID, "node-a"): [inv_file]},
    )
    assert len(plan.edges) == 1


def test_two_inventory_files_naming_one_host_are_two_true_assertions():
    """Each file genuinely asserts it, so each gets an edge — as the engine would."""
    a = inventory_file(rid="r-inv-a", name="repo/inventories/prod/hosts.yml")
    b = inventory_file(rid="r-inv-b", name="repo/inventories/dr/hosts.yml")
    plan = run(
        [legacy("a", "ANSIBLE_MANAGES", project(), host())],
        project_ids={"r-proj-new": PROJECT_ID},
        inv=witnesses({"node_a": [a, b]}),
    )
    assert {e.source_identity[1] for e in plan.edges} == {a.name, b.name}


# ---------------------------------------------------------------------------
# The rename doubling
# ---------------------------------------------------------------------------


def test_the_rename_doubling_collapses_onto_one_edge_keeping_the_earlier_start():
    """14 legacy rows, 7 assertions: both project rows re-anchor onto one file.

    The retired pre-rename row carries the TRUE first observation, which the
    legacy store's ``UNIQUE(from, to, type)`` could express only by accident of
    which row survived. Recovering it is the point of the whole backfill.
    """
    inv_file = inventory_file()
    old_project = project(rid="r-proj-old", name="homelab-ansible", retired=T_RENAME)
    rows = [
        legacy("old", "ANSIBLE_MANAGES", old_project, host(), created_at=T_OLD),
        legacy("new", "ANSIBLE_MANAGES", project(), host(), created_at=T_NEW),
    ]
    plan = run(
        rows,
        project_ids={"r-proj-old": PROJECT_ID, "r-proj-new": PROJECT_ID},
        inv=witnesses({"node_a": [inv_file]}),
    )

    (edge,) = plan.edges
    assert edge.valid_from == T_OLD
    assert edge.evidence["merged_legacy_rows"] == 2
    assert sorted(edge.evidence["legacy_relationship_ids"]) == ["new", "old"]
    assert plan.counts["ANSIBLE_MANAGES"][p4.MERGED] == 1


def test_a_retired_project_row_does_not_close_the_reanchored_edge():
    """THE divergence from P3's ``_closing_time``, and the bug it prevents.

    The retired ``gitlab_project`` row is not an endpoint of the edge being
    written. Closing on its ``retired_at`` would record that Ansible stopped
    managing a host on the day a repo was renamed, which is false.
    """
    inv_file = inventory_file()
    old_project = project(rid="r-proj-old", name="homelab-ansible", retired=T_RENAME)
    plan = run(
        [legacy("old", "ANSIBLE_MANAGES", old_project, host(), created_at=T_OLD)],
        project_ids={"r-proj-old": PROJECT_ID},
        inv=witnesses({"node_a": [inv_file]}),
    )

    (edge,) = plan.edges
    assert edge.valid_to is None, "still true — the repo was renamed, not the inventory"
    assert edge.disposition == p4.MIGRATED_ACTIVE


def test_a_retired_resolved_endpoint_does_close_it():
    """The rule is not "ignore retirement" — it is "consult the RIGHT endpoints"."""
    retired_inv = inventory_file(retired=T_RENAME)
    plan = run(
        [legacy("old", "ANSIBLE_MANAGES", project(), host(), created_at=T_OLD)],
        project_ids={"r-proj-new": PROJECT_ID},
        inv=witnesses({"node_a": [retired_inv]}),
    )

    (edge,) = plan.edges
    assert edge.valid_to == T_RENAME
    assert edge.disposition == p4.MIGRATED_CLOSED


# ---------------------------------------------------------------------------
# ISSUED_BY takes the plain path
# ---------------------------------------------------------------------------


def test_issued_by_is_copied_endpoint_for_endpoint():
    """No re-anchor: pki declares the edge with exactly the deriver's ends."""
    plan = run([legacy("c", "ISSUED_BY", certificate(), ca(), source="PKIAgent")])

    (edge,) = plan.edges
    assert edge.source_identity == ("Certificate", "E1375F2F")
    assert edge.target_identity == ("CertificateAuthority", "CN = vmi-example-runner.contaboserver.net")
    assert "reanchored" not in edge.evidence
    assert edge.source == "p4_backfill:PKIAgent"


def test_issued_by_needs_no_witness_tables_at_all():
    """A deployment whose iac tables are empty still backfills pki's edges."""
    plan = run([legacy("c", "ISSUED_BY", certificate(), ca())], project_ids={}, inv={})
    assert len(plan.edges) == 1


def test_both_certificate_nodes_are_minted_because_pkis_declaration_is_new():
    """pki declared nothing before this branch, so its nodes do not exist yet."""
    plan = run([legacy("c", "ISSUED_BY", certificate(), ca())], existing_nodes=set())

    assert {n.node_type for n in plan.nodes} == {"Certificate", "CertificateAuthority"}
    cert_node = next(n for n in plan.nodes if n.node_type == "Certificate")
    assert cert_node.source == "pki", "declared BY pki even though the row is in `security`"
    assert cert_node.resource_id == "r-cert", "both ends are inventory items"
    assert cert_node.attributes["issuer"] == "CN = vmi-example-runner.contaboserver.net"


# ---------------------------------------------------------------------------
# Prehistory, idempotence, honesty
# ---------------------------------------------------------------------------


def test_an_existing_active_engine_edge_turns_the_row_into_closed_prehistory():
    inv_file = inventory_file()
    triple = (("AnsibleInventoryFile", inv_file.name), ("LinuxHost", "node_a"), "ANSIBLE_MANAGES")
    plan = run(
        [legacy("a", "ANSIBLE_MANAGES", project(), host(), created_at=T_OLD)],
        active_edges={triple: T_ENGINE},
        project_ids={"r-proj-new": PROJECT_ID},
        inv=witnesses({"node_a": [inv_file]}),
    )

    (edge,) = plan.edges
    assert (edge.valid_from, edge.valid_to) == (T_OLD, T_ENGINE)
    assert edge.disposition == p4.MIGRATED_HISTORY
    assert edge.evidence["superseded_by_active_edge_valid_from"] == T_ENGINE.isoformat()


def test_no_prehistory_is_skipped_rather_than_written_as_a_lie():
    """Re-running after the engine adopted the row must write nothing.

    This is the idempotence path that matters for THIS wave specifically: it
    writes ACTIVE edges, the engine refreshes them in place (keeping
    ``valid_from``), and a second upgrade then sees an active edge starting no
    later than the legacy row.
    """
    inv_file = inventory_file()
    triple = (("AnsibleInventoryFile", inv_file.name), ("LinuxHost", "node_a"), "ANSIBLE_MANAGES")
    plan = run(
        [legacy("a", "ANSIBLE_MANAGES", project(), host(), created_at=T_OLD)],
        active_edges={triple: T_OLD},
        project_ids={"r-proj-new": PROJECT_ID},
        inv=witnesses({"node_a": [inv_file]}),
    )

    assert plan.edges == []
    assert plan.counts["ANSIBLE_MANAGES"] == {p4.SKIPPED_NO_PREHISTORY: 1}


def test_a_row_already_backfilled_is_counted_once_and_not_rewritten():
    inv_file = inventory_file()
    triple = (("AnsibleInventoryFile", inv_file.name), ("LinuxHost", "node_a"), "ANSIBLE_MANAGES")
    plan = run(
        [legacy("a", "ANSIBLE_MANAGES", project(), host())],
        existing_backfill={triple},
        project_ids={"r-proj-new": PROJECT_ID},
        inv=witnesses({"node_a": [inv_file]}),
    )

    assert plan.edges == []
    assert plan.counts["ANSIBLE_MANAGES"] == {p4.SKIPPED_ALREADY: 1}


def test_confidence_is_the_weakest_supporting_evidence_and_method_follows_it():
    """P3's rule, unchanged: an edge is only as strong as its weakest evidence.

    The live data makes this concrete — the inventory half claimed 0.9 and the
    plays half 0.8 for the same pair, and the legacy store kept whichever wrote
    last. Merging honestly takes the minimum. The engine's next pass refreshes
    it to the declared 0.900 in place, which is the right direction of travel:
    a live derivation supersedes a frozen historical claim.
    """
    inv_file = inventory_file()
    rows = [
        legacy("a", "ANSIBLE_MANAGES", project(), host(), created_at=T_OLD, confidence=0.9),
        legacy("b", "ANSIBLE_MANAGES", project(), host(), created_at=T_NEW, confidence=0.8),
    ]
    plan = run(
        rows,
        project_ids={"r-proj-new": PROJECT_ID},
        inv=witnesses({"node_a": [inv_file]}),
    )

    (edge,) = plan.edges
    assert edge.confidence == Decimal("0.800")
    assert edge.method == "deterministic_match", "0.800 may never claim 'declared'"


def test_a_1000_confidence_row_is_recorded_as_declared():
    """The honesty rule ``upsert_edge`` enforces, respected by the raw INSERT."""
    plan = run([legacy("c", "ISSUED_BY", certificate(), ca(), confidence=1.0)])
    (edge,) = plan.edges
    assert edge.method == "declared"


def test_an_endpoint_with_no_declared_convention_is_skipped_never_guessed():
    """The P3 guarantee, kept: no node identity is invented."""
    weird = p4.ResourceRow(
        id="r-x", domain="octopus", type="octopus_machine", name="m1", retired_at=None, metadata={}
    )
    plan = run([legacy("c", "ISSUED_BY", certificate(), weird)])
    assert plan.edges == []
    assert plan.counts["ISSUED_BY"] == {p4.SKIPPED_UNMAPPABLE: 1}


def test_the_backfill_marker_is_p4_so_downgrade_cannot_touch_p3s_rows():
    plan = run([legacy("c", "ISSUED_BY", certificate(), ca(), source="PKIAgent")])
    (edge,) = plan.edges
    assert edge.source.startswith("p4_backfill:")
    assert not edge.source.startswith("p3_backfill:")


def test_the_report_is_stable_and_greppable():
    lines = p4.format_report({"ISSUED_BY": {p4.MIGRATED_ACTIVE: 2}})
    assert lines == [f"  {'ISSUED_BY':<20} {p4.MIGRATED_ACTIVE:<34} 2"]


# ---------------------------------------------------------------------------
# The live shape, end to end
# ---------------------------------------------------------------------------


def test_the_live_shape_produces_seven_ansible_manages_and_two_issued_by():
    """14 + 3 legacy rows in, 7 + 2 edges out — counted, not trusted.

    Built from the deployed database: one inventory file, seven hosts, each
    ``(project, host)`` pair doubled by the rename; two certificates, each
    naming one of three CA rows.
    """
    inv_file = inventory_file()
    hosts = [
        "cloudflare_tunnel_a",
        "cloudflare_tunnel_b",
        "ai_node",
        "node_a",
        "git_runner",
        "media_host",
        "storage_node",
    ]
    old_project = project(rid="r-proj-old", name="homelab-ansible", retired=T_RENAME)
    rows = []
    for i, name in enumerate(hosts):
        target = host(rid=f"r-host-{i}", name=name)
        rows.append(legacy(f"old-{i}", "ANSIBLE_MANAGES", old_project, target, created_at=T_OLD))
        rows.append(legacy(f"new-{i}", "ANSIBLE_MANAGES", project(), target, created_at=T_NEW))
    rows.append(
        legacy(
            "cert-1",
            "ISSUED_BY",
            certificate(rid="r-cert-1", thumb="93057A88", issuer="CN=ACCVRAIZ1, O=ACCV"),
            ca(rid="r-ca-1", name="CN=ACCVRAIZ1, O=ACCV"),
            created_at=T_OLD,
            source="PKIAgent",
        )
    )
    rows.append(
        legacy(
            "cert-2",
            "ISSUED_BY",
            certificate(rid="r-cert-2"),
            ca(),
            created_at=T_OLD,
            source="PKIAgent",
        )
    )

    plan = run(
        rows,
        project_ids={"r-proj-old": PROJECT_ID, "r-proj-new": PROJECT_ID},
        inv=witnesses({name: [inv_file] for name in hosts}),
    )

    by_type = {}
    for edge in plan.edges:
        by_type.setdefault(edge.edge_type, []).append(edge)

    assert len(by_type["ANSIBLE_MANAGES"]) == 7
    assert len(by_type["ISSUED_BY"]) == 2
    assert plan.total() == 16, "14 ANSIBLE_MANAGES rows + 2 ISSUED_BY rows"
    assert plan.counts["ANSIBLE_MANAGES"][p4.MERGED] == 7, "one merge per pair"
    assert all(e.valid_from == T_OLD for e in by_type["ANSIBLE_MANAGES"])
    assert all(e.valid_to is None for e in plan.edges), (
        "nothing on this estate has stopped being true — the rename retired a "
        "project row, not a relationship"
    )
    # And every ANSIBLE_MANAGES edge points at the inventory file, never a project.
    assert {e.source_identity[0] for e in by_type["ANSIBLE_MANAGES"]} == {"AnsibleInventoryFile"}


def test_the_planner_is_deterministic_under_row_reordering():
    """Same rows, any order, same plan — a migration must not depend on scan order."""
    inv_file = inventory_file()
    a = legacy("a", "ANSIBLE_MANAGES", project(), host(), created_at=T_OLD)
    b = legacy("b", "ANSIBLE_MANAGES", project(), host(rid="r-h2", name="ai_node"), created_at=T_NEW)
    kwargs = dict(
        project_ids={"r-proj-new": PROJECT_ID},
        inv=witnesses({"node_a": [inv_file], "ai_node": [inv_file]}),
    )

    forward = run([a, b], **kwargs)
    backward = run([b, a], **kwargs)

    def shape(plan):
        return [
            (e.source_identity, e.target_identity, e.edge_type, e.valid_from, e.valid_to)
            for e in plan.edges
        ]

    assert shape(forward) == shape(backward)
    assert forward.counts == backward.counts


def test_a_row_created_after_its_engine_edge_by_a_second_leaves_no_lie():
    """The interval guard is strict, not tolerant — an empty span is skipped."""
    inv_file = inventory_file()
    triple = (("AnsibleInventoryFile", inv_file.name), ("LinuxHost", "node_a"), "ANSIBLE_MANAGES")
    plan = run(
        [
            legacy(
                "a",
                "ANSIBLE_MANAGES",
                project(),
                host(),
                created_at=T_ENGINE + timedelta(seconds=1),
            )
        ],
        active_edges={triple: T_ENGINE},
        project_ids={"r-proj-new": PROJECT_ID},
        inv=witnesses({"node_a": [inv_file]}),
    )
    assert plan.edges == []
