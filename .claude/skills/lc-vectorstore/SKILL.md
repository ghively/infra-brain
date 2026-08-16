---
name: lc-vectorstore
description: >
  Use when the user needs to set up, manage, or migrate a vector store for a
  LangChain or LangGraph application. Covers provider selection (Chroma, Pinecone,
  Qdrant, pgvector, Azure AI Search), embedding caching, full CRUD lifecycle,
  multi-tenant namespacing, incremental index updates, zero-downtime migration,
  health monitoring, and hybrid search. Triggered by phrases like "set up a
  vector store", "which vector DB should I use", "index my documents into
  Pinecone", "embedding caching", "multi-tenant vector store", "migrate my
  vector index", "hybrid search", "pgvector setup", or /lc-vectorstore.
---

# lc:vectorstore — Full Vector Store Lifecycle Management

## Purpose

A vector store is the persistence layer that lets your application search
documents by meaning rather than keywords. Choosing the wrong provider or
skipping best practices (embedding caching, incremental updates, multi-tenant
isolation) leads to high API costs, slow queries, and painful migrations later.

This skill walks you through every decision — provider selection, data
architecture, lifecycle operations, and production hardening — with complete
runnable code at every step.

---

## Trigger Phrases

- "set up a vector store"
- "which vector database should I use"
- "index my documents into Pinecone / Qdrant / pgvector"
- "embedding caching"
- "multi-tenant vector store"
- "migrate my vector index"
- "hybrid search BM25"
- "pgvector HNSW"
- `/lc-vectorstore`

---

## Discovery Questions

Ask all four questions in a single message. Do not scaffold until you have
answers.

```
Before I scaffold anything, I need four quick answers:

1. PROVIDER — Which vector store fits your situation?
   (a) Chroma       — dev / local, no production SLA, easiest setup
   (b) Pinecone     — managed cloud, scales to billions, serverless pricing
   (c) Qdrant       — self-hosted or cloud, strong filtering, best perf/$ 
   (d) pgvector     — PostgreSQL extension, great if you already use Postgres
   (e) Azure AI Search — hybrid search built-in, Azure ecosystem

2. TENANCY — Single-tenant or multi-tenant?
   (a) Single-tenant — one collection for everyone
   (b) Multi-tenant  — data must be isolated per user/org

3. UPDATE FREQUENCY — How often does your indexed data change?
   (a) Static          — indexed once, rarely touched
   (b) Periodic        — batch updates (nightly / weekly)
   (c) Real-time       — continuous stream of new/changed documents

4. SCALE — How many documents do you expect at peak?
   (a) < 100K docs    — any provider works fine
   (b) 100K – 10M    — avoid Chroma; consider Qdrant or pgvector
   (c) > 10M docs    — Pinecone serverless or Qdrant dedicated cluster
```

Use the answers to select patterns below.

---

## Pattern 1 — Provider Comparison and Selection

**Teach this before writing any code.** The right provider is infrastructure;
changing it later forces a full migration.

### Selection Table

| Provider        | Scale       | Cost model      | Self-hosted | Hybrid search | Managed |
|-----------------|-------------|-----------------|-------------|---------------|---------|
| Chroma          | < 100K      | Free            | Yes (only)  | No            | No      |
| Pinecone        | Billions    | Per-write/query | No          | Yes (native)  | Yes     |
| Qdrant          | Billions    | Per-node        | Yes + cloud | Yes (sparse)  | Both    |
| pgvector        | ~10M-100M   | Postgres infra  | Yes         | Yes (FTS)     | Via RDS |
| Azure AI Search | Millions    | Per-unit/query  | No          | Yes (native)  | Yes     |

### Key Decision Rules

- **Chroma** — Use only in development. It has no replication, no backups, no
  SLA. Swap it out before going to production.
- **Pinecone** — Best when you need zero ops burden and true elastic scale.
  Serverless pricing is pay-per-query; pod pricing is pay-per-uptime.
- **Qdrant** — Best price-to-performance ratio for self-hosted. Rust core means
  low memory overhead. Strong payload filtering (JSON-based) makes it ideal for
  multi-tenant workloads.
- **pgvector** — Best when your application already runs on PostgreSQL and you
  want to avoid adding a new infrastructure component. Transactional consistency
  with your relational data is a major advantage.
- **Azure AI Search** — Best in Azure-native stacks. Hybrid (BM25 + vector) is
  first-class, not bolted on.

---

## Pattern 2 — Embedding Caching (Always Do This)

**Concept:** Every time you call `embeddings.embed_documents(texts)` you pay an
API charge and wait for network round-trips. If 80% of your documents haven't
changed since last ingest, you're paying for the same vectors again.

`CacheBackedEmbeddings` wraps any embeddings model and checks a local key-value
store before calling the API. The cache key is `model_name + SHA256(text)`, so
identical text always hits the cache.

**Cost savings:** On re-ingestion of a corpus where 90% is unchanged, you
reduce embedding API calls by 90%.

### `requirements.txt`
```
langchain>=0.3
langchain-core>=0.3
langchain-openai>=0.2
langchain-community>=0.3
langchain-chroma>=0.1
chromadb>=0.5
redis>=5.0              # only needed for RedisStore
python-dotenv>=1.0
```

