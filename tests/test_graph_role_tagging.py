"""Tests for role/environment tagging (GitLab issue #128).

FIXTURE-ONLY — no vCenter, no network I/O. Mirrors
``tests/test_graph_phase2_emitters.py``'s fixture/session style.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra_brain.db.models import GraphEdge, GraphNode
from infra_brain import graph_role_tagging as rt

from tests.support.pg import make_engine


VC = "vcenter.example.com"


@pytest.fixture()
def session():
    engine = make_engine()
    with Session(engine) as s:
        yield s
        s.rollback()


def _vm(session, **kwargs):
    from infra_brain.db.models import VsphereVm

    defaults = dict(
        id=uuid.uuid4(),
        vcenter=VC,
        moref=f"vm-{uuid.uuid4().hex[:8]}",
        is_template=False,
        power_state="poweredOn",
        datastore_names=[],
        network_names=[],
    )
    defaults.update(kwargs)
    vm = VsphereVm(**defaults)
    session.add(vm)
    session.flush()
    return vm


def _has_role_edges(session, vm, role_slug=None):
    vm_node = (
        session.execute(
            select(GraphNode).where(
                GraphNode.node_type == "VsphereVM",
                GraphNode.natural_key == f"{vm.vcenter}:{vm.moref}",
            )
        )
        .scalars()
        .one_or_none()
    )
    if vm_node is None:
        return []
    edges = (
        session.execute(
            select(GraphEdge).where(
                GraphEdge.source_id == vm_node.id,
                GraphEdge.edge_type == rt.EDGE_TYPE_HAS_ROLE,
                GraphEdge.valid_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    if role_slug is None:
        return edges
    return [e for e in edges if session.get(GraphNode, e.target_id).natural_key == role_slug]


def _in_environment_edges(session, vm):
    vm_node = (
        session.execute(
            select(GraphNode).where(
                GraphNode.node_type == "VsphereVM",
                GraphNode.natural_key == f"{vm.vcenter}:{vm.moref}",
            )
        )
        .scalars()
        .one_or_none()
    )
    if vm_node is None:
        return []
    return (
        session.execute(
            select(GraphEdge).where(
                GraphEdge.source_id == vm_node.id,
                GraphEdge.edge_type == rt.EDGE_TYPE_IN_ENVIRONMENT,
                GraphEdge.valid_to.is_(None),
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Pass 1 — hostname regex, one example per role category
# ---------------------------------------------------------------------------

HOSTNAME_EXAMPLES = [
    ("prod-win-dc-03", "domain_controller"),
    ("prod-win-dns-01", "dns"),
    ("prod-win-dhcp-02", "dhcp"),
    ("prod-win-sql-07", "sql_database"),
    ("prod-win-web-12", "web_iis_proxy"),
    ("prod-win-fs-04", "file_server"),
    ("prod-win-bak-01", "backup"),
    ("prod-win-ca-02", "certificate_authority"),
    ("prod-win-mon-05", "monitoring"),
    ("prod-win-prt-01", "print_server"),
    ("prod-win-jmp-01", "jump_bastion"),
    ("prod-win-lb-03", "load_balancer"),
]


@pytest.mark.parametrize("hostname,expected_slug", HOSTNAME_EXAMPLES)
def test_hostname_pattern_matches_each_role(session, hostname, expected_slug):
    vm = _vm(session, name=hostname)

    written, claimed = rt.emit_roles_from_hostname(session)
    session.commit()

    assert written == 1
    assert claimed[vm.id] == expected_slug
    edges = _has_role_edges(session, vm, expected_slug)
    assert len(edges) == 1
    assert edges[0].method == rt.METHOD_HOSTNAME_PATTERN
    assert edges[0].confidence == rt.CONFIDENCE_HOSTNAME_PATTERN
    assert edges[0].confidence < Decimal("1.000")


def test_hostname_pass_skips_templates(session):
    _vm(session, name="prod-win-dc-03", is_template=True)

    written, claimed = rt.emit_roles_from_hostname(session)
    session.commit()

    assert written == 0
    assert claimed == {}


def test_hostname_pass_no_match_emits_no_edge(session):
    vm = _vm(session, name="prod-win-app-01")

    written, claimed = rt.emit_roles_from_hostname(session)
    session.commit()

    assert written == 0
    assert vm.id not in claimed
    assert _has_role_edges(session, vm) == []


# ---------------------------------------------------------------------------
# Pass 2 — annotation keyword mining, incl. ambiguous/combined-role cases
# ---------------------------------------------------------------------------


def test_annotation_mining_matches_ambiguous_hostname(session):
    """Hostname gives no signal; annotation prose carries the role."""
    vm = _vm(
        session,
        name="prod-win-app-07",
        annotation="Primary SQL Server for the billing application, do not reboot without notice.",
    )

    written, claimed = rt.emit_roles_from_hostname(session)
    session.commit()
    assert written == 0

    written2 = rt.emit_roles_from_annotation(session, claimed)
    session.commit()

    assert written2 == 1
    edges = _has_role_edges(session, vm, "sql_database")
    assert len(edges) == 1
    assert edges[0].method == rt.METHOD_ANNOTATION
    assert edges[0].confidence == rt.CONFIDENCE_ANNOTATION
    assert edges[0].confidence < rt.CONFIDENCE_HOSTNAME_PATTERN


def test_annotation_mining_combined_role_box(session):
    """One VM's annotation states TWO roles — both edges must be emitted."""
    vm = _vm(
        session,
        name="prod-win-dc-03",
        annotation="Primary domain controller for the site, also runs DNS server for the subnet.",
    )

    written, claimed = rt.emit_roles_from_hostname(session)
    session.commit()
    assert written == 1
    assert claimed[vm.id] == "domain_controller"

    written2 = rt.emit_roles_from_annotation(session, claimed)
    session.commit()

    # DC already claimed by hostname pass — annotation must NOT re-claim it
    # at a lower confidence, but DNS is a genuinely additional role.
    assert written2 == 1
    dc_edges = _has_role_edges(session, vm, "domain_controller")
    dns_edges = _has_role_edges(session, vm, "dns")
    assert len(dc_edges) == 1
    assert dc_edges[0].method == rt.METHOD_HOSTNAME_PATTERN  # not overwritten
    assert len(dns_edges) == 1
    assert dns_edges[0].method == rt.METHOD_ANNOTATION


