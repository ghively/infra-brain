# lc:rag — LangChain RAG Patterns (Naive → Agentic)

## Purpose

Scaffold the right Retrieval-Augmented Generation (RAG) pattern for the user's
documents and query complexity. Covers the full spectrum from a beginner
three-file naive RAG pipeline to production-grade Self-RAG and Agentic RAG
implemented with LangGraph.

---

## Trigger Phrases

- "build a RAG pipeline"
- "set up RAG"
- "index my documents"
- "question answering over documents"
- "chat with my PDF"
- "retrieval augmented generation"
- "I want to query my docs"
- `/rag`

---

## Discovery Questions (ask before scaffolding)

Ask these four questions in a single message. Do not scaffold until you have answers.

```
1. DOCUMENT TYPE — What are you indexing?
   (a) PDF files
   (b) Web pages / URLs
   (c) Plain text / Markdown files
   (d) A directory of mixed files
   (e) Database / structured data

2. QUERY COMPLEXITY — What kinds of questions will users ask?
   (a) Simple factual look-up ("What is the return policy?")
   (b) Multi-part or comparative ("Compare pricing in section 2 and 5")
   (c) Complex multi-hop reasoning ("Why did revenue fall given the market conditions?")

3. ACCURACY REQUIREMENT — How bad is a wrong answer?
   (a) Low stakes — wrong answers are annoying but fine
   (b) Medium — should usually be right
   (c) High stakes — hallucinations are unacceptable (legal, medical, finance)

4. DEPLOYMENT TARGET — Where is this running?
   (a) Local / dev machine (no paid vector DB needed)
   (b) Production (need hosted vector store)
```

Use the answers to select the pattern:

| Query | Accuracy | Pattern |
|-------|----------|---------|
| Simple | Low | Naive RAG |
| Multi-part | Medium | Multi-Query or Contextual Compression |
| Multi-part | High | Self-RAG or CRAG |
| Complex | Medium | Query Transformation |
| Complex | High | Agentic RAG |
| Any — poor chunk quality | Any | Contextual Retrieval (Pattern 9) |
| Any — mixed keyword+semantic | Medium/High | Hybrid Search with RRF (Pattern 10) |
| Any — top-k ranking matters | High | Cross-Encoder Re-Ranking (Pattern 11) |
| Diverse query types in one app | High | Adaptive RAG / Query Router (Pattern 12) |

---

## Pattern 1 — Naive RAG

**When to use:** Simple factual Q&A, dev prototyping, beginner starting point.

**Files to create:**
```
rag_naive/
  ingest.py        # load, split, embed, store
  chain.py         # retriever + LCEL chain
  ask.py           # CLI entry point
  requirements.txt
```

### `requirements.txt`
```
langchain>=0.3
langchain-openai>=0.2
langchain-community>=0.3
langchain-chroma>=0.1
chromadb>=0.5
pypdf>=4.0          # for PDFs
beautifulsoup4      # for web loader
```

### `ingest.py`
```python
"""
Ingest documents into a Chroma vector store.
Run once: python ingest.py
"""
import os
from pathlib import Path

from langchain_community.document_loaders import (
    PyPDFLoader,
    WebBaseLoader,
    TextLoader,
    DirectoryLoader,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma

# ── configuration ──────────────────────────────────────────────────────────────
PERSIST_DIR = "./chroma_db"
CHUNK_SIZE   = 1000
CHUNK_OVERLAP = 200

# ── loaders (pick one) ─────────────────────────────────────────────────────────

def load_pdf(path: str):
    """Load a single PDF file."""
    return PyPDFLoader(path).load()

def load_web(urls: list[str]):
    """Load one or more web pages."""
    return WebBaseLoader(urls).load()

def load_text(path: str):
    """Load a plain-text or Markdown file."""
    return TextLoader(path, encoding="utf-8").load()

def load_directory(directory: str, glob: str = "**/*.pdf"):
    """Load all matching files in a directory tree."""
    return DirectoryLoader(directory, glob=glob, loader_cls=PyPDFLoader).load()

# ── splitter ───────────────────────────────────────────────────────────────────

def split_documents(docs):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        add_start_index=True,   # adds 'start_index' metadata — useful for debugging
    )
    return splitter.split_documents(docs)

# ── embed + store ──────────────────────────────────────────────────────────────

def build_vectorstore(chunks):
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vectorstore = Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        persist_directory=PERSIST_DIR,
    )
    return vectorstore

# ── main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Change this to your source:
    docs = load_pdf("docs/my_document.pdf")
    # docs = load_web(["https://example.com/page"])
    # docs = load_directory("./docs", glob="**/*.pdf")

    chunks = split_documents(docs)
    print(f"Split into {len(chunks)} chunks")

    vs = build_vectorstore(chunks)
    print(f"Stored in {PERSIST_DIR}")
```

### `chain.py`
```python
"""
Build the retrieval chain. Import and call ask() from ask.py.
"""
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough

PERSIST_DIR = "./chroma_db"

# ── retriever ──────────────────────────────────────────────────────────────────

def load_retriever(k: int = 4):
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vectorstore = Chroma(
        persist_directory=PERSIST_DIR,
        embedding_function=embeddings,
    )
    return vectorstore.as_retriever(
        search_type="similarity",          # or "mmr" for diversity
        search_kwargs={"k": k},
    )

# ── prompt ─────────────────────────────────────────────────────────────────────

RAG_PROMPT = ChatPromptTemplate.from_template("""
You are an assistant answering questions based on the provided context.
If the context does not contain the answer, say "I don't know."

Context:
{context}

Question: {question}

Answer:
""")

# ── format helper ──────────────────────────────────────────────────────────────

def format_docs(docs):
    return "\n\n".join(doc.page_content for doc in docs)

# ── chain ──────────────────────────────────────────────────────────────────────

def build_chain():
    retriever = load_retriever()
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    chain = (
        {
            "context":  retriever | format_docs,
            "question": RunnablePassthrough(),
        }
        | RAG_PROMPT
        | llm
        | StrOutputParser()
    )
    return chain
```

### `ask.py`
```python
"""CLI entry point. Usage: python ask.py "Your question here" """
import sys
from chain import build_chain

def main():
    question = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else input("Question: ")
    chain = build_chain()
    answer = chain.invoke(question)
    print(answer)

if __name__ == "__main__":
    main()
```

**HuggingFace embeddings alternative** (no OpenAI key needed):
```python
from langchain_huggingface import HuggingFaceEmbeddings

embeddings = HuggingFaceEmbeddings(
    model_name="sentence-transformers/all-MiniLM-L6-v2",
    model_kwargs={"device": "cpu"},
)
```

**Pinecone/Qdrant swap-in** (production):
```python
# Pinecone
from langchain_pinecone import PineconeVectorStore
vectorstore = PineconeVectorStore.from_documents(chunks, embeddings, index_name="my-index")

# Qdrant
from langchain_qdrant import QdrantVectorStore
vectorstore = QdrantVectorStore.from_documents(
    chunks, embeddings,
    url="http://localhost:6333",
    collection_name="my_collection",
)
```

---

## Pattern 2 — Multi-Query RAG

**When to use:** Single query often misses relevant documents because of wording
variance; you want higher recall at the cost of a few extra LLM calls.

**How it works:** An LLM generates N alternative phrasings of the question.
Each phrasing is sent to the retriever independently. Results are deduplicated
and merged before the answer chain.

```python
"""multi_query_rag.py — drop-in replacement for chain.py retriever"""
from langchain.retrievers.multi_query import MultiQueryRetriever
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
import logging

logging.basicConfig()
logging.getLogger("langchain.retrievers.multi_query").setLevel(logging.INFO)

PERSIST_DIR = "./chroma_db"

def build_chain():
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vectorstore = Chroma(persist_directory=PERSIST_DIR, embedding_function=embeddings)
    base_retriever = vectorstore.as_retriever(search_kwargs={"k": 4})

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    # MultiQueryRetriever wraps the base retriever.
    # It generates 3 alternative phrasings by default and deduplicates results.
    retriever = MultiQueryRetriever.from_llm(
        retriever=base_retriever,
        llm=llm,
        # Optional: override the default prompt that generates alternative queries
    )

    prompt = ChatPromptTemplate.from_template("""
Answer the question based on the context below.
If unsure, say "I don't know."

Context: {context}
Question: {question}
Answer:
""")

    def format_docs(docs):
        return "\n\n".join(d.page_content for d in docs)

    chain = (
        {"context": retriever | format_docs, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )
    return chain


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or input("Question: ")
    print(build_chain().invoke(q))
```

