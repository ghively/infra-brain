"""Tests for the P3 backfill planner (migration ``8965b6329b94``).

The migration's decisions live in a pure, database-free planning layer for
exactly this reason: the interesting behaviour is *which* legacy rows become
*what kind of* edge, and that is a function of the rows, not of Postgres.

The module is loaded by path — an alembic version file is not importable as a
package member, and the same importlib pattern is already used by
``tests/test_migration_parity.py`` and ``tests/test_create_admin.py``.

It is loaded here EXACTLY the way alembic loads it — by path, and NOT
registered in ``sys.modules``. That detail is a regression guard, not an
oversight: ``@dataclass`` on a module with ``from __future__ import
annotations`` resolves its string annotations through
``sys.modules[cls.__module__]``, which is ``None`` for a path-loaded module, so
a future import in the migration would blow up during ``alembic upgrade`` and
nowhere else. Registering the module here would hide exactly that failure.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "8965b6329b94_p3_backfill_resource_relationships_into_.py"
)
_spec = importlib.util.spec_from_file_location("_p3_backfill_migration", _MIGRATION_PATH)
assert _spec is not None and _spec.loader is not None
p3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p3)  # NB: intentionally not registered in sys.modules


T0 = datetime(2026, 8, 3, 0, 20, tzinfo=timezone.utc)
T_ENGINE = datetime(2026, 8, 11, 23, 38, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def compose_file(rid="r-file", name="proj/docker-compose.yml", retired=None):
    return p3.ResourceRow(
        id=rid,
        domain="iac",
        type="docker_compose",
        name=name,
        retired_at=retired,
        metadata={"project_id": 42, "file_path": "docker-compose.yml", "ref": "main"},
    )


def project(rid="r-proj", name="acct/thing", retired=None, project_id=42):
    return p3.ResourceRow(
        id=rid,
        domain="cicd",
        type="gitlab_project",
        name=name,
        retired_at=retired,
        metadata={"project_id": project_id, "default_branch": "main"},
    )


def image(rid="r-img", name="postgres:17"):
    return p3.ResourceRow(
        id=rid,
        domain="container",
        type="container_image",
        name=name,
        retired_at=None,
        metadata={"image": name},
    )


def legacy(
    rid,
    rel_type,
    frm,
    to,
    *,
    created_at=T0,
    last_seen=None,
    confidence=1.0,
    status="active",
    source="iac_collector",
):
    return p3.LegacyRow(
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


def run(rows, *, active_edges=None, existing_backfill=None, existing_nodes=None):
    return p3.plan_backfill(
        rows,
        active_edges=active_edges or {},
        existing_backfill=existing_backfill or set(),
        existing_nodes=existing_nodes if existing_nodes is not None else _all_identities(rows),
    )


def _all_identities(rows):
    out = set()
    for row in rows:
        for resource in (row.from_resource, row.to_resource):
            node = p3.node_for(resource)
            if node is not None:
                out.add(node.identity)
    return out


BELONGS_TRIPLE = (
    ("DockerComposeFile", "proj/docker-compose.yml"),
    ("GitlabProject", "42"),
    "BELONGS_TO",
)


# ---------------------------------------------------------------------------
# Scope: what is copied and what is refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_type,expected",
    [
        ("BELONGS_TO", "migrate"),
        ("DEFINED_IN", "migrate"),
        ("RUNS_IMAGE", "migrate"),
        ("ANSIBLE_MANAGES", p3.SKIPPED_DEFERRED),
        ("HAS_DRIFT", p3.SKIPPED_CONTAINMENT),
        ("ON_FIELD", p3.SKIPPED_CONTAINMENT),
        ("HAS_ACCOUNT", p3.SKIPPED_CONTAINMENT),
        ("EXPOSES_PORT", p3.SKIPPED_CONTAINMENT),
        ("HAS_MOUNT", p3.SKIPPED_CONTAINMENT),
        ("HAS_CRON", p3.SKIPPED_CONTAINMENT),
        ("HAS_FIREWALL_RULE", p3.SKIPPED_CONTAINMENT),
        ("HAS_PENDING_UPDATE", p3.SKIPPED_CONTAINMENT),
        ("HAS_SOFTWARE", p3.SKIPPED_CONTAINMENT),
        ("TAGGED_AS", p3.SKIPPED_CONTAINMENT),
        # No collector declares these, so nothing would ever maintain them.
        ("MEMBER_OF", p3.SKIPPED_UNDECLARED),
        ("RUNS_EOL", p3.SKIPPED_UNDECLARED),
        ("ISSUED_BY", p3.SKIPPED_UNDECLARED),
        ("TRIGGERED_BY", p3.SKIPPED_UNDECLARED),
        ("A_TYPE_INVENTED_TOMORROW", p3.SKIPPED_UNDECLARED),
    ],
)
def test_classify_is_an_allow_list(rel_type, expected):
    assert p3.classify(rel_type) == expected


def test_containment_and_deferred_rows_are_counted_but_never_written():
    rows = [
        legacy("a", "HAS_DRIFT", compose_file(), project()),
        legacy("b", "ON_FIELD", compose_file(), project()),
        legacy("c", "ANSIBLE_MANAGES", compose_file(), project()),
        legacy("d", "MEMBER_OF", compose_file(), project()),
    ]
    plan = run(rows)
    assert plan.edges == []
    assert plan.nodes == []
    assert plan.counts["HAS_DRIFT"] == {p3.SKIPPED_CONTAINMENT: 1}
    assert plan.counts["ON_FIELD"] == {p3.SKIPPED_CONTAINMENT: 1}
    assert plan.counts["ANSIBLE_MANAGES"] == {p3.SKIPPED_DEFERRED: 1}
    assert plan.counts["MEMBER_OF"] == {p3.SKIPPED_UNDECLARED: 1}


def test_every_row_lands_in_exactly_one_disposition_bucket():
    rows = [
        legacy("a", "HAS_DRIFT", compose_file(), project()),
        legacy("b", "ANSIBLE_MANAGES", compose_file(), project()),
        legacy("c", "BELONGS_TO", compose_file(), project()),
        legacy("d", "BELONGS_TO", compose_file(), project("r-old", retired=T0, project_id=42)),
        legacy("e", "RUNS_IMAGE", compose_file(), image()),
        legacy("f", "BELONGS_TO", compose_file(), _unmappable()),
    ]
    plan = run(rows, active_edges={BELONGS_TRIPLE: T_ENGINE})
    assert plan.total() == len(rows)


def _unmappable():
    return p3.ResourceRow(
        id="r-x", domain="wazuh", type="wazuh_alert", name="alert-1", retired_at=None, metadata={}
    )


def test_endpoint_with_no_declared_convention_is_refused_not_guessed():
    rows = [legacy("a", "BELONGS_TO", compose_file(), _unmappable())]
    plan = run(rows)
    assert plan.edges == []
    assert plan.counts["BELONGS_TO"] == {p3.SKIPPED_UNMAPPABLE: 1}


# ---------------------------------------------------------------------------
# Node identity — must match graph_engine._emit_nodes exactly
# ---------------------------------------------------------------------------


def test_gitlab_project_node_is_keyed_on_the_immutable_numeric_id():
    node = p3.node_for(project(name="acct/renamed"))
    assert node.identity == ("GitlabProject", "42")
    assert node.name == "acct/renamed"
    assert node.source == "cicd"
    assert node.resource_id == "r-proj"
    assert node.attributes == {"project_id": 42, "default_branch": "main"}


def test_iac_file_node_is_keyed_on_resources_name():
    node = p3.node_for(compose_file())
    assert node.identity == ("DockerComposeFile", "proj/docker-compose.yml")
    assert node.source == "iac"
    assert node.attributes == {
        "project_id": 42,
        "file_path": "docker-compose.yml",
        "ref": "main",
    }


def test_container_image_is_a_shared_value_node_with_no_resource_id():
    node = p3.node_for(image())
    assert node.identity == ("ContainerImage", "postgres:17")
    # Declared and owned by iac even though the resource lives in `container`.
    assert node.source == "iac"
    assert node.resource_id is None
    assert node.attributes == {"image": "postgres:17"}


def test_a_resource_with_no_stable_key_yields_no_node():
    blank = p3.ResourceRow(
        id="r-p", domain="cicd", type="gitlab_project", name="x", retired_at=None, metadata={}
    )
    assert p3.node_for(blank) is None


def test_nodes_are_minted_only_when_absent_and_prefer_the_live_resource():
    rows = [
        legacy("a", "BELONGS_TO", compose_file(), project("r-old", retired=T0, project_id=42)),
        legacy(
            "b",
            "BELONGS_TO",
            compose_file(),
            project("r-new", project_id=42),
            created_at=T0 + timedelta(days=8),
        ),
    ]
    plan = run(rows, existing_nodes=set())
    minted = {n.identity: n for n in plan.nodes}
    assert set(minted) == {
        ("DockerComposeFile", "proj/docker-compose.yml"),
        ("GitlabProject", "42"),
    }
    # The retired row must not become the node's resource_id.
    assert minted[("GitlabProject", "42")].resource_id == "r-new"

    # And when the engine already has them, nothing is minted.
    plan2 = run(rows)
    assert plan2.nodes == []


# ---------------------------------------------------------------------------
# The duplicate-active-edge policy
# ---------------------------------------------------------------------------


def test_an_active_engine_edge_makes_the_legacy_row_closed_prehistory():
    rows = [legacy("a", "BELONGS_TO", compose_file(), project())]
    plan = run(rows, active_edges={BELONGS_TRIPLE: T_ENGINE})
    (edge,) = plan.edges
    assert edge.disposition == p3.MIGRATED_HISTORY
    assert edge.valid_from == T0
    # Closed exactly where the engine's active edge begins — no overlap, and
    # nothing competes for the partial unique index on active edges.
    assert edge.valid_to == T_ENGINE
    assert edge.evidence["superseded_by_active_edge_valid_from"] == T_ENGINE.isoformat()
    assert plan.counts["BELONGS_TO"] == {p3.MIGRATED_HISTORY: 1}


def test_no_active_edge_is_ever_written_for_a_pair_the_engine_already_covers():
    rows = [
        legacy("a", "BELONGS_TO", compose_file(), project()),
        legacy("b", "RUNS_IMAGE", compose_file(), image()),
    ]
    runs_image_triple = (
        ("DockerComposeFile", "proj/docker-compose.yml"),
        ("ContainerImage", "postgres:17"),
        "RUNS_IMAGE",
    )
    plan = run(rows, active_edges={BELONGS_TRIPLE: T_ENGINE, runs_image_triple: T_ENGINE})
    assert all(e.valid_to is not None for e in plan.edges)


def test_a_legacy_row_younger_than_the_engine_edge_has_no_prehistory_to_keep():
    rows = [
        legacy("a", "BELONGS_TO", compose_file(), project(), created_at=T_ENGINE + timedelta(1))
    ]
    plan = run(rows, active_edges={BELONGS_TRIPLE: T_ENGINE})
    assert plan.edges == []
    assert plan.counts["BELONGS_TO"] == {p3.SKIPPED_NO_PREHISTORY: 1}


def test_equal_timestamps_are_also_refused_rather_than_written_as_a_zero_interval():
    rows = [legacy("a", "BELONGS_TO", compose_file(), project(), created_at=T_ENGINE)]
    plan = run(rows, active_edges={BELONGS_TRIPLE: T_ENGINE})
    assert plan.counts["BELONGS_TO"] == {p3.SKIPPED_NO_PREHISTORY: 1}


# ---------------------------------------------------------------------------
# Merging: the L-8b rename produced two legacy rows for one relationship
# ---------------------------------------------------------------------------


def test_two_legacy_rows_for_one_renamed_project_become_one_edge():
    old, new = T0, T0 + timedelta(days=8)
    rows = [
        legacy(
            "b",
            "BELONGS_TO",
            compose_file(),
            project("r-new", name="acct/new", project_id=42),
            created_at=new,
            confidence=1.0,
        ),
        legacy(
            "a",
            "BELONGS_TO",
            compose_file(),
            project("r-old", name="acct/old", retired=new, project_id=42),
            created_at=old,
            confidence=1.0,
        ),
    ]
    plan = run(rows, active_edges={BELONGS_TRIPLE: T_ENGINE})
    (edge,) = plan.edges
    # The true first observation, which is on the row the rename orphaned —
    # history the legacy store's UNIQUE(from,to,type) could never express.
    assert edge.valid_from == old
    assert sorted(edge.evidence["legacy_relationship_ids"]) == ["a", "b"]
    assert edge.evidence["merged_legacy_rows"] == 2
    assert plan.counts["BELONGS_TO"] == {p3.MIGRATED_HISTORY: 1, p3.MERGED: 1}


def test_a_merged_edge_takes_the_weakest_supporting_confidence():
    rows = [
        legacy("a", "BELONGS_TO", compose_file(), project("r1", project_id=42), confidence=1.0),
        legacy(
            "b",
            "BELONGS_TO",
            compose_file(),
            project("r2", project_id=42),
            created_at=T0 + timedelta(1),
            confidence=0.9,
        ),
    ]
    plan = run(rows, active_edges={BELONGS_TRIPLE: T_ENGINE})
    (edge,) = plan.edges
    assert edge.confidence == Decimal("0.900")
    # ...and the method follows the confidence, never contradicts it.
    assert edge.method == "deterministic_match"


# ---------------------------------------------------------------------------
# Closing rules where the engine has nothing
# ---------------------------------------------------------------------------


def test_a_live_relationship_the_engine_never_saw_is_written_active():
    rows = [legacy("a", "BELONGS_TO", compose_file(), project())]
    plan = run(rows)
    (edge,) = plan.edges
    assert edge.valid_to is None
    assert edge.disposition == p3.MIGRATED_ACTIVE


def test_a_retired_endpoint_closes_the_edge_instead_of_reviving_it():
    """P2 deliberately excluded retired rows; the backfill must not undo that."""
    retired_at = T0 + timedelta(days=2)
    rows = [legacy("a", "BELONGS_TO", compose_file(retired=retired_at), project())]
    plan = run(rows)
    (edge,) = plan.edges
    assert edge.disposition == p3.MIGRATED_CLOSED
    assert edge.valid_to == retired_at


def test_a_non_active_legacy_row_closes_at_last_seen():
    rows = [
        legacy(
            "a",
            "BELONGS_TO",
            compose_file(),
            project(),
            status="stale",
            last_seen=T0 + timedelta(days=1),
        )
    ]
    plan = run(rows)
    (edge,) = plan.edges
    assert edge.disposition == p3.MIGRATED_CLOSED
    assert edge.valid_to == T0 + timedelta(days=1)


def test_valid_to_is_never_earlier_than_valid_from():
    """A resource retired BEFORE the edge was first recorded must not invert."""
    rows = [legacy("a", "BELONGS_TO", compose_file(retired=T0 - timedelta(days=5)), project())]
    plan = run(rows)
    (edge,) = plan.edges
    assert edge.valid_to == edge.valid_from


# ---------------------------------------------------------------------------
# Provenance and idempotence
# ---------------------------------------------------------------------------


def test_every_backfilled_edge_is_marked_auto_and_traceable_to_its_legacy_row():
    rows = [legacy("a", "BELONGS_TO", compose_file(), project(), source="iac_collector")]
    plan = run(rows, active_edges={BELONGS_TRIPLE: T_ENGINE})
    (edge,) = plan.edges
    assert edge.source == "p3_backfill:iac_collector"
    assert edge.source.startswith(p3.BACKFILL_SOURCE_PREFIX)
    assert edge.evidence["legacy_table"] == "resource_relationships"
    assert edge.evidence["legacy_relationship_ids"] == ["a"]
    assert edge.evidence["legacy_from_resource_id"] == "r-file"


def test_merged_rows_from_different_emitters_record_both_sources():
    rows = [
        legacy("a", "BELONGS_TO", compose_file(), project("r1", project_id=42), source="zzz"),
        legacy(
            "b",
            "BELONGS_TO",
            compose_file(),
            project("r2", project_id=42),
            created_at=T0 + timedelta(1),
            source="iac_collector",
        ),
    ]
    plan = run(rows, active_edges={BELONGS_TRIPLE: T_ENGINE})
    (edge,) = plan.edges
    assert edge.source == "p3_backfill:iac_collector+zzz"
    assert edge.evidence["legacy_sources"] == ["iac_collector", "zzz"]


def test_rerunning_writes_nothing_once_the_edge_is_already_backfilled():
    rows = [legacy("a", "BELONGS_TO", compose_file(), project())]
    plan = run(rows, existing_backfill={BELONGS_TRIPLE})
    assert plan.edges == []
    assert plan.nodes == []
    assert plan.counts["BELONGS_TO"] == {p3.SKIPPED_ALREADY: 1}


def test_confidence_honesty_rule_holds_for_every_planned_edge():
    rows = [
        legacy("a", "BELONGS_TO", compose_file(), project(), confidence=1.0),
        legacy("b", "RUNS_IMAGE", compose_file(), image(), confidence=0.75),
    ]
    plan = run(rows)
    for edge in plan.edges:
        assert Decimal("0") <= edge.confidence <= Decimal("1.000")
        if edge.confidence >= Decimal("1.000"):
            assert edge.method == "declared"
        else:
            assert edge.method != "declared"


def test_out_of_range_legacy_confidence_is_clamped_not_propagated():
    assert p3._quantize(1.7) == Decimal("1.000")
    assert p3._quantize(-0.2) == Decimal("0.000")
    assert p3._quantize(0.9876) == Decimal("0.988")


def test_report_lines_cover_every_counted_bucket():
    rows = [
        legacy("a", "HAS_DRIFT", compose_file(), project()),
        legacy("b", "BELONGS_TO", compose_file(), project()),
    ]
    plan = run(rows)
    lines = p3.format_report(plan.counts)
    assert any("HAS_DRIFT" in line and p3.SKIPPED_CONTAINMENT in line for line in lines)
    assert any("BELONGS_TO" in line and p3.MIGRATED_ACTIVE in line for line in lines)


def test_migration_declares_the_expected_single_parent():
    assert p3.revision == "8965b6329b94"
    assert p3.down_revision == "adacc8308153"
