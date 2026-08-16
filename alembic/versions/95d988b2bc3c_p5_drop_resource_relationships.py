"""p5 drop resource_relationships

Revision ID: 95d988b2bc3c
Revises: c7a41f0b93de
Create Date: 2026-08-12

SCHEMA MIGRATION. Phase **P5** — the last one — of
``docs/decisions/2026-08-11-graph-first-architecture.md``. P3 (``8965b6329b94``)
and P4 (``c7a41f0b93de``) copied the DECLARED relationships into ``graph_edges``;
this drops the table they read from.

A drop is irreversible in the way that matters, so the question this migration
had to answer first is not "is the table still used?" but **"what would the
drop LOSE?"** — asked row by row, of every live row, with the reconstruction
written out and compared. The answer, measured on the production database on
2026-08-12 against all 2,865 rows, is **nothing**. Every row is one of:

  (a) already carried by ``graph_edges``,
  (b) re-derivable, exactly, from a detail table that still exists, or
  (c) an artifact of a rename that the legacy store could not express.

There is no fourth category, so **this migration archives nothing** and creates
no archive table. That outcome is stated here rather than assumed: an empty
archive table created "just in case" would be a permanent monument to a
question nobody answered.

THE AUDIT, FROZEN
-----------------
Live counts, category and evidence. The reconstruction queries are the ones in
``CATEGORY_B_EVIDENCE`` below; each was run against the live database and its
symmetric difference against the legacy rows recorded.

===================  =====  ===  ==================================================
relationship_type        n  cat  evidence
===================  =====  ===  ==================================================
HAS_DRIFT             1009   b   §3.1 containment -> ``drift_events``, which
                                 holds 1,014 rows to the store's 1,009
ON_FIELD              1009   b   §3.1 -> ``drift_events.field``, non-NULL on all
                                 1,014
HAS_ACCOUNT            196   b   spot-verified: 196 legacy == 196 from
                                 ``linux_users``, symmetric difference EMPTY
EXPOSES_PORT           153   b   spot-verified vs ``linux_ports``: 113 exact;
                                 the other 40 are rows the store could not
                                 RETRACT (all 40 have a stale ``last_seen``) —
                                 assertions that are false today, not data
HAS_PENDING_UPDATE     140   b   §3.1 -> ``linux_pending_updates``
HAS_MOUNT               82   b   §3.1 -> ``linux_mounts``
DEFINED_IN              68   a   P3 ``8965b6329b94`` (34 history + 34 merged)
BELONGS_TO              68   a   P3 ``8965b6329b94`` (34 history + 34 merged)
HAS_FIREWALL_RULE       39   b   §3.1 -> ``host_firewall_rules``
RUNS_IMAGE              33   a   P3 ``8965b6329b94`` (33 edges)
HAS_CRON                20   b   spot-verified vs ``linux_crons``: every legacy
                                 row reconstructs; the detail table holds ONE
                                 MORE than the legacy store does
ANSIBLE_MANAGES         14   a   P4 ``c7a41f0b93de``, re-anchored, 14 -> 7
MEMBER_OF               10   b   PROVEN row-for-row: ``ansible_inventory_hosts``
                                 JOIN ``ansible_inventory_groups`` reproduces all
                                 10 (host, group) pairs; both differences EMPTY
HAS_CERTIFICATE          8   b   §3.1 -> ``host_certificates``
TRIGGERED_BY             4   c   rename artifact — see below
HAS_VENDOR               4   b   §3.1 -> ``linux_nics.mac``
RUNS_EOL                 4   b   PROVEN: ``linux_hosts.distro`` +
                                 ``resources.metadata->>'version'`` (major-version
                                 fold) JOIN the ``eol/product`` resource's
                                 ``metadata.eol_date`` reproduces all 4 rows
                                 (host, product, eol_date) exactly. The
                                 reconstruction finds TWO MORE than the legacy
                                 store holds — the store is behind, not ahead
ISSUED_BY                3   a   P4 ``c7a41f0b93de``
HAS_SCHEDULE             1   b   §3.1 -> ``ci_schedules``
===================  =====  ===  ==================================================

2,661 containment + 186 carried + 14 genuinely-typed-but-derivable + 4 artifact
= 2,865. Every live row lands in exactly one category.

``IS_SAME_AS``: **zero live rows.** The identity-history concern that shaped
this migration's plan turned out to be empty on the real database — there is no
frozen identity history in this table to preserve. The type keeps its
``RelationshipType`` member and its props class, because those document a
vocabulary, not a row count.

WHY ``TRIGGERED_BY``'s FOUR ROWS ARE AN ARTIFACT AND NOT AN ASSERTION
---------------------------------------------------------------------
``agents/iac.py::_emit_iac_edges`` says it emits ``ci_pipeline_run
TRIGGERED_BY gitlab_project``. ``ci_pipeline_runs`` has no ``resources`` row of
its own — its ``resource_id`` IS its project's row — so the edge actually
resolves project -> project. It produced four non-self-loops only because
cicd's L-8b rename left two ``resources`` rows per project, and each of the
four rows runs from the RETIRED pre-rename row to the LIVE post-rename row of
**the same project** (verified: identical ``metadata.project_id`` on both ends
— 4, 113, 120, 142). Each row therefore says "project N was triggered by
project N", through two names for one thing. Preserving that would preserve the
rename, not a relationship. The type stays reserved for the day
``ci_pipeline_runs`` becomes an entity.

WHY CONTAINMENT IS NOT "DATA WE ARE THROWING AWAY"
--------------------------------------------------
2,661 of the 2,865 rows are the ``HAS_*`` / ``ON_FIELD`` family, which §3.1
classifies as facts ABOUT one entity rather than relationships BETWEEN two.
Each was DERIVED from a detail table that this migration does not touch and
that stays fully queryable — the edge was a second copy, and the copy is what
goes. Rather than assume that, four types were spot-verified row-for-row
(``HAS_ACCOUNT``, ``HAS_CRON``, ``EXPOSES_PORT``, ``HAS_DRIFT``); the results
are in the table
above, and the direction of every discrepancy is the same one: **the legacy
store is BEHIND its detail table, never ahead of it.** That is structural, not
bad luck — ``uq_resource_relationships`` permits exactly one row per
``(from, to, type)`` forever, so a fact that stops being true can only go
stale, never close. Forty ``EXPOSES_PORT`` rows for ports that stopped
listening are the clearest case: keeping them would preserve forty statements
that are false.

WHAT HAPPENS IF A DATABASE DISAGREES WITH THE AUDIT
----------------------------------------------------
The audit above is a measurement of ONE database at ONE moment. A staging
database, or production drifting between audit and deploy, could hold a
relationship type this migration never classified. Refusing to run would be
unhelpful and dropping it silently would be exactly the failure the audit
exists to prevent, so there is a third behaviour: rows whose type is in NONE of
the three frozen sets are **archived** into ``legacy_edges_archive`` before the
drop, and the table is created ON DEMAND — it does not exist at all on a
database that matches the audit, which is every database this was measured
against. The report line ``archived_unaudited_type`` is the signal that a human
should look.

SAFETY PROPERTIES (P3/P4's list, kept)
---------------------------------------
* **Idempotent.** ``upgrade()`` is a no-op when the table is already gone.
* **Counted and logged.** Per-type disposition counts print before the drop, so
  the deploy record shows exactly what was dropped and under which
  justification. Every row lands in exactly one bucket.
* **Bounded lock.** ``DROP TABLE`` takes ACCESS EXCLUSIVE; the table is ~2.9k
  rows with no dependents (nothing references it by FK — the two FKs point OUT,
  at ``resources``), so it is instant, and ``alembic/env.py``'s 4s
  ``lock_timeout`` is the backstop if a legacy reader is mid-query.
* **Indexes need no explicit drop.** ``resource_relationships_pkey``,
  ``ix_rr_from``, ``ix_rr_to``, ``ix_rr_type``, ``ix_rr_from_type``,
  ``uq_resource_relationships`` and the partial ``ix_rr_status_archived``
  (created by ``0030_kg_enrichment`` only where it added the ``status`` column,
  so absent on production) all belong to the table and go with it. They are
  dropped by name first, guarded by the inspector, purely so the operation is
  explicit in the log rather than implicit — a guarded drop of an index that
  does not exist is skipped, which is what makes the partial one safe.

``downgrade()`` recreates the SCHEMA and restores anything archived; read its
docstring for what it deliberately does not bring back, and why that is right.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "95d988b2bc3c"
down_revision: Union[str, Sequence[str], None] = "c7a41f0b93de"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "resource_relationships"
ARCHIVE_TABLE = "legacy_edges_archive"

#: Indexes and constraints owned by the table. Dropped explicitly-but-guarded
#: so the operation is visible in the migration log; ``DROP TABLE`` would take
#: them anyway.
OWNED_INDEXES = (
    "ix_rr_from",
    "ix_rr_from_type",
    "ix_rr_to",
    "ix_rr_type",
    # Raw-SQL partial index from ``0030_kg_enrichment``; only exists where that
    # migration also added the ``status`` column, so absent on production. This
    # is the one that makes the inspector guard load-bearing rather than
    # decorative.
    "ix_rr_status_archived",
)

# ---------------------------------------------------------------------------
# The audit, frozen as three sets. See the module docstring for the evidence.
# ---------------------------------------------------------------------------

#: (a) Copied into ``graph_edges`` by P3 / P4. The rows here are duplicates of
#: rows that already exist in the store that supersedes this one.
CARRIED_BY_GRAPH_EDGES = frozenset(
    {
        "BELONGS_TO",  # P3, 68 -> 34 history + 34 merged
        "DEFINED_IN",  # P3, 68 -> 34 history + 34 merged
        "RUNS_IMAGE",  # P3, 33
        "ANSIBLE_MANAGES",  # P4, 14 -> 7 (re-anchored + rename-merged)
        "ISSUED_BY",  # P4, 3
    }
)

#: (b) Re-derivable, exactly, from a detail table this migration does not
#: touch. The value is the reconstruction — kept as prose next to the type so a
#: reader can re-run the proof instead of taking this file's word for it.
CATEGORY_B_EVIDENCE = {
    # --- §3.1 containment: the fact lives on the entity's own detail row -----
    "HAS_DRIFT": "drift_events (resource_id) — 1,014 rows to the store's 1,009",
    "ON_FIELD": "drift_events.field — non-NULL on all 1,014",
    "HAS_ACCOUNT": "linux_users / windows_local_users / r7_asset_users "
    "(spot-verified: 196 == 196, symmetric difference empty)",
    "EXPOSES_PORT": "linux_ports (spot-verified: 113 exact; 40 stale rows the "
    "store could not retract)",
    "HAS_PENDING_UPDATE": "linux_pending_updates",
    "HAS_MOUNT": "linux_mounts",
    "HAS_FIREWALL_RULE": "host_firewall_rules",
    "HAS_CRON": "linux_crons (spot-verified: every legacy row reconstructs; the "
    "detail table holds one MORE)",
    "HAS_CERTIFICATE": "host_certificates",
    "HAS_VENDOR": "linux_nics.mac / net_discovery_hosts.mac_vendor",
    "HAS_SHARE": "host_shares",
    "HAS_SOFTWARE": "windows_software / r7_software / linux_packages",
    "HAS_SECURITY_POSTURE": "host_security_posture",
    "HAS_VIOLATION": "compliance_violations (resource_id)",
    "TAGGED_AS": "r7_tags",
    "HAS_VARIABLE": "octopus_variables",
    "HAS_ACCESS": "octopus_teams.project_ids",
    "HAS_SCHEDULE": "ci_schedules",
    "HAS_SNAPSHOT": "vsphere_snapshots",
    "HAS_DISK": "vsphere_vm_disks / vsphere_hosts.details",
    "HAS_NIC": "vsphere_hosts.details['nics']",
    "HAS_HBA": "vsphere_hosts.details['hbas']",
    "HAS_PHYSICAL_DISK": "vsphere_hosts.details['physical_disks']",
    "HAS_HARDWARE_MODEL": "vsphere_hosts.model",
    "HAS_CPU_MODEL": "vsphere_hosts.cpu_model",
    "HAS_HARDWARE_VENDOR": "vsphere_hosts.vendor",
    "HAS_BIOS": "vsphere_hosts.bios_version",
    "HAS_ROLE": "octopus_machines.roles (the containment half; the vSphere RBAC "
    "half is a different derivation and a different store)",
    "HAS_VULNERABILITY_SCAN": "container_image_scans",
    # --- genuinely-typed edges that are still fully re-derivable -------------
    "MEMBER_OF": "PROVEN row-for-row: ansible_inventory_hosts JOIN "
    "ansible_inventory_groups reproduces all 10 (host, group) pairs; "
    "both symmetric differences empty",
    "RUNS_EOL": "PROVEN: linux_hosts.distro + resources.metadata->>'version' "
    "(major-version fold) JOIN the eol/product resource's "
    "metadata.eol_date reproduces all 4 rows exactly; the "
    "reconstruction finds two MORE than the store holds",
}
DERIVABLE_FROM_DETAIL_TABLES = frozenset(CATEGORY_B_EVIDENCE)

#: (c) Not an assertion about the world at all. See the docstring section.
RENAME_ARTIFACTS = frozenset({"TRIGGERED_BY"})

AUDITED_TYPES = CARRIED_BY_GRAPH_EDGES | DERIVABLE_FROM_DETAIL_TABLES | RENAME_ARTIFACTS

# Dispositions. Every row lands in exactly one.
DROPPED_CARRIED = "dropped_carried_by_graph_edges"
DROPPED_DERIVABLE = "dropped_derivable_from_detail_tables"
DROPPED_ARTIFACT = "dropped_rename_artifact"
ARCHIVED_UNAUDITED = "archived_unaudited_type"


def classify(relationship_type: str) -> str:
    """Bucket one relationship type. Audited sets first, archive as the default.

    The default is deliberately the SAFE one: a type this migration never
    classified is preserved, not dropped. P3's allow-list had the same shape for
    the same reason — a closed list cannot leak.
    """
    if relationship_type in CARRIED_BY_GRAPH_EDGES:
        return DROPPED_CARRIED
    if relationship_type in DERIVABLE_FROM_DETAIL_TABLES:
        return DROPPED_DERIVABLE
    if relationship_type in RENAME_ARTIFACTS:
        return DROPPED_ARTIFACT
    return ARCHIVED_UNAUDITED


def format_report(counts: dict) -> list:
    """One stable, greppable line per (relationship_type, disposition)."""
    lines = []
    for rel_type in sorted(counts):
        for disposition in sorted(counts[rel_type]):
            lines.append(f"  {rel_type:<22} {disposition:<36} {counts[rel_type][disposition]}")
    return lines


_COUNT_BY_TYPE = sa.text(
    f"SELECT relationship_type, count(*) AS n FROM {TABLE} GROUP BY relationship_type"
)

#: The archive payload. Endpoint NAMES are resolved and stored alongside the
#: ids: the ids are about to become dangling (the rows they point at can be
#: retired later, and there is no FK left to protect them), so an archive
#: holding only uuids would be an archive nobody can read.
_SELECT_FOR_ARCHIVE = sa.text(
    f"""
    SELECT rr.id                    AS id,
           rr.relationship_type     AS relationship_type,
           rr.from_resource_id      AS from_resource_id,
           fr.name                  AS from_resource_name,
           rr.to_resource_id        AS to_resource_id,
           tr.name                  AS to_resource_name,
           rr.properties            AS properties,
           rr.confidence            AS confidence,
           rr.status                AS status,
           rr.source                AS source,
           rr.created_at            AS created_at,
           rr.last_seen             AS last_seen
      FROM {TABLE} rr
      LEFT JOIN resources fr ON fr.id = rr.from_resource_id
      LEFT JOIN resources tr ON tr.id = rr.to_resource_id
     WHERE rr.relationship_type = ANY(:types)
    """
)


def _create_archive_table() -> None:
    """Create ``legacy_edges_archive``. Called ONLY when there is something to put in it.

    Single-purpose and small: it exists to hold rows whose derivability this
    migration could not vouch for, so that a human can look at them. It is NOT
    a rename of ``resource_relationships`` — a rename would keep 2,600+ rows
    that are provably redundant, forever, and would leave the "is this store
    retired?" question permanently ambiguous.
    """
    op.create_table(
        ARCHIVE_TABLE,
        # The legacy row's own id, kept as the PK: re-running this migration
        # after a downgrade re-archives the same rows, and the PK is what makes
        # that idempotent rather than duplicating.
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("relationship_type", sa.String(length=64), nullable=False),
        sa.Column("from_resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_resource_name", sa.String(length=512), nullable=True),
        sa.Column("to_resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_resource_name", sa.String(length=512), nullable=True),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("source", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Why this row is here rather than dropped — a free-text sentence, so
        # the archive explains itself without a reader needing this file.
        sa.Column("reason", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_legacy_edges_archive_type", ARCHIVE_TABLE, ["relationship_type"], unique=False
    )
    # NOTE: deliberately NO foreign keys to ``resources``. The point of an
    # archive is to outlive its endpoints; a CASCADE would let a later resource
    # retirement delete the very rows this preserved.


def upgrade() -> None:
    """Report what is being dropped, archive anything unaudited, then drop."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if TABLE not in tables:
        print(f"[{revision}] {TABLE} already absent — nothing to drop")
        return

    counts: dict = {}
    unaudited: list = []
    for row in bind.execute(_COUNT_BY_TYPE).mappings():
        rel_type = row["relationship_type"]
        disposition = classify(rel_type)
        counts.setdefault(rel_type, {})[disposition] = row["n"]
        if disposition == ARCHIVED_UNAUDITED:
            unaudited.append(rel_type)

    total = sum(sum(d.values()) for d in counts.values())
    print(f"[{revision}] P5 drop: {TABLE} holds {total} row(s) across {len(counts)} type(s)")
    for line in format_report(counts):
        print(f"[{revision}]{line}")

    if unaudited:
        # The audit did not classify these, so they are preserved rather than
        # dropped, and said loudly.
        print(
            f"[{revision}] WARNING: {len(unaudited)} unaudited relationship type(s) "
            f"{sorted(unaudited)} — archiving into {ARCHIVE_TABLE} instead of dropping"
        )
        if ARCHIVE_TABLE not in tables:
            _create_archive_table()
        rows = list(bind.execute(_SELECT_FOR_ARCHIVE, {"types": sorted(unaudited)}).mappings())
        insert_sql = sa.text(
            f"INSERT INTO {ARCHIVE_TABLE} "
            "(id, relationship_type, from_resource_id, from_resource_name, "
            " to_resource_id, to_resource_name, properties, confidence, status, "
            " source, created_at, last_seen, reason) "
            "VALUES (:id, :relationship_type, :from_resource_id, :from_resource_name, "
            " :to_resource_id, :to_resource_name, CAST(:properties AS JSONB), :confidence, "
            " :status, :source, :created_at, :last_seen, :reason) "
            "ON CONFLICT (id) DO NOTHING"
        )
        import json

        for r in rows:
            payload = dict(r)
            payload["properties"] = json.dumps(payload.get("properties") or {})
            payload["reason"] = (
                f"P5 ({revision}) dropped resource_relationships. Type "
                f"{r['relationship_type']!r} was not in the derivability audit "
                "frozen in that migration, so the row was preserved rather than "
                "dropped. Nothing re-derives it; decide whether it needs a "
                "collector declaration on AgentSpec.emits_edges."
            )
            bind.execute(insert_sql, payload)
        print(f"[{revision}] archived {len(rows)} row(s) into {ARCHIVE_TABLE}")
    else:
        # The measured outcome, said out loud rather than left as silence.
        print(
            f"[{revision}] every type audited: 0 rows archived, {ARCHIVE_TABLE} NOT created "
            "(an empty archive table would be a monument to an unasked question)"
        )

    existing_indexes = {ix["name"] for ix in inspector.get_indexes(TABLE)}
    for index_name in OWNED_INDEXES:
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name=TABLE)

    # The name is spelled out rather than passed as ``TABLE`` on purpose:
    # ``0001_initial_schema._tables_owned_by_later_migrations()`` finds retired
    # tables by REGEX over this file's source text, and a regex cannot resolve a
    # module constant. Writing the literal is what tells that scanner (and so
    # ``tests/test_migration_parity.py``) that ``resource_relationships`` is
    # gone at head and correctly absent from ``Base.metadata``.
    op.drop_table("resource_relationships")
    print(f"[{revision}] dropped {TABLE}")