**Custom query-generation prompt:**
```python
from langchain_core.prompts import PromptTemplate

QUERY_PROMPT = PromptTemplate(
    input_variables=["question"],
    template="""You are an AI assistant helping with document search.
Generate 3 different search queries for the following question.
Output one query per line with no numbering or extra text.

Original question: {question}
Queries:""",
)

retriever = MultiQueryRetriever.from_llm(
    retriever=base_retriever, llm=llm, prompt=QUERY_PROMPT
)
```

---

## Pattern 3 — RAG with Query Transformation

**When to use:** Queries are ambiguous, too specific, or benefit from a broader
framing before retrieval.

### 3a — Step-Back Prompting

Ask a broader background question first; retrieve against that; combine with the
original question.

```python
"""step_back_rag.py"""
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate, FewShotChatMessagePromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough, RunnableLambda

PERSIST_DIR = "./chroma_db"

# Few-shot examples for generating step-back questions
EXAMPLES = [
    {
        "input": "What was the impact of the 2008 financial crisis on Lehman Brothers?",
        "output": "What are the general causes and effects of financial crises on investment banks?",
    },
    {
        "input": "What is the drug Metformin used for?",
        "output": "What are the general principles of diabetes treatment?",
    },
]

example_prompt = FewShotChatMessagePromptTemplate(
    example_prompt=ChatPromptTemplate.from_messages(
        [("human", "{input}"), ("ai", "{output}")]
    ),
    examples=EXAMPLES,
)

STEP_BACK_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "You are an expert. Given a specific question, generate a more general "
               "step-back question that captures the underlying concept."),
    example_prompt,
    ("human", "{question}"),
])

ANSWER_PROMPT = ChatPromptTemplate.from_template("""
Answer the specific question using both the step-back context (background) and
the direct context (specific docs). If the context does not contain the answer,
say "I don't know."

Step-back context (background knowledge):
{step_back_context}

Direct context:
{direct_context}

Question: {question}
Answer:
""")


def build_chain():
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vectorstore = Chroma(persist_directory=PERSIST_DIR, embedding_function=embeddings)
    retriever = vectorstore.as_retriever(search_kwargs={"k": 4})

    def format_docs(docs):
        return "\n\n".join(d.page_content for d in docs)

    # Generate the step-back question
    step_back_chain = STEP_BACK_PROMPT | llm | StrOutputParser()

    chain = (
        RunnablePassthrough.assign(
            step_back_question=step_back_chain,
        )
        | RunnablePassthrough.assign(
            step_back_context=lambda x: format_docs(retriever.invoke(x["step_back_question"])),
            direct_context=lambda x: format_docs(retriever.invoke(x["question"])),
        )
        | ANSWER_PROMPT
        | llm
        | StrOutputParser()
    )
    return chain


if __name__ == "__main__":
    chain = build_chain()
    result = chain.invoke({"question": "What are the key findings in section 3?"})
    print(result)
```

### 3b — HyDE (Hypothetical Document Embeddings)

Generate a hypothetical answer document, embed it, retrieve by its embedding.
Often outperforms query embedding alone.

```python
"""hyde_rag.py"""
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough

PERSIST_DIR = "./chroma_db"

HYPOTHETICAL_PROMPT = ChatPromptTemplate.from_template("""
Write a short paragraph (2-4 sentences) that would directly answer the following question.
Write as if you are excerpting from a document. Do not preface with "Here is a passage...".

Question: {question}
Passage:
""")

ANSWER_PROMPT = ChatPromptTemplate.from_template("""
Answer the question based on the context.
If the context does not contain the answer, say "I don't know."

Context: {context}
Question: {question}
Answer:
""")


def build_chain():
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vectorstore = Chroma(persist_directory=PERSIST_DIR, embedding_function=embeddings)

    def retrieve_by_hypothesis(inputs):
        hypothesis = inputs["hypothesis"]
        # embed the hypothesis and use it to retrieve real docs
        docs = vectorstore.similarity_search(hypothesis, k=4)
        return "\n\n".join(d.page_content for d in docs)

    chain = (
        RunnablePassthrough.assign(
            hypothesis=HYPOTHETICAL_PROMPT | llm | StrOutputParser()
        )
        | RunnablePassthrough.assign(context=retrieve_by_hypothesis)
        | ANSWER_PROMPT
        | llm
        | StrOutputParser()
    )
    return chain
```

### 3c — Query Rewriting

```python
"""query_rewrite_rag.py"""
REWRITE_PROMPT = ChatPromptTemplate.from_template("""
Rewrite the following question to be more specific and retrieve better results
from a vector database. Output only the rewritten question, nothing else.

Original question: {question}
Rewritten question:
""")

def build_chain():
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vectorstore = Chroma(persist_directory=PERSIST_DIR, embedding_function=embeddings)
    retriever = vectorstore.as_retriever(search_kwargs={"k": 4})

    def format_docs(docs):
        return "\n\n".join(d.page_content for d in docs)

    chain = (
        RunnablePassthrough.assign(
            rewritten=REWRITE_PROMPT | llm | StrOutputParser()
        )
        | RunnablePassthrough.assign(
            context=lambda x: format_docs(retriever.invoke(x["rewritten"]))
        )
        | ChatPromptTemplate.from_template(
            "Answer based on context.\nContext: {context}\nQuestion: {question}\nAnswer:"
        )
        | llm
        | StrOutputParser()
    )
    return chain
```

---

## Pattern 4 — Contextual Compression RAG

**When to use:** Retrieved chunks contain a lot of irrelevant text around the
useful portion, causing context bloat and worse answers.

```python
"""contextual_compression_rag.py"""
from langchain.retrievers import ContextualCompressionRetriever
from langchain.retrievers.document_compressors import (
    LLMChainExtractor,
    EmbeddingsFilter,
    DocumentCompressorPipeline,
)
from langchain_community.document_transformers import EmbeddingsRedundantFilter
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough

PERSIST_DIR = "./chroma_db"


def build_chain(compressor_type: str = "pipeline"):
    """
    compressor_type:
      "llm"       — LLMChainExtractor: extract the relevant sentence(s)
      "embedding" — EmbeddingsFilter: drop chunks below similarity threshold
      "pipeline"  — both: filter then extract
    """
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vectorstore = Chroma(persist_directory=PERSIST_DIR, embedding_function=embeddings)
    base_retriever = vectorstore.as_retriever(search_kwargs={"k": 6})

    if compressor_type == "llm":
        compressor = LLMChainExtractor.from_llm(llm)

    elif compressor_type == "embedding":
        compressor = EmbeddingsFilter(
            embeddings=embeddings,
            similarity_threshold=0.76,   # drop chunks below this cosine similarity
        )

    else:  # pipeline
        redundant_filter = EmbeddingsRedundantFilter(embeddings=embeddings)
        relevance_filter = EmbeddingsFilter(
            embeddings=embeddings, similarity_threshold=0.76
        )
        extractor = LLMChainExtractor.from_llm(llm)
        compressor = DocumentCompressorPipeline(
            transformers=[redundant_filter, relevance_filter, extractor]
        )

    compression_retriever = ContextualCompressionRetriever(
        base_compressor=compressor,
        base_retriever=base_retriever,
    )

    prompt = ChatPromptTemplate.from_template("""
Answer based on context. Say "I don't know" if unsure.
Context: {context}
Question: {question}
Answer:
""")

    def format_docs(docs):
        return "\n\n".join(d.page_content for d in docs)

    chain = (
        {"context": compression_retriever | format_docs, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )
    return chain
```

---

## Pattern 5 — Self-RAG (LangGraph)

**When to use:** High-stakes answers where hallucinations are unacceptable.
Self-RAG adds grading loops: verify doc relevance, verify answer groundedness,
verify answer quality — and retries if any grade fails.

```
pip install langgraph
```

