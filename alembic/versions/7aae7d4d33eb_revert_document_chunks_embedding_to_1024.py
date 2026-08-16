"""revert_document_chunks_embedding_to_1024

Revision ID: 7aae7d4d33eb
Revises: 06e3b86260d5
Create Date: 2026-08-02 23:30:00.000000

Reverts document_chunks.embedding from Vector(1536) back to Vector(1024).

Reason: 06e3b86260d5 (earlier today) widened this column to 1536 to
accommodate a cloud gateway's Gemini embedding model (natively 3072-dim,
truncated client-side). That gateway proxies Gemini's free tier, which
caps at 100 embed_content requests/minute -- a single bulk-ingest run of a
few hundred documents exceeds it immediately and makes bulk ingestion
impractical even with retry/backoff (retrying the SAME oversized batch
just hits the same wall).

Switched instead to a local Ollama instance (gpu-host, mxbai-embed-large)
which natively emits exactly 1024 dims -- no truncation needed, and no
external rate limit at all (local inference). This migration reverts to
the ORIGINAL RAG knowledge-store dimension, which mxbai-embed-large was specifically
chosen to match.

document_chunks is empty in every environment (confirmed again at this
migration's authoring time) -- there is no data to migrate or lose.

Same technique as 06e3b86260d5 / 1562b407c9cb: the HNSW index must be
dropped before ALTER COLUMN TYPE and recreated after (pgvector rejects a
type change while a dependent index exists). Postgres-only,
inspector-guarded; sqlite's embedding column is plain JSON and untouched.
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

from infra_brain.db.models import _EMBED_DIM

# revision identifiers, used by Alembic.
revision: str = "7aae7d4d33eb"
down_revision: Union[str, Sequence[str], None] = "06e3b86260d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "ix_document_chunks_embedding_hnsw"
_OLD_DIM = 1536


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if not is_pg:
        return  # sqlite's embedding column is JSON -- nothing to change.

    inspector = inspect(bind)
    chunk_indexes = {ix["name"] for ix in inspector.get_indexes("document_chunks")}

    if _INDEX_NAME in chunk_indexes:
        op.drop_index(_INDEX_NAME, table_name="document_chunks")

    op.execute(f"ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector({_EMBED_DIM})")

    op.create_index(
        _INDEX_NAME,
        "document_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    """Downgrade schema.

    Safe today: document_chunks is empty everywhere. If this ever runs
    against a populated table with genuine 1024-dim vectors, widening to
    vector(1536) succeeds (widening never fails on existing data) but
    every prior row's vector stays 1024-wide with 512 implicit trailing
    zero dims -- cosine similarity is unaffected by trailing zeros, but a
    full re-embed at the new width would still be needed to actually use
    the extra dimensions for anything.
    """
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if not is_pg:
        return

    inspector = inspect(bind)
    chunk_indexes = {ix["name"] for ix in inspector.get_indexes("document_chunks")}

    if _INDEX_NAME in chunk_indexes:
        op.drop_index(_INDEX_NAME, table_name="document_chunks")

    op.execute(f"ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector({_OLD_DIM})")

    op.create_index(
        _INDEX_NAME,
        "document_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
