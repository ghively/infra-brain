"""KnowledgeAgent (RAG knowledge-store revival) collector tests.

Mirrors tests/agents/test_cicd.py: uses the shared ``make_agent`` /
``sqlite_engine`` / ``session_patcher`` fixtures, mocks ``readonly_get`` (the
GET-only Confluence client) and ``get_embeddings`` (so no network / no real
embedding model is needed), and covers the required cases: disabled no-op,
success, empty space, fetch failure (recorded not swallowed), incremental
skip-unchanged, stale marking, and spec metadata.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from infra_brain.agents.knowledge import KnowledgeAgent
from infra_brain.db.models import Document, DocumentChunk
from infra_brain.etl.base import CollectorSkipped

# The real pgvector column width. A 4-element stub is silently accepted by
# SQLite's JSON variant and rejected by PostgreSQL ("expected 1024
# dimensions, not 4") — caught by the agent-orm-check gate, TRK-356.
from infra_brain.db.models.core import _EMBED_DIM

_MODULE = "infra_brain.agents.knowledge"


def _settings(**overrides):
    base = dict(
        rag_enabled=True,
        confluence_url="https://conf.example.com",
        confluence_rag_spaces="",
        confluence_space_key="INFRA",
        confluence_user_email="bot@example.com",
        confluence_token="tok",
        rag_chunk_size=1000,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _resp(payload):
    """A stand-in httpx.Response with .raise_for_status() + .json()."""
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = payload
    return r


def _page(page_id, title, body, version=1):
    return {
        "id": page_id,
        "title": title,
        "version": {"number": version},
        "_links": {"webui": f"/spaces/INFRA/pages/{page_id}"},
        "body": {"storage": {"value": body, "representation": "storage"}},
    }


def _content_page(pages, has_next=False):
    """A /rest/api/content response wrapping the given result pages."""
    return _resp({"results": pages, "_links": {"next": "/next" if has_next else None}})


# --- 1. disabled no-op ---------------------------------------------------


def test_knowledge_collect_skips_when_rag_disabled(make_agent):
    """Self-skip, not an empty success.

    Returning an empty CollectOutcome records status="completed" with 0 rows,
    which the R3 monitor correctly reads as "silently empty?" — it escalated
    200x in a row on a fleet where rag_enabled was on but confluence_url was
    never set. "skipped" is what actually describes an absent dependency.
    """
    agent = make_agent(KnowledgeAgent, settings=_settings(rag_enabled=False))
    with patch(f"{_MODULE}.readonly_get") as mock_get:
        with pytest.raises(CollectorSkipped, match="rag_enabled is off"):
            agent.collect(scope="all")
    mock_get.assert_not_called()


def test_knowledge_collect_skips_when_confluence_unconfigured(make_agent):
    """The live case: rag_enabled on, confluence_url empty."""
    agent = make_agent(KnowledgeAgent, settings=_settings(confluence_url=""))
    with patch(f"{_MODULE}.readonly_get") as mock_get:
        with pytest.raises(CollectorSkipped, match="confluence_url not configured"):
            agent.collect(scope="all")
    mock_get.assert_not_called()


def test_knowledge_disabled_writer_makes_no_rows(make_agent, sqlite_engine, session_patcher):
    """A no-op collect must leave the detail writer inert — and crucially must
    NOT mark any existing confluence Document stale (scanned_spaces is empty)."""
    agent = make_agent(KnowledgeAgent, settings=_settings(rag_enabled=False))
    with Session(sqlite_engine) as s:
        s.add(
            Document(
                title="old",
                source="confluence",
                external_id="999",
                space="INFRA",
                content_hash="h",
                status="current",
            )
        )
        s.commit()

    with patch(f"{_MODULE}.readonly_get"):
        with pytest.raises(CollectorSkipped):
            agent.collect(scope="all")
    # The skip must leave the writer just as inert as the old empty-return did:
    # scanned_spaces stays empty, so no stale sweep can run. This is the safety
    # property the test exists for — a collector that did not scan must never
    # mark existing documents stale.
    with session_patcher(_MODULE):
        written = agent._write_knowledge_documents()

    assert written == 0
    with Session(sqlite_engine) as v:
        assert v.query(DocumentChunk).count() == 0
        # untouched — no stale sweep when nothing was scanned
        assert v.query(Document).one().status == "current"


# --- 2. success ----------------------------------------------------------


def test_knowledge_success_creates_documents_and_chunks(make_agent, sqlite_engine, session_patcher):
    agent = make_agent(KnowledgeAgent, settings=_settings())
    pages = [
        _page("101", "Runbook", "<h1>Runbook</h1><p>Restart the widget service.</p>"),
        _page("102", "Onboarding", "<p>Welcome to the team.</p>"),
    ]
    with patch(f"{_MODULE}.readonly_get", return_value=_content_page(pages)):
        outcome = agent.collect(scope="all")

    assert len(outcome.items) == 2
    assert outcome.errors == []
    assert {i["name"] for i in outcome.items} == {"101", "102"}
    assert outcome.items[0]["type"] == "confluence_page"
    assert outcome.items[0]["data"]["url"].startswith("https://conf.example.com/")

    fake_embed = MagicMock()
    fake_embed.embed_documents.side_effect = lambda chunks: [[0.0] * _EMBED_DIM for _ in chunks]
    with session_patcher(_MODULE), patch(f"{_MODULE}.get_embeddings", return_value=fake_embed):
        written = agent._write_knowledge_documents()

    assert written > 0
    with Session(sqlite_engine) as v:
        assert v.query(Document).count() == 2
        assert v.query(DocumentChunk).count() == written
        doc = v.query(Document).filter_by(external_id="101").one()
        assert doc.source == "confluence"
        assert doc.status == "current"
        assert doc.content_hash


# --- 3. empty result -----------------------------------------------------


def test_knowledge_collect_empty_space(make_agent):
    agent = make_agent(KnowledgeAgent, settings=_settings())
    with patch(f"{_MODULE}.readonly_get", return_value=_content_page([])):
        outcome = agent.collect(scope="all")
    assert outcome.items == []
    assert outcome.errors == []
    assert agent._scanned_spaces == ["INFRA"]  # scanned successfully, just empty


# --- 4. fetch exception recorded (F-007) ---------------------------------


def test_knowledge_fetch_failure_recorded_not_raised(make_agent):
    agent = make_agent(KnowledgeAgent, settings=_settings())
    with patch(f"{_MODULE}.readonly_get", side_effect=RuntimeError("confluence 500")):
        outcome = agent.collect(scope="all")
    assert outcome.items == []
    assert any("confluence 500" in e for e in outcome.errors)
    assert outcome.status in ("partial", "failed")
    # a space whose fetch failed is NOT marked scanned (no stale sweep on it)
    assert agent._scanned_spaces == []


# --- 5. incremental: unchanged content is skipped ------------------------


def test_knowledge_incremental_skips_unchanged(make_agent, sqlite_engine, session_patcher):
    agent = make_agent(KnowledgeAgent, settings=_settings())
    pages = [_page("101", "Runbook", "<p>Restart the widget service.</p>")]

    fake_embed = MagicMock()
    fake_embed.embed_documents.side_effect = lambda chunks: [[0.0] * _EMBED_DIM for _ in chunks]

    # First run indexes the page.
    with patch(f"{_MODULE}.readonly_get", return_value=_content_page(pages)):
        agent.collect(scope="all")
    with session_patcher(_MODULE), patch(f"{_MODULE}.get_embeddings", return_value=fake_embed):
        first = agent._write_knowledge_documents()
    assert first > 0

    with Session(sqlite_engine) as v:
        chunks_after_first = v.query(DocumentChunk).count()

    # Second run, identical page (same content_hash) -> skip, no re-embed.
    fake_embed.embed_documents.reset_mock()
    with patch(f"{_MODULE}.readonly_get", return_value=_content_page(pages)):
        agent.collect(scope="all")
    with session_patcher(_MODULE), patch(f"{_MODULE}.get_embeddings", return_value=fake_embed):
        second = agent._write_knowledge_documents()

    assert second == 0
    fake_embed.embed_documents.assert_not_called()
    with Session(sqlite_engine) as v:
        assert v.query(Document).count() == 1
        assert v.query(DocumentChunk).count() == chunks_after_first


# --- 6. stale marking: vanished page -> status "stale", row kept ---------


def test_knowledge_marks_vanished_page_stale(make_agent, sqlite_engine, session_patcher):
    agent = make_agent(KnowledgeAgent, settings=_settings())
    # Seed a prior-run document in the space we are about to scan.
    with Session(sqlite_engine) as s:
        s.add(
            Document(
                title="gone",
                source="confluence",
                external_id="500",
                space="INFRA",
                content_hash="oldhash",
                status="current",
            )
        )
        s.commit()

    pages = [_page("101", "Kept", "<p>Still here.</p>")]  # 500 is absent now
    fake_embed = MagicMock()
    fake_embed.embed_documents.side_effect = lambda chunks: [[0.0] * _EMBED_DIM for _ in chunks]

    with patch(f"{_MODULE}.readonly_get", return_value=_content_page(pages)):
        agent.collect(scope="all")
    with session_patcher(_MODULE), patch(f"{_MODULE}.get_embeddings", return_value=fake_embed):
        agent._write_knowledge_documents()

    with Session(sqlite_engine) as v:
        vanished = v.query(Document).filter_by(external_id="500").one()  # NOT deleted
        assert vanished.status == "stale"
        kept = v.query(Document).filter_by(external_id="101").one()
        assert kept.status == "current"


# --- 8. Fable Finding 4: stale page resurrected on unchanged hash --------


def test_knowledge_resurrects_stale_page_on_unchanged_hash(
    make_agent, sqlite_engine, session_patcher
):
    """A page marked ``stale`` in a prior run that returns byte-identical must
    flip back to ``current`` (else it stays excluded from search_knowledge
    forever) even though the unchanged-hash path skips re-embedding."""
    import hashlib

    agent = make_agent(KnowledgeAgent, settings=_settings())
    body = "<p>Stable content that never changes.</p>"
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    with Session(sqlite_engine) as s:
        s.add(
            Document(
                title="Runbook",
                source="confluence",
                external_id="101",
                space="INFRA",
                content_hash=content_hash,
                status="stale",  # left a scanned space last run, now back
            )
        )
        s.commit()

    pages = [_page("101", "Runbook", body)]
    fake_embed = MagicMock()
    fake_embed.embed_documents.side_effect = lambda chunks: [[0.0] * _EMBED_DIM for _ in chunks]
    with patch(f"{_MODULE}.readonly_get", return_value=_content_page(pages)):
        agent.collect(scope="all")
    with session_patcher(_MODULE), patch(f"{_MODULE}.get_embeddings", return_value=fake_embed):
        agent._write_knowledge_documents()

    fake_embed.embed_documents.assert_not_called()  # unchanged hash -> no re-embed
    with Session(sqlite_engine) as v:
        assert v.query(Document).filter_by(external_id="101").one().status == "current"


# --- 9. Fable Finding 3: Confluence CDATA code blocks survive extraction --


def test_knowledge_strip_html_preserves_cdata_code():
    """Confluence code macros wrap their body in <![CDATA[...]]>, which the
    default HTMLParser silently drops. It must survive extraction, fenced."""
    from infra_brain.agents.knowledge import _strip_html

    html = (
        "<p>Run this:</p>"
        '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
        "<![CDATA[systemctl restart nginx\nsystemctl status nginx]]>"
        "</ac:plain-text-body></ac:structured-macro>"
    )
    text = _strip_html(html)
    assert "systemctl restart nginx" in text
    assert "systemctl status nginx" in text  # both lines (internal newline kept)
    assert "```" in text  # fenced so the chunker treats it as a code block


# --- 10. Fable Finding 8: Confluence tables route to the table chunker ----


def test_knowledge_table_page_routes_to_table_chunker(make_agent, sqlite_engine, session_patcher):
    """Table cells must arrive pipe-delimited and the page must route to
    chunk_table_rows (not chunk_markdown), mirroring LocalDocsAgent."""
    from infra_brain.agents.knowledge import _strip_html
    from infra_brain.rag.ingest import chunk_table_rows as real_chunk_table_rows
    from infra_brain.rag.ingest import looks_like_markdown_table

    body = (
        "<table>"
        "<tr><th>ID</th><th>Title</th></tr>"
        "<tr><td>TRK-1</td><td>First finding here</td></tr>"
        "<tr><td>TRK-2</td><td>Second finding here</td></tr>"
        "</table>"
    )
    # Extraction preserves pipe delimiters and yields a real markdown table.
    text = _strip_html(body)
    assert "|" in text
    assert looks_like_markdown_table(text)

    agent = make_agent(KnowledgeAgent, settings=_settings())
    pages = [_page("201", "Findings Table", body)]
    fake_embed = MagicMock()
    fake_embed.embed_documents.side_effect = lambda chunks: [[0.0] * _EMBED_DIM for _ in chunks]

    with patch(f"{_MODULE}.readonly_get", return_value=_content_page(pages)):
        agent.collect(scope="all")
    with (
        session_patcher(_MODULE),
        patch(f"{_MODULE}.get_embeddings", return_value=fake_embed),
        patch(f"{_MODULE}.chunk_table_rows", side_effect=real_chunk_table_rows) as spy_table,
        patch(f"{_MODULE}.chunk_markdown") as spy_markdown,
    ):
        agent._write_knowledge_documents()

    spy_table.assert_called_once()  # routed to the table-aware chunker
    spy_markdown.assert_not_called()  # NOT the prose chunker


# --- Fable Finding 5: embedding-count mismatch fails loudly ---------------


def test_knowledge_embedding_count_mismatch_raises(make_agent, sqlite_engine, session_patcher):
    """A gateway returning fewer vectors than chunks is a structural malfunction:
    raise (surfaced as a failed run by _write_details) rather than silently
    storing None embeddings that search_knowledge then filters out."""
    import pytest

    agent = make_agent(KnowledgeAgent, settings=_settings())
    pages = [_page("101", "Runbook", "<h1>Runbook</h1><p>Some content to chunk.</p>")]
    with patch(f"{_MODULE}.readonly_get", return_value=_content_page(pages)):
        agent.collect(scope="all")

    bad_embed = MagicMock()
    bad_embed.embed_documents.side_effect = lambda chunks: []  # 0 vectors for >0 chunks
    with session_patcher(_MODULE), patch(f"{_MODULE}.get_embeddings", return_value=bad_embed):
        with pytest.raises(RuntimeError, match="embedding count mismatch"):
            agent._write_knowledge_documents()


# --- 11. spec metadata ---------------------------------------------------


def test_knowledge_domain_and_schedule():
    assert KnowledgeAgent.domain == "knowledge"
    assert KnowledgeAgent.spec.schedule == "35 2 * * *"
    assert KnowledgeAgent.spec.tier.value == "collector"