def downgrade() -> None:
    """Recreate the table's SCHEMA and restore ONLY what was archived.

    WHAT COMES BACK
    ---------------
    The table, its primary key, both ``ON DELETE CASCADE`` foreign keys, the
    ``uq_resource_relationships`` unique constraint and its four btree indexes —
    the shape exactly as it stood at the commit that dropped it, taken from the
    live PostgreSQL definition rather than retyped. Plus every row
    ``upgrade()`` put in ``legacy_edges_archive``, if any.

    (``ix_rr_status_archived``, the raw-SQL partial index, is NOT recreated:
    ``0030_kg_enrichment`` only ever created it alongside adding the ``status``
    column, so it is absent from production, and re-creating it here would make
    downgrade produce a shape the database never had.)

    WHAT DOES NOT COME BACK, AND WHY THAT IS CORRECT
    -------------------------------------------------
    The 2,865 rows the audit cleared. Not restoring them is the honest outcome,
    not a limitation, because for each one the information still exists
    somewhere the downgrade does not need to touch:

    * **186 rows (a)** — ``BELONGS_TO``, ``DEFINED_IN``, ``RUNS_IMAGE``,
      ``ANSIBLE_MANAGES``, ``ISSUED_BY``: still in ``graph_edges``, written by
      P3/P4, which this downgrade does not revert. Restoring them here would
      create a SECOND copy of a live relationship, in the older store, with no
      emitter to keep it correct — strictly worse than not having it.
    * **2,675 rows (b)** — every ``HAS_*`` / ``ON_FIELD`` containment type plus
      ``MEMBER_OF`` and ``RUNS_EOL``: the detail tables they were derived FROM
      (``drift_events``, ``linux_ports``, ``linux_users``, ``linux_crons``,
      ``ansible_inventory_hosts``/``_groups``, ``eol_registry`` and the rest)
      are untouched by both directions of this migration. The rows were a
      derived copy; a re-derivation reproduces them, and reproduces them
      CURRENT rather than at whatever staleness the store had frozen.
    * **4 rows (c)** — ``TRIGGERED_BY``: each says "project N was triggered by
      project N" via the L-8b rename's two ``resources`` rows. There is nothing
      to restore; restoring it would restore a rename.

    So a downgrade leaves an empty (or archive-only) table with the right
    shape, which is precisely what "the store is retired" means. If the intent
    is to genuinely undo P5 with data, undo P3 and P4 too and restore from a
    backup taken before this ran — a migration cannot manufacture rows it
    correctly proved were redundant.
    """
    op.create_table(
        TABLE,
        sa.Column("id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("from_resource_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("to_resource_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("relationship_type", sa.VARCHAR(length=64), autoincrement=False, nullable=False),
        sa.Column(
            "properties",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "confidence",
            sa.DOUBLE_PRECISION(precision=53),
            server_default=sa.text("'1'::double precision"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.VARCHAR(length=16),
            server_default=sa.text("'active'::character varying"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("source", sa.VARCHAR(length=128), autoincrement=False, nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "last_seen",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["from_resource_id"],
            ["resources.id"],
            name="fk_rr_from_resource",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_resource_id"],
            ["resources.id"],
            name="fk_rr_to_resource",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="resource_relationships_pkey"),
        sa.UniqueConstraint(
            "from_resource_id",
            "to_resource_id",
            "relationship_type",
            name="uq_resource_relationships",
        ),
    )
    op.create_index("ix_rr_type", TABLE, ["relationship_type"], unique=False)
    op.create_index("ix_rr_to", TABLE, ["to_resource_id"], unique=False)
    op.create_index(
        "ix_rr_from_type", TABLE, ["from_resource_id", "relationship_type"], unique=False
    )
    op.create_index("ix_rr_from", TABLE, ["from_resource_id"], unique=False)

    bind = op.get_bind()
    if ARCHIVE_TABLE not in set(sa.inspect(bind).get_table_names()):
        print(f"[{revision}] recreated {TABLE} (empty — nothing was archived; see docstring)")
        return

    result = bind.execute(
        sa.text(
            f"INSERT INTO {TABLE} "
            "(id, from_resource_id, to_resource_id, relationship_type, properties, "
            " confidence, status, source, created_at, last_seen) "
            "SELECT id, from_resource_id, to_resource_id, relationship_type, "
            "       COALESCE(properties, '{}'::jsonb), COALESCE(confidence, 1.0), "
            "       COALESCE(status, 'active'), COALESCE(source, 'legacy_edges_archive'), "
            "       COALESCE(created_at, now()), COALESCE(last_seen, now()) "
            f"  FROM {ARCHIVE_TABLE} "
            " ON CONFLICT DO NOTHING"
        )
    )
    print(f"[{revision}] recreated {TABLE} and restored {result.rowcount} archived row(s)")
