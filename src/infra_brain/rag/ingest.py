"""Structure-aware RAG chunking (TRK-122 R5).

Shared by BOTH the Confluence path (``KnowledgeAgent``) and the local repo-docs
path (``LocalDocsAgent``), so the chunking logic lives in one place.

Two chunkers:

  * :func:`chunk_markdown` — header-aware split with a *breadcrumb prefix*
    embedded IN each chunk. The breadcrumb (``[<doc> > <h1> > <h2>]``)
    participates in the embedding AND is always visible to the consuming LLM,
    which eliminates the "orphaned instruction with no context" failure mode
    naive character-window chunking produces.

  * :func:`chunk_table_rows` — row-group chunking for pipe-delimited markdown
    tables (``TRACKER.md``-shaped, the fact-densest content). The table header
    row AND the file breadcrumb are prepended to EVERY chunk, so each row (e.g.
    one ``TRK-nnn`` finding) stays independently retrievable with its column
    semantics intact — naive chunking otherwise puts the header row in only 1
    of ~181 chunks.

Both degrade to a stdlib sliding-window fallback when ``langchain_text_splitters``
is not importable (the same fallback contract ``KnowledgeAgent`` already used),
so ingestion never hard-fails at chunk time.

Token-window safety: mxbai-embed-large has a 512-token window and the Ollama
gateway *silently truncates* beyond it. Two correctness rules make the
char-budget honest against that window:

  * The breadcrumb/header prefix is subtracted FROM the budget in BOTH chunkers
    (``chunk_size - len(prefix)``) so the *total* chunk — prefix included —
    never exceeds ``chunk_size``. Previously ``chunk_markdown`` split the body
    to ``chunk_size`` and then prepended the prefix on top, so real chunks ran
    ``chunk_size + len(prefix)`` long.
  * A single table row longer than the budget is SPLIT (see
    :func:`_split_oversized_row`) instead of emitted whole — an over-window
    chunk is silently truncated by Ollama, dropping the finding content.

The default ``chunk_size`` is 1000 chars (was 1200): mxbai uses WordPiece
tokenization, which yields ~1.1-1.3x more tokens than the cl100k estimate used
to size 1200, so 1000 keeps a real margin under the 512-token window. The value
is config-driven (``Settings.rag_chunk_size``) — changing the embedding model
requires revisiting it.
"""

from __future__ import annotations

import importlib
import logging
import re

logger = logging.getLogger(__name__)

# Defaults mirror the pre-TRK-122 KnowledgeAgent constants, except overlap
# drops 200 -> 150 (header-aware sections already carry cross-boundary context
# via the breadcrumb, so less raw character overlap is needed) and chunk_size
# drops 1200 -> 1000 (WordPiece tokenizes ~1.1-1.3x heavier than the cl100k
# estimate that sized 1200; see module docstring). Kept in sync with
# ``Settings.rag_chunk_size`` — callers thread the config value in.
_CHUNK_SIZE = 1000
_CHUNK_OVERLAP = 150

# Matches a markdown table separator row: | --- | :---: | --- | ...
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