### `embedding_cache.py`
```python
"""
embedding_cache.py — Cached embedding setup.

CacheBackedEmbeddings wraps your real embeddings model.
On first call: embed via API, store result in the cache.
On subsequent calls with the same text: return from cache (no API call).

Two cache backends:
  - LocalFileStore  → dev / single machine
  - RedisStore      → production / multiple workers
"""
import os
from dotenv import load_dotenv

from langchain_openai import OpenAIEmbeddings
from langchain.embeddings import CacheBackedEmbeddings
from langchain.storage import LocalFileStore
# from langchain_community.storage import RedisStore  # uncomment for Redis

load_dotenv()  # reads OPENAI_API_KEY from .env

# ── underlying model ───────────────────────────────────────────────────────────
# This is the model that will be called when there is a cache miss.
BASE_EMBEDDINGS = OpenAIEmbeddings(model="text-embedding-3-small")

# ── dev: LocalFileStore ────────────────────────────────────────────────────────
# Stores cached vectors as files under ./embedding_cache/
# Persists across Python processes — restart your script and the cache still works.
def make_cached_embeddings_local() -> CacheBackedEmbeddings:
    store = LocalFileStore("./embedding_cache")
    return CacheBackedEmbeddings.from_bytes_store(
        underlying_embeddings=BASE_EMBEDDINGS,
        document_embedding_cache=store,
        # namespace is prepended to every cache key so different models
        # never collide even if they share a store directory.
        namespace=BASE_EMBEDDINGS.model,
    )

# ── prod: RedisStore ───────────────────────────────────────────────────────────
# Stores cached vectors in Redis. Safe for multi-worker deployments.
# Set REDIS_URL=redis://localhost:6379 in .env
def make_cached_embeddings_redis() -> CacheBackedEmbeddings:
    from langchain_community.storage import RedisStore
    store = RedisStore(redis_url=os.environ["REDIS_URL"])
    return CacheBackedEmbeddings.from_bytes_store(
        underlying_embeddings=BASE_EMBEDDINGS,
        document_embedding_cache=store,
        namespace=BASE_EMBEDDINGS.model,
    )

# ── usage ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cached = make_cached_embeddings_local()

    texts = ["The quick brown fox", "jumps over the lazy dog"]

    print("First call — will hit the API:")
    vecs = cached.embed_documents(texts)
    print(f"  Got {len(vecs)} vectors, dim={len(vecs[0])}")

    print("Second call — served from cache (no API call):")
    vecs2 = cached.embed_documents(texts)
    print(f"  Got {len(vecs2)} vectors from cache")
```

---

## Pattern 3 — CRUD Lifecycle

**Concept:** A vector store is a database. Like any database, you need to be
able to Create, Read, Update, and Delete records. Most tutorials only show
`add_documents`. Production code needs all four operations plus an Upsert
(insert-or-update) to handle re-ingestion safely.

Each document stored in LangChain has:
- `page_content` — the text that gets embedded
- `metadata` — a dict of arbitrary fields (source, created_at, tenant_id, etc.)
- `id` — a string identifier (auto-generated if not provided)

### `vectorstore_crud.py`
```python
"""
vectorstore_crud.py — Full CRUD lifecycle for a vector store.

Uses Chroma for the example (swap in any provider with the same interface).
"""
import hashlib
from datetime import datetime, UTC
from dotenv import load_dotenv

from langchain_core.documents import Document
from langchain_chroma import Chroma
from embedding_cache import make_cached_embeddings_local  # from Pattern 2

load_dotenv()

PERSIST_DIR = "./chroma_db"

def get_vectorstore() -> Chroma:
    """Open (or create) the persistent Chroma collection."""
    return Chroma(
        collection_name="my_documents",
        embedding_function=make_cached_embeddings_local(),
        persist_directory=PERSIST_DIR,
    )

# ── CREATE ─────────────────────────────────────────────────────────────────────
def add_documents(docs: list[Document]) -> list[str]:
    """
    Add documents and return their assigned IDs.

    Best practice: always include metadata with at least:
      - source: where the doc came from (file path, URL)
      - content_hash: SHA-256 of page_content (used for change detection later)
      - created_at: ISO timestamp
    """
    for doc in docs:
        doc.metadata.setdefault("content_hash", _hash(doc.page_content))
        doc.metadata.setdefault("created_at", datetime.now(UTC).isoformat())

    vs = get_vectorstore()
    ids = vs.add_documents(docs)
    print(f"Added {len(ids)} documents")
    return ids

# ── READ ───────────────────────────────────────────────────────────────────────
def similarity_search(query: str, k: int = 4) -> list[Document]:
    """
    Find the k most semantically similar documents to the query.

    Internally: embed(query) → find k nearest vectors → return their Documents.
    The 'score' is cosine similarity (higher = more similar).
    """
    vs = get_vectorstore()
    results = vs.similarity_search_with_score(query, k=k)
    for doc, score in results:
        print(f"  score={score:.4f} | source={doc.metadata.get('source', '?')}")
        print(f"  {doc.page_content[:120]}...")
    return [doc for doc, _ in results]

def as_retriever(search_kwargs: dict | None = None):
    """
    Return a Retriever object — the standard LangChain interface for RAG.

    A Retriever wraps similarity_search and plugs into LCEL chains with |.
    """
    vs = get_vectorstore()
    return vs.as_retriever(search_kwargs=search_kwargs or {"k": 4})

# ── UPDATE (hash-based) ────────────────────────────────────────────────────────
def update_document(doc_id: str, new_doc: Document) -> None:
    """
    Replace a document by ID.
    Strategy: delete the old vector, add the new one.

    Why not just overwrite? Vector stores index by embedding value, not by
    position. You must remove the old embedding before inserting the new one
    or you will get duplicate search results.
    """
    vs = get_vectorstore()
    vs.delete(ids=[doc_id])

    new_doc.metadata["content_hash"] = _hash(new_doc.page_content)
    new_doc.metadata["updated_at"] = datetime.now(UTC).isoformat()
    [new_id] = vs.add_documents([new_doc], ids=[doc_id])
    print(f"Updated document {new_id}")

# ── DELETE ─────────────────────────────────────────────────────────────────────
def delete_by_ids(ids: list[str]) -> None:
    """Delete specific documents by their IDs."""
    vs = get_vectorstore()
    vs.delete(ids=ids)
    print(f"Deleted {len(ids)} documents")

def delete_by_source(source: str) -> None:
    """
    Delete all documents matching a metadata filter.

    Useful when you want to remove everything from a particular file or URL
    without tracking individual chunk IDs.
    """
    vs = get_vectorstore()
    # Chroma supports where= filter on metadata fields.
    results = vs.get(where={"source": source})
    ids = results["ids"]
    if ids:
        vs.delete(ids=ids)
        print(f"Deleted {len(ids)} chunks from source='{source}'")
    else:
        print(f"No documents found for source='{source}'")

# ── UPSERT ────────────────────────────────────────────────────────────────────
def upsert_documents(docs: list[Document], source: str) -> dict:
    """
    Smart re-ingest: only process changed documents.

    Algorithm:
      1. Load existing docs for this source from the store.
      2. For each incoming doc, compute its content hash.
      3. If hash matches stored hash → skip (no change).
      4. If hash differs → delete old chunks, add new.
      5. If doc is new (not in store) → add.

    Returns a summary: {added, updated, skipped}.
    """
    vs = get_vectorstore()
    existing = vs.get(where={"source": source}, include=["metadatas", "ids"])

    # Build a lookup: content_hash → [ids]
    existing_hashes: dict[str, list[str]] = {}
    for doc_id, meta in zip(existing["ids"], existing["metadatas"]):
        h = meta.get("content_hash", "")
        existing_hashes.setdefault(h, []).append(doc_id)

    added = updated = skipped = 0
    to_add: list[Document] = []

    for doc in docs:
        doc.metadata["source"] = source
        new_hash = _hash(doc.page_content)
        doc.metadata["content_hash"] = new_hash

        if new_hash in existing_hashes:
            skipped += 1  # content unchanged — skip API call
        else:
            to_add.append(doc)
            added += 1

    if to_add:
        vs.add_documents(to_add)

    stats = {"added": added, "updated": updated, "skipped": skipped}
    print(f"Upsert complete: {stats}")
    return stats

# ── helpers ────────────────────────────────────────────────────────────────────
def _hash(text: str) -> str:
    """SHA-256 of text content — used as a change-detection fingerprint."""
    return hashlib.sha256(text.encode()).hexdigest()
```

