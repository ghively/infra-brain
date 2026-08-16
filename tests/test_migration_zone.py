"""Live-volume proof for the zone-canonicalize repair migration.

The bug this test guards against is the exact production 500 pattern
documented in ``alembic/versions/0021_repair_r7_resource_id_drift.py``: a
migration that only stamps ``alembic_version`` forward is a NO-OP on a DB
that is already at (or past) that revision. If the ``corporate`` -> ``corpor``
data normalization had been embedded in an already-passed revision, it would
never execute against a live DB already stamped at head.

To prove the repair migration actually reaches a *live* volume (not merely a
fresh ``create_all``), this test:

1. Builds the full ORM schema via ``Base.metadata.create_all`` (so every
   table/column exists, exactly like a real deployed DB).
2. Stamps ``alembic_version`` at the OLD head (``f6a7b8c9d0e1`` —
   ``0023_netdiscovery_tables``, the head immediately BEFORE this repair
   revision) instead of replaying the whole chain. This simulates a live DB
   that was migrated up through 0023 by earlier deploys, before this repair
   revision existed.
3. Inserts rows with ``zone='corporate'`` directly into ``instincts`` and
   ``resources`` (the pre-fix data any live DB may still be holding), plus a
   ``corpor`` row and an unrelated-value row that must NOT be touched.
4. Runs ``alembic upgrade head``.
5. Asserts the ``corporate`` rows flipped to ``corpor``, everything else is
   untouched, and ``alembic_version`` advanced to the new head.

Because the new revision is a FRESH HEAD (not an edit to an already-passed
revision), step 4 is guaranteed to execute the data UPDATE regardless of
what revision the live DB was previously stamped at.
"""

import os
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import pytest
from alembic import command
from alembic.config import Config

# The head BEFORE the zone-canonicalize repair revision (0023_netdiscovery_tables).
_OLD_HEAD = "f6a7b8c9d0e1"
# The zone-canonicalize revision itself (0024_zone_canonicalize). This test
# exercises THAT migration on a live create_all'd volume; it must upgrade to
# exactly this revision, NOT "head" — later schema-adding migrations would
# otherwise re-CREATE tables create_all already built (SQLite: "table exists").
_ZONE_HEAD = "a7b8c9d0e1f2"


def _alembic_config(sqlite_url: str) -> Config:
    """Alembic Config pointed at the workspace's alembic/ dir.

    env.py binds sqlalchemy.url unconditionally to
    ``get_settings().postgres_url`` (single source of truth — see env.py's
    ``_resolve_url``), so pointing alembic at a temp SQLite DB means setting
    POSTGRES_URL and clearing the get_settings() lru_cache, exactly like
    tests/test_e2e_smoke.py does. The Config object's own sqlalchemy.url is
    irrelevant once env.py overrides it, but we still construct a valid one so
    offline/CLI-style introspection doesn't choke on the alembic.ini
    placeholder.
    """
    os.environ["POSTGRES_URL"] = sqlite_url

    import infra_brain.config as _config_mod

    _config_mod.get_settings.cache_clear()

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", sqlite_url)
    return cfg


@pytest.fixture
def zone_migration_db(tmp_path):
    """A temp SQLite DB with the full ORM schema, stamped at the OLD head."""
    db_path = tmp_path / "zone_migration.db"
    sqlite_url = f"sqlite:///{db_path}"

    from infra_brain.db.models import Base

    engine = sa.create_engine(sqlite_url)
    Base.metadata.create_all(engine)

    cfg = _alembic_config(sqlite_url)
    # Stamp at the OLD head only — do NOT replay the whole chain on SQLite
    # (several early revisions issue ALTER TABLE / ADD CONSTRAINT DDL that
    # SQLite cannot apply outside batch mode). create_all() already built the
    # up-to-date schema; the stamp simulates "this DB has already been
    # migrated up through 0023 by earlier deploys."
    command.stamp(cfg, _OLD_HEAD)

    yield engine, cfg, sqlite_url

    os.environ.pop("POSTGRES_URL", None)
    import infra_brain.config as _config_mod

    _config_mod.get_settings.cache_clear()


