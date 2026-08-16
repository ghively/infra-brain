from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool, text
from alembic import context
from infra_brain.config import get_settings
from infra_brain.db.models import Base

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=True (the fileConfig default) sets .disabled=True
    # on every logger that already exists at import time — including
    # infra_brain.* application loggers — and that flag persists for the rest
    # of the process. When test_migration_zone (or any test that imports
    # alembic.env) runs before other tests in the same pytest session, it was
    # silently muting application logging suite-wide. Keep existing loggers
    # enabled; this file only wants to configure its own formatters/handlers.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# 2026-07-30 incident: `compare_server_default` was never set here, so
# `alembic check`/`alembic revision --autogenerate` (and the CI
# `migration-parity` gate, which uses this same env.py) were structurally
# blind to server_default drift — while `db/schema_check.py`'s runtime
# deploy-gate `_full_compare()` DID pass `compare_server_default=True` to its
# own `compare_metadata` call. Result: 46 server_default mismatches across 18
# tables accumulated silently for an unknown period, invisible to every
# migration-authoring/CI tool, only surfacing when the stricter runtime gate
# finally caught it and blocked a deploy. Fixed by aligning env.py's
# comparison config with schema_check.py's — see
# fix/schema-drift-server-default-and-live-reconcile.
#
# Objects that live in the database but are NOT owned by our ORM metadata, so
# autogenerate (and `alembic check`) must ignore them — otherwise every new
# migration spuriously tries to DROP them:
#   * LangGraph's Postgres checkpointer tables (created at runtime by the
#     checkpointer library, never by our models).
#   * Functional/partial indexes created directly in migrations that the ORM
#     Index() construct can't fully round-trip (trigram GIN, partial WHERE).
#   * ``legacy_edges_archive`` — created CONDITIONALLY by the P5 migration
#     (``95d988b2bc3c``) and only on a database holding a relationship type its
#     derivability audit never classified. It is a migration-owned evidence
#     table with no ORM model on purpose: giving it one would put a "living"
#     table in the schema for something whose entire job is to be dead. It does
#     not exist on any database that matches the audit (production included),
#     but where it DOES exist, autogenerate would otherwise propose dropping the
#     one thing P5 went out of its way to preserve.
_IGNORE_TABLES = {
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
    "legacy_edges_archive",
}
# ``ix_rr_status_archived`` left the set with its table: P5 dropped
# ``resource_relationships``, so neither side of the comparison can produce it.
#
# ``ix_document_chunks_text_tsv`` (TRK-297 R6) joins them: it is a GIN index
# over ``document_chunks.text_tsv``, a PostgreSQL-only
# ``GENERATED ALWAYS AS (to_tsvector(...)) STORED`` column that is deliberately
# NOT in ``Base.metadata`` (SQLite validates generated-column expressions at
# CREATE TABLE time and has no ``to_tsvector``, so modelling it would break
# ``create_all()`` for the entire SQLite suite). Without these two entries every
# future autogenerate would propose dropping both.
_IGNORE_INDEXES = {
    "ix_resources_name_trgm",
    "ix_legacy_edges_archive_type",
    "ix_document_chunks_text_tsv",
}
# (table, column) pairs owned by a migration rather than by the ORM. Mirror of
# ``infra_brain.db.schema_check._UNMODELED_COLUMNS`` — the two comparison
# configs must stay aligned (see the 2026-07-30 server_default incident note
# above for what happens when they drift apart).
_IGNORE_COLUMNS = {("document_chunks", "text_tsv")}


def _include_object(object, name, type_, reflected, compare_to):  # noqa: A002
    if type_ == "table" and name in _IGNORE_TABLES:
        return False
    if type_ == "index" and name in _IGNORE_INDEXES:
        return False
    if type_ == "column":
        table = getattr(getattr(object, "table", None), "name", None)
        if (table, name) in _IGNORE_COLUMNS:
            return False
    return True


def _resolve_url() -> str:
    """The migration target MUST equal the DSN the application uses.

    Historically env.py only overrode ``sqlalchemy.url`` when it was empty or the
    ``driver://...`` placeholder from alembic.ini. That left the door open for the
    ``migrate`` step to run against a *different* database than the live app: the
    placeholder fall-through depended on whatever ``POSTGRES_URL``/``DATABASE_URL``
    happened to resolve to in the migrate container at deploy time, which is not
    guaranteed to be the volume the app later adopts. The migrate step then
    "succeeded" against a throwaway/empty DB while the live DB stayed unmigrated.

    Fix: bind the migration URL unconditionally to ``get_settings().postgres_url``
    — the exact accessor the application uses — so migrate and app can never
    target divergent databases. Tests that need a different URL set
    ``POSTGRES_URL`` in the environment (get_settings() reads it), which keeps the
    single source of truth intact.
    """
    return get_settings().postgres_url


def run_migrations_online():
    # Bind the migration URL to the app's real DSN (single source of truth).
    config.set_main_option("sqlalchemy.url", _resolve_url())
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            compare_server_default=True,
        )
        with context.begin_transaction():
            # IMPORTANT: issue lock_timeout INSIDE alembic's transaction block.
            #
            # In SQLAlchemy 2.0 a bare ``connection.execute(...)`` autobegins a
            # transaction on the connection. Running ``SET lock_timeout`` BEFORE
            # ``context.begin_transaction()`` therefore opened a transaction that
            # alembic did not own: on PostgreSQL (transactional DDL) every
            # migration's DDL ran inside that stray transaction, and with the
            # NullPool engine the connection closed WITHOUT a commit — so the DBAPI
            # rolled the whole thing back. Migrations logged "success" while the
            # live database stayed empty/unmigrated (the silent drift behind the
            # r7_vulnerabilities.resource_id 500 and the "create_all makes new
            # tables but ALTERs/indexes never land" symptom). The new
            # migration-parity CI gate caught it: alembic head reflected zero
            # tables against the very DSN it had just "upgraded".
            #
            # Setting it here makes the statement part of the transaction alembic
            # actually commits, so the SET and all subsequent DDL persist together.
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET lock_timeout = '4s'"))
            context.run_migrations()


def run_migrations_offline():
    # Same single-source-of-truth URL as online mode (offline emits SQL only).
    url = _resolve_url()
    config.set_main_option("sqlalchemy.url", url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