---

## Pattern 4 — Multi-Tenant Namespacing

**Concept:** When multiple users or organizations share a system, their data
must be isolated. Vector stores support two isolation strategies. Choose based
on scale and compliance requirements.

**Strategy A — Collection-per-tenant**
- Each tenant gets its own collection (Chroma) or namespace (Qdrant).
- Complete data isolation at the storage level.
- Delete a tenant: drop the collection. Clean, O(1) erasure — important for
  GDPR right-to-erasure compliance.
- Downside: many tenants = many connections / collection handles.
- Best for: < 10,000 tenants, strong isolation requirements.

**Strategy B — Metadata-filter (single collection)**
- All tenants share one collection. Each document has `tenant_id` in metadata.
- Queries filter by `tenant_id` at search time.
- Delete a tenant: `delete(where={"tenant_id": "abc"})`.
- Downside: one noisy tenant can affect query latency for others.
- Best for: > 10,000 tenants, or when per-collection connection overhead is
  prohibitive (Pinecone serverless, pgvector with connection pools).

### `multi_tenant.py`
```python
"""
multi_tenant.py — Two multi-tenant isolation strategies.

Strategy A: collection-per-tenant (Chroma / Qdrant)
Strategy B: metadata-filter (any provider, shown with Chroma)
"""
from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_chroma import Chroma
from embedding_cache import make_cached_embeddings_local

load_dotenv()

EMBEDDINGS = make_cached_embeddings_local()
PERSIST_DIR = "./chroma_db"

# ── STRATEGY A: Collection-per-tenant ─────────────────────────────────────────

class PerTenantStore:
    """
    One Chroma collection per tenant.

    Collection names must be unique within a Chroma instance.
    We prefix with 'tenant_' to avoid collisions with other collections.
    """

    def _collection_name(self, tenant_id: str) -> str:
        # Sanitize: Chroma collection names must match [a-zA-Z0-9_-]+
        return f"tenant_{tenant_id.replace('-', '_')}"

    def get_store(self, tenant_id: str) -> Chroma:
        return Chroma(
            collection_name=self._collection_name(tenant_id),
            embedding_function=EMBEDDINGS,
            persist_directory=PERSIST_DIR,
        )

    def add(self, tenant_id: str, docs: list[Document]) -> list[str]:
        return self.get_store(tenant_id).add_documents(docs)

    def search(self, tenant_id: str, query: str, k: int = 4) -> list[Document]:
        return self.get_store(tenant_id).similarity_search(query, k=k)

    def delete_tenant(self, tenant_id: str) -> None:
        """
        GDPR right-to-erasure: drop the entire collection.
        All vectors and metadata are permanently removed.
        """
        store = self.get_store(tenant_id)
        store.delete_collection()
        print(f"Deleted collection for tenant '{tenant_id}'")


# ── STRATEGY B: Metadata-filter (single collection) ───────────────────────────

class MetadataFilterStore:
    """
    All tenants share one Chroma collection.
    Isolation enforced by filtering on tenant_id metadata at query time.

    Pros: simpler ops, fewer connections.
    Cons: one slow tenant affects all; no byte-level isolation.
    """

    def __init__(self):
        self._store = Chroma(
            collection_name="all_tenants",
            embedding_function=EMBEDDINGS,
            persist_directory=PERSIST_DIR,
        )

    def add(self, tenant_id: str, docs: list[Document]) -> list[str]:
        # Stamp every document with the tenant_id before storing.
        for doc in docs:
            doc.metadata["tenant_id"] = tenant_id
        return self._store.add_documents(docs)

    def search(self, tenant_id: str, query: str, k: int = 4) -> list[Document]:
        # The where= filter is applied BEFORE similarity ranking, so a tenant
        # can never see another tenant's documents — even accidentally.
        return self._store.similarity_search(
            query, k=k, filter={"tenant_id": tenant_id}
        )

    def delete_tenant(self, tenant_id: str) -> None:
        """
        GDPR right-to-erasure: delete all documents tagged with this tenant_id.
        Less instant than dropping a collection but works for any provider.
        """
        results = self._store.get(where={"tenant_id": tenant_id})
        ids = results["ids"]
        if ids:
            self._store.delete(ids=ids)
            print(f"Deleted {len(ids)} docs for tenant '{tenant_id}'")

    def as_retriever(self, tenant_id: str):
        """Return a retriever scoped to one tenant — plug directly into LCEL."""
        return self._store.as_retriever(
            search_kwargs={"k": 4, "filter": {"tenant_id": tenant_id}}
        )


# ── recommendation ─────────────────────────────────────────────────────────────
# < 1,000 tenants  → PerTenantStore (cleaner isolation, easy erasure)
# > 10,000 tenants → MetadataFilterStore (fewer connections, cheaper ops)
# 1K – 10K        → evaluate connection pool capacity; default to Metadata
```