```python
"""self_rag.py — LangGraph implementation of Self-RAG"""
from __future__ import annotations
from typing import Annotated, TypedDict, Literal
import operator

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.documents import Document
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, END

PERSIST_DIR = "./chroma_db"

# ── state ──────────────────────────────────────────────────────────────────────

class GraphState(TypedDict):
    question:          str
    documents:         list[Document]
    generation:        str
    retry_count:       int

# ── graders (structured output) ───────────────────────────────────────────────

class GradeDocuments(BaseModel):
    """Binary score for relevance check."""
    binary_score: Literal["yes", "no"] = Field(
        description="'yes' if the document is relevant to the question, 'no' otherwise."
    )

class GradeHallucinations(BaseModel):
    """Binary score for hallucination check."""
    binary_score: Literal["yes", "no"] = Field(
        description="'yes' if the answer is grounded in the documents, 'no' if it hallucinates."
    )

class GradeAnswer(BaseModel):
    """Binary score for answer quality check."""
    binary_score: Literal["yes", "no"] = Field(
        description="'yes' if the answer addresses the question, 'no' otherwise."
    )

# ── shared resources ───────────────────────────────────────────────────────────

def _get_retriever():
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vs = Chroma(persist_directory=PERSIST_DIR, embedding_function=embeddings)
    return vs.as_retriever(search_kwargs={"k": 4})

def _get_llm():
    return ChatOpenAI(model="gpt-4o-mini", temperature=0)

# ── nodes ──────────────────────────────────────────────────────────────────────

def retrieve(state: GraphState) -> GraphState:
    """Retrieve documents for the question."""
    retriever = _get_retriever()
    docs = retriever.invoke(state["question"])
    return {"documents": docs, "retry_count": state.get("retry_count", 0)}


def grade_documents(state: GraphState) -> GraphState:
    """Keep only relevant documents."""
    llm = _get_llm().with_structured_output(GradeDocuments)

    grade_prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a relevance grader. Score whether a document is relevant "
                   "to a question. Respond with binary_score 'yes' or 'no'."),
        ("human", "Document:\n{document}\n\nQuestion: {question}"),
    ])
    grader = grade_prompt | llm

    relevant = []
    for doc in state["documents"]:
        result = grader.invoke({"document": doc.page_content, "question": state["question"]})
        if result.binary_score == "yes":
            relevant.append(doc)

    return {"documents": relevant}


def generate(state: GraphState) -> GraphState:
    """Generate an answer from the (filtered) documents."""
    llm = _get_llm()
    prompt = ChatPromptTemplate.from_template("""
Answer the question based ONLY on the context below. If unsure, say "I don't know."

Context:
{context}

Question: {question}
Answer:
""")
    chain = prompt | llm | StrOutputParser()
    context = "\n\n".join(d.page_content for d in state["documents"])
    generation = chain.invoke({"context": context, "question": state["question"]})
    return {"generation": generation}


def rewrite_query(state: GraphState) -> GraphState:
    """Rewrite the query when retrieval was poor."""
    llm = _get_llm()
    prompt = ChatPromptTemplate.from_template(
        "Rewrite this question to improve retrieval. Output only the rewritten question.\n\n"
        "Question: {question}\nRewritten:"
    )
    new_q = (prompt | llm | StrOutputParser()).invoke({"question": state["question"]})
    return {"question": new_q, "retry_count": state.get("retry_count", 0) + 1}

# ── conditional edges ──────────────────────────────────────────────────────────

def decide_after_grading(state: GraphState) -> str:
    """If no relevant docs and retries remain, rewrite; else generate."""
    if not state["documents"]:
        if state.get("retry_count", 0) < 2:
            return "rewrite"
        return "generate"   # give up and generate with empty context
    return "generate"


def decide_after_generation(state: GraphState) -> str:
    """Check hallucination and answer quality."""
    llm = _get_llm()

    # Hallucination check
    hall_prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a hallucination grader. Score whether the answer is grounded "
                   "in the provided documents. 'yes' = grounded, 'no' = hallucination."),
        ("human", "Documents:\n{documents}\n\nAnswer: {generation}"),
    ])
    hall_grader = hall_prompt | llm.with_structured_output(GradeHallucinations)
    hall_result = hall_grader.invoke({
        "documents": "\n\n".join(d.page_content for d in state["documents"]),
        "generation": state["generation"],
    })

    if hall_result.binary_score == "no":
        if state.get("retry_count", 0) < 2:
            return "regenerate"
        return "end"   # give up

    # Answer quality check
    ans_prompt = ChatPromptTemplate.from_messages([
        ("system", "Does the answer address the question? 'yes' or 'no'."),
        ("human", "Question: {question}\nAnswer: {generation}"),
    ])
    ans_grader = ans_prompt | llm.with_structured_output(GradeAnswer)
    ans_result = ans_grader.invoke({
        "question": state["question"],
        "generation": state["generation"],
    })

    if ans_result.binary_score == "yes":
        return "end"
    if state.get("retry_count", 0) < 2:
        return "rewrite"
    return "end"

# ── graph assembly ─────────────────────────────────────────────────────────────

def build_graph():
    graph = StateGraph(GraphState)

    graph.add_node("retrieve",        retrieve)
    graph.add_node("grade_documents", grade_documents)
    graph.add_node("generate",        generate)
    graph.add_node("rewrite_query",   rewrite_query)

    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve",        "grade_documents")
    graph.add_conditional_edges(
        "grade_documents",
        decide_after_grading,
        {"generate": "generate", "rewrite": "rewrite_query"},
    )
    graph.add_edge("rewrite_query",   "retrieve")
    graph.add_conditional_edges(
        "generate",
        decide_after_generation,
        {"end": END, "regenerate": "generate", "rewrite": "rewrite_query"},
    )

    return graph.compile()


if __name__ == "__main__":
    import sys
    app = build_graph()
    question = " ".join(sys.argv[1:]) or input("Question: ")
    result = app.invoke({"question": question, "retry_count": 0})
    print(result["generation"])
```

---

## Pattern 6 — Corrective RAG (CRAG)

**When to use:** Your document corpus may be incomplete. When retrieval is poor,
fall back to a web search to supplement with external knowledge, then rewrite and
re-retrieve.

```
pip install langgraph tavily-python
```

```python
"""crag.py — Corrective RAG with LangGraph + Tavily web search fallback"""
from __future__ import annotations
from typing import TypedDict, Literal

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.documents import Document
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, END

PERSIST_DIR = "./chroma_db"

# ── state ──────────────────────────────────────────────────────────────────────

class GraphState(TypedDict):
    question:   str
    documents:  list[Document]
    generation: str
    web_used:   bool

# ── grader ─────────────────────────────────────────────────────────────────────

class RelevanceGrade(BaseModel):
    score: Literal["relevant", "irrelevant", "ambiguous"] = Field(
        description="Relevance grade for the retrieved document."
    )

def _grade_doc(doc: Document, question: str, llm) -> str:
    prompt = ChatPromptTemplate.from_messages([
        ("system", "Grade how relevant this document is to the question. "
                   "Respond with 'relevant', 'irrelevant', or 'ambiguous'."),
        ("human", "Document:\n{doc}\n\nQuestion: {question}"),
    ])
    grader = prompt | llm.with_structured_output(RelevanceGrade)
    return grader.invoke({"doc": doc.page_content, "question": question}).score

# ── nodes ──────────────────────────────────────────────────────────────────────

def retrieve(state: GraphState) -> GraphState:
    retriever = Chroma(
        persist_directory=PERSIST_DIR,
        embedding_function=OpenAIEmbeddings(model="text-embedding-3-small"),
    ).as_retriever(search_kwargs={"k": 4})
    docs = retriever.invoke(state["question"])
    return {"documents": docs, "web_used": False}


def grade_and_filter(state: GraphState) -> GraphState:
    """Grade docs; if all irrelevant trigger web search path."""
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    graded = []
    for doc in state["documents"]:
        grade = _grade_doc(doc, state["question"], llm)
        if grade in ("relevant", "ambiguous"):
            graded.append(doc)
    return {"documents": graded}


def web_search(state: GraphState) -> GraphState:
    """Fallback: rewrite query and search the web."""
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    # Rewrite for web search
    rewrite_prompt = ChatPromptTemplate.from_template(
        "Rewrite this question as a web search query:\n{question}\nQuery:"
    )
    query = (rewrite_prompt | llm | StrOutputParser()).invoke({"question": state["question"]})

    search = TavilySearchResults(max_results=3)
    results = search.invoke(query)
    web_docs = [Document(page_content=r["content"], metadata={"source": r["url"]}) for r in results]

    return {"documents": state["documents"] + web_docs, "web_used": True}


def generate(state: GraphState) -> GraphState:
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = ChatPromptTemplate.from_template("""
Answer the question based on the context. If the context is insufficient, say so.
{"(Note: web search was used to supplement the documents.)" if web_used else ""}

Context:
{context}

Question: {question}
Answer:
""")
    context = "\n\n".join(d.page_content for d in state["documents"])
    chain = prompt | llm | StrOutputParser()
    generation = chain.invoke({
        "context": context,
        "question": state["question"],
        "web_used": state.get("web_used", False),
    })
    return {"generation": generation}

# ── routing ────────────────────────────────────────────────────────────────────

def route_after_grading(state: GraphState) -> str:
    """If no relevant docs after grading, fall back to web search."""
    return "generate" if state["documents"] else "web_search"

# ── graph ──────────────────────────────────────────────────────────────────────

def build_graph():
    graph = StateGraph(GraphState)

    graph.add_node("retrieve",          retrieve)
    graph.add_node("grade_and_filter",  grade_and_filter)
    graph.add_node("web_search",        web_search)
    graph.add_node("generate",          generate)

    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve",         "grade_and_filter")
    graph.add_conditional_edges(
        "grade_and_filter",
        route_after_grading,
        {"generate": "generate", "web_search": "web_search"},
    )
    graph.add_edge("web_search",       "generate")
    graph.add_edge("generate",         END)

    return graph.compile()


if __name__ == "__main__":
    import sys
    app = build_graph()
    q = " ".join(sys.argv[1:]) or input("Question: ")
    result = app.invoke({"question": q})
    print(result["generation"])
    if result.get("web_used"):
        print("\n[Web search was used to supplement local documents]")
```

