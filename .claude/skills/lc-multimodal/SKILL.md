---
name: lc-multimodal
description: Multimodal AI patterns for LangChain with Claude Vision. Teaches image input, PDF processing, table extraction, document layout understanding, multimodal RAG, audio transcription, multimodal agents, batch document processing, and content moderation. Triggered by /lc-multimodal or phrases like "process images", "PDF with images", "Claude vision", "extract tables from PDF", "multimodal RAG", "audio transcription pipeline", "document understanding", "invoice extraction", "batch PDF processing".
argument-hint: "[images|pdf|tables|layout|rag|audio|agent|batch|moderation]"
---

# lc:multimodal — Multimodal AI Patterns for LangChain with Claude Vision

## What Is Multimodal in LangChain?

**Multimodal** means your LLM can process more than just text. Instead of feeding Claude
a string, you send it a **content block** — a structured payload that can contain:

- **Images** (PNG, JPG, GIF, WebP) — pixel data the model "sees"
- **PDFs** — scanned or native, with embedded text, tables, and images
- **Audio** — speech converted to text (via Whisper) before reaching the LLM

LangChain wraps these in `HumanMessage` with a `content` list instead of a plain string.
Each item in the list is a dict with a `type` key: `"text"`, `"image_url"`, or
`"image"` (base64). The LLM vendor (Anthropic, OpenAI, etc.) receives these natively.

### Why This Matters

| Old approach | Multimodal approach |
|---|---|
| OCR text from image, lose layout | Send raw image, Claude reads it |
| Extract PDF text only, miss tables | Send page image, preserve table structure |
| Manually describe a chart | Ask Claude "what does this chart show?" |
| Audio → third-party transcription silo | Whisper tool → LangGraph pipeline |

---

## Skill Flow — Ask These 3 Questions First

Before scaffolding, ask the user:

```
1. What inputs do you need to handle?
   a) Images (photos, screenshots, diagrams)
   b) PDFs with images and/or tables
   c) Audio files (speech-to-text)
   d) All of the above

2. Are your documents structured or unstructured?
   Structured  = forms, invoices, receipts, spreadsheet exports (predictable layout)
   Unstructured = research papers, reports, general PDFs (free-form text)

3. What is the output?
   a) Text analysis / description / Q&A
   b) Structured data extraction (Pydantic model / JSON)
   c) RAG — retrieve relevant chunks from a large document corpus
```

Use the answers to jump to the matching pattern(s) below.

---

## Pattern 1 — Image Input with Claude Vision

### Concept: Content Blocks

A normal LangChain message looks like:
```python
HumanMessage(content="What is the capital of France?")
```

A multimodal message looks like:
```python
HumanMessage(content=[
    {"type": "text", "text": "What is in this image?"},
    {"type": "image_url", "image_url": {"url": "https://..."}},
])
```

The `content` field becomes a **list of dicts** instead of a plain string.
Claude reads each block in order, combining text instructions with image data.

### Supported Formats and Limits

| Format | Max size | Max dimension |
|---|---|---|
| PNG | 5 MB | 8000 px per side |
| JPG / JPEG | 5 MB | 8000 px per side |
| GIF (first frame) | 5 MB | 8000 px per side |
| WebP | 5 MB | 8000 px per side |

If your image is larger: resize before sending, or use a tile strategy.

### Complete Code

```python
# vision.py
import base64
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

load_dotenv()  # reads ANTHROPIC_API_KEY from .env

# ── helpers ──────────────────────────────────────────────────────────────────

def encode_image_to_b64(path: str | Path) -> str:
    """Read an image file from disk and return a base64-encoded string.

    LLM APIs cannot access your filesystem directly — base64 encoding
    embeds the raw pixel bytes inside the JSON request payload.
    """
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def image_media_type(path: str | Path) -> str:
    """Derive the MIME type from a file extension.

    Claude requires an explicit media_type so it knows how to decode the bytes.
    """
    suffix = Path(path).suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "image/png")


# ── single image analysis ─────────────────────────────────────────────────────

def analyze_image(path: str, question: str) -> str:
    """Send one image to Claude and ask a question about it.

    The content block uses type="image" with base64 source — this is the
    Anthropic-native format. langchain-anthropic translates it correctly.
    """
    llm = ChatAnthropic(model="claude-sonnet-4-6")

    b64 = encode_image_to_b64(path)
    media_type = image_media_type(path)

    # Content is a LIST — Claude receives text + image in a single turn
    message = HumanMessage(content=[
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": b64,           # the raw base64 string
            },
        },
        {
            "type": "text",
            "text": question,
        },
    ])

    response = llm.invoke([message])
    return response.content


# ── URL-based image (no download required) ───────────────────────────────────

def analyze_image_url(url: str, question: str) -> str:
    """Send a publicly accessible image URL to Claude.

    Use this when you already have a URL — Claude downloads it server-side.
    Use base64 for local files or private URLs.
    """
    llm = ChatAnthropic(model="claude-sonnet-4-6")

    message = HumanMessage(content=[
        {
            "type": "image",
            "source": {
                "type": "url",
                "url": url,
            },
        },
        {"type": "text", "text": question},
    ])

    return llm.invoke([message]).content


# ── multi-image comparison ────────────────────────────────────────────────────

def compare_images(paths: list[str], question: str) -> str:
    """Send multiple images in one turn for comparison.

    Claude can hold all images in context simultaneously and reason across them.
    Useful for: before/after, chart comparison, product variants.
    """
    llm = ChatAnthropic(model="claude-sonnet-4-6")

    # Build a content block for each image, then append the question
    content: list[dict] = []
    for path in paths:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image_media_type(path),
                "data": encode_image_to_b64(path),
            },
        })
    content.append({"type": "text", "text": question})

    return llm.invoke([HumanMessage(content=content)]).content


# ── vision inside a LangGraph node ───────────────────────────────────────────

from typing import TypedDict
from langgraph.graph import StateGraph, END

class VisionState(TypedDict):
    image_path: str       # input: path to image file
    question: str         # input: what to ask
    description: str      # output: Claude's answer

def vision_node(state: VisionState) -> VisionState:
    """LangGraph node that wraps analyze_image.

    Nodes receive the full state dict and return a partial update.
    LangGraph merges the returned dict into the existing state.
    """
    description = analyze_image(state["image_path"], state["question"])
    return {"description": description}

# Build a minimal graph with a single vision node
vision_graph = (
    StateGraph(VisionState)
    .add_node("vision", vision_node)
    .set_entry_point("vision")
    .add_edge("vision", END)
    .compile()
)

if __name__ == "__main__":
    # Test single image
    result = analyze_image("screenshot.png", "Describe what you see.")
    print(result)

    # Test via graph
    out = vision_graph.invoke({
        "image_path": "screenshot.png",
        "question": "List any UI components visible.",
        "description": "",
    })
    print(out["description"])
```