# Sentence-boundary split used when a single table cell is itself over budget.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def estimate_tokens(text: str) -> int:
    """Approximate a chunk's token count via the codebase's ~4-chars/token rule.

    Shared by both RAG collectors (Finding 10) so ``DocumentChunk.token_count``
    is computed one way — knowledge.py and local_docs.py previously carried
    slightly different inline formulas. This is an estimate, not exact WordPiece
    tokenization; it only needs to be honest enough for the token_count column.
    """
    return max(1, len(text or "") // 4)


def _splitter_classes():
    """Return (MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter) or (None, None)."""
    try:
        module = importlib.import_module("langchain_text_splitters")
        return module.MarkdownHeaderTextSplitter, module.RecursiveCharacterTextSplitter
    except Exception:
        return None, None


def _sliding_window(text: str, chunk_size: int, overlap: int, prefix: str = "") -> list[str]:
    """Stdlib fallback: fixed-size overlapping character windows, each prefixed.

    The window is sized ``chunk_size - len(prefix)`` so the emitted chunk
    (prefix + window) stays within ``chunk_size`` — matching the token-window
    accounting the real splitter path does per section.
    """
    text = (text or "").strip()
    if not text:
        return []
    window = max(1, chunk_size - len(prefix))
    step = max(1, window - overlap)
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        piece = text[index : index + window]
        if piece.strip():
            out.append(prefix + piece)
        index += step
    return out


def chunk_markdown(
    text: str,
    doc_label: str,
    chunk_size: int = _CHUNK_SIZE,
    overlap: int = _CHUNK_OVERLAP,
) -> list[str]:
    """Header-aware chunking with a breadcrumb prefix embedded in each chunk.

    Splits on ``#``/``##``/``###`` headings (kept in the section body), then
    sub-splits each section to ``chunk_size``. Every emitted chunk is prefixed
    with ``[<doc_label>[ > <h1>[ > <h2>...]]]\\n`` so the chunk is never an
    orphaned fragment. Falls back to a prefixed sliding window when the splitter
    package is unavailable.
    """
    text = (text or "").strip()
    if not text:
        return []

    mhts_cls, rcts_cls = _splitter_classes()
    if mhts_cls is None or rcts_cls is None:
        return _sliding_window(text, chunk_size, overlap, prefix=f"[{doc_label}]\n")

    sections = mhts_cls(
        headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")],
        strip_headers=False,
    ).split_text(text)

    out: list[str] = []
    for sec in sections:
        crumb = " > ".join(str(v) for v in sec.metadata.values() if v)
        prefix = f"[{doc_label}{' > ' + crumb if crumb else ''}]\n"
        # Subtract the prefix from the body budget so prefix + body <= chunk_size.
        # The prefix length varies per section (heading depth/length), so the
        # sub-splitter is built per section, not once globally.
        body_size = max(1, chunk_size - len(prefix))
        body_overlap = min(overlap, max(0, body_size - 1))
        sub = rcts_cls(chunk_size=body_size, chunk_overlap=body_overlap)
        out.extend(
            prefix + piece for piece in sub.split_text(sec.page_content) if piece and piece.strip()
        )
    return out


def looks_like_markdown_table(text: str) -> bool:
    """True if *text* contains a markdown table (a header row + a separator row).

    Used by the local-docs collector to route ``TRACKER.md``-shaped files to
    :func:`chunk_table_rows`. Deliberately conservative — it requires an actual
    ``|---|`` separator so ordinary prose with a stray pipe is not misrouted.
    """
    return any(_TABLE_SEPARATOR_RE.match(line) for line in (text or "").splitlines())


def _row_cells(row: str) -> list[str]:
    """Split a pipe-delimited table row into its interior cell strings."""
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return s.split("|")


def _split_long_text(text: str, budget: int) -> list[str]:
    """Split *text* into <=budget pieces at sentence boundaries.

    Any single sentence still longer than ``budget`` is hard-sliced at the char
    budget as a last resort. Never drops content.
    """
    pieces: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= budget:
            pieces.append(sentence)
        else:
            pieces.extend(sentence[i : i + budget] for i in range(0, len(sentence), budget))
    return pieces or [text[:budget] or ""]


def _split_oversized_row(row: str, budget: int) -> list[str]:
    """Split a single over-*budget* table row into ID-tagged sub-rows.

    Splits at cell (``|``) boundaries first; an individual over-budget cell is
    further split at sentence boundaries, then at a hard char budget as a last
    resort. Each returned sub-row repeats the row's leading ID cell tagged
    ``(cont. k/n)`` so every piece stays identifiable and no content is silently
    dropped past the embedding window. Column semantics are preserved where they
    fit — only a single cell too large to fit at all is broken internally.
    """
    cells = _row_cells(row)
    id_cell = cells[0].strip() if cells else ""
    payload_cells = [c.strip() for c in cells[1:]] if len(cells) > 1 else []

    # Reserve room for the "| <id> (cont. k/n) | ... |" wrapper (3-digit k/n,
    # padded) so the numbering pass below never pushes a sub-row over budget.
    tag_reserve = len(f"| {id_cell} (cont. 999/999) |") + 6
    cell_budget = max(1, budget - tag_reserve)

    # 1. Break any single over-long cell into fitting fragments.
    fragments: list[str] = []
    for cell in payload_cells:
        if len(cell) <= cell_budget:
            fragments.append(cell)
        else:
            fragments.extend(_split_long_text(cell, cell_budget))
    if not fragments:
        fragments = [""]

    # 2. Greedily pack fragments into sub-rows under cell_budget.
    groups: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for frag in fragments:
        add = len(frag) + 3  # " | " joiner
        if cur and cur_len + add > cell_budget:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(frag)
        cur_len += add
    if cur:
        groups.append(cur)

    # 3. Assemble ID-tagged sub-rows, now that the total count is known.
    total = len(groups)
    out: list[str] = []
    for k, grp in enumerate(groups, start=1):
        tag = id_cell if total == 1 else f"{id_cell} (cont. {k}/{total})"
        out.append("| " + tag + " | " + " | ".join(grp) + " |")
    return out


def chunk_table_rows(
    text: str,
    doc_label: str,
    chunk_size: int = _CHUNK_SIZE,
    overlap: int = _CHUNK_OVERLAP,
) -> list[str]:
    """Row-group chunk a pipe-delimited markdown table, header on every chunk.

    Any non-table preamble (title, intro paragraphs before the table) is chunked
    with :func:`chunk_markdown` and returned first. The table body rows are then
    grouped greedily up to the budget (``chunk_size`` minus the prepended
    breadcrumb + header + separator), with that prefix on EVERY chunk so each
    finding stays independently retrievable with its column semantics intact.

    A single row longer than the budget is SPLIT via :func:`_split_oversized_row`
    (never emitted whole) so its content is never silently truncated past the
    embedding window — each split piece repeats the header prefix AND the row's
    leading ID cell so it stays identifiable. A ``logger.warning`` fires if any
    produced chunk still exceeds ``chunk_size`` (the pathological case of a lone
    cell whose ID tag alone overflows the budget).

    Falls back to :func:`chunk_markdown` over the whole text if no table
    separator is present.
    """
    text = (text or "").strip()
    if not text:
        return []

    lines = text.splitlines()
    # Locate the table: first pipe-line is the header, the following separator
    # row (|---|---|) confirms it. Everything before the header is preamble.
    header_idx = None
    for i, line in enumerate(lines):
        if (
            line.lstrip().startswith("|")
            and i + 1 < len(lines)
            and _TABLE_SEPARATOR_RE.match(lines[i + 1])
        ):
            header_idx = i
            break

    if header_idx is None:
        # No real table — treat as ordinary markdown.
        return chunk_markdown(text, doc_label, chunk_size=chunk_size, overlap=overlap)

    preamble = "\n".join(lines[:header_idx]).strip()
    header_row = lines[header_idx]
    separator_row = lines[header_idx + 1]
    body_rows = [
        line for line in lines[header_idx + 2 :] if line.lstrip().startswith("|") and line.strip()
    ]

    out: list[str] = []
    if preamble:
        out.extend(chunk_markdown(preamble, doc_label, chunk_size=chunk_size, overlap=overlap))

    prefix = f"[{doc_label} > table]\n{header_row}\n{separator_row}\n"
    budget = max(1, chunk_size - len(prefix))

    group: list[str] = []
    group_len = 0

    def _flush() -> None:
        nonlocal group, group_len
        if group:
            out.append(prefix + "\n".join(group))
            group, group_len = [], 0

    for row in body_rows:
        if len(row) > budget:
            # Oversized single row: flush the current group, then split THIS row
            # so its content survives the embedding window intact.
            _flush()
            out.extend(prefix + sub for sub in _split_oversized_row(row, budget))
            continue
        if group and group_len + len(row) + 1 > budget:
            _flush()
        group.append(row)
        group_len += len(row) + 1
    _flush()

    # Safety net: a produced chunk longer than chunk_size would be silently
    # truncated by the embedding gateway — surface it instead of dropping data.
    for chunk in out:
        if len(chunk) > chunk_size:
            logger.warning(
                "chunk_table_rows produced a %d-char chunk exceeding chunk_size=%d "
                "(doc_label=%r); the embedding gateway may truncate it",
                len(chunk),
                chunk_size,
                doc_label,
            )
    return out