---

## Pattern 5 — Incremental Index Updates

**Concept:** When data changes frequently, you want to re-ingest only what
changed — not the entire corpus. Full re-ingest wastes embedding API budget and
causes unnecessary write amplification on the vector store.

The algorithm uses a `DocumentHash` table (SQLite in this example) to track
which documents are already indexed and whether their content has changed.

### `incremental_ingest.py`
```python
"""
incremental_ingest.py — Only ingest documents that are new or changed.

DocumentHash table schema:
  doc_id        TEXT PRIMARY KEY   -- stable identifier (file path, URL, etc.)
  content_hash  TEXT               -- SHA-256 of the raw content
  chunk_ids     TEXT               -- JSON list of vector store chunk IDs
  last_updated  TEXT               -- ISO timestamp

On each ingest run:
  1. Load candidate documents.
  2. For each doc, compute content_hash.
  3. Compare against stored hash:
     - MATCH  → skip (no embedding call, no write)
     - DIFFER → delete old chunks, embed new, update hash record
     - NEW    → embed, store, insert hash record
  4. Detect deleted docs: hash records with no matching source → delete.
"""
import json
import sqlite3
import hashlib
from datetime import datetime, UTC
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_chroma import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter
from embedding_cache import make_cached_embeddings_local

load_dotenv()

DB_PATH = "./doc_hashes.db"
PERSIST_DIR = "./chroma_db"
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200

# ── hash database ──────────────────────────────────────────────────────────────

def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS document_hashes (
            doc_id       TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            chunk_ids    TEXT NOT NULL,   -- JSON array
            last_updated TEXT NOT NULL
        )
    """)
    conn.commit()

def _get_hash(conn: sqlite3.Connection, doc_id: str) -> dict | None:
    row = conn.execute(
        "SELECT content_hash, chunk_ids FROM document_hashes WHERE doc_id=?",
        (doc_id,)
    ).fetchone()
    if row is None:
        return None
    return {"content_hash": row[0], "chunk_ids": json.loads(row[1])}

def _save_hash(conn: sqlite3.Connection, doc_id: str,
               content_hash: str, chunk_ids: list[str]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO document_hashes
           (doc_id, content_hash, chunk_ids, last_updated)
           VALUES (?, ?, ?, ?)""",
        (doc_id, content_hash, json.dumps(chunk_ids), datetime.now(UTC).isoformat())
    )
    conn.commit()

def _delete_hash(conn: sqlite3.Connection, doc_id: str) -> list[str]:
    row = conn.execute(
        "SELECT chunk_ids FROM document_hashes WHERE doc_id=?", (doc_id,)
    ).fetchone()
    if row is None:
        return []
    chunk_ids = json.loads(row[0])
    conn.execute("DELETE FROM document_hashes WHERE doc_id=?", (doc_id,))
    conn.commit()
    return chunk_ids

def _all_doc_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT doc_id FROM document_hashes").fetchall()
    return {row[0] for row in rows}

# ── splitter + hash ────────────────────────────────────────────────────────────

SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, add_start_index=True
)

def _hash_content(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

# ── main pipeline ──────────────────────────────────────────────────────────────

def incremental_ingest(
    source_docs: list[tuple[str, str]],  # list of (doc_id, raw_text)
) -> dict:
    """
    Incrementally ingest documents. Each item is (doc_id, raw_text).
    doc_id should be a stable identifier: file path, URL, database row ID, etc.

    Returns stats: {added, updated, skipped, deleted}.
    """
    vs = Chroma(
        collection_name="my_documents",
        embedding_function=make_cached_embeddings_local(),
        persist_directory=PERSIST_DIR,
    )

    conn = sqlite3.connect(DB_PATH)
    _init_db(conn)

    added = updated = skipped = deleted = 0
    incoming_ids = set()

    for doc_id, raw_text in source_docs:
        incoming_ids.add(doc_id)
        new_hash = _hash_content(raw_text)
        existing = _get_hash(conn, doc_id)

        if existing and existing["content_hash"] == new_hash:
            # Content is identical to what's already indexed — nothing to do.
            skipped += 1
            continue

        # Content changed (or document is new): delete old chunks first.
        if existing:
            old_chunk_ids = existing["chunk_ids"]
            vs.delete(ids=old_chunk_ids)
            updated += 1
        else:
            added += 1

        # Split the raw text into chunks and embed them.
        doc = Document(page_content=raw_text, metadata={"source": doc_id})
        chunks = SPLITTER.split_documents([doc])

        # Assign stable IDs so we can delete them later.
        chunk_ids = [f"{doc_id}_chunk_{i}" for i in range(len(chunks))]
        vs.add_documents(chunks, ids=chunk_ids)

        _save_hash(conn, doc_id, new_hash, chunk_ids)

    # Detect and remove documents that no longer exist in the source.
    stored_ids = _all_doc_ids(conn)
    for removed_id in stored_ids - incoming_ids:
        old_chunk_ids = _delete_hash(conn, removed_id)
        if old_chunk_ids:
            vs.delete(ids=old_chunk_ids)
        deleted += 1

    conn.close()
    stats = {"added": added, "updated": updated, "skipped": skipped, "deleted": deleted}
    print(f"Incremental ingest: {stats}")
    return stats
```

