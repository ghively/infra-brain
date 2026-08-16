"""widen_document_chunks_embedding_to_1536

Revision ID: 06e3b86260d5
Revises: f8dfd053ec66
Create Date: 2026-08-02 16:53:22.102950

Widens document_chunks.embedding from Vector(1024) to Vector(1536).

Reason: the RAG knowledge store (TRK-067) originally targeted a 1024-dim
embedding model. The live LLM gateway now in use (an OpenAI-compatible
router) only serves Gemini-family embedding models, which natively emit
3072-dim vectors -- verified live; no 1024-dim option exists there.

3072 itself is NOT usable directly: pgvector's HNSW index has a hard cap of
2000 dimensions (confirmed live -- CREATE INDEX ... USING hnsw fails with
"column cannot have more than 2000 dimensions for hnsw index" above that).
1536 was chosen as a safe value under that cap; the embeddings factory
(infra_brain/embeddings.py, _TruncatedEmbeddings) truncates the model's
native 3072-dim output to the first 1536 dims and L2-renormalizes
client-side, since the gateway also rejects the OpenAI `dimensions`
server-side truncation param outright (verified live, separately).

document_chunks is empty in every environment as of this migration (no
Confluence credentials have ever been configured, so RAG ingestion has
never run) -- there is no data to migrate or lose. The migration is still
written generically (drop index -> alter type -> recreate index) rather
than assuming emptiness, matching this repo's existing migration style.

A plain ALTER COLUMN TYPE on a pgvector column fails while an HNSW index
built against the old dimension still exists, so the index must be
dropped first and recreated after, same as how it was first built in
1562b407c9cb (m=16, ef_construction=64, vector_cosine_ops -- unchanged
here, only the column width changes). Postgres-only, inspector-guarded,
matching 1562b407c9cb's pattern; sqlite's embedding column is plain JSON
(via with_variant) and is untouched by this migration.
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

from infra_brain.db.models import _EMBED_DIM

# revision identifiers, used by Alembic.
revision: str = "06e3b86260d5"
down_revision: Union[str, Sequence[str], None] = "f8dfd053ec66"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "ix_document_chunks_embedding_hnsw"
_OLD_DIM = 1024


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if not is_pg:
        return  # sqlite's embedding column is JSON -- nothing to widen.

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
    against a populated table with genuine 1536-dim vectors, narrowing to
    vector(1024) FAILS LOUDLY (pgvector rejects the narrowing ALTER outright)
    rather than silently truncating/corrupting data -- the hard failure is
    the safe outcome, not a defect to guard against here.
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