def _insert_zone_row(engine, table: str, zone_value: str) -> str:
    row_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        if table == "instincts":
            conn.execute(
                sa.text(
                    "INSERT INTO instincts "
                    "(id, zone, domain, pattern, confidence, promoted_by, promoted_at) "
                    "VALUES (:id, :zone, 'test-domain', 'test-pattern', 0.9, "
                    "'test-suite', :now)"
                ),
                {"id": row_id, "zone": zone_value, "now": now},
            )
        elif table == "resources":
            conn.execute(
                sa.text(
                    "INSERT INTO resources (id, domain, type, name, source, zone, last_seen) "
                    "VALUES (:id, 'test-domain', 'test_type', :name, 'test-suite', :zone, :now)"
                ),
                {"id": row_id, "name": f"host-{row_id[:8]}", "zone": zone_value, "now": now},
            )
        elif table == "net_discovery_hosts":
            octet = int(row_id[:2], 16) % 255
            conn.execute(
                sa.text(
                    "INSERT INTO net_discovery_hosts "
                    "(id, ip, zone, first_seen, last_seen, responded, discovery_tier, "
                    "is_fragile, is_known, is_shadow_it, threat_level) "
                    "VALUES (:id, :ip, :zone, :now, :now, 0, 'passive', 0, 0, 0, 'none')"
                ),
                {"id": row_id, "ip": f"10.0.0.{octet}", "zone": zone_value, "now": now},
            )
        else:
            raise ValueError(f"unsupported table in test helper: {table}")
    return row_id


def _zone_of(engine, table: str, row_id: str) -> str:
    with engine.connect() as conn:
        result = conn.execute(
            sa.text(f"SELECT zone FROM {table} WHERE id = :id"), {"id": row_id}
        ).scalar_one()
    return result


def _current_alembic_version(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()


def test_upgrade_head_normalizes_corporate_rows_on_live_volume(zone_migration_db):
    """The repair migration flips zone='corporate' -> 'corpor' on a DB already
    stamped at the old head — proving it applies to a LIVE volume, not just a
    fresh create_all(). This is the FIRST assertion that must fail before the
    new revision exists (a plain `upgrade head` at the old head is a no-op)."""
    engine, cfg, _url = zone_migration_db

    corporate_instinct_id = _insert_zone_row(engine, "instincts", "corporate")
    corporate_resource_id = _insert_zone_row(engine, "resources", "corporate")
    corporate_host_id = _insert_zone_row(engine, "net_discovery_hosts", "corporate")

    # Control rows: must be left untouched by the migration.
    corpor_resource_id = _insert_zone_row(engine, "resources", "corpor")
    other_resource_id = _insert_zone_row(engine, "resources", "in-zone")

    # Sanity: confirm the DB is stamped at the OLD head before upgrading.
    assert _current_alembic_version(engine) == _OLD_HEAD

    command.upgrade(cfg, _ZONE_HEAD)

    # (a) previously-'corporate' rows now read 'corpor'.
    assert _zone_of(engine, "instincts", corporate_instinct_id) == "corpor"
    assert _zone_of(engine, "resources", corporate_resource_id) == "corpor"
    assert _zone_of(engine, "net_discovery_hosts", corporate_host_id) == "corpor"

    # (b) rows that were already 'corpor' or some other value are unchanged.
    assert _zone_of(engine, "resources", corpor_resource_id) == "corpor"
    assert _zone_of(engine, "resources", other_resource_id) == "in-zone"

    # (c) alembic_version advanced past the old head (new fresh-head revision).
    new_head = _current_alembic_version(engine)
    assert new_head != _OLD_HEAD


def test_upgrade_head_is_idempotent(zone_migration_db):
    """Running `upgrade head` twice must be safe (no error, same end state)."""
    engine, cfg, _url = zone_migration_db

    corporate_resource_id = _insert_zone_row(engine, "resources", "corporate")

    command.upgrade(cfg, _ZONE_HEAD)
    assert _zone_of(engine, "resources", corporate_resource_id) == "corpor"

    # Second upgrade (already at head) — must be a safe no-op.
    command.upgrade(cfg, _ZONE_HEAD)
    assert _zone_of(engine, "resources", corporate_resource_id) == "corpor"