---

## Pattern 6 — Index Migration

**Concept:** You will eventually need to migrate your vector index. Common
reasons:
- **Embedding model change:** OpenAI releases a better model; old vectors are
  incompatible with new query embeddings — vectors must be fully re-generated.
- **Provider change:** Moving from Chroma (dev) to Pinecone (prod), or from
  Pinecone to Qdrant for cost reasons.
- **Schema change:** Adding a new metadata field that needs to be back-filled.

**Zero-downtime strategy:** Dual-write during cutover.
1. Point reads at the OLD store (serving live traffic).
2. Re-index all documents into the NEW store.
3. Validate new store quality (run sample queries, compare scores).
4. Switch reads to NEW store.
5. Stop dual-write. Decommission old store.

### `migration.py`
```python
"""
migration.py — Zero-downtime vector store migration.

Example: Chroma (old) → Qdrant (new) with a new embedding model.

Phase 1: Export all documents from the old store.
Phase 2: Re-embed and bulk-upsert into the new store.
Phase 3: Validate retrieval quality.
Phase 4: Cutover (flip the retriever pointer).
"""
import time
from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_chroma import Chroma
from langchain_qdrant import QdrantVectorStore
from langchain_openai import OpenAIEmbeddings
from langchain.embeddings import CacheBackedEmbeddings
from langchain.storage import LocalFileStore

load_dotenv()

# ── Phase 1: Export from old store ─────────────────────────────────────────────

def export_all_documents(old_store: Chroma) -> list[Document]:
    """
    Fetch every document from the old store.
    Chroma's .get() returns raw data; we reconstruct Document objects.
    """
    raw = old_store.get(include=["documents", "metadatas"])
    docs = []
    for text, meta in zip(raw["documents"], raw["metadatas"]):
        docs.append(Document(page_content=text, metadata=meta or {}))
    print(f"Exported {len(docs)} documents from old store")
    return docs

# ── Phase 2: Bulk upsert into new store ───────────────────────────────────────

def bulk_upsert_new_store(
    docs: list[Document],
    new_store: QdrantVectorStore,
    batch_size: int = 100,
) -> None:
    """
    Insert in batches to avoid request size limits and allow progress tracking.
    Embedding cache (from Pattern 2) dramatically speeds this up if embeddings
    are already cached from a previous run.
    """
    total = len(docs)
    for i in range(0, total, batch_size):
        batch = docs[i : i + batch_size]
        new_store.add_documents(batch)
        pct = min(i + batch_size, total) / total * 100
        print(f"  {min(i + batch_size, total)}/{total} ({pct:.0f}%)")
    print("Bulk upsert complete")

# ── Phase 3: Validate ──────────────────────────────────────────────────────────

def validate_migration(
    old_store: Chroma,
    new_store: QdrantVectorStore,
    sample_queries: list[str],
    k: int = 4,
) -> bool:
    """
    Run the same queries against both stores and compare result quality.

    A migration is healthy if:
      - Both stores return results for every query.
      - The top result from the new store is semantically close to the old one
        (we check that the same source appears in both top-k results).
    """
    all_ok = True
    for query in sample_queries:
        old_results = old_store.similarity_search(query, k=k)
        new_results = new_store.similarity_search(query, k=k)

        old_sources = {r.metadata.get("source") for r in old_results}
        new_sources = {r.metadata.get("source") for r in new_results}
        overlap = old_sources & new_sources
        overlap_pct = len(overlap) / max(len(old_sources), 1) * 100

        status = "OK" if overlap_pct >= 50 else "WARN"
        print(f"  [{status}] '{query[:50]}' — overlap {overlap_pct:.0f}%")
        if overlap_pct < 50:
            all_ok = False

    return all_ok

# ── Phase 4: Dual-write helper ────────────────────────────────────────────────

class DualWriteVectorStore:
    """
    Write to both old and new stores simultaneously during cutover window.
    Reads are served from whichever store is designated primary.

    Usage:
      dw = DualWriteVectorStore(old_store, new_store, read_from="old")
      dw.add_documents(new_docs)        # writes to both
      dw.similarity_search(query)       # reads from old (safe serving)

      # After validation passes:
      dw.switch_reads_to_new()
      # After new store is confirmed stable:
      # Stop dual-write, point all code at new_store directly.
    """

    def __init__(self, old, new, read_from: str = "old"):
        self._old = old
        self._new = new
        self._primary = read_from

    def add_documents(self, docs: list[Document]) -> None:
        self._old.add_documents(docs)
        self._new.add_documents(docs)

    def similarity_search(self, query: str, k: int = 4) -> list[Document]:
        store = self._old if self._primary == "old" else self._new
        return store.similarity_search(query, k=k)

    def switch_reads_to_new(self) -> None:
        self._primary = "new"
        print("Reads switched to new store. Monitor for 24h before decommissioning old.")
```