---

## Pattern 2 — PDF Document Processing

### When to Use Which Loader

| Loader | Speed | Extracts images | Extracts tables | Needs AWS | Best for |
|---|---|---|---|---|---|
| `PyPDFLoader` | Fast | No | No | No | Native-text PDFs, quick extraction |
| `UnstructuredPDFLoader(hi_res)` | Slow | Yes | Yes | No | Research papers, mixed content |
| `PyMuPDFLoader` | Fast | Yes (raw) | Partial | No | When you need embedded images |
| `AmazonTextractPDFLoader` | Medium | No | Yes (structured) | Yes | Scanned docs, forms, receipts |

### Complete Code

```python
# pdf_loaders.py
from dotenv import load_dotenv
from langchain_community.document_loaders import (
    PyPDFLoader,
    UnstructuredPDFLoader,
    PyMuPDFLoader,
    AmazonTextractPDFLoader,
)

load_dotenv()

# ── Pattern 2a: Simple text extraction ───────────────────────────────────────
# Best for: native PDFs where you only need the text, no images or tables.
# PyPDFLoader splits by page — each Document has page_content and metadata.page.

def load_pdf_text(path: str) -> list:
    """Load a PDF page by page. Fast. Does NOT extract images or table structure."""
    loader = PyPDFLoader(path)
    pages = loader.load()           # returns List[Document]
    print(f"Loaded {len(pages)} pages")
    return pages


# ── Pattern 2b: Layout-aware extraction ──────────────────────────────────────
# Best for: PDFs with mixed text, images, tables, headers.
# "hi_res" strategy uses a layout detection model (detectron2) — slower but richer.
# Requires: pip install "unstructured[pdf]" detectron2 (or unstructured-inference)

def load_pdf_layout(path: str) -> list:
    """Load a PDF preserving table structure and figure captions."""
    loader = UnstructuredPDFLoader(
        path,
        mode="elements",            # return individual elements (table, text, image…)
        strategy="hi_res",          # use layout detection model
        infer_table_structure=True, # parse tables into HTML
    )
    elements = loader.load()
    print(f"Found {len(elements)} elements")
    # Each element has metadata.category: Table | NarrativeText | Title | Image …
    return elements


# ── Pattern 2c: Image extraction ─────────────────────────────────────────────
# Best for: PDFs with embedded charts, diagrams, or scanned pages you want
# to send directly to Claude Vision.
# Requires: pip install pymupdf

def load_pdf_with_images(path: str) -> tuple[list, list]:
    """Load PDF text via PyMuPDF and extract embedded images separately."""
    import fitz  # PyMuPDF

    # Text via LangChain loader
    loader = PyMuPDFLoader(path)
    pages = loader.load()

    # Images extracted directly via fitz
    doc = fitz.open(path)
    images = []
    for page_num, page in enumerate(doc):
        for img_index, img in enumerate(page.get_images(full=True)):
            xref = img[0]
            base_image = doc.extract_image(xref)
            images.append({
                "page": page_num,
                "index": img_index,
                "bytes": base_image["image"],   # raw bytes
                "ext": base_image["ext"],        # "png", "jpeg", etc.
            })
    doc.close()
    return pages, images


# ── Pattern 2d: AWS Textract (enterprise OCR) ─────────────────────────────────
# Best for: scanned PDFs, handwritten forms, bank statements, government docs.
# Requires: AWS credentials in .env, pip install amazon-textract-caller
# Set: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION

def load_pdf_textract(s3_path: str) -> list:
    """Load a scanned PDF from S3 using Amazon Textract for OCR.

    Textract understands key-value pairs in forms and cell boundaries in tables —
    no other loader does this for scanned documents.

    s3_path format: s3://bucket-name/path/to/file.pdf
    """
    loader = AmazonTextractPDFLoader(s3_path)
    docs = loader.load()
    return docs
```