---

## Pattern 7 — Agentic RAG

**When to use:** Queries require multi-hop reasoning (the answer to one
sub-question determines what to retrieve next), or the agent must decide whether
to retrieve at all.

**Architecture:** RAG becomes a tool inside a ReAct agent. The agent plans,
calls retrieve, reads, plans again, and synthesizes.

```python
"""agentic_rag.py — RAG as a ReAct agent tool with LangGraph"""
from __future__ import annotations
from typing import TypedDict, Annotated
import operator

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.tools import tool
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode, tools_condition

PERSIST_DIR = "./chroma_db"

# ── tool definition ────────────────────────────────────────────────────────────

@tool
def retrieve_documents(query: str) -> str:
    """
    Search the document store for information relevant to the query.
    Use this when you need to look up facts, definitions, or specific information.
    Returns relevant text passages.
    """
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vs = Chroma(persist_directory=PERSIST_DIR, embedding_function=embeddings)
    docs = vs.similarity_search(query, k=4)
    if not docs:
        return "No relevant documents found."
    return "\n\n---\n\n".join(
        f"[Source: {d.metadata.get('source', 'unknown')}]\n{d.page_content}" for d in docs
    )

# ── state ──────────────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], operator.add]

# ── agent node ─────────────────────────────────────────────────────────────────

TOOLS = [retrieve_documents]

def agent_node(state: AgentState) -> AgentState:
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0).bind_tools(TOOLS)
    response = llm.invoke(state["messages"])
    return {"messages": [response]}

# ── graph ──────────────────────────────────────────────────────────────────────

def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("agent",  agent_node)
    graph.add_node("tools",  ToolNode(TOOLS))

    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent",
        tools_condition,  # routes to "tools" if tool_calls present, else END
    )
    graph.add_edge("tools", "agent")   # after tool call, back to agent to synthesize

    return graph.compile()


def ask(question: str) -> str:
    app = build_graph()
    system = (
        "You are a research assistant with access to a document store. "
        "For questions that need specific facts, use the retrieve_documents tool. "
        "You may call the tool multiple times with different queries for multi-hop questions. "
        "Synthesize a complete answer once you have enough information."
    )
    messages = [
        {"role": "system", "content": system},
        HumanMessage(content=question),
    ]
    result = app.invoke({"messages": messages})
    return result["messages"][-1].content


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or input("Question: ")
    print(ask(q))
```

**Multi-hop decompose-retrieve-synthesize variant:**
```python
"""multi_hop_rag.py — explicit decomposition before retrieval"""
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from pydantic import BaseModel
from typing import List

class SubQuestions(BaseModel):
    questions: List[str]

DECOMPOSE_PROMPT = ChatPromptTemplate.from_template("""
Break the following complex question into 2-4 simpler sub-questions that can each
be answered independently from a document store. Return JSON with key "questions".

Complex question: {question}
JSON:
""")

def decompose_and_answer(question: str, chain) -> str:
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    decomposer = DECOMPOSE_PROMPT | llm | JsonOutputParser()

    sub = decomposer.invoke({"question": question})
    sub_answers = []
    for sq in sub["questions"]:
        ans = chain.invoke(sq)
        sub_answers.append(f"Q: {sq}\nA: {ans}")

    synthesis_prompt = ChatPromptTemplate.from_template("""
Using the following sub-answers, write a comprehensive answer to the original question.

Sub-answers:
{sub_answers}

Original question: {question}
Final answer:
""")
    synthesis = synthesis_prompt | llm
    from langchain_core.output_parsers import StrOutputParser
    return (synthesis | StrOutputParser()).invoke({
        "sub_answers": "\n\n".join(sub_answers),
        "question": question,
    })
```

---

## Pattern 8 — Advanced Retrieval Topics

### 8a — Multi-Vector Retriever (Parent-Child Documents)

Store small child chunks for retrieval precision; return large parent chunks for
answer context.

```python
"""multi_vector_retriever.py"""
import uuid
from langchain.retrievers.multi_vector import MultiVectorRetriever
from langchain.storage import InMemoryByteStore
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document

def build_multi_vector_retriever(parent_docs: list[Document]):
    """
    parent_docs: large documents (e.g. full PDF pages)
    Returns a retriever that indexes small child chunks but returns parent docs.
    """
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vectorstore = Chroma(
        collection_name="child_chunks",
        embedding_function=embeddings,
    )
    # docstore maps parent IDs → parent Document objects
    docstore = InMemoryByteStore()
    id_key = "doc_id"

    retriever = MultiVectorRetriever(
        vectorstore=vectorstore,
        byte_store=docstore,
        id_key=id_key,
    )

    # Create parent IDs
    doc_ids = [str(uuid.uuid4()) for _ in parent_docs]

    # Split parents into small child chunks
    child_splitter = RecursiveCharacterTextSplitter(chunk_size=400, chunk_overlap=50)
    child_docs = []
    for doc_id, parent in zip(doc_ids, parent_docs):
        children = child_splitter.split_documents([parent])
        for child in children:
            child.metadata[id_key] = doc_id   # link child → parent
        child_docs.extend(children)

    # Add children to vectorstore, parents to docstore
    retriever.vectorstore.add_documents(child_docs)
    retriever.docstore.mset(list(zip(doc_ids, parent_docs)))

    return retriever
```

### 8b — Ensemble Retriever (BM25 + Semantic Hybrid Search)

```python
"""ensemble_retriever.py"""
from langchain.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document

def build_ensemble_retriever(docs: list[Document]):
    # BM25 (keyword / sparse)
    bm25_retriever = BM25Retriever.from_documents(docs)
    bm25_retriever.k = 4

    # Semantic (dense)
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vs = Chroma.from_documents(docs, embeddings)
    semantic_retriever = vs.as_retriever(search_kwargs={"k": 4})

    # Ensemble with equal weight; tune weights=[0.3, 0.7] to favour semantic
    ensemble = EnsembleRetriever(
        retrievers=[bm25_retriever, semantic_retriever],
        weights=[0.5, 0.5],
    )
    return ensemble
```

### 8c — Re-Ranking with Cohere Rerank

```python
"""rerank_rag.py"""
# pip install cohere langchain-cohere
from langchain.retrievers.contextual_compression import ContextualCompressionRetriever
from langchain_cohere import CohereRerank
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma

def build_reranked_retriever(top_n: int = 3):
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vs = Chroma(persist_directory="./chroma_db", embedding_function=embeddings)
    # Over-retrieve then rerank
    base_retriever = vs.as_retriever(search_kwargs={"k": 20})

    reranker = CohereRerank(
        model="rerank-english-v3.0",
        top_n=top_n,
    )
    return ContextualCompressionRetriever(
        base_compressor=reranker,
        base_retriever=base_retriever,
    )
```

### 8d — Metadata Filtering

```python
"""metadata_filter_rag.py"""
# During ingest, attach metadata
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

def ingest_with_metadata(filepath: str, source_tag: str, year: int):
    docs = PyPDFLoader(filepath).load()
    for doc in docs:
        doc.metadata["source_tag"] = source_tag
        doc.metadata["year"]       = year
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    return splitter.split_documents(docs)

# At query time, filter by metadata
from langchain_chroma import Chroma

def filtered_retriever(source_tag: str = None, year_min: int = None):
    from langchain_openai import OpenAIEmbeddings
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vs = Chroma(persist_directory="./chroma_db", embedding_function=embeddings)

    where = {}
    if source_tag:
        where["source_tag"] = {"$eq": source_tag}
    if year_min:
        where["year"] = {"$gte": year_min}

    return vs.as_retriever(
        search_kwargs={"k": 4, "filter": where if where else None}
    )
```

---

## Retrieval Evaluation with LangSmith

