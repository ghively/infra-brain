"""PersonalWikiAgent (Hermes-wiki -> fenced RAG ingestion) collector tests.

Mirrors tests/agents/test_local_docs.py: uses the shared ``make_agent`` /
``sqlite_engine`` / ``session_patcher`` fixtures and mocks ``get_embeddings``
(no network / no real embedding model, no real Hermes wiki on disk — every
"personal wiki root" in this file is a throwaway tmp_path fixture). Covers the
required cases — disabled no-op (both gate flags independently), unconfigured
no-op, success (including runtime/ being skipped and non-.md files ignored),
empty result, read failure (recorded not swallowed), source-scoped stale
marking, stale-doc resurrection, and spec metadata.

Per the parent task's explicit instruction: this test suite proves the
ingestion pipeline works correctly on a small SEEDED sample via the sqlite
fixture pattern. It never touches the real ~/.hermes/wiki directory or the
real Postgres database — no full (or partial) ingestion of real personal
content is performed by this file or anywhere else in this change.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

import infra_brain.agents.personal_wiki as personal_wiki_module
from infra_brain.agents.personal_wiki import PersonalWikiAgent
from infra_brain.db.models import Document, DocumentChunk
from infra_brain.etl.base import CollectorSkipped

# The real pgvector column width. A 4-element stub is silently accepted by
# SQLite's JSON variant and rejected by PostgreSQL ("expected 1024
# dimensions, not 4") — caught by the agent-orm-check gate, TRK-356.
from infra_brain.db.models.core import _EMBED_DIM

_MODULE = "infra_brain.agents.personal_wiki"


def _settings(root="", **overrides):
    base = dict(
        rag_enabled=True,
        personal_wiki_ingest_enabled=True,
        personal_wiki_root=str(root),
        rag_chunk_size=1000,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _seed_wiki(root: Path) -> None:
    """Lay out a small synthetic wiki tree — entities/, wiki/, raw/, runtime/."""
    (root / "entities").mkdir(parents=True, exist_ok=True)
    (root / "entities" / "devices").mkdir(parents=True, exist_ok=True)
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "raw").mkdir(parents=True, exist_ok=True)
    (root / "runtime").mkdir(parents=True, exist_ok=True)  # must NOT be ingested
    (root / "_archive").mkdir(parents=True, exist_ok=True)  # out of scope

    (root / "entities" / "gitlab.md").write_text("# GitLab\n\nSelf-hosted GitLab instance.\n")
    (root / "entities" / "devices" / "router.md").write_text("# Router\n\nCore router notes.\n")
    (root / "wiki" / "runbook.md").write_text("# Runbook\n\nHow to recover the agent.\n")
    (root / "raw" / "2026-07-30.md").write_text("# 2026-07-30\n\nDated raw note.\n")
    (root / "runtime" / "live-state.md").write_text("# Live State\n\nCron-refreshed scratch.\n")
    (root / "_archive" / "old.md").write_text("# Old\n\nOut of scope subdir.\n")
    (root / "entities" / "catalog.json").write_text('{"not": "markdown"}')  # non-.md ignored


def _fake_embeddings():
    fake = MagicMock()
    fake.embed_documents.side_effect = lambda chunks: [[0.0] * _EMBED_DIM for _ in chunks]
    return fake


# --- 0. docstring/scope consistency ------------------------------------------


def test_module_docstring_scope_matches_allowed_subdirs():
    """Regression guard for the SCOPE bullet drifting from ``_ALLOWED_SUBDIRS``.

    ``_ALLOWED_SUBDIRS`` was expanded 2026-08-02 to include ``intelligence``,
    ``concepts``, ``comparisons``, ``lessons``, ``runbooks``, ``references``,
    and ``incidents`` — every one of those must be documented in the module
    docstring's SCOPE bullet as walked (not listed as an out-of-scope
    example), since that docstring is the only place fencing/scope is
    explained to a maintainer or reviewer deciding what personal content
    gets embedded and made retrievable via search_knowledge.
    """
    doc = personal_wiki_module.__doc__ or ""

    for name in personal_wiki_module._ALLOWED_SUBDIRS:
        assert f"``{name}/``" in doc, (
            f"{name}/ is walked (in _ALLOWED_SUBDIRS) but not documented as "
            "in-scope in the module docstring's SCOPE bullet"
        )

    # runtime/ must stay documented as the deliberately-excluded directory,
    # and must never appear in _ALLOWED_SUBDIRS itself.
    assert "runtime" not in personal_wiki_module._ALLOWED_SUBDIRS
    assert "``runtime/``" in doc


# --- 1. no-op cases ---------------------------------------------------------


def test_personal_wiki_noop_when_rag_disabled(make_agent, tmp_path):
    """M-6: self-skip, not an empty success — matches KnowledgeAgent /
    SaaSInventoryAgent / LocalDocsAgent. An empty CollectOutcome records
    status="completed" with 0 rows, which the R3 completeness monitor reads
    as "silently empty?" and escalates; CollectorSkipped is the outcome that
    actually describes an intentionally-off/unconfigured collector."""
    _seed_wiki(tmp_path)
    agent = make_agent(PersonalWikiAgent, settings=_settings(root=tmp_path, rag_enabled=False))
    with pytest.raises(CollectorSkipped, match="rag_enabled is off"):
        agent.collect(scope="all")


def test_personal_wiki_noop_when_ingest_flag_disabled(make_agent, tmp_path):
    """rag_enabled alone must NOT be sufficient — this is the separate,
    deliberate opt-in gate for real personal data."""
    _seed_wiki(tmp_path)
    agent = make_agent(
        PersonalWikiAgent, settings=_settings(root=tmp_path, personal_wiki_ingest_enabled=False)
    )
    with pytest.raises(CollectorSkipped, match="personal_wiki_ingest_enabled is off"):
        agent.collect(scope="all")


def test_personal_wiki_noop_when_root_unconfigured(make_agent):
    agent = make_agent(PersonalWikiAgent, settings=_settings(root=""))
    with pytest.raises(CollectorSkipped, match="personal_wiki_root not configured"):
        agent.collect(scope="all")


# --- 2. success --------------------------------------------------------------


def test_personal_wiki_success_creates_documents_and_chunks(
    make_agent, sqlite_engine, session_patcher, tmp_path
):
    _seed_wiki(tmp_path)
    agent = make_agent(PersonalWikiAgent, settings=_settings(root=tmp_path))
    outcome = agent.collect(scope="all")

    names = {i["name"] for i in outcome.items}
    assert "entities/gitlab.md" in names
    assert "entities/devices/router.md" in names
    assert "wiki/runbook.md" in names
    assert "raw/2026-07-30.md" in names
    # out-of-scope content must never be ingested
    assert "runtime/live-state.md" not in names  # cron-refreshed scratch, never walked
    assert "_archive/old.md" not in names  # only entities/wiki/raw are walked
    assert not any(n.endswith(".json") for n in names)  # non-markdown ignored
    assert outcome.errors == []
    assert outcome.items[0]["type"] == "personal_wiki_doc"

    with (
        session_patcher(_MODULE),
        patch(f"{_MODULE}.get_embeddings", return_value=_fake_embeddings()),
    ):
        written = agent._write_personal_wiki_docs()

    assert written > 0
    with Session(sqlite_engine) as v:
        docs = v.query(Document).all()
        assert {d.external_id for d in docs} == names
        assert all(d.source == "personal_wiki" for d in docs)
        assert all(d.status == "current" for d in docs)
        assert all(d.url.startswith("personal-wiki:") for d in docs)
        assert v.query(DocumentChunk).count() == written


# --- 3. empty result -----------------------------------------------------


def test_personal_wiki_empty_when_no_matching_files(make_agent, tmp_path):
    (tmp_path / "entities").mkdir()
    agent = make_agent(PersonalWikiAgent, settings=_settings(root=tmp_path))
    outcome = agent.collect(scope="all")
    assert outcome.items == []
    assert outcome.errors == []
    assert agent._scanned is True  # clean scan, just empty


# --- 4. read failure recorded (F-007) -------------------------------------


def test_personal_wiki_read_failure_recorded_not_raised(make_agent, tmp_path):
    _seed_wiki(tmp_path)
    agent = make_agent(PersonalWikiAgent, settings=_settings(root=tmp_path))

    orig_read = Path.read_bytes

    def _boom(self):
        if self.name == "gitlab.md":
            raise OSError("disk gremlin")
        return orig_read(self)

    with patch.object(Path, "read_bytes", _boom):
        outcome = agent.collect(scope="all")

    assert any("disk gremlin" in e for e in outcome.errors)
    assert outcome.status in ("partial", "failed")
    assert agent._scanned is False  # a read error blocks the stale sweep


# --- 5. stale marking is source-scoped ------------------------------------


def test_personal_wiki_stale_marking_scoped_to_source(
    make_agent, sqlite_engine, session_patcher, tmp_path
):
    _seed_wiki(tmp_path)
    with Session(sqlite_engine) as s:
        s.add(
            Document(
                title="gone",
                source="personal_wiki",
                external_id="entities/deleted.md",
                space="entities",
                content_hash="old",
                status="current",
            )
        )
        s.add(
            Document(
                title="local doc",
                source="local_docs",
                external_id="docs/overview.md",
                space="docs",
                content_hash="c",
                status="current",
            )
        )
        s.commit()

    agent = make_agent(PersonalWikiAgent, settings=_settings(root=tmp_path))
    agent.collect(scope="all")
    with (
        session_patcher(_MODULE),
        patch(f"{_MODULE}.get_embeddings", return_value=_fake_embeddings()),
    ):
        agent._write_personal_wiki_docs()

    with Session(sqlite_engine) as v:
        gone = v.query(Document).filter_by(external_id="entities/deleted.md").one()
        assert gone.status == "stale"  # vanished personal_wiki file
        local = v.query(Document).filter_by(external_id="docs/overview.md").one()
        assert local.status == "current"  # local_docs untouched


# --- 6. stale doc resurrected on unchanged hash -----------------------------


def test_personal_wiki_resurrects_stale_doc_on_unchanged_hash(
    make_agent, sqlite_engine, session_patcher, tmp_path
):
    (tmp_path / "entities").mkdir()
    (tmp_path / "entities" / "gitlab.md").write_text("# GitLab\n\nStable content.\n")

    agent = make_agent(PersonalWikiAgent, settings=_settings(root=tmp_path))
    agent.collect(scope="all")
    with (
        session_patcher(_MODULE),
        patch(f"{_MODULE}.get_embeddings", return_value=_fake_embeddings()),
    ):
        agent._write_personal_wiki_docs()

    with Session(sqlite_engine) as s:
        d = (
            s.query(Document)
            .filter_by(external_id="entities/gitlab.md", source="personal_wiki")
            .one()
        )
        d.status = "stale"
        s.commit()

    agent2 = make_agent(PersonalWikiAgent, settings=_settings(root=tmp_path))
    agent2.collect(scope="all")
    fake = _fake_embeddings()
    with session_patcher(_MODULE), patch(f"{_MODULE}.get_embeddings", return_value=fake):
        agent2._write_personal_wiki_docs()

    fake.embed_documents.assert_not_called()  # unchanged hash -> no re-embed
    with Session(sqlite_engine) as v:
        assert (
            v.query(Document).filter_by(external_id="entities/gitlab.md").one().status
            == "current"
        )


# --- 7. spec metadata --------------------------------------------------------


def test_personal_wiki_domain_and_schedule():
    assert PersonalWikiAgent.domain == "personal_wiki"
    assert PersonalWikiAgent.spec.schedule == "35 4 * * *"
    assert PersonalWikiAgent.spec.tier.value == "collector"