---

## Pattern 3 — Table Extraction

### Concept: Why Tables Are Hard

PDFs represent tables as positioned text fragments — "Revenue", "2024", "$1.2M" scattered
across coordinates. Standard text extraction loses the row/column relationship.
`UnstructuredPDFLoader` with `infer_table_structure=True` rebuilds tables as HTML.
Then you convert that HTML to Markdown — a format Claude reads well.

```python
# table_extraction.py
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_community.document_loaders import UnstructuredPDFLoader

load_dotenv()

# ── Step 1: Extract raw table HTML ───────────────────────────────────────────

def extract_tables_from_pdf(path: str) -> list[dict]:
    """Extract all Table elements from a PDF, returning HTML strings."""
    loader = UnstructuredPDFLoader(
        path,
        mode="elements",
        strategy="hi_res",
        infer_table_structure=True,   # tables become HTML in element.metadata
    )
    elements = loader.load()

    tables = []
    for el in elements:
        # metadata.category tells you the element type
        if el.metadata.get("category") == "Table":
            tables.append({
                "page": el.metadata.get("page_number"),
                "html": el.metadata.get("text_as_html", ""),  # structured HTML
                "text": el.page_content,                        # fallback plain text
            })
    return tables


# ── Step 2: Convert HTML table to Markdown ────────────────────────────────────
# LLMs handle Markdown tables better than raw HTML — fewer tokens, cleaner layout.

def html_table_to_markdown(html: str) -> str:
    """Convert an HTML table string to Markdown using pandas.

    Requires: pip install pandas lxml
    """
    import pandas as pd
    from io import StringIO

    try:
        # pd.read_html parses all <table> tags from an HTML string
        dfs = pd.read_html(StringIO(html))
        if not dfs:
            return html  # fallback: return raw HTML if parsing fails
        # Convert first table to Markdown
        return dfs[0].to_markdown(index=False)
    except Exception:
        return html


# ── Step 3: Structured extraction from table → Pydantic ──────────────────────
# with_structured_output() tells Claude to return JSON matching your schema.
# This is more reliable than parsing Claude's prose output.

class FinancialRow(BaseModel):
    year: int = Field(description="Fiscal year")
    revenue_usd: float = Field(description="Revenue in USD")
    profit_usd: float = Field(description="Net profit in USD")


class FinancialTable(BaseModel):
    rows: list[FinancialRow]
    currency: str = Field(default="USD")


def extract_financial_data(table_markdown: str) -> FinancialTable:
    """Use Claude to parse a Markdown table into a typed Pydantic model.

    with_structured_output() wraps the LLM call in a JSON-mode tool call
    so the response is guaranteed to match FinancialTable's schema.
    """
    llm = ChatAnthropic(model="claude-sonnet-4-6")
    structured_llm = llm.with_structured_output(FinancialTable)

    prompt = f"""Extract all financial data from this table.

{table_markdown}

Return every row you find."""

    return structured_llm.invoke(prompt)


# ── Full pipeline ─────────────────────────────────────────────────────────────

def process_pdf_tables(path: str) -> list[FinancialTable]:
    tables = extract_tables_from_pdf(path)
    results = []
    for t in tables:
        md = html_table_to_markdown(t["html"])
        try:
            data = extract_financial_data(md)
            results.append(data)
        except Exception as e:
            print(f"Page {t['page']}: could not extract — {e}")
    return results
```

---

## Pattern 4 — Document Layout Understanding (Forms, Invoices, Receipts)

### Concept: Element-Type Filtering

When you load with `mode="elements"`, each chunk has a `metadata.category`:

| Category | What it is |
|---|---|
| `Title` | Section heading |
| `NarrativeText` | Body paragraph |
| `Table` | Tabular data |
| `ListItem` | Bullet point |
| `Image` | Embedded image reference |
| `Address` | Detected address block |
| `Header` / `Footer` | Page header or footer |

You can filter by category to route each element differently — tables go to
structured extraction, narrative text goes to the vector store, etc.