```python
"""eval_rag.py — measure retrieval quality with LangSmith + RAGAS-style metrics"""
# pip install langsmith ragas
import os
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_API_KEY"]    = "your-langsmith-key"
os.environ["LANGCHAIN_PROJECT"]    = "rag-evaluation"

from langsmith import Client
from langsmith.evaluation import evaluate
from langchain_core.runnables import RunnableLambda
from chain import build_chain   # your chain from chain.py

# ── dataset ────────────────────────────────────────────────────────────────────
# Create a dataset in LangSmith UI or programmatically:
client = Client()

# dataset = client.create_dataset("rag-test-set")
# client.create_examples(
#     inputs=[{"question": "What is the return policy?"}],
#     outputs=[{"answer": "30 days, no questions asked."}],
#     dataset_id=dataset.id,
# )

# ── evaluators ─────────────────────────────────────────────────────────────────
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

CORRECTNESS_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "Score whether the AI answer is correct compared to the reference answer. "
               "Return a JSON object with key 'score' (0.0–1.0) and 'reasoning'."),
    ("human", "Reference: {reference}\nAI Answer: {prediction}"),
])

def correctness_evaluator(run, example):
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    from langchain_core.output_parsers import JsonOutputParser
    chain = CORRECTNESS_PROMPT | llm | JsonOutputParser()
    result = chain.invoke({
        "reference":  example.outputs["answer"],
        "prediction": run.outputs["output"],
    })
    return {"key": "correctness", "score": result["score"], "comment": result.get("reasoning")}

# ── run evaluation ─────────────────────────────────────────────────────────────

def predict(inputs: dict) -> dict:
    chain = build_chain()
    return {"output": chain.invoke(inputs["question"])}

# results = evaluate(
#     predict,
#     data="rag-test-set",
#     evaluators=[correctness_evaluator],
#     experiment_prefix="naive-rag-v1",
# )
```

---

## Pattern Selection Reference

```
SIMPLE QUESTION + LOW STAKES  → Pattern 1 (Naive RAG)
   └─ wording variance issues     → Pattern 2 (Multi-Query)

COMPLEX QUESTION + MEDIUM STAKES
   └─ better framing needed       → Pattern 3a (Step-Back)
   └─ query too specific          → Pattern 3c (Query Rewriting)
   └─ context too noisy           → Pattern 4 (Contextual Compression)

ANY + HIGH STAKES               → Pattern 5 (Self-RAG)

CORPUS MAY BE INCOMPLETE        → Pattern 6 (CRAG)

MULTI-HOP REASONING             → Pattern 7 (Agentic RAG)

PRODUCTION TUNING
   └─ precision vs recall        → Pattern 8b (Ensemble BM25 + Semantic)
   └─ large docs                 → Pattern 8a (Multi-Vector)
   └─ ranking quality            → Pattern 8c (Cohere Rerank)
   └─ filter by date/tag         → Pattern 8d (Metadata Filter)

ADVANCED RETRIEVAL QUALITY
   └─ chunks lose surrounding context → Pattern 9  (Contextual Retrieval)
   └─ keyword + semantic hybrid       → Pattern 10 (Hybrid Search / RRF)
   └─ > 10 candidates, quality matters → Pattern 11 (Cross-Encoder Re-Ranking)
   └─ mixed query intent in one app   → Pattern 12 (Adaptive RAG / Router)
```

---

## Pattern 9 — Contextual Retrieval (Anthropic Technique)

**When to use:** Any production RAG where chunks are losing meaning when
separated from their parent document. Anthropic's research shows this technique
alone reduces retrieval failures by **49 %**. Combine with Pattern 10 (Hybrid
Search) for up to 67 % failure reduction.

**The problem:** A chunk that says "The defendant argued this point in section
IV" is meaningless without knowing which case, which document, and which point.
Standard embedding of the bare chunk produces a poor vector.

**The solution:** Before embedding, ask a cheap LLM (claude-haiku-4-5) to write
2-3 sentences situating the chunk inside the document. Prepend that context to
the chunk text. Embed the enriched text. Store the original chunk for display.

**Cost:** `claude-haiku-4-5` costs ~$0.25 / 1 M input tokens. A 1 000-chunk
corpus with 200-token average context prompt costs roughly $0.05 to process once
at ingest time.

```
pip install langchain langchain-anthropic langchain-openai langchain-chroma chromadb
```

```python
"""
contextual_retrieval.py

Ingest pipeline that enriches each chunk with LLM-generated document context
before embedding. Uses claude-haiku-4-5 for cheap parallel contextualization.

Run once: python contextual_retrieval.py ingest
Query:    python contextual_retrieval.py "Your question"
"""
from __future__ import annotations
import asyncio
import sys
from typing import Optional

from langchain_anthropic import ChatAnthropic
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader, DirectoryLoader

PERSIST_DIR = "./chroma_contextual"
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
CONTEXT_MODEL = "claude-haiku-4-5"   # cheap; upgrade to claude-sonnet-4-5 for higher fidelity
MAX_CONCURRENCY = 20                  # parallel LLM calls during ingest

# ── context-generation prompt ──────────────────────────────────────────────────

CONTEXT_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        "You are a helpful assistant that situates document excerpts in their broader context.",
    ),
    (
        "human",
        """Here is the full document:
<document>
{full_document}
</document>

Here is the specific chunk we need to situate:
<chunk>
{chunk_text}
</chunk>

Write 2-3 sentences that briefly explain:
1. What document this chunk comes from (title, type, topic).
2. Where this chunk fits in the document's flow (e.g., "This chunk is from the
   methodology section of …").
3. Any key surrounding context a reader would need to interpret this chunk.

Output ONLY those 2-3 sentences. No preamble, no headings.
""",
    ),
])


# ── async contextualization ────────────────────────────────────────────────────

async def _contextualize_chunk(
    chunk: Document,
    full_text: str,
    chain,
) -> Document:
    """Return a NEW Document whose page_content is context + original chunk text."""
    context = await chain.ainvoke({
        "full_document": full_text[:12_000],   # stay within context window safely
        "chunk_text": chunk.page_content,
    })
    enriched_content = f"{context}\n\n{chunk.page_content}"
    # Preserve original text in metadata for display / citation
    return Document(
        page_content=enriched_content,
        metadata={**chunk.metadata, "original_chunk": chunk.page_content},
    )


async def contextualize_chunks(
    chunks: list[Document],
    full_texts: dict[str, str],   # source path → full document text
) -> list[Document]:
    """
    Enrich all chunks in parallel, respecting MAX_CONCURRENCY.
    full_texts maps each chunk's metadata['source'] to its full document text.
    """
    llm = ChatAnthropic(model=CONTEXT_MODEL, temperature=0, max_tokens=256)
    chain = CONTEXT_PROMPT | llm | StrOutputParser()

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def guarded(chunk: Document) -> Document:
        source = chunk.metadata.get("source", "")
        full_text = full_texts.get(source, chunk.page_content)
        async with semaphore:
            return await _contextualize_chunk(chunk, full_text, chain)

    tasks = [guarded(c) for c in chunks]
    return await asyncio.gather(*tasks)


# ── ingest pipeline ────────────────────────────────────────────────────────────

def load_and_split(path: str) -> tuple[list[Document], dict[str, str]]:
    """Load documents, return (chunks, full_texts_by_source)."""
    import os
    if os.path.isdir(path):
        loader = DirectoryLoader(path, glob="**/*.pdf", loader_cls=PyPDFLoader)
    else:
        loader = PyPDFLoader(path)

    raw_docs = loader.load()

    # Group pages by source to reconstruct full-document text
    by_source: dict[str, list[str]] = {}
    for doc in raw_docs:
        src = doc.metadata.get("source", "unknown")
        by_source.setdefault(src, []).append(doc.page_content)
    full_texts = {src: "\n\n".join(pages) for src, pages in by_source.items()}

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        add_start_index=True,
    )
    chunks = splitter.split_documents(raw_docs)
    return chunks, full_texts


def ingest(path: str) -> None:
    print(f"Loading documents from: {path}")
    chunks, full_texts = load_and_split(path)
    print(f"Split into {len(chunks)} chunks. Contextualizing with {CONTEXT_MODEL}...")

    enriched = asyncio.run(contextualize_chunks(chunks, full_texts))
    print(f"Contextualized {len(enriched)} chunks. Embedding...")

    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    Chroma.from_documents(
        documents=enriched,
        embedding=embeddings,
        persist_directory=PERSIST_DIR,
    )
    print(f"Stored in {PERSIST_DIR}")


# ── retrieval chain ────────────────────────────────────────────────────────────

def build_chain():
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vs = Chroma(persist_directory=PERSIST_DIR, embedding_function=embeddings)
    retriever = vs.as_retriever(search_type="similarity", search_kwargs={"k": 6})

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = ChatPromptTemplate.from_template("""
Answer the question based on the context below.
The context passages include situating information; use it to disambiguate.
If the context does not contain the answer, say "I don't know."

Context:
{context}

Question: {question}
Answer:
""")

    from langchain_core.runnables import RunnablePassthrough

    def format_docs(docs):
        # Show enriched content (context + chunk); hide raw original_chunk metadata
        return "\n\n---\n\n".join(d.page_content for d in docs)

    return (
        {"context": retriever | format_docs, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:\n  python contextual_retrieval.py ingest [path]\n  python contextual_retrieval.py \"Your question\"")
        sys.exit(1)

    if sys.argv[1] == "ingest":
        path = sys.argv[2] if len(sys.argv) > 2 else "./docs"
        ingest(path)
    else:
        question = " ".join(sys.argv[1:])
        print(build_chain().invoke(question))
```