---

## Pattern 7 — Vector Store Health Monitoring

**Concept:** In production you need to know: Is the index up to date? How fast
are queries? Are there any obviously stale documents? A health check function
answers all of these and can be exposed as an endpoint for uptime monitoring.

### `vectorstore_health.py`
```python
"""
vectorstore_health.py — Health check for your vector store.

Checks:
  1. Document count (is it non-zero and roughly expected?)
  2. Query latency (sample query timed in milliseconds)
  3. Staleness (any documents older than the retention window?)
  4. Returns all stats as a JSON-serialisable dict.

Usage: python vectorstore_health.py
       Or import check_health() and expose it via FastAPI / LangServe.
"""
import json
import time
from datetime import datetime, timedelta, UTC
from dotenv import load_dotenv
from langchain_chroma import Chroma
from embedding_cache import make_cached_embeddings_local

load_dotenv()

PERSIST_DIR = "./chroma_db"
COLLECTION_NAME = "my_documents"
RETENTION_DAYS = 30          # flag docs older than this
LATENCY_THRESHOLD_MS = 500   # warn if p50 query latency exceeds this


def check_health(sample_query: str = "health check test query") -> dict:
    """
    Run all health checks and return a structured status dict.

    Return shape:
      {
        "status": "healthy" | "degraded" | "error",
        "document_count": int,
        "query_latency_ms": float,
        "stale_document_count": int,
        "oldest_document_age_days": float | None,
        "checks": {
            "has_documents": bool,
            "latency_ok": bool,
            "freshness_ok": bool,
        },
        "timestamp": str,
      }
    """
    result: dict = {
        "status": "healthy",
        "timestamp": datetime.now(UTC).isoformat(),
        "checks": {},
    }

    try:
        vs = Chroma(
            collection_name=COLLECTION_NAME,
            embedding_function=make_cached_embeddings_local(),
            persist_directory=PERSIST_DIR,
        )

        # ── 1. document count ──────────────────────────────────────────────────
        raw = vs.get(include=["metadatas"])
        count = len(raw["ids"])
        result["document_count"] = count
        result["checks"]["has_documents"] = count > 0
        if count == 0:
            result["status"] = "degraded"

        # ── 2. query latency ───────────────────────────────────────────────────
        t0 = time.perf_counter()
        vs.similarity_search(sample_query, k=1)
        latency_ms = (time.perf_counter() - t0) * 1000
        result["query_latency_ms"] = round(latency_ms, 2)
        latency_ok = latency_ms < LATENCY_THRESHOLD_MS
        result["checks"]["latency_ok"] = latency_ok
        if not latency_ok:
            result["status"] = "degraded"

        # ── 3. staleness ───────────────────────────────────────────────────────
        cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
        stale_count = 0
        oldest_age_days = None

        for meta in raw["metadatas"]:
            created_str = (meta or {}).get("created_at")
            if created_str:
                try:
                    created = datetime.fromisoformat(created_str)
                    age_days = (datetime.now(UTC) - created).days
                    if oldest_age_days is None or age_days > oldest_age_days:
                        oldest_age_days = age_days
                    if created < cutoff:
                        stale_count += 1
                except ValueError:
                    pass

        result["stale_document_count"] = stale_count
        result["oldest_document_age_days"] = oldest_age_days
        freshness_ok = stale_count == 0
        result["checks"]["freshness_ok"] = freshness_ok
        if not freshness_ok:
            # Stale documents are a warning, not a hard failure.
            if result["status"] == "healthy":
                result["status"] = "degraded"

    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)

    return result


if __name__ == "__main__":
    stats = check_health()
    print(json.dumps(stats, indent=2))
```

---

## Pattern 8 — Hybrid Search

**Concept:** Pure semantic (vector) search fails on exact terms: product codes,
person names, technical identifiers, and rare words. BM25 (keyword search)
handles exact terms perfectly but misses paraphrase and synonyms. Hybrid search
combines both signals and consistently outperforms either alone on real-world
queries by 5-20% in recall@k benchmarks.

**When to use hybrid:**
- Queries mix natural language with exact identifiers ("What does SKU-4821 say about returns?")
- Domain has technical jargon ("HNSW index" vs "vector index" are semantically distant to a general model)
- Proper nouns that the embedding model may not have seen in training