```python
# invoice_extraction.py
from pydantic import BaseModel, Field
from typing import Optional
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_community.document_loaders import UnstructuredPDFLoader

load_dotenv()


# ── Pydantic schema for an invoice ────────────────────────────────────────────

class LineItem(BaseModel):
    description: str
    quantity: float
    unit_price: float
    total: float


class Invoice(BaseModel):
    invoice_number: str
    vendor_name: str
    vendor_address: Optional[str] = None
    invoice_date: str
    due_date: Optional[str] = None
    line_items: list[LineItem]
    subtotal: float
    tax: Optional[float] = None
    total_due: float
    currency: str = Field(default="USD")


# ── Load and partition the invoice PDF ───────────────────────────────────────

def partition_invoice(path: str) -> dict[str, list]:
    """Load a PDF and bucket elements by category."""
    loader = UnstructuredPDFLoader(
        path,
        mode="elements",
        strategy="hi_res",
        infer_table_structure=True,
    )
    elements = loader.load()

    # Group by category for selective processing
    buckets: dict[str, list] = {}
    for el in elements:
        cat = el.metadata.get("category", "Unknown")
        buckets.setdefault(cat, []).append(el)

    return buckets


# ── Assemble a text representation for the LLM ───────────────────────────────

def elements_to_text(buckets: dict[str, list]) -> str:
    """Flatten relevant element categories into a single string.

    We include Table (as HTML for structure), NarrativeText, Address, Title.
    We skip Image, Header, Footer — they rarely help with invoice extraction.
    """
    parts = []

    for cat in ("Title", "NarrativeText", "Address", "Table", "ListItem"):
        for el in buckets.get(cat, []):
            if cat == "Table":
                # Include the structured HTML so the LLM sees cell boundaries
                html = el.metadata.get("text_as_html", el.page_content)
                parts.append(f"[TABLE]\n{html}\n[/TABLE]")
            else:
                parts.append(el.page_content)

    return "\n\n".join(parts)


# ── Extract structured invoice data ──────────────────────────────────────────

def extract_invoice(path: str) -> Invoice:
    """Full invoice extraction pipeline: partition → flatten → structured LLM call."""
    buckets = partition_invoice(path)
    text = elements_to_text(buckets)

    llm = ChatAnthropic(model="claude-sonnet-4-6")
    structured_llm = llm.with_structured_output(Invoice)

    return structured_llm.invoke(
        f"Extract all invoice details from the following document:\n\n{text}"
    )
```

---

## Pattern 5 — Multimodal RAG

### Concept: The Image-in-RAG Problem

Standard RAG embeds text chunks. Images have no text to embed — you need a bridge.
Three strategies exist:

| Strategy | How it works | Best for |
|---|---|---|
| **Image descriptions at ingest** | Run Claude at ingest time, embed the description | Small corpora, high accuracy |
| **CLIP embeddings** | Embed images directly in visual vector space | Large image sets, fast retrieval |
| **Mixed retrieval** | Text chunks + image descriptions in same store | Documents where text and figures complement each other |

### Complete Pipeline (Image Descriptions Strategy)

```python
# multimodal_rag.py
import base64
from pathlib import Path
from dotenv import load_dotenv

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain_anthropic import AnthropicEmbeddings

# NOTE: If AnthropicEmbeddings is unavailable, swap in:
# from langchain_openai import OpenAIEmbeddings
# embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

load_dotenv()


# ── Step 1: Describe each image with Claude at ingest time ───────────────────

def describe_image(image_bytes: bytes, media_type: str = "image/png") -> str:
    """Generate a rich text description of an image using Claude Vision.

    This description becomes the 'text' that gets embedded and retrieved.
    The prompt instructs Claude to include all details a retrieval system needs.
    """
    llm = ChatAnthropic(model="claude-sonnet-4-6")
    b64 = base64.b64encode(image_bytes).decode()

    message = HumanMessage(content=[
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        },
        {
            "type": "text",
            "text": (
                "Describe this image in detail for a search index. "
                "Include: main subject, any text visible, colors, layout, "
                "numbers/data if it's a chart, and any notable features. "
                "Be specific — your description will be the only representation "
                "of this image in a retrieval system."
            ),
        },
    ])
    return llm.invoke([message]).content


# ── Step 2: Ingest a mixed PDF (text + images) ───────────────────────────────

def ingest_pdf_with_images(pdf_path: str, collection_name: str = "multimodal_rag"):
    """Parse a PDF, describe all images, chunk text, and build a vector store."""
    import fitz  # PyMuPDF

    doc = fitz.open(pdf_path)
    documents: list[Document] = []

    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)

    for page_num, page in enumerate(doc):
        # ── Text chunks ────────────────────────────────────────────────────
        text = page.get_text()
        if text.strip():
            for chunk in splitter.split_text(text):
                documents.append(Document(
                    page_content=chunk,
                    metadata={"source": pdf_path, "page": page_num, "type": "text"},
                ))

        # ── Image descriptions ─────────────────────────────────────────────
        for img_index, img in enumerate(page.get_images(full=True)):
            xref = img[0]
            base_image = doc.extract_image(xref)
            img_bytes = base_image["image"]
            ext = base_image["ext"]
            media_type = f"image/{ext}" if ext != "jpg" else "image/jpeg"

            description = describe_image(img_bytes, media_type)
            documents.append(Document(
                page_content=description,
                metadata={
                    "source": pdf_path,
                    "page": page_num,
                    "type": "image_description",
                    "image_index": img_index,
                },
            ))
            print(f"  Described image {img_index} on page {page_num}")

    doc.close()

    # ── Build vector store ─────────────────────────────────────────────────
    # AnthropicEmbeddings or any embedding model works here
    embeddings = AnthropicEmbeddings(model="voyage-3")
    vectorstore = Chroma.from_documents(
        documents,
        embeddings,
        collection_name=collection_name,
    )
    print(f"Indexed {len(documents)} chunks ({collection_name})")
    return vectorstore


# ── Step 3: Multimodal RAG chain ─────────────────────────────────────────────

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser


def build_multimodal_rag_chain(vectorstore):
    """LCEL chain: retrieve relevant chunks → answer with Claude.

    The retrieved chunks may include image descriptions — Claude answers
    as if it had seen the original images (via the descriptions).
    """
    retriever = vectorstore.as_retriever(search_kwargs={"k": 6})

    prompt = ChatPromptTemplate.from_template("""Answer the question using the context below.
The context may include descriptions of images, charts, or tables extracted from a document.
Treat image descriptions as if you had seen the image.

Context:
{context}

Question: {question}

Answer:""")

    llm = ChatAnthropic(model="claude-sonnet-4-6")

    def format_docs(docs):
        return "\n\n---\n\n".join(
            f"[{d.metadata.get('type','text').upper()} p.{d.metadata.get('page',0)}]\n{d.page_content}"
            for d in docs
        )

    # LCEL pipe syntax: input flows left to right through each component
    chain = (
        {"context": retriever | format_docs, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )
    return chain
```