**Why this works:**

The core insight is that embedding quality degrades when a chunk's meaning is
implicit rather than explicit. By prepending a short situated context sentence,
the embedding now captures "this is a clause from the force-majeure section of
the Acme Corp 2024 supply agreement" rather than "either party may terminate
this agreement without penalty". The latter matches many documents; the former
matches the right one.

**Combining with BM25 (maximum effect):**

After enriching chunks, pass them to Pattern 10's `BM25Retriever` as well.
Anthropic's benchmarks show contextual retrieval alone gives 49 % failure
reduction; contextual retrieval + BM25 hybrid gives 67 %.

---

## Pattern 10 — Hybrid Search with Reciprocal Rank Fusion (RRF)

**When to use:** Your corpus contains exact identifiers (product codes, legal
citations, people's names, error codes) where BM25 keyword search wins; AND
semantic paraphrase queries where dense embeddings win. Hybrid RRF gives you
both with one retriever.

**How RRF works:**

Each retriever independently ranks all documents. RRF merges the rankings using
the formula:

```
RRF_score(doc) = sum_over_retrievers( 1 / (k + rank_in_retriever_i) )
```

where `k=60` is a smoothing constant. A document that ranks #1 in BM25 and #3
in dense gets a higher combined score than one that ranks #5 in both. The final
list is sorted descending by RRF score.

**When each retriever wins:**

| Situation | BM25 wins | Dense wins |
|-----------|-----------|-----------|
| "Find clause 4.2(b)" | Yes — exact string | No |
| "What's the cancellation fee?" | Sometimes | Yes — paraphrase |
| "Error code ERR_AUTH_403" | Yes | No |
| "How do I handle expired sessions?" | No | Yes |
| Author name search | Yes | No |

```
pip install langchain langchain-community langchain-openai langchain-chroma rank-bm25
```

```python
"""
hybrid_rrf.py — Hybrid BM25 + Dense retrieval with Reciprocal Rank Fusion.

The EnsembleRetriever already implements RRF internally; weights control
how much each retriever's ranking contributes to the final merged score.
"""
from __future__ import annotations
from typing import Optional

from langchain.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough

PERSIST_DIR = "./chroma_db"


# ── retriever construction ─────────────────────────────────────────────────────

def build_hybrid_retriever(
    docs: Optional[list[Document]] = None,
    k: int = 6,
    bm25_weight: float = 0.5,
    dense_weight: float = 0.5,
) -> EnsembleRetriever:
    """
    Build an RRF-fused BM25 + dense retriever.

    Args:
        docs:         Documents to index. If None, loads from PERSIST_DIR (dense only).
        k:            Number of candidates each sub-retriever fetches before fusion.
        bm25_weight:  RRF contribution weight for BM25. Increase for keyword-heavy corpora.
        dense_weight: RRF contribution weight for dense. Increase for semantic queries.

    The weights do NOT directly cap result counts -- they scale each retriever's
    rank scores before summation. After fusion the top-k are returned overall.
    """
    assert abs(bm25_weight + dense_weight - 1.0) < 1e-6, "Weights must sum to 1.0"

    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

    # Dense retriever (loads from existing persist dir or builds fresh)
    if docs is not None:
        vectorstore = Chroma.from_documents(
            documents=docs,
            embedding=embeddings,
            persist_directory=PERSIST_DIR,
        )
    else:
        vectorstore = Chroma(persist_directory=PERSIST_DIR, embedding_function=embeddings)

    dense_retriever = vectorstore.as_retriever(search_kwargs={"k": k})

    # BM25 retriever (in-memory; rebuild on each start if docs supplied)
    if docs is None:
        # No docs provided for BM25 -- load all stored docs as a one-time pull
        all_docs = vectorstore.get()
        docs_for_bm25 = [
            Document(page_content=pc, metadata=meta)
            for pc, meta in zip(all_docs["documents"], all_docs["metadatas"])
        ]
    else:
        docs_for_bm25 = docs

    bm25_retriever = BM25Retriever.from_documents(docs_for_bm25)
    bm25_retriever.k = k

    return EnsembleRetriever(
        retrievers=[bm25_retriever, dense_retriever],
        weights=[bm25_weight, dense_weight],
    )


# ── full RAG chain using hybrid retriever ─────────────────────────────────────

def build_chain(
    docs: Optional[list[Document]] = None,
    bm25_weight: float = 0.5,
    dense_weight: float = 0.5,
):
    retriever = build_hybrid_retriever(
        docs=docs,
        bm25_weight=bm25_weight,
        dense_weight=dense_weight,
    )

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = ChatPromptTemplate.from_template("""
Answer the question based on the context below.
If the context does not contain the answer, say "I don't know."

Context:
{context}

Question: {question}
Answer:
""")

    def format_docs(docs):
        return "\n\n---\n\n".join(
            f"[Source: {d.metadata.get('source', 'unknown')}]\n{d.page_content}"
            for d in docs
        )

    return (
        {"context": retriever | format_docs, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )


# ── weight-tuning guidance (printed to stdout during dev) ─────────────────────

TUNING_GUIDE = """
Hybrid search weight tuning guide
===================================
Default: bm25=0.5, dense=0.5  (balanced; good starting point)

Increase BM25 weight when:
  - corpus has many product codes, IDs, or proper nouns
  - users copy-paste exact phrases from documents
  - dense retrieval misses exact-match queries

Increase dense weight when:
  - users ask conceptual / paraphrased questions
  - corpus is mostly prose with little unique terminology
  - BM25 over-fetches noisy keyword matches

Evaluate with a held-out set of (question, expected_chunk) pairs:
  from langchain.evaluation import load_evaluator
  evaluator = load_evaluator("context_qa")
  for q, ref in test_set:
      docs = retriever.invoke(q)
      hit = any(ref in d.page_content for d in docs)
      print(f"{'HIT' if hit else 'MISS'}: {q}")
"""


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--help" in sys.argv:
        print(TUNING_GUIDE)
        sys.exit(0)

    question = " ".join(sys.argv[1:]) or input("Question: ")
    chain = build_chain()   # loads from existing PERSIST_DIR
    print(chain.invoke(question))
```

**Stacking with Contextual Retrieval (Pattern 9):**

After running Pattern 9's `ingest()`, pass the enriched chunks to both BM25 and
the dense store. The situated context text that was prepended for embedding also
improves BM25 term matching because document titles and section names now appear
explicitly in every chunk.

---

## Pattern 11 — Cross-Encoder Re-Ranking

**When to use:**
- You retrieve 10-20 candidates but only want the top 3-5 for the LLM context.
- Answer quality is critical (legal, medical, finance, code search).
- Your bi-encoder (embedding model) ranks well on average but occasionally
  promotes wrong documents into the top-4.

**Bi-encoder vs cross-encoder:**

| | Bi-encoder (embeddings) | Cross-encoder (re-ranker) |
|---|---|---|
| Speed | Fast -- vectors pre-computed | Slow -- runs full model on each query+doc pair |
| Quality | Good recall | Better precision |
| Usage | Initial retrieval (k=20) | Re-rank top-k, keep top-3 |
| Cost | Paid API or local model | Cohere API or free local model |

The standard pipeline is: over-retrieve with bi-encoder (k=20) then re-rank with
cross-encoder then pass top-3 to LLM. This separates the recall problem (get it
in the set) from the precision problem (put the best docs first).

```
pip install langchain langchain-cohere cohere
# OR for free local re-ranking:
pip install sentence-transformers
```

```python
"""
rerank_rag.py -- Cross-encoder re-ranking via Cohere API or local HuggingFace model.

Choose RERANKER_BACKEND = "cohere" or "local" below.
"""
from __future__ import annotations
import os
from typing import Literal

from langchain.retrievers.contextual_compression import ContextualCompressionRetriever
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough

PERSIST_DIR = "./chroma_db"
RERANKER_BACKEND: Literal["cohere", "local"] = "cohere"   # change to "local" for free


# ── Cohere re-ranker (API-based, highest quality) ──────────────────────────────

def _cohere_reranker(top_n: int = 4):
    """
    Uses Cohere's rerank-english-v3.0 model.
    Requires COHERE_API_KEY environment variable.
    Best quality; ~$0.002 per 1 000 document-query pairs reranked.
    """
    from langchain_cohere import CohereRerank
    return CohereRerank(
        model="rerank-english-v3.0",
        top_n=top_n,
        # cohere_api_key=os.environ["COHERE_API_KEY"],  # or set via env var
    )


# ── Local cross-encoder (HuggingFace, free) ────────────────────────────────────

def _local_reranker(top_n: int = 4):
    """
    Uses a local cross-encoder model via sentence-transformers.
    No API key needed. Runs on CPU (slower) or GPU (fast).

    Good free models:
      cross-encoder/ms-marco-MiniLM-L-6-v2   -- fast, English, 6-layer
      cross-encoder/ms-marco-MiniLM-L-12-v2  -- better quality, 12-layer
      BAAI/bge-reranker-large                 -- multilingual, high quality
    """
    from langchain.retrievers.document_compressors import CrossEncoderReranker
    from langchain_community.cross_encoders import HuggingFaceCrossEncoder

    model = HuggingFaceCrossEncoder(
        model_name="cross-encoder/ms-marco-MiniLM-L-6-v2"
        # model_kwargs={"device": "cuda"}  # uncomment for GPU
    )
    return CrossEncoderReranker(model=model, top_n=top_n)


# ── build retriever ────────────────────────────────────────────────────────────

def build_reranked_retriever(
    initial_k: int = 20,
    final_top_n: int = 4,
    backend: Literal["cohere", "local"] = RERANKER_BACKEND,
):
    """
    Over-retrieves with bi-encoder (initial_k docs), then re-ranks to final_top_n.

    initial_k should be generous (15-25). The re-ranker is accurate enough that
    the true answer is almost always in the re-ranked top-4, even if it was #14
    in the initial bi-encoder ranking.
    """
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vs = Chroma(persist_directory=PERSIST_DIR, embedding_function=embeddings)
    base_retriever = vs.as_retriever(search_kwargs={"k": initial_k})

    reranker = _cohere_reranker(top_n=final_top_n) if backend == "cohere" \
               else _local_reranker(top_n=final_top_n)

    # ContextualCompressionRetriever is a thin wrapper that runs a compressor
    # (here: a re-ranker) on top of any base retriever.
    return ContextualCompressionRetriever(
        base_compressor=reranker,
        base_retriever=base_retriever,
    )


# ── full chain ─────────────────────────────────────────────────────────────────

def build_chain(
    initial_k: int = 20,
    final_top_n: int = 4,
    backend: Literal["cohere", "local"] = RERANKER_BACKEND,
):
    retriever = build_reranked_retriever(
        initial_k=initial_k,
        final_top_n=final_top_n,
        backend=backend,
    )

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = ChatPromptTemplate.from_template("""
Answer the question based on the context below.
If the context does not contain the answer, say "I don't know."

Context:
{context}

Question: {question}
Answer:
""")

    def format_docs(docs):
        return "\n\n---\n\n".join(
            f"[Rank {i+1} | Source: {d.metadata.get('source', 'unknown')}]\n{d.page_content}"
            for i, d in enumerate(docs)
        )

    return (
        {"context": retriever | format_docs, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )


# ── stacking with Pattern 10 (hybrid) ─────────────────────────────────────────

def build_hybrid_reranked_chain(
    docs=None,
    initial_k: int = 20,
    final_top_n: int = 4,
    backend: Literal["cohere", "local"] = RERANKER_BACKEND,
):
    """
    Maximum quality pipeline:
      Hybrid BM25+Dense (Pattern 10) -> Cross-encoder re-rank (Pattern 11)

    Use when you need the absolute best precision and can afford the latency
    of two retrieval passes + a re-ranking pass.
    """
    from hybrid_rrf import build_hybrid_retriever  # from Pattern 10

    hybrid_retriever = build_hybrid_retriever(docs=docs, k=initial_k)

    reranker = _cohere_reranker(top_n=final_top_n) if backend == "cohere" \
               else _local_reranker(top_n=final_top_n)

    reranked_retriever = ContextualCompressionRetriever(
        base_compressor=reranker,
        base_retriever=hybrid_retriever,
    )

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = ChatPromptTemplate.from_template("""
Answer the question based on the context. Say "I don't know" if unsure.

Context:
{context}

Question: {question}
Answer:
""")

    def format_docs(docs):
        return "\n\n---\n\n".join(d.page_content for d in docs)

    return (
        {"context": reranked_retriever | format_docs, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    question = " ".join(sys.argv[1:]) or input("Question: ")
    chain = build_chain()
    print(chain.invoke(question))
```

**Choosing `initial_k`:**

Bigger `initial_k` gives the re-ranker more material to find the true best
document but costs more in re-ranking latency. Recommended values:

| Corpus size | initial_k | final_top_n |
|-------------|-----------|-------------|
| < 1 000 chunks | 10 | 3 |
| 1 000 - 50 000 | 20 | 4 |
| > 50 000 | 30 | 5 |

---

## Pattern 12 — Adaptive RAG (Query Router)

**When to use:** A single application receives multiple query intents -- simple
factual look-ups, multi-step reasoning, calculation requests, and current-events
questions -- and no single retrieval strategy handles all of them equally well.
A router classifies each incoming query and directs it to the cheapest strategy
that can answer it correctly.

**Routing targets:**

| Query type | Example | Best strategy |
|------------|---------|---------------|
| Factual lookup | "What does clause 7.3 say?" | Vector store (Pattern 1) |
| Multi-step reasoning | "Why did costs rise given the context?" | Agentic RAG (Pattern 7) |
| Calculation | "What is the 12-month average of column X?" | Python REPL / code tool |
| Current events | "What happened to the stock today?" | Web search (Tavily) |

**Architecture:** A LangGraph graph with a classification node at the entry
point. Each route is its own node. A fallback route handles anything the
classifier is unsure about.

```
pip install langgraph langchain langchain-openai langchain-community tavily-python
```

```python
"""
adaptive_rag.py -- LangGraph query router that dispatches to the right RAG strategy.
"""
from __future__ import annotations
from typing import TypedDict, Literal, Annotated
import operator

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode, tools_condition

PERSIST_DIR = "./chroma_db"

# ── state ──────────────────────────────────────────────────────────────────────

QueryType = Literal["factual", "reasoning", "calculation", "current_events", "unknown"]

class RouterState(TypedDict):
    question:    str
    query_type:  QueryType
    answer:      str
    messages:    Annotated[list[BaseMessage], operator.add]


# ── query classifier ───────────────────────────────────────────────────────────

class QueryClassification(BaseModel):
    query_type: QueryType = Field(
        description=(
            "factual         -- specific fact retrievable from a static document corpus\n"
            "reasoning       -- requires multi-step analysis or synthesis across documents\n"
            "calculation     -- requires arithmetic, statistics, or code execution\n"
            "current_events  -- requires up-to-date information not in the corpus\n"
            "unknown         -- cannot be reliably classified"
        )
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Confidence score for the classification."
    )
    reasoning: str = Field(description="One sentence explaining the classification.")


CLASSIFIER_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        "You are a query intent classifier for a RAG system. "
        "Classify the user's question into one of the query types described below. "
        "Be conservative -- if in doubt, choose 'unknown' so the fallback chain runs.",
    ),
    ("human", "Question: {question}"),
])


def classify_query(state: RouterState) -> RouterState:
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    classifier = CLASSIFIER_PROMPT | llm.with_structured_output(QueryClassification)
    result: QueryClassification = classifier.invoke({"question": state["question"]})

    # Treat low-confidence classifications as unknown
    query_type = result.query_type if result.confidence >= 0.7 else "unknown"
    return {"query_type": query_type}


# ── route 1: factual lookup (standard vector store RAG) ───────────────────────

def route_factual(state: RouterState) -> RouterState:
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vs = Chroma(persist_directory=PERSIST_DIR, embedding_function=embeddings)
    retriever = vs.as_retriever(search_kwargs={"k": 5})

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = ChatPromptTemplate.from_template("""
Answer the factual question from the context. Say "I don't know" if the context
does not contain the answer.

Context:
{context}

Question: {question}
Answer:
""")

    docs = retriever.invoke(state["question"])
    context = "\n\n".join(d.page_content for d in docs)
    answer = (prompt | llm | StrOutputParser()).invoke({
        "context": context,
        "question": state["question"],
    })
    return {"answer": answer}


# ── route 2: reasoning (agentic RAG with document tool) ───────────────────────

@tool
def search_documents(query: str) -> str:
    """Search the document store. Use multiple targeted queries for multi-hop questions."""
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vs = Chroma(persist_directory=PERSIST_DIR, embedding_function=embeddings)
    docs = vs.similarity_search(query, k=5)
    return "\n\n---\n\n".join(d.page_content for d in docs) or "No relevant documents found."


REASONING_TOOLS = [search_documents]


def route_reasoning(state: RouterState) -> RouterState:
    """Run a ReAct agent that can call search_documents multiple times."""
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0).bind_tools(REASONING_TOOLS)

    system = (
        "You are a research assistant. Use the search_documents tool to gather "
        "information, potentially with multiple queries, before synthesizing a final answer. "
        "Think step by step."
    )
    messages = [
        {"role": "system", "content": system},
        HumanMessage(content=state["question"]),
    ]

    # Simple single-turn agentic loop (up to 5 tool calls)
    tool_node = ToolNode(REASONING_TOOLS)
    for _ in range(5):
        response = llm.invoke(messages)
        messages.append(response)
        if not response.tool_calls:
            break
        tool_results = tool_node.invoke({"messages": messages})
        messages.extend(tool_results["messages"])

    answer = messages[-1].content if hasattr(messages[-1], "content") else str(messages[-1])
    return {"answer": answer, "messages": messages}


# ── route 3: calculation (Python REPL tool) ───────────────────────────────────

@tool
def python_repl(code: str) -> str:
    """
    Execute Python code and return the output.
    Use for arithmetic, statistics, data manipulation.
    Always print the final result.
    """
    import io
    import contextlib

    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            exec(code, {"__builtins__": __builtins__})  # noqa: S102
        return output.getvalue() or "Code executed with no output."
    except Exception as exc:
        return f"Error: {exc}"


CALCULATION_TOOLS = [python_repl, search_documents]


def route_calculation(state: RouterState) -> RouterState:
    """Agent with python_repl + search_documents for calculation queries."""
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0).bind_tools(CALCULATION_TOOLS)

    system = (
        "You are a data analyst. Use search_documents to retrieve relevant data and "
        "python_repl to perform calculations. Show your reasoning."
    )
    messages = [
        {"role": "system", "content": system},
        HumanMessage(content=state["question"]),
    ]

    tool_node = ToolNode(CALCULATION_TOOLS)
    for _ in range(6):
        response = llm.invoke(messages)
        messages.append(response)
        if not response.tool_calls:
            break
        tool_results = tool_node.invoke({"messages": messages})
        messages.extend(tool_results["messages"])

    answer = messages[-1].content if hasattr(messages[-1], "content") else str(messages[-1])
    return {"answer": answer, "messages": messages}


# ── route 4: current events (web search) ──────────────────────────────────────

def route_current_events(state: RouterState) -> RouterState:
    search = TavilySearchResults(max_results=5)
    results = search.invoke(state["question"])

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = ChatPromptTemplate.from_template("""
Answer the question using the web search results below.
Cite the source URLs where relevant.

Search results:
{results}

Question: {question}
Answer:
""")

    results_text = "\n\n".join(
        f"[{r['url']}]\n{r['content']}" for r in results
    )
    answer = (prompt | llm | StrOutputParser()).invoke({
        "results": results_text,
        "question": state["question"],
    })
    return {"answer": answer}


# ── route 5: fallback (try vector store, warn if thin) ────────────────────────

def route_fallback(state: RouterState) -> RouterState:
    """
    Fallback: attempt vector store retrieval, then web search if retrieval is thin.
    Adds a disclaimer to the answer.
    """
    # Try local docs first
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vs = Chroma(persist_directory=PERSIST_DIR, embedding_function=embeddings)
    docs = vs.similarity_search(state["question"], k=4)

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    if docs:
        context = "\n\n".join(d.page_content for d in docs)
        prompt = ChatPromptTemplate.from_template(
            "Answer from context if possible, otherwise say what you know.\n\n"
            "Context:\n{context}\n\nQuestion: {question}\nAnswer:"
        )
        answer = (prompt | llm | StrOutputParser()).invoke({
            "context": context,
            "question": state["question"],
        })
        return {"answer": f"{answer}\n\n[Note: query type was unclear; answer may be incomplete]"}

    # No docs -- fall back to web search
    web_result = route_current_events(state)
    return {"answer": web_result["answer"] + "\n\n[Note: answered via web search fallback]"}


# ── routing function ───────────────────────────────────────────────────────────

def router(state: RouterState) -> str:
    """Map query_type to the next node name."""
    return {
        "factual":        "factual",
        "reasoning":      "reasoning",
        "calculation":    "calculation",
        "current_events": "current_events",
        "unknown":        "fallback",
    }.get(state["query_type"], "fallback")


# ── graph assembly ─────────────────────────────────────────────────────────────

def build_graph():
    graph = StateGraph(RouterState)

    # Nodes
    graph.add_node("classify",       classify_query)
    graph.add_node("factual",        route_factual)
    graph.add_node("reasoning",      route_reasoning)
    graph.add_node("calculation",    route_calculation)
    graph.add_node("current_events", route_current_events)
    graph.add_node("fallback",       route_fallback)

    # Edges
    graph.set_entry_point("classify")
    graph.add_conditional_edges(
        "classify",
        router,
        {
            "factual":        "factual",
            "reasoning":      "reasoning",
            "calculation":    "calculation",
            "current_events": "current_events",
            "fallback":       "fallback",
        },
    )

    # All routes terminate
    for node in ("factual", "reasoning", "calculation", "current_events", "fallback"):
        graph.add_edge(node, END)

    return graph.compile()


# ── public API ─────────────────────────────────────────────────────────────────

def ask(question: str, verbose: bool = False) -> str:
    app = build_graph()
    result = app.invoke({"question": question, "messages": []})
    if verbose:
        print(f"[Router] classified as: {result.get('query_type', 'unknown')}")
    return result["answer"]


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    question = " ".join(sys.argv[1:]) or input("Question: ")
    print(ask(question, verbose=True))
```

**Extending the router:**

Add new routes by:
1. Adding a new `Literal` value to `QueryType`.
2. Updating `CLASSIFIER_PROMPT` system message to describe the new type.
3. Writing a new node function.
4. Adding it to `graph.add_node`, `graph.add_conditional_edges`, and
   `graph.add_edge(..., END)`.

**Routing accuracy tip:** Test the classifier in isolation before wiring the full
graph:

```python
from adaptive_rag import classify_query

tests = [
    ("What does section 4 say about liability?", "factual"),
    ("Why did costs increase in Q3 given supply chain issues?", "reasoning"),
    ("What is the average of these five numbers: 12, 14, 11, 18, 15?", "calculation"),
    ("What did the Fed announce today?", "current_events"),
]

for q, expected in tests:
    result = classify_query({"question": q, "query_type": "unknown", "answer": "", "messages": []})
    status = "OK" if result["query_type"] == expected else f"WRONG (expected {expected})"
    print(f"[{status}] {q[:50]}... -> {result['query_type']}")
```

---

## Environment Variables Required

```bash
# Always required
OPENAI_API_KEY=sk-...

# For CRAG web search fallback (Pattern 6) and Adaptive RAG current-events route (Pattern 12)
TAVILY_API_KEY=tvly-...

# For Cohere reranking (Pattern 8c, Pattern 11)
COHERE_API_KEY=...

# For Contextual Retrieval context generation (Pattern 9)
ANTHROPIC_API_KEY=sk-ant-...

# For evaluation / tracing
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=my-rag-project

# For production vector stores
PINECONE_API_KEY=...
QDRANT_URL=http://...
QDRANT_API_KEY=...
```

---

## Quick-Start Commands

```bash
# 1. Install base deps
pip install langchain langchain-openai langchain-community langchain-chroma chromadb pypdf

# 2. Ingest documents
python ingest.py

# 3. Ask a question
python ask.py "What are the main findings?"

# 4. Switch to a more powerful pattern
# Copy the pattern's file into your project and replace chain.py imports

# ── Pattern 9: Contextual Retrieval ───────────────────────────────────────────
pip install langchain-anthropic
python contextual_retrieval.py ingest ./docs
python contextual_retrieval.py "What does section 4 say about liability?"

# ── Pattern 10: Hybrid BM25 + Dense ──────────────────────────────────────────
pip install rank-bm25
# Use build_chain() from hybrid_rrf.py; pass your loaded docs on first run

# ── Pattern 11: Cross-encoder re-ranking ─────────────────────────────────────
pip install langchain-cohere cohere          # API (best quality)
# OR
pip install sentence-transformers            # local (free)
python rerank_rag.py "Your question"

# ── Pattern 12: Adaptive RAG router ──────────────────────────────────────────
pip install langgraph tavily-python
python adaptive_rag.py "Your question"
```