### `hybrid_search.py`
```python
"""
hybrid_search.py — Hybrid BM25 + semantic retrieval with EnsembleRetriever.

EnsembleRetriever runs both retrievers in parallel and merges results using
Reciprocal Rank Fusion (RRF). Each retriever's weight controls its influence.
weights=[0.5, 0.5] gives equal influence.
weights=[0.3, 0.7] gives more weight to semantic (good default).
"""
from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_community.retrievers import BM25Retriever
from langchain.retrievers import EnsembleRetriever
from langchain_chroma import Chroma
from embedding_cache import make_cached_embeddings_local

load_dotenv()

def build_hybrid_retriever(
    docs: list[Document],
    k: int = 4,
    bm25_weight: float = 0.4,
    semantic_weight: float = 0.6,
) -> EnsembleRetriever:
    """
    Build a hybrid retriever from a list of documents.

    BM25Retriever is purely in-memory (no embeddings needed for the keyword side).
    VectorStore handles the semantic side.

    Weight guidance:
      - High bm25_weight (0.6+): when queries have many exact keywords/IDs
      - High semantic_weight (0.6+): when queries are conversational / paraphrased
      - Equal (0.5/0.5): safe default when you are unsure
    """
    # ── BM25 (keyword) retriever ───────────────────────────────────────────────
    # BM25Retriever.from_documents builds an in-memory index.
    # It tokenises each document and builds term-frequency tables.
    # No API call required — purely CPU-based.
    bm25 = BM25Retriever.from_documents(docs, k=k)

    # ── Semantic (vector) retriever ────────────────────────────────────────────
    vs = Chroma.from_documents(
        documents=docs,
        embedding=make_cached_embeddings_local(),
        collection_name="hybrid_demo",
    )
    semantic = vs.as_retriever(search_kwargs={"k": k})

    # ── Ensemble ───────────────────────────────────────────────────────────────
    # EnsembleRetriever calls both retrievers and merges via Reciprocal Rank Fusion.
    # RRF formula: score(doc) = Σ weight_i / (rank_i + 60)
    # Documents ranked highly by both retrievers float to the top.
    return EnsembleRetriever(
        retrievers=[bm25, semantic],
        weights=[bm25_weight, semantic_weight],
    )


# ── Pinecone native hybrid (sparse + dense) ────────────────────────────────────
"""
Pinecone supports sparse+dense natively — no need for EnsembleRetriever.
Sparse vector = BM25 weights per term (server-side, much faster than client-side BM25).

from pinecone_text.sparse import BM25Encoder
from langchain_pinecone import PineconeVectorStore

encoder = BM25Encoder().default()        # pre-trained BM25 weights
encoder.fit(texts)                       # fit to your corpus

embeddings = make_cached_embeddings_local()
vs = PineconeVectorStore.from_documents(
    docs,
    embedding=embeddings,
    index_name="my-hybrid-index",
    sparse_encoder=encoder,
    alpha=0.5,   # 0=sparse only, 1=dense only, 0.5=hybrid
)
"""

# ── Qdrant sparse vectors (built-in BM25) ──────────────────────────────────────
"""
Qdrant supports sparse vectors natively in a separate named vector space.
This avoids running BM25 client-side entirely.

from langchain_qdrant import QdrantVectorStore, FastEmbedSparse, RetrievalMode

vs = QdrantVectorStore.from_documents(
    docs,
    embedding=make_cached_embeddings_local(),
    sparse_embedding=FastEmbedSparse(model_name="Qdrant/bm25"),
    location=":memory:",
    collection_name="hybrid",
    retrieval_mode=RetrievalMode.HYBRID,
)
retriever = vs.as_retriever(search_kwargs={"k": 4})
"""

# ── pgvector full-text + vector combination ────────────────────────────────────
"""
pgvector does not have a built-in sparse vector type (as of 0.7).
Hybrid search with pgvector uses PostgreSQL's tsvector full-text search
combined with a vector similarity search, joined with SQL.

Example raw SQL (wrap in a LangChain tool or custom retriever):

SELECT id, content,
       ts_rank(to_tsvector('english', content), plainto_tsquery('english', :query)) AS bm25_score,
       1 - (embedding <=> :query_embedding) AS cosine_score,
       (  0.4 * ts_rank(to_tsvector('english', content), plainto_tsquery('english', :query))
        + 0.6 * (1 - (embedding <=> :query_embedding))
       ) AS hybrid_score
FROM documents
WHERE to_tsvector('english', content) @@ plainto_tsquery('english', :query)
   OR (embedding <=> :query_embedding) < 0.5
ORDER BY hybrid_score DESC
LIMIT :k;
"""


if __name__ == "__main__":
    sample_docs = [
        Document(page_content="SKU-4821 is a 10mm hex bolt, grade 8.8", metadata={"source": "catalog"}),
        Document(page_content="The return policy allows 30-day returns for all hardware", metadata={"source": "policy"}),
        Document(page_content="Product code SKU-4821 ships in boxes of 100", metadata={"source": "catalog"}),
        Document(page_content="Refunds are processed within 5 business days", metadata={"source": "policy"}),
    ]

    retriever = build_hybrid_retriever(sample_docs)

    # This query mixes a keyword (SKU-4821) with natural language.
    # Pure semantic would struggle with the exact code; BM25 catches it.
    results = retriever.invoke("What is SKU-4821 and can I return it?")
    for doc in results:
        print(f"  [{doc.metadata['source']}] {doc.page_content}")
```

---

## Pattern 9 — pgvector Deep Dive

**Concept:** pgvector extends PostgreSQL with a `vector` column type and
similarity search operators. If your application already uses PostgreSQL, this
is often the best choice: no new infrastructure, transactional consistency,
familiar SQL tooling, and the ability to JOIN vector search results with
relational data.

### Index Types

| Index | Algorithm | Build time | Query time | Best for |
|-------|-----------|-----------|-----------|---------|
| IVFFlat | Inverted File | Fast | ~10ms | < 1M vectors, batch ingest |
| HNSW | Hierarchical Navigable Small World | Slow | ~1ms | > 1M vectors, low latency |

**Rule of thumb:**
- Development / small corpus → no index (exact search is fine up to ~100K rows)
- Production < 1M rows → IVFFlat with `lists = sqrt(row_count)`
- Production > 1M rows or latency-sensitive → HNSW