def test_annotation_mining_no_match_emits_no_edge(session):
    vm = _vm(session, name="prod-win-app-09", annotation="Runs the nightly billing reconciliation job.")

    written, claimed = rt.emit_roles_from_hostname(session)
    session.commit()
    written2 = rt.emit_roles_from_annotation(session, claimed)
    session.commit()

    assert written == 0
    assert written2 == 0
    assert _has_role_edges(session, vm) == []


# ---------------------------------------------------------------------------
# Environment segmentation — vSphere guest_hostname FQDN suffix
# ---------------------------------------------------------------------------

ENVIRONMENT_EXAMPLES = [
    ("prod-win-sql-07.example.internal", "prod"),
    ("prod-win-sql-07.dev.example.internal", "dev"),
    ("prod-win-sql-07.test.example.internal", "test"),
    ("prod-win-sql-07.cao.example.internal", "cao"),
]


@pytest.mark.parametrize("guest_hostname,expected_slug", ENVIRONMENT_EXAMPLES)
def test_environment_extraction_from_each_fqdn_suffix(session, guest_hostname, expected_slug):
    vm = _vm(session, name="prod-win-sql-07", guest_hostname=guest_hostname)

    written = rt.emit_environments(session)
    session.commit()

    assert written == 1
    edges = _in_environment_edges(session, vm)
    assert len(edges) == 1
    assert edges[0].method == rt.METHOD_VSPHERE_FQDN_SUFFIX
    assert edges[0].confidence == rt.CONFIDENCE_ENVIRONMENT
    env_node = session.get(GraphNode, edges[0].target_id)
    assert env_node.natural_key == expected_slug