---

## Pattern 6 — Audio Input via Whisper Transcription

### Concept: Audio as a Pre-Processing Step

Claude does not accept raw audio files. The pattern is:
1. Whisper transcribes audio → text string
2. Text feeds into a normal LangChain chain or agent

You can expose Whisper as a LangGraph tool so an agent can transcribe on demand.

```python
# audio_tools.py
import os
from pathlib import Path
from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_core.tools import ToolException

load_dotenv()

# ── OpenAI Whisper API (cloud) ────────────────────────────────────────────────
# Pros: no GPU, fast, supports 99 languages, handles accents well
# Cons: sends audio to OpenAI, $0.006/minute, max 25 MB per file
# Requires: pip install openai

@tool
def transcribe_audio_api(audio_file_path: str) -> str:
    """Transcribe an audio file to text using the OpenAI Whisper API.

    Supports: mp3, mp4, mpeg, mpga, m4a, wav, webm
    Max file size: 25 MB
    Returns the full transcript as a string.
    """
    from openai import OpenAI

    path = Path(audio_file_path)
    if not path.exists():
        raise ToolException(f"Audio file not found: {audio_file_path}")
    if path.stat().st_size > 25 * 1024 * 1024:
        raise ToolException("File exceeds 25 MB Whisper API limit. Use local Whisper.")

    client = OpenAI()  # reads OPENAI_API_KEY from env
    with open(path, "rb") as f:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="text",
        )
    return transcript


# ── Local Whisper model (on-device) ──────────────────────────────────────────
# Pros: free, private, no size limit, works offline
# Cons: needs GPU for speed, ~1.5 GB model download, slower than API
# Requires: pip install openai-whisper torch

@tool
def transcribe_audio_local(audio_file_path: str, model_size: str = "base") -> str:
    """Transcribe an audio file locally using the open-source Whisper model.

    model_size options: tiny, base, small, medium, large
    Larger models are more accurate but slower and require more VRAM.
    'base' is a good default for English speech.
    """
    import whisper  # the open-source library, not openai

    path = Path(audio_file_path)
    if not path.exists():
        raise ToolException(f"Audio file not found: {audio_file_path}")

    model = whisper.load_model(model_size)
    result = model.transcribe(str(path))
    return result["text"]


# ── Post-transcription processing pipeline ───────────────────────────────────

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

def build_meeting_summary_chain():
    """LCEL chain: transcribed text → structured meeting notes.

    The | operator chains: prompt template → LLM → output parser.
    Each component's output becomes the next component's input.
    """
    llm = ChatAnthropic(model="claude-sonnet-4-6")

    prompt = ChatPromptTemplate.from_template("""You are a meeting notes assistant.

Transcript:
{transcript}

Extract:
1. Key decisions made
2. Action items (who, what, by when)
3. Open questions
4. Summary (3 sentences max)""")

    return prompt | llm | StrOutputParser()


def process_meeting_audio(audio_path: str) -> str:
    """Full pipeline: audio file → structured meeting notes."""
    # Step 1: Transcribe
    transcript = transcribe_audio_api.invoke({"audio_file_path": audio_path})

    # Step 2: Summarize via LCEL chain
    chain = build_meeting_summary_chain()
    return chain.invoke({"transcript": transcript})
```

---

## Pattern 7 — Multimodal Agent

### Concept: Agents That See

A LangGraph agent with multimodal tools can:
- Accept images alongside text in the initial message
- Call tools that capture screenshots
- Run a document processing pipeline on demand