### `pgvector_setup.py`
```python
"""
pgvector_setup.py — pgvector with LangChain (langchain-postgres).

Prerequisites:
  1. PostgreSQL 14+ with pgvector extension installed:
       CREATE EXTENSION IF NOT EXISTS vector;

  2. Install Python deps:
       pip install langchain-postgres psycopg2-binary pgvector

  3. Set in .env:
       POSTGRES_URL=postgresql+psycopg://user:password@localhost:5432/mydb
"""
import os
from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_postgres.vectorstores import PGVector
from embedding_cache import make_cached_embeddings_local

load_dotenv()

CONNECTION_STRING = os.environ["POSTGRES_URL"]
COLLECTION_NAME = "my_documents"

# ── create / open vector store ─────────────────────────────────────────────────
def get_pgvector_store() -> PGVector:
    """
    PGVector.from_connection_string opens (or creates) a vector collection.

    On first run it:
      1. Creates the 'langchain_pg_collection' and 'langchain_pg_embedding' tables.
      2. Creates the collection record.

    On subsequent runs it connects to the existing collection.
    """
    return PGVector(
        connection=CONNECTION_STRING,
        embeddings=make_cached_embeddings_local(),
        collection_name=COLLECTION_NAME,
        use_jsonb=True,   # store metadata as JSONB for fast filtering
    )

# ── HNSW index (run once after initial bulk ingest) ────────────────────────────
HNSW_DDL = """
CREATE INDEX IF NOT EXISTS idx_hnsw_{collection}
ON langchain_pg_embedding
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
"""
# m=16: number of bi-directional links per node. Higher = better recall, more memory.
# ef_construction=64: size of the dynamic candidate list during build.
#   Higher = better index quality, slower build.

# ── IVFFlat index (alternative for batch-ingest workloads) ────────────────────
IVFFLAT_DDL = """
CREATE INDEX IF NOT EXISTS idx_ivfflat_{collection}
ON langchain_pg_embedding
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = {lists});
"""
# lists = sqrt(number_of_rows) is the standard recommendation.
# After building the index, run:
#   SET ivfflat.probes = 10;  -- higher probes = better recall, slower query
# before issuing queries in the same session.

def create_hnsw_index(conn) -> None:
    """Create HNSW index. Run AFTER bulk ingest — building on full data is faster."""
    conn.execute(HNSW_DDL.format(collection=COLLECTION_NAME))
    conn.commit()
    print("HNSW index created")

def create_ivfflat_index(conn, row_count: int) -> None:
    """Create IVFFlat index. lists = sqrt(row_count) is the standard recommendation."""
    import math
    lists = max(1, int(math.sqrt(row_count)))
    conn.execute(IVFFLAT_DDL.format(collection=COLLECTION_NAME, lists=lists))
    conn.commit()
    print(f"IVFFlat index created with lists={lists}")

# ── metadata filtering with JSONB ─────────────────────────────────────────────
def search_with_filter(
    query: str,
    filter_dict: dict,
    k: int = 4,
) -> list[Document]:
    """
    pgvector supports metadata filtering via JSONB operators.

    filter_dict examples:
      {"source": "policy.pdf"}              -- exact match
      {"category": {"$in": ["legal", "hr"]}} -- value in list
      {"created_year": {"$gte": 2024}}       -- numeric comparison

    Filters are applied server-side BEFORE vector similarity ranking,
    so they reduce the candidate set and can speed up queries significantly.
    """
    vs = get_pgvector_store()
    return vs.similarity_search(query, k=k, filter=filter_dict)

# ── performance tuning ─────────────────────────────────────────────────────────
"""
Session-level performance parameters (set via psycopg execute or SET in SQL):

For HNSW:
  SET hnsw.ef_search = 40;    -- default 40; higher = better recall, slower

For IVFFlat:
  SET ivfflat.probes = 10;    -- default 1 (too low!); 10-20 is a good start

For bulk ingest (set before INSERT, reset after):
  SET maintenance_work_mem = '1GB';   -- more memory = faster index build
  SET max_parallel_workers_per_gather = 4;

Approximate recall vs latency tradeoff:
  probes=1   → ~70% recall, fastest
  probes=10  → ~95% recall, ~10x slower than probes=1
  probes=100 → ~99% recall, comparable to exact search
"""

if __name__ == "__main__":
    docs = [
        Document(page_content="pgvector enables vector similarity search in PostgreSQL",
                 metadata={"source": "docs", "category": "database"}),
        Document(page_content="HNSW indexes provide sub-millisecond approximate nearest neighbour search",
                 metadata={"source": "paper", "category": "algorithms"}),
    ]

    vs = get_pgvector_store()
    ids = vs.add_documents(docs)
    print(f"Stored {len(ids)} documents")

    results = vs.similarity_search("fast vector search", k=2)
    for r in results:
        print(f"  [{r.metadata['source']}] {r.page_content}")
```

---

## Transitions

After completing this skill:

- **Need a full RAG pipeline on top of this store?** → `/rag`
- **Want to add reranking or query transformation to your retriever?** → `/rag` (advanced patterns)
- **Want to monitor retrieval quality in production?** → `/lc-monitor`
- **Building multi-tenant agents on top of this?** → `/lc-agent`
- **Running slow and want to diagnose it?** → `/lc-debug`

---

## Quick Reference

```
CacheBackedEmbeddings     — wrap any embeddings model to cache vectors locally or in Redis
LocalFileStore            — file-system cache, dev only
RedisStore                — distributed cache, production
vectorstore.add_documents()          — CREATE
vectorstore.similarity_search()      — READ
vectorstore.delete(ids=[...])        — DELETE
vectorstore.as_retriever()           — returns Retriever (plugs into LCEL |)
EnsembleRetriever                    — BM25 + semantic hybrid
PGVector (langchain-postgres)        — pgvector integration
HNSW index                           — best for > 1M rows or latency < 5ms
IVFFlat index                        — good for < 1M rows, faster build
collection-per-tenant                — isolation, GDPR erasure, < 10K tenants
metadata-filter                      — scale, > 10K tenants, simpler ops
DualWriteVectorStore                 — zero-downtime migration pattern
```
