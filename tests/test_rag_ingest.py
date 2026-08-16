"""Tests for the shared structure-aware RAG chunkers (TRK-122 R5).

Covers header-aware markdown chunking with breadcrumb prefixes, table row-group
chunking with the header on every chunk, and the table detector — all pure,
DB-free logic.
"""

from infra_brain.rag.ingest import chunk_markdown, chunk_table_rows, looks_like_markdown_table


def test_chunk_markdown_empty_returns_empty():
    assert chunk_markdown("", "Doc") == []
    assert chunk_markdown("   \n  ", "Doc") == []


def test_chunk_markdown_prefixes_every_chunk_with_breadcrumb():
    text = "# Runbook\n\nRestart the widget.\n\n## Recovery\n\nFail over to standby."
    chunks = chunk_markdown(text, "Ops Doc")
    assert chunks
    # Every chunk carries the doc-label breadcrumb prefix.
    assert all(c.startswith("[Ops Doc") for c in chunks)
    # The heading path participates in at least one breadcrumb.
    assert any("Recovery" in c.split("\n", 1)[0] for c in chunks)


def test_chunk_markdown_breadcrumb_includes_heading_hierarchy():
    text = "# Top\n\nintro\n\n## Sub\n\nbody text here"
    chunks = chunk_markdown(text, "D")
    # A chunk from the ## Sub section should show Top > Sub in the breadcrumb.
    sub_chunks = [c for c in chunks if "body text here" in c]
    assert sub_chunks
    header_line = sub_chunks[0].split("\n", 1)[0]
    assert "Top" in header_line and "Sub" in header_line


def test_looks_like_markdown_table():
    assert looks_like_markdown_table("| A | B |\n|---|---|\n| 1 | 2 |")
    assert not looks_like_markdown_table("just prose with a | pipe but no separator")
    assert not looks_like_markdown_table("")


def test_chunk_table_rows_header_on_every_chunk():
    header = "| ID | Title |"
    sep = "|----|-------|"
    rows = [f"| TRK-{i:03d} | finding number {i} with some descriptive text |" for i in range(40)]
    text = "# Findings\n\nIntro paragraph.\n\n" + "\n".join([header, sep, *rows])

    chunks = chunk_table_rows(text, "TRACKER", chunk_size=400)
    table_chunks = [c for c in chunks if "TRK-" in c]
    assert len(table_chunks) > 1  # 40 long rows at 400-char budget -> multiple chunks
    # The header + separator row is prepended to EVERY table chunk.
    assert all(header in c and sep in c for c in table_chunks)
    # Every finding row is present across the chunks (none dropped).
    joined = "\n".join(table_chunks)
    assert all(f"TRK-{i:03d}" in joined for i in range(40))


def test_chunk_table_rows_falls_back_to_markdown_without_table():
    text = "# Just Prose\n\nNo table here at all, only paragraphs."
    chunks = chunk_table_rows(text, "Doc")
    assert chunks
    assert all(c.startswith("[Doc") for c in chunks)


def test_chunk_table_rows_splits_oversized_single_row():
    """Fable Finding 1: a single row larger than the budget is SPLIT into
    ID-tagged sub-chunks (never emitted whole and silently truncated past the
    embedding window), with all content preserved and each piece identifiable.

    Renamed from ``test_chunk_table_rows_never_splits_a_single_row``, which
    asserted the (now-fixed) buggy never-split behavior.
    """
    header = "| ID | Data |"
    sep = "|----|------|"
    # One row far larger than the budget, made of many distinct cells so the
    # cell-boundary split keeps every unit whole and checkable.
    payload = " | ".join(f"detail{i:03d}" for i in range(200))
    huge = f"| TRK-999 | {payload} |"
    text = "\n".join([header, sep, huge])
    chunk_size = 400
    chunks = chunk_table_rows(text, "TRACKER", chunk_size=chunk_size)

    trk_chunks = [c for c in chunks if "TRK-999" in c]
    assert len(trk_chunks) > 1  # split into multiple pieces (was exactly 1)
    assert all(len(c) <= chunk_size for c in chunks)  # every chunk fits the window
    assert all(header in c and sep in c for c in trk_chunks)  # header on every chunk
    assert all("TRK-999" in c for c in trk_chunks)  # each piece stays identifiable
    assert any("cont." in c for c in trk_chunks)  # tagged as a continuation
    joined = "".join(trk_chunks)
    assert all(f"detail{i:03d}" in joined for i in range(200))  # nothing dropped