```python
# multimodal_agent.py
import base64
from pathlib import Path
from typing import Annotated
from typing_extensions import TypedDict

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import tool, ToolException
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

load_dotenv()


# ── Tools ─────────────────────────────────────────────────────────────────────

@tool
def capture_screenshot(filename: str = "screenshot.png") -> str:
    """Capture the current screen and save it to a file.

    Returns the file path so the agent can then analyze it.
    Requires: pip install pyautogui pillow
    """
    try:
        import pyautogui
        screenshot = pyautogui.screenshot()
        screenshot.save(filename)
        return f"Screenshot saved to {filename}"
    except Exception as e:
        raise ToolException(f"Screenshot failed: {e}")


@tool
def analyze_image_file(image_path: str, question: str) -> str:
    """Analyze an image file with Claude Vision and return the answer.

    Use this after capture_screenshot to interpret what's on screen,
    or to analyze any image the user provides.
    """
    path = Path(image_path)
    if not path.exists():
        raise ToolException(f"Image not found: {image_path}")

    llm = ChatAnthropic(model="claude-sonnet-4-6")
    b64 = base64.b64encode(path.read_bytes()).decode()
    suffix = path.suffix.lower().lstrip(".")
    media_type = f"image/{'jpeg' if suffix == 'jpg' else suffix}"

    msg = HumanMessage(content=[
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
        {"type": "text", "text": question},
    ])
    return llm.invoke([msg]).content


@tool
def extract_pdf_text(pdf_path: str) -> str:
    """Extract all text from a PDF file.

    Returns plain text concatenated from all pages.
    Use analyze_image_file on individual pages for image-heavy PDFs.
    """
    try:
        from langchain_community.document_loaders import PyPDFLoader
        pages = PyPDFLoader(pdf_path).load()
        return "\n\n".join(p.page_content for p in pages)
    except Exception as e:
        raise ToolException(f"PDF extraction failed: {e}")


# ── Agent State ───────────────────────────────────────────────────────────────
# add_messages is a reducer: new messages are appended, not replaced.
# This is the key difference from a plain TypedDict — LangGraph knows how to
# merge message lists correctly.

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


# ── Graph Nodes ───────────────────────────────────────────────────────────────

tools = [capture_screenshot, analyze_image_file, extract_pdf_text]
tool_node = ToolNode(tools)  # handles all tool calls automatically

llm = ChatAnthropic(model="claude-sonnet-4-6")
llm_with_tools = llm.bind_tools(tools)  # tells Claude what tools are available


def agent_node(state: AgentState) -> AgentState:
    """Call the LLM. It decides whether to respond or call a tool."""
    response = llm_with_tools.invoke(state["messages"])
    return {"messages": [response]}


def should_continue(state: AgentState) -> str:
    """Route: if last message has tool_calls → tools node, else → end."""
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    return END


# ── Build the graph ───────────────────────────────────────────────────────────

graph = (
    StateGraph(AgentState)
    .add_node("agent", agent_node)
    .add_node("tools", tool_node)
    .set_entry_point("agent")
    .add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    .add_edge("tools", "agent")   # after tools, always go back to agent
    .compile({"recursion_limit": 25})
)


# ── Multimodal input helper ───────────────────────────────────────────────────

def chat_with_image(text: str, image_path: str | None = None) -> str:
    """Send a message (optionally with an image) to the multimodal agent."""
    if image_path:
        path = Path(image_path)
        b64 = base64.b64encode(path.read_bytes()).decode()
        suffix = path.suffix.lower().lstrip(".")
        media_type = f"image/{'jpeg' if suffix == 'jpg' else suffix}"
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": text},
        ]
    else:
        content = text

    result = graph.invoke({"messages": [HumanMessage(content=content)]})
    return result["messages"][-1].content


if __name__ == "__main__":
    # Pure text
    print(chat_with_image("What tools do you have available?"))

    # With image
    print(chat_with_image("What does this chart show?", "chart.png"))
```

---

## Pattern 8 — Batch Document Processing

### Concept: Async + Rate Limiting

Processing 100 PDFs serially takes hours. `asyncio` parallelizes API calls.
A `Semaphore` caps concurrent requests so you don't hit rate limits (Anthropic
default: 50 req/min on Sonnet). LangSmith traces each document run individually.