def test_environment_unknown_suffix_emits_no_edge(session):
    vm = _vm(session, name="prod-win-sql-08", guest_hostname="prod-win-sql-08.unknown-domain.example")

    written = rt.emit_environments(session)
    session.commit()

    assert written == 0
    assert _in_environment_edges(session, vm) == []


def test_environment_missing_guest_hostname_emits_no_edge(session):
    vm = _vm(session, name="prod-win-sql-09", guest_hostname=None)

    written = rt.emit_environments(session)
    session.commit()

    assert written == 0
    assert _in_environment_edges(session, vm) == []


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def test_emit_all_runs_all_three_passes(session):
    _vm(
        session,
        name="prod-win-dc-03",
        annotation="Also functions as backup target for the site.",
        guest_hostname="prod-win-dc-03.example.internal",
    )

    counts, errors = rt.emit_all(session)
    session.commit()

    assert errors == []
    assert counts["roles_from_hostname"] == 1
    assert counts["roles_from_annotation"] == 1  # backup, additional to DC
    assert counts["environments"] == 1


# ---------------------------------------------------------------------------
# Column-width regression guard (2026-07-29 graph_maintenance outage)
# ---------------------------------------------------------------------------
# METHOD_VSPHERE_FQDN_SUFFIX was "inferred_from_vsphere_fqdn_suffix" (33 chars),
# one over GraphEdge.method's String(32). sqlite does NOT enforce VARCHAR
# lengths, so the whole suite stayed green while every scheduled
# graph_maintenance run failed on real Postgres with
# psycopg2.errors.StringDataRightTruncation. These assertions check the string
# constants directly against the declared column lengths in the ORM, which
# catches the overflow regardless of test-database dialect.


def _col_len(column):
    length = column.type.length
    assert length is not None, f"{column} unexpectedly has no declared length"
    return length


@pytest.mark.parametrize(
    "constant_name",
    ["METHOD_HOSTNAME_PATTERN", "METHOD_ANNOTATION", "METHOD_VSPHERE_FQDN_SUFFIX"],
)
def test_method_constants_fit_graph_edge_method_column(constant_name):
    value = getattr(rt, constant_name)
    max_len = _col_len(GraphEdge.__table__.c.method)
    assert len(value) <= max_len, (
        f"rt.{constant_name} = {value!r} is {len(value)} chars, but "
        f"graph_edges.method is VARCHAR({max_len}) on Postgres — this insert "
        f"will fail with StringDataRightTruncation in production even though "
        f"sqlite lets it through"
    )


@pytest.mark.parametrize(
    "constant_name",
    ["EMITTER_ROLE_HOSTNAME", "EMITTER_ROLE_ANNOTATION", "EMITTER_ENVIRONMENT"],
)
def test_emitter_constants_fit_graph_edge_source_column(constant_name):
    value = getattr(rt, constant_name)
    max_len = _col_len(GraphEdge.__table__.c.source)
    assert len(value) <= max_len


@pytest.mark.parametrize(
    "constant_name", ["EDGE_TYPE_HAS_ROLE", "EDGE_TYPE_IN_ENVIRONMENT"]
)
def test_edge_type_constants_fit_graph_edge_type_column(constant_name):
    value = getattr(rt, constant_name)
    max_len = _col_len(GraphEdge.__table__.c.edge_type)
    assert len(value) <= max_len


@pytest.mark.parametrize(
    "constant_name", ["NODE_TYPE_ROLE", "NODE_TYPE_ENVIRONMENT"]
)
def test_node_type_constants_fit_graph_node_type_column(constant_name):
    value = getattr(rt, constant_name)
    max_len = _col_len(GraphNode.__table__.c.node_type)
    assert len(value) <= max_len