def test_chunk_table_rows_bounds_realistic_tracker_rows():
    """Fable Finding 1: real TRACKER.md-shaped rows (1000-6000 chars) at the
    production default chunk_size never produce a chunk over the window budget.
    This exact bound test did not exist before and would have caught Finding 1.
    """
    header = "| ID | Status | Finding |"
    sep = "|----|--------|---------|"
    rows = []
    for i in range(30):
        sentences = " ".join(
            f"Finding {i} sentence {j} describes an infrastructure drift event in detail."
            for j in range((i % 6 + 1) * 15)
        )
        rows.append(f"| TRK-{i:03d} | open | {sentences} |")
    text = "\n".join([header, sep, *rows])
    chunk_size = 1200  # the real production default

    chunks = chunk_table_rows(text, "TRACKER", chunk_size=chunk_size)
    assert chunks
    assert all(len(c) <= chunk_size for c in chunks)
    joined = "\n".join(chunks)
    assert all(f"TRK-{i:03d}" in joined for i in range(30))  # every finding survives


def test_chunk_markdown_chunks_bounded_at_default_size():
    """Fable Finding 2: the breadcrumb prefix is subtracted from the body budget,
    so prefix + body never exceeds chunk_size (was chunk_size + len(prefix))."""
    body = " ".join(
        f"Paragraph {i} explains a subsystem behavior in some operational detail."
        for i in range(400)
    )
    text = f"# Deep Heading One With A Fairly Long Descriptive Section Title\n\n{body}"
    chunks = chunk_markdown(text, "A Reasonably Long Document Label", chunk_size=1000)
    assert chunks
    assert all(len(c) <= 1000 for c in chunks)


def test_chunk_table_rows_bounded_at_default_size():
    """Fable Finding 2: table chunks (prefix + rows) stay within chunk_size."""
    header = "| ID | Finding |"
    sep = "|----|---------|"
    rows = [
        f"| TRK-{i:03d} | " + " ".join(f"word{j}" for j in range(120)) + " |" for i in range(40)
    ]
    text = "\n".join([header, sep, *rows])
    chunks = chunk_table_rows(text, "TRACKER Long Document Label", chunk_size=1000)
    assert chunks
    assert all(len(c) <= 1000 for c in chunks)


def test_chunk_markdown_sliding_window_fallback_bounded_and_prefixed(monkeypatch):
    """Fable Finding 9: with the splitter package unavailable, chunk_markdown
    falls back to a prefixed sliding window that is still breadcrumb-prefixed and
    correctly bounded (prefix subtracted from the window)."""
    monkeypatch.setattr("infra_brain.rag.ingest._splitter_classes", lambda: (None, None))
    body = " ".join(f"sentence{i}" for i in range(400))
    text = f"# Heading\n\n{body}"
    chunk_size = 200
    chunks = chunk_markdown(text, "Doc Label", chunk_size=chunk_size)
    assert chunks
    assert all(c.startswith("[Doc Label]") for c in chunks)
    assert all(len(c) <= chunk_size for c in chunks)


def test_chunk_markdown_heading_free_plain_text():
    """Fable Finding 9: text with no #/##/### headings still chunks sanely with
    just the doc-label breadcrumb (no heading crumb)."""
    text = "Just some plain prose with no markdown headings at all. " * 5
    chunks = chunk_markdown(text, "Plain Doc")
    assert chunks
    assert all(c.startswith("[Plain Doc]") for c in chunks)
    # The prefix is exactly the doc label — no heading crumb appended.
    assert all(c.split("\n", 1)[0] == "[Plain Doc]" for c in chunks)