```python
# batch_processor.py
import asyncio
import json
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_community.document_loaders import PyPDFLoader

load_dotenv()

# ── Async single-document processor ──────────────────────────────────────────

async def process_single_pdf(
    pdf_path: Path,
    semaphore: asyncio.Semaphore,
    llm: ChatAnthropic,
) -> dict[str, Any]:
    """Process one PDF under a semaphore to respect rate limits.

    The semaphore is shared across all tasks — only N tasks run at once.
    asyncio.Semaphore is NOT thread-safe; only use inside async code.
    """
    async with semaphore:  # blocks until a slot is free
        try:
            # Load text (sync loader wrapped in executor for non-blocking I/O)
            loop = asyncio.get_event_loop()
            pages = await loop.run_in_executor(
                None,
                lambda: PyPDFLoader(str(pdf_path)).load()
            )
            text = "\n".join(p.page_content for p in pages[:5])  # first 5 pages

            # Async LLM call — does not block the event loop
            response = await llm.ainvoke(
                f"Summarize this document in 3 sentences:\n\n{text}"
            )

            return {
                "file": pdf_path.name,
                "status": "success",
                "summary": response.content,
                "pages": len(pages),
            }

        except Exception as e:
            # Never let one bad PDF crash the whole batch
            return {
                "file": pdf_path.name,
                "status": "error",
                "error": str(e),
            }


# ── Batch runner ──────────────────────────────────────────────────────────────

async def batch_process_pdfs(
    pdf_dir: str,
    max_concurrent: int = 5,   # tune based on your API tier
    output_file: str = "results.jsonl",
) -> list[dict]:
    """Process all PDFs in a directory concurrently with progress tracking.

    max_concurrent=5 is safe for Anthropic's default rate limit.
    Results stream to a JSONL file so you can resume if interrupted.
    """
    pdf_paths = list(Path(pdf_dir).glob("**/*.pdf"))
    print(f"Found {len(pdf_paths)} PDFs")

    llm = ChatAnthropic(model="claude-sonnet-4-6")
    semaphore = asyncio.Semaphore(max_concurrent)

    # Create all tasks upfront — asyncio schedules them
    tasks = [
        process_single_pdf(path, semaphore, llm)
        for path in pdf_paths
    ]

    results = []
    completed = 0

    # as_completed yields futures as they finish (not in submission order)
    for coro in asyncio.as_completed(tasks):
        result = await coro
        results.append(result)
        completed += 1

        # Stream results to disk — safe even if process is killed mid-run
        with open(output_file, "a") as f:
            f.write(json.dumps(result) + "\n")

        status = result["status"]
        print(f"[{completed}/{len(pdf_paths)}] {result['file']} — {status}")

    success = sum(1 for r in results if r["status"] == "success")
    print(f"\nDone: {success}/{len(pdf_paths)} succeeded")
    return results


# ── Error recovery: skip already-processed files ─────────────────────────────

def load_completed(output_file: str) -> set[str]:
    """Read a JSONL results file and return the set of already-processed filenames.

    Use this to resume a crashed batch without reprocessing completed files.
    """
    completed = set()
    path = Path(output_file)
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                record = json.loads(line)
                if record.get("status") == "success":
                    completed.add(record["file"])
            except json.JSONDecodeError:
                pass
    return completed


if __name__ == "__main__":
    asyncio.run(batch_process_pdfs("./documents", max_concurrent=5))
```

---

## Pattern 9 — Content Moderation for Images

### Concept: Two-Stage Moderation

Never send user-uploaded images directly to an expensive model.
Use **Claude Haiku** (fast, cheap) as a pre-screener. Only forward images
that pass the content check to your main pipeline.

```python
# moderation.py
import base64
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

load_dotenv()


class ModerationResult(str, Enum):
    SAFE = "safe"
    UNSAFE = "unsafe"
    UNCERTAIN = "uncertain"


class ModerationResponse(BaseModel):
    result: ModerationResult
    reason: str
    categories_flagged: list[str]


def moderate_image(image_path: str) -> ModerationResponse:
    """Run content moderation on an image using Claude Haiku.

    Haiku is 10x cheaper and faster than Sonnet — ideal for high-volume
    screening. Only images that pass go to the main pipeline.

    Returns a structured ModerationResponse with:
    - result: safe | unsafe | uncertain
    - reason: one-sentence explanation
    - categories_flagged: list of policy categories violated (empty if safe)
    """
    # Use Haiku for cost-effective pre-screening
    # claude-haiku-4-5 is the current fast/cheap model in the claude-sonnet-4-6 era
    llm = ChatAnthropic(model="claude-haiku-4-5")
    structured_llm = llm.with_structured_output(ModerationResponse)

    path = Path(image_path)
    b64 = base64.b64encode(path.read_bytes()).decode()
    suffix = path.suffix.lower().lstrip(".")
    media_type = f"image/{'jpeg' if suffix == 'jpg' else suffix}"

    message = HumanMessage(content=[
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        },
        {
            "type": "text",
            "text": (
                "You are a content moderation assistant. "
                "Evaluate this image for policy violations. "
                "Flag these categories if present: "
                "violence, explicit_content, hate_speech, self_harm, "
                "illegal_activity, personal_information, spam. "
                "Return safe if none apply. "
                "Return uncertain if you cannot determine from the image."
            ),
        },
    ])

    return structured_llm.invoke([message])


# ── Moderation node for LangGraph ─────────────────────────────────────────────

from typing import TypedDict
from langgraph.graph import StateGraph, END


class PipelineState(TypedDict):
    image_path: str
    moderation: ModerationResponse | None
    analysis: str
    blocked: bool


def moderation_node(state: PipelineState) -> PipelineState:
    """LangGraph node: screen image before expensive processing."""
    result = moderate_image(state["image_path"])
    blocked = result.result == ModerationResult.UNSAFE
    return {"moderation": result, "blocked": blocked}


def analysis_node(state: PipelineState) -> PipelineState:
    """LangGraph node: full analysis with Claude Sonnet (only reached if safe)."""
    llm = ChatAnthropic(model="claude-sonnet-4-6")

    path = Path(state["image_path"])
    b64 = base64.b64encode(path.read_bytes()).decode()
    suffix = path.suffix.lower().lstrip(".")
    media_type = f"image/{'jpeg' if suffix == 'jpg' else suffix}"

    msg = HumanMessage(content=[
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
        {"type": "text", "text": "Describe this image in detail."},
    ])
    response = llm.invoke([msg])
    return {"analysis": response.content}


def route_after_moderation(state: PipelineState) -> str:
    """Route: blocked images go to END, safe images proceed to analysis."""
    return END if state["blocked"] else "analysis"


# Build the moderated pipeline graph
moderated_pipeline = (
    StateGraph(PipelineState)
    .add_node("moderation", moderation_node)
    .add_node("analysis", analysis_node)
    .set_entry_point("moderation")
    .add_conditional_edges(
        "moderation",
        route_after_moderation,
        {"analysis": "analysis", END: END},
    )
    .add_edge("analysis", END)
    .compile()
)


if __name__ == "__main__":
    result = moderated_pipeline.invoke({
        "image_path": "test_image.png",
        "moderation": None,
        "analysis": "",
        "blocked": False,
    })

    if result["blocked"]:
        mod = result["moderation"]
        print(f"BLOCKED: {mod.reason}")
        print(f"Categories: {mod.categories_flagged}")
    else:
        print(f"SAFE: {result['analysis'][:200]}")
```

