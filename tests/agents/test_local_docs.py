"""LocalDocsAgent (TRK-122 R1 — repo-docs RAG ingestion) collector tests.

Mirrors tests/agents/test_knowledge.py: uses the shared ``make_agent`` /
``sqlite_engine`` / ``session_patcher`` fixtures and mocks ``get_embeddings``
(no network / no real embedding model). Covers the required cases — disabled
no-op, unconfigured no-op, success, empty result, read failure (recorded not
swallowed), the allowlist/denylist boundary, source-scoped stale marking, and
spec metadata.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.local_docs import LocalDocsAgent
from infra_brain.db.models import Document, DocumentChunk
from infra_brain.etl.base import CollectorSkipped

# The real pgvector column width. A 4-element stub is silently accepted by
# SQLite's JSON variant and rejected by PostgreSQL ("expected 1024
# dimensions, not 4") — caught by the agent-orm-check gate, TRK-356.
from infra_brain.db.models.core import _EMBED_DIM

_MODULE = "infra_brain.agents.local_docs"


def _settings(root="", **overrides):
    base = dict(rag_enabled=True, repo_docs_root=str(root), gitlab_url="", rag_chunk_size=1000)
    base.update(overrides)
    return SimpleNamespace(**base)


def _seed_repo(root: Path) -> None:
    """Lay out a curated-allowlist repo docs tree plus denied files."""
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "decisions").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "agents").mkdir(parents=True, exist_ok=True)  # DENIED (frozen)

    (root / "docs" / "overview.md").write_text(
        "# Overview\n\nInfra Brain audits infrastructure read-only.\n"
    )
    (root / "docs" / "decisions" / "DR-1.md").write_text(
        "# DR-1 Dashboard\n\nWe chose Vite+React.\n"
    )
    (root / "docs" / "agents" / "frozen.md").write_text("# Frozen\n\nHistorical evidence.\n")
    (root / "README.md").write_text("# infra-brain\n\nProject readme.\n")
    (root / "CLAUDE.md").write_text("# CLAUDE\n\nOperational instructions.\n")


def _fake_embeddings():
    fake = MagicMock()
    fake.embed_documents.side_effect = lambda chunks: [[0.0] * _EMBED_DIM for _ in chunks]
    return fake


# --- 1. no-op cases -------------------------------------------------------


def test_local_docs_noop_when_rag_disabled(make_agent, tmp_path):
    """M-6: self-skip, not an empty success — matches KnowledgeAgent /
    SaaSInventoryAgent. An empty CollectOutcome records status="completed"
    with 0 rows, which the R3 completeness monitor reads as "silently
    empty?" and escalates; CollectorSkipped is the outcome that actually
    describes an intentionally-off/unconfigured collector."""
    _seed_repo(tmp_path)
    agent = make_agent(LocalDocsAgent, settings=_settings(root=tmp_path, rag_enabled=False))
    with pytest.raises(CollectorSkipped, match="rag_enabled is off"):
        agent.collect(scope="all")


def test_local_docs_noop_when_root_unconfigured(make_agent):
    agent = make_agent(LocalDocsAgent, settings=_settings(root=""))
    with pytest.raises(CollectorSkipped, match="repo_docs_root not configured"):
        agent.collect(scope="all")


# --- 2. success -----------------------------------------------------------


def test_local_docs_success_creates_documents_and_chunks(
    make_agent, sqlite_engine, session_patcher, tmp_path
):
    _seed_repo(tmp_path)
    agent = make_agent(LocalDocsAgent, settings=_settings(root=tmp_path))
    outcome = agent.collect(scope="all")

    names = {i["name"] for i in outcome.items}
    # allowed files present; denied files absent
    assert "docs/overview.md" in names
    assert "docs/decisions/DR-1.md" in names
    assert "README.md" in names
    assert "docs/agents/frozen.md" not in names  # frozen historical evidence
    assert "CLAUDE.md" not in names  # operational instructions, not knowledge
    assert outcome.errors == []
    assert outcome.items[0]["type"] == "repo_doc"

    with (
        session_patcher(_MODULE),
        patch(f"{_MODULE}.get_embeddings", return_value=_fake_embeddings()),
    ):
        written = agent._write_local_docs()

    assert written > 0
    with Session(sqlite_engine) as v:
        docs = v.query(Document).all()
        assert {d.external_id for d in docs} == names
        assert all(d.source == "local_docs" for d in docs)
        assert all(d.status == "current" for d in docs)
        assert v.query(DocumentChunk).count() == written


# --- 3. empty result ------------------------------------------------------


def test_local_docs_empty_when_no_matching_files(make_agent, tmp_path):
    (tmp_path / "docs").mkdir()  # no .md files anywhere
    agent = make_agent(LocalDocsAgent, settings=_settings(root=tmp_path))
    outcome = agent.collect(scope="all")
    assert outcome.items == []
    assert outcome.errors == []
    assert agent._scanned is True  # clean scan, just empty


# --- 4. read failure recorded (F-007) -------------------------------------


def test_local_docs_read_failure_recorded_not_raised(make_agent, tmp_path):
    _seed_repo(tmp_path)
    agent = make_agent(LocalDocsAgent, settings=_settings(root=tmp_path))

    orig_read = Path.read_bytes

    def _boom(self):
        if self.name == "overview.md":
            raise OSError("disk gremlin")
        return orig_read(self)

    with patch.object(Path, "read_bytes", _boom):
        outcome = agent.collect(scope="all")

    assert any("disk gremlin" in e for e in outcome.errors)
    assert outcome.status in ("partial", "failed")
    assert agent._scanned is False  # a read error blocks the stale sweep


# --- 5. stale marking is source-scoped ------------------------------------


def test_local_docs_stale_marking_scoped_to_source(
    make_agent, sqlite_engine, session_patcher, tmp_path
):
    _seed_repo(tmp_path)
    # Seed a prior-run local_docs doc that no longer exists, and a confluence
    # doc that must NOT be swept by this agent.
    with Session(sqlite_engine) as s:
        s.add(
            Document(
                title="gone",
                source="local_docs",
                external_id="docs/deleted.md",
                space="docs",
                content_hash="old",
                status="current",
            )
        )
        s.add(
            Document(
                title="conf",
                source="confluence",
                external_id="999",
                space="INFRA",
                content_hash="c",
                status="current",
            )
        )
        s.commit()

    agent = make_agent(LocalDocsAgent, settings=_settings(root=tmp_path))
    agent.collect(scope="all")
    with (
        session_patcher(_MODULE),
        patch(f"{_MODULE}.get_embeddings", return_value=_fake_embeddings()),
    ):
        agent._write_local_docs()

    with Session(sqlite_engine) as v:
        gone = v.query(Document).filter_by(external_id="docs/deleted.md").one()
        assert gone.status == "stale"  # vanished local_docs file
        conf = v.query(Document).filter_by(external_id="999").one()
        assert conf.status == "current"  # confluence untouched


# --- 7. Fable Finding 4: stale doc resurrected on unchanged hash ----------


def test_local_docs_resurrects_stale_doc_on_unchanged_hash(
    make_agent, sqlite_engine, session_patcher, tmp_path
):
    """A doc marked ``stale`` in a prior run (file briefly deleted) that returns
    byte-identical must flip back to ``current`` (else excluded from
    search_knowledge forever) even though the unchanged-hash path skips
    re-embedding."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "overview.md").write_text("# Overview\n\nStable content.\n")

    agent = make_agent(LocalDocsAgent, settings=_settings(root=tmp_path))
    agent.collect(scope="all")
    with (
        session_patcher(_MODULE),
        patch(f"{_MODULE}.get_embeddings", return_value=_fake_embeddings()),
    ):
        agent._write_local_docs()

    # Simulate a prior run having marked it stale.
    with Session(sqlite_engine) as s:
        d = s.query(Document).filter_by(external_id="docs/overview.md", source="local_docs").one()
        d.status = "stale"
        s.commit()

    # Re-run with unchanged content -> resurrect to current, no re-embed.
    agent2 = make_agent(LocalDocsAgent, settings=_settings(root=tmp_path))
    agent2.collect(scope="all")
    fake = _fake_embeddings()
    with session_patcher(_MODULE), patch(f"{_MODULE}.get_embeddings", return_value=fake):
        agent2._write_local_docs()

    fake.embed_documents.assert_not_called()  # unchanged hash -> no re-embed
    with Session(sqlite_engine) as v:
        assert v.query(Document).filter_by(external_id="docs/overview.md").one().status == "current"


# --- 6. spec metadata -----------------------------------------------------


def test_local_docs_domain_and_schedule():
    assert LocalDocsAgent.domain == "local_docs"
    assert LocalDocsAgent.spec.schedule == "50 3 * * *"
    assert LocalDocsAgent.spec.tier.value == "collector"
