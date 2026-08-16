"""trk067_rag_knowledge_store

Revision ID: 1562b407c9cb
Revises: c534a2dd6870
Create Date: 2026-07-21 21:52:08.367779

Adds the RAG knowledge-store schema:
  * six new metadata columns + a ``status`` column on ``documents``
  * a new ``document_chunks`` table with a pgvector embedding column
  * an HNSW (cosine) index on the embedding column (postgres only)
  * supporting plain indexes

The migration is inspector-guarded so it is safe on fresh installs (where
``create_all`` has already built the tables/columns) as well as on existing
databases. All vector-specific paths are postgres-only; on sqlite the
embedding column falls back to JSON and the HNSW index is skipped.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from infra_brain.db.models import _EMBED_DIM


# revision identifiers, used by Alembic.
revision: str = "1562b407c9cb"
down_revision: Union[str, Sequence[str], None] = "c534a2dd6870"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# New nullable metadata columns on ``documents`` (name -> column type factory).
_NULLABLE_DOCUMENT_COLUMNS = [
    ("external_id", lambda: sa.String(length=128)),
    ("space", lambda: sa.String(length=64)),
    ("url", lambda: sa.String(length=1024)),
    ("source_version", lambda: sa.Integer()),
    ("content_hash", lambda: sa.String(length=64)),
    ("last_updated", lambda: sa.DateTime(timezone=True)),
]


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    inspector = inspect(bind)

    # --- documents: new columns -------------------------------------------
    existing_document_columns = {col["name"] for col in inspector.get_columns("documents")}

    for name, type_factory in _NULLABLE_DOCUMENT_COLUMNS:
        if name not in existing_document_columns:
            op.add_column(
                "documents",
                sa.Column(name, type_factory(), nullable=True),
            )

    if "status" not in existing_document_columns:
        op.add_column(
            "documents",
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="current",
            ),
        )

    # --- document_chunks: new table ---------------------------------------
    if is_pg:
        from pgvector.sqlalchemy import Vector

        embedding_type = Vector(_EMBED_DIM)
    else:
        embedding_type = sa.JSON()

    if "document_chunks" not in inspector.get_table_names():
        op.create_table(
            "document_chunks",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("document_id", sa.Uuid(), nullable=False),
            sa.Column(
                "chunk_index",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("embedding", embedding_type, nullable=True),
            sa.Column("token_count", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(
                ["document_id"],
                ["documents.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    # --- indexes ----------------------------------------------------------
    document_indexes = {ix["name"] for ix in inspector.get_indexes("documents")}
    if "ix_documents_external_id" not in document_indexes:
        op.create_index("ix_documents_external_id", "documents", ["external_id"])
    if "ix_documents_space" not in document_indexes:
        op.create_index("ix_documents_space", "documents", ["space"])

    # document_chunks indexes -- re-inspect since the table may be new.
    inspector = inspect(bind)
    chunk_indexes = {ix["name"] for ix in inspector.get_indexes("document_chunks")}
    if "ix_document_chunks_document_id" not in chunk_indexes:
        op.create_index(
            "ix_document_chunks_document_id",
            "document_chunks",
            ["document_id"],
        )

    if is_pg and "ix_document_chunks_embedding_hnsw" not in chunk_indexes:
        op.create_index(
            "ix_document_chunks_embedding_hnsw",
            "document_chunks",
            ["embedding"],
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    inspector = inspect(bind)

    chunk_indexes = (
        {ix["name"] for ix in inspector.get_indexes("document_chunks")}
        if "document_chunks" in inspector.get_table_names()
        else set()
    )

    if is_pg and "ix_document_chunks_embedding_hnsw" in chunk_indexes:
        op.drop_index(
            "ix_document_chunks_embedding_hnsw",
            table_name="document_chunks",
        )
    if "ix_document_chunks_document_id" in chunk_indexes:
        op.drop_index(
            "ix_document_chunks_document_id",
            table_name="document_chunks",
        )

    if "document_chunks" in inspector.get_table_names():
        op.drop_table("document_chunks")

    document_indexes = {ix["name"] for ix in inspector.get_indexes("documents")}
    if "ix_documents_space" in document_indexes:
        op.drop_index("ix_documents_space", table_name="documents")
    if "ix_documents_external_id" in document_indexes:
        op.drop_index("ix_documents_external_id", table_name="documents")

    existing_document_columns = {col["name"] for col in inspector.get_columns("documents")}

    if "status" in existing_document_columns:
        op.drop_column("documents", "status")

    for name, _ in reversed(_NULLABLE_DOCUMENT_COLUMNS):
        if name in existing_document_columns:
            op.drop_column("documents", name)