---

## Quick Reference — Dependencies

Add these to `pyproject.toml` based on which patterns you use:

```toml
dependencies = [
    # Core (always required)
    "langchain>=0.3.0",
    "langchain-anthropic>=0.3.0",
    "langchain-community>=0.3.0",
    "langgraph>=1.2.0",
    "langsmith>=0.2.0",
    "python-dotenv>=1.0.0",
    "pydantic>=2.0",

    # Pattern 2b/4 — layout-aware PDF
    "unstructured[pdf]>=0.14.0",
    # Note: hi_res strategy also needs: detectron2 or unstructured-inference

    # Pattern 2c/5 — image extraction from PDF
    "pymupdf>=1.24.0",

    # Pattern 3 — table to markdown
    "pandas>=2.0",
    "lxml>=5.0",
    "tabulate>=0.9",     # required by DataFrame.to_markdown()

    # Pattern 5 — multimodal RAG with Voyage embeddings
    "langchain-anthropic[voyage]>=0.3.0",   # or use openai embeddings

    # Pattern 6 — audio
    "openai>=1.0.0",           # for Whisper API
    # "openai-whisper>=20231117",  # for local Whisper (also needs torch)

    # Pattern 7 — screenshot capture
    "pyautogui>=0.9",
    "pillow>=10.0",
]
```

---

## Environment Variables (.env)

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-...

# LangSmith tracing (add to every project — zero code change required)
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=my-multimodal-project

# Pattern 6 — Whisper API
OPENAI_API_KEY=sk-...

# Pattern 2d — AWS Textract
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
```

---

## Decision Tree

```
What do you need to process?
│
├── Images only
│   └── Pattern 1 (analyze_image / compare_images)
│
├── PDF — text only, fast
│   └── Pattern 2a (PyPDFLoader)
│
├── PDF — needs table structure
│   ├── Digital PDF → Pattern 2b + Pattern 3 (UnstructuredPDFLoader)
│   └── Scanned PDF → Pattern 2d (AmazonTextractPDFLoader)
│
├── PDF — has embedded images you need
│   └── Pattern 2c (PyMuPDFLoader) + Pattern 1 per image
│
├── Invoice / form extraction → Pydantic output
│   └── Pattern 4 (partition + extract_invoice)
│
├── Large document corpus → Q&A
│   └── Pattern 5 (multimodal RAG)
│
├── Audio files
│   └── Pattern 6 (Whisper tool + post-processing chain)
│
├── Agent that can see AND use tools
│   └── Pattern 7 (multimodal agent)
│
├── 100s of documents
│   └── Pattern 8 (async batch processor)
│
└── User-uploaded images (untrusted input)
    └── Pattern 9 (moderation gate) → then Pattern 1 or 5
```

---

## Concepts Taught in This Skill

| Concept | Introduced in |
|---|---|
| Multimodal content blocks (`type: image`, `type: text`) | Pattern 1 |
| Base64 encoding for local images | Pattern 1 (`encode_image_to_b64`) |
| URL-based vs base64 image input | Pattern 1 |
| Multi-image comparison in one turn | Pattern 1 |
| Vision inside a LangGraph node | Pattern 1 |
| PDF loader trade-offs | Pattern 2 (trade-off table) |
| `mode="elements"` for structured PDF parsing | Pattern 2b |
| `strategy="hi_res"` layout detection | Pattern 2b |
| `infer_table_structure=True` HTML tables | Pattern 3 |
| HTML → Markdown table conversion | Pattern 3 |
| `with_structured_output()` for typed extraction | Pattern 3, 4, 9 |
| `metadata.category` element filtering | Pattern 4 |
| Multimodal RAG: image descriptions at ingest | Pattern 5 |
| `asyncio.Semaphore` for rate limiting | Pattern 8 |
| `asyncio.as_completed` for streaming progress | Pattern 8 |
| JSONL checkpointing for resumable batch jobs | Pattern 8 |
| Two-stage moderation (Haiku pre-screener) | Pattern 9 |
| Conditional edges in LangGraph | Pattern 7, 9 |
| LCEL pipe syntax (`|`) | Pattern 6, 5 |

---

## Transitions

After this skill, the user is ready for:

- `/lc-agent` — build a full ReAct agent with these multimodal tools wired in
- `/rag` — deeper RAG patterns (hybrid search, reranking, eval)
- `/lc-test` — evaluate your multimodal pipeline with LangSmith datasets
- `/lc-trace` — debug multimodal runs in the LangSmith UI
- `/lc-deploy` — serve your multimodal pipeline as an API
