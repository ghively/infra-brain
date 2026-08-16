---
name: lc-ui
description: Chat UI scaffolding for LangChain/LangGraph applications. Triggered by /lc-ui or requests to "add a UI", "build a chat interface", "connect to Chainlit/Gradio/Streamlit", "make a frontend for my agent", or "stream tokens to the browser". Scaffolds complete working UI code for Chainlit (production), Gradio (demos), Streamlit (data apps), or FastAPI+HTMX (lightweight web). Always ask 3 discovery questions before generating any code.
argument-hint: "[chainlit|gradio|streamlit|fastapi]"
---

# lc:ui — Chat UI Scaffolding for LangChain/LangGraph

## Purpose

Scaffold a complete, working chat UI wired to your LangGraph agent or LCEL chain.
Every template includes:
- Real-time token streaming (users see words appear as the model generates them)
- Session/thread management (each user gets their own conversation thread)
- LangSmith tracing (automatic — just set `LANGSMITH_TRACING=true`)
- Full inline comments explaining every non-obvious concept

---

## Trigger Phrases

- "add a UI to my agent"
- "build a chat interface"
- "Chainlit", "Gradio", "Streamlit"
- "stream tokens to the browser"
- "make a frontend for my LangGraph app"
- `/lc-ui`

---

## Discovery Flow

Ask ALL THREE questions in a single message before writing any code.
Do not scaffold until you have the answers.

```
Before I scaffold the UI, three quick questions:

1. Which UI framework?
   a) Chainlit  — production chat (streaming first-class, auth built-in, file upload)
   b) Gradio    — demos and prototypes (shareable link, Hugging Face Spaces)
   c) Streamlit — data apps (plots, dataframes, rich layout)
   d) FastAPI + HTMX — lightweight web UI (no JS framework, SSE streaming)

2. Does the UI need authentication?
   (yes / no — if yes: which method? OAuth, username/password, API key gate)

3. Does it need file upload?
   (yes / no — if yes: what file types? PDFs for RAG, images, CSVs, …)
```

Use the answers to select a framework section below and fill in the auth/upload
variants accordingly.

---

## Framework Recommendation Guide

| Situation | Recommend |
|---|---|
| Production chatbot for real users | Chainlit |
| Quick demo to share with stakeholders | Gradio |
| Dashboard with charts + chat | Streamlit |
| Minimal footprint, existing FastAPI backend | FastAPI + HTMX |

---

## Concept: Why Streaming Matters

Without streaming: user stares at a blank screen for 5–15 seconds, then sees the
full response appear all at once. Feels broken.

With streaming: words appear as they are generated. Response feels immediate.
The LLM generates tokens sequentially — streaming just sends each token to the
browser as soon as it exists rather than buffering the whole response.

Every framework below uses a different mechanism to achieve the same result:
- Chainlit: `cl.Message.stream_token()`
- Gradio: Python generator (`yield` keyword)
- Streamlit: `st.write_stream()`
- FastAPI: Server-Sent Events (SSE) — a standard browser API

---

## 1. CHAINLIT (PRIMARY RECOMMENDATION)

**Why Chainlit for production:**
- Built specifically for LangChain/LangGraph — first-class streaming support
- `AsyncLangchainCallbackHandler` auto-displays tool calls and intermediate steps
- Authentication built in (OAuth, custom callback)
- File upload, message feedback, conversation history — all included
- Hot reload during development

### Install

```bash
pip install chainlit langchain-anthropic langgraph
chainlit hello    # smoke test — opens browser with example app
```

### Minimal Working App — `app.py`

```python
"""
Minimal Chainlit app wired to a LangGraph agent.

Chainlit works through decorators on async functions:
  @cl.on_chat_start   — called once when a user opens the chat
  @cl.on_message      — called every time the user sends a message

Run with:
    chainlit run app.py --watch    # --watch enables hot reload
"""
import os

import chainlit as cl
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langchain_core.tools import tool, ToolException

load_dotenv()


# ─── Build the agent graph ────────────────────────────────────────────────────
# This runs once at module import, not per user session.
# Heavy initialisation (model loading, DB connections) belongs here.

@tool
def get_time() -> str:
    """Return the current UTC time. Use when the user asks what time it is."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


TOOLS = [get_time]

_llm = ChatAnthropic(
    model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    temperature=0,
    streaming=True,   # IMPORTANT: must be True for Chainlit streaming to work
).bind_tools(TOOLS)

def _build_graph():
    g = StateGraph(MessagesState)
    g.add_node("agent", lambda s: {"messages": [_llm.invoke(s["messages"])]})
    g.add_node("tools", ToolNode(TOOLS, handle_tool_errors=True))
    g.add_edge(START, "agent")
    g.add_conditional_edges(
        "agent",
        lambda s: "tools" if s["messages"][-1].tool_calls else END,
    )
    g.add_edge("tools", "agent")
    # recursion_limit: max number of node visits before the graph raises.
    # Prevents infinite loops. Adjust based on your expected tool-call depth.
    return g.compile(
        checkpointer=MemorySaver(),
        # recursion_limit is set at invoke time, not compile time:
        # config={"recursion_limit": 25}
    )

_graph = _build_graph()


# ─── Per-session setup ────────────────────────────────────────────────────────
# cl.user_session is a per-user dict that persists across messages in a session.
# Use it to store the thread_id, user preferences, or any session state.

@cl.on_chat_start
async def on_chat_start():
    """
    Called once when a user opens the chat tab.

    This is where you initialise per-user state. Never store user state in
    module-level globals — that would mix data across concurrent users.
    """
    import uuid
    # Each user gets a unique thread_id so LangGraph checkpointer keeps
    # their conversation history separate from all other users.
    thread_id = str(uuid.uuid4())
    cl.user_session.set("thread_id", thread_id)

    await cl.Message(
        content="Hello! I'm your AI assistant. How can I help you today?",
        author="Assistant",
    ).send()


# ─── Message handler ──────────────────────────────────────────────────────────

@cl.on_message
async def on_message(message: cl.Message):
    """
    Called every time the user sends a message.

    Pattern: create an empty Chainlit message, then stream tokens into it.
    The empty message shows a loading indicator until the first token arrives.
    """
    thread_id = cl.user_session.get("thread_id")
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 25,
    }

    # Create an empty message — the loading spinner shows until we stream tokens
    response_msg = cl.Message(content="", author="Assistant")
    await response_msg.send()

    # astream_events yields fine-grained events (token, tool_start, tool_end, etc.)
    # stream_mode="values" yields the full state after each node.
    # Use "messages" for token-level streaming:
    async for event in _graph.astream_events(
        {"messages": [HumanMessage(content=message.content)]},
        config=config,
        version="v2",   # v2 is the current stable event schema
    ):
        kind = event["event"]

        if kind == "on_chat_model_stream":
            # Each on_chat_model_stream event contains one token chunk.
            # chunk.content is the token text (may be empty for tool-call chunks).
            chunk = event["data"]["chunk"]
            if chunk.content:
                await response_msg.stream_token(chunk.content)

        elif kind == "on_tool_start":
            # Show the user which tool is being called — builds trust.
            tool_name = event["name"]
            await cl.Message(
                content=f"Using tool: **{tool_name}**",
                author="System",
                indent=1,   # visually indent tool messages under the assistant
            ).send()

    # Finalise the message — required to mark streaming as complete
    await response_msg.update()
```

### Message History Across Sessions

```python
"""
Persist conversation history across browser refreshes using PostgreSQL.

By default, MemorySaver loses history when the process restarts.
Replace it with AsyncPostgresSaver for production.
"""
import os
from contextlib import asynccontextmanager

import chainlit as cl
from dotenv import load_dotenv
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

load_dotenv()

_checkpointer = None


@asynccontextmanager
async def lifespan():
    """
    Set up the PostgreSQL checkpointer once at startup.

    Chainlit calls this context manager when the server starts.
    Declare it in chainlit.md or pass via config — see Chainlit docs.
    """
    global _checkpointer
    async with AsyncPostgresSaver.from_conn_string(
        os.environ["DATABASE_URL"]
    ) as saver:
        await saver.setup()   # creates LangGraph tables if they don't exist
        _checkpointer = saver
        yield


@cl.on_chat_resume
async def on_chat_resume(thread):
    """
    Called when a user resumes an existing conversation.

    thread.id is the LangGraph thread_id — store it so on_message can use it.
    cl.on_chat_resume only fires when Chainlit data persistence is configured.
    """
    cl.user_session.set("thread_id", thread.id)
```

### File Upload → Document Ingestion

```python
"""
Accept uploaded PDFs or text files and ingest them into a RAG pipeline.

Chainlit handles file upload UI automatically — just declare the accept list.
"""
import chainlit as cl
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings


@cl.on_chat_start
async def on_chat_start():
    # Ask for file upload before the conversation begins
    files = await cl.AskFileMessage(
        content="Please upload a PDF or text file to chat with.",
        accept=["application/pdf", "text/plain"],
        max_size_mb=20,
        timeout=120,   # seconds to wait for user to upload
    ).send()

    if not files:
        await cl.Message(content="No file uploaded. Starting without context.").send()
        return

    uploaded = files[0]   # cl.File — has .path, .name, .type attributes

    # Process and ingest
    msg = cl.Message(content=f"Processing **{uploaded.name}**...")
    await msg.send()

    # Load the file
    if uploaded.type == "application/pdf":
        loader = PyPDFLoader(uploaded.path)
    else:
        loader = TextLoader(uploaded.path, encoding="utf-8")

    docs = loader.load()

    # Split into chunks
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
    chunks = splitter.split_documents(docs)

    # Embed and store — per-session vectorstore so users don't see each other's docs
    vectorstore = Chroma.from_documents(
        documents=chunks,
        embedding=OpenAIEmbeddings(model="text-embedding-3-small"),
        # No persist_directory = in-memory, auto-cleaned when session ends
    )
    retriever = vectorstore.as_retriever(search_kwargs={"k": 4})

    # Store retriever in user session for the message handler to use
    cl.user_session.set("retriever", retriever)

    msg.content = f"Ready! Loaded **{len(chunks)} chunks** from {uploaded.name}."
    await msg.update()
```

### Authentication — OAuth

```python
"""
Chainlit OAuth authentication.

Configure in .env:
    OAUTH_GOOGLE_CLIENT_ID=...
    OAUTH_GOOGLE_CLIENT_SECRET=...
    CHAINLIT_AUTH_SECRET=<random 32-char string>   # for JWT signing

Supported providers: google, github, azure-ad, okta, auth0, cognito.
"""
import chainlit as cl


@cl.oauth_callback
def oauth_callback(
    provider_id: str,
    token: str,
    raw_user_data: dict,
    default_user: cl.User,
) -> cl.User | None:
    """
    Called after the OAuth provider redirects back to Chainlit.

    Return a cl.User to allow access, or None to deny.

    raw_user_data contains provider-specific profile fields (email, name, etc.).
    default_user.identifier is the email or username from the provider.
    """
    # Example: only allow users from your company domain
    email = raw_user_data.get("email", "")
    if not email.endswith("@yourcompany.com"):
        return None   # deny access

    # Return a user with metadata — accessible via cl.user_session later
    return cl.User(
        identifier=email,
        metadata={
            "name": raw_user_data.get("name", ""),
            "provider": provider_id,
            "role": "admin" if email in ADMIN_EMAILS else "user",
        },
    )

ADMIN_EMAILS = {"alice@yourcompany.com", "bob@yourcompany.com"}


# ─── Custom password auth (simpler than OAuth) ────────────────────────────────

@cl.password_auth_callback
def password_auth_callback(username: str, password: str) -> cl.User | None:
    """
    Simple username/password auth. Store hashed passwords in a DB — never plaintext.

    Return cl.User to allow, None to deny.
    """
    import hashlib
    # TODO: replace with real DB lookup
    USERS = {
        "alice": hashlib.sha256(b"secret123").hexdigest(),
    }
    expected_hash = USERS.get(username)
    if not expected_hash:
        return None
    actual_hash = hashlib.sha256(password.encode()).hexdigest()
    if actual_hash != expected_hash:
        return None
    return cl.User(identifier=username, metadata={"role": "user"})
```

### Complete Production App — `chainlit_app.py`

```python
"""
Production Chainlit + LangGraph chat app.

Features:
- Streaming token display
- Tool call visualization
- File upload + RAG ingestion
- Message history (MemorySaver, swap for PostgresSaver in prod)
- Error handling with user-friendly messages
- Loading states

Run:
    chainlit run chainlit_app.py --watch
"""
import os
import uuid

import chainlit as cl
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool, ToolException
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

load_dotenv()


# ─── Tools ────────────────────────────────────────────────────────────────────

@tool
def search(query: str) -> str:
    """Search the web for current information about a topic."""
    # TODO: wire Tavily or SerpAPI
    return f"[Placeholder search results for: {query}]"


@tool
def calculate(expression: str) -> str:
    """Evaluate a mathematical expression. Example: '2 + 2 * 3'."""
    try:
        allowed = {"__builtins__": {}}
        result = eval(expression, allowed)  # noqa: S307
        return str(result)
    except Exception as e:
        raise ToolException(f"Cannot evaluate '{expression}': {e}") from e


TOOLS = [search, calculate]


# ─── Graph (module-level, shared across all users) ────────────────────────────

_llm = ChatAnthropic(
    model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    temperature=0,
    streaming=True,
).bind_tools(TOOLS)


def _agent_node(state: MessagesState) -> dict:
    return {"messages": [_llm.invoke(state["messages"])]}


_graph = (
    StateGraph(MessagesState)
    .add_node("agent", _agent_node)
    .add_node("tools", ToolNode(TOOLS, handle_tool_errors=True))
    .add_edge(START, "agent")
    .add_conditional_edges(
        "agent",
        lambda s: "tools" if s["messages"][-1].tool_calls else END,
    )
    .add_edge("tools", "agent")
    .compile(checkpointer=MemorySaver())
)


# ─── Chainlit lifecycle ───────────────────────────────────────────────────────

@cl.on_chat_start
async def start():
    cl.user_session.set("thread_id", str(uuid.uuid4()))
    await cl.Message(
        content="Hello! I can search the web and do maths. What would you like to know?"
    ).send()


@cl.on_message
async def handle_message(message: cl.Message):
    thread_id = cl.user_session.get("thread_id")
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 25}

    # Empty message with loading indicator
    response_msg = cl.Message(content="")
    await response_msg.send()

    try:
        async for event in _graph.astream_events(
            {"messages": [HumanMessage(content=message.content)]},
            config=config,
            version="v2",
        ):
            kind = event["event"]

            if kind == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                if chunk.content:
                    await response_msg.stream_token(chunk.content)

            elif kind == "on_tool_start":
                # Show tool name + args as a collapsible step
                tool_name = event["name"]
                tool_input = event["data"].get("input", {})
                async with cl.Step(name=tool_name, type="tool") as step:
                    step.input = str(tool_input)

            elif kind == "on_tool_end":
                pass  # cl.Step auto-closes

    except Exception as e:
        # User-friendly error — never expose stack traces in production
        await response_msg.update()
        await cl.Message(
            content=f"Sorry, something went wrong: {type(e).__name__}. Please try again.",
            author="System",
        ).send()
        return

    await response_msg.update()


# ─── Chainlit config (create .chainlit/config.toml alongside this file) ───────
# [project]
# name = "My Assistant"
# [UI]
# name = "My Assistant"
# default_collapse_content = true
# [features]
# multi_modal = true    # enable file uploads
```

### Deployment

```dockerfile
# Dockerfile for Chainlit app
FROM python:3.11-slim

RUN pip install chainlit langchain-anthropic langgraph python-dotenv

WORKDIR /app
COPY . .

# Chainlit default port is 8000
EXPOSE 8000

CMD ["chainlit", "run", "chainlit_app.py", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
# Local run
chainlit run chainlit_app.py --watch

# Docker
docker build -t my-chainlit-app .
docker run -p 8000:8000 --env-file .env my-chainlit-app
```

---

## 2. GRADIO (DEMOS AND PROTOTYPES)

**Why Gradio for demos:**
- One-line shareable link via `share=True` — stakeholders click, no install needed
- Deploy instantly to Hugging Face Spaces (free tier available)
- `gr.ChatInterface` provides a complete chat UI with zero config
- Good for prototyping — iterate fast before committing to production UI

### Install

```bash
pip install gradio langchain-anthropic langgraph
```

### Minimal Gradio App — `gradio_app.py`

```python
"""
Gradio chat UI wired to a LangGraph agent.

gr.ChatInterface handles all the UI — history display, input box, submit button.
Your job is to write the response function that takes a message and returns a reply.

For streaming: return a generator (use `yield` instead of `return`).
Gradio detects the generator and updates the UI token by token.

Run:
    python gradio_app.py
    # Opens at http://localhost:7860
    # Add share=True to get a public URL
"""
import asyncio
import os
from typing import Generator

import gradio as gr
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool, ToolException
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

load_dotenv()


# ─── Build graph (shared, module-level) ──────────────────────────────────────

@tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return f"Sunny, 22°C in {city}"  # TODO: real API


TOOLS = [get_weather]

_llm = ChatAnthropic(
    model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    temperature=0,
    streaming=True,
).bind_tools(TOOLS)

_graph = (
    StateGraph(MessagesState)
    .add_node("agent", lambda s: {"messages": [_llm.invoke(s["messages"])]})
    .add_node("tools", ToolNode(TOOLS, handle_tool_errors=True))
    .add_edge(START, "agent")
    .add_conditional_edges(
        "agent",
        lambda s: "tools" if s["messages"][-1].tool_calls else END,
    )
    .add_edge("tools", "agent")
    .compile(checkpointer=MemorySaver())
)


# ─── Gradio response function ─────────────────────────────────────────────────
# history: list of {"role": "user"|"assistant", "content": str}
# thread_id: stored in gr.State — unique per browser session

def respond(
    message: str,
    history: list[dict],
    thread_id: str,
) -> Generator[str, None, None]:
    """
    Generator function for streaming responses.

    Gradio calls this function when the user submits a message.
    Yielding partial strings causes Gradio to update the assistant message
    incrementally — this IS the streaming mechanism for Gradio.

    history is provided by gr.ChatInterface automatically.
    thread_id comes from gr.State — persists across messages.
    """
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 25}
    full_response = ""

    # asyncio.run() bridges sync Gradio with async LangGraph
    # In production, consider using gr.Blocks with an async function instead
    async def _stream():
        nonlocal full_response
        async for event in _graph.astream_events(
            {"messages": [HumanMessage(content=message)]},
            config=config,
            version="v2",
        ):
            if event["event"] == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                if chunk.content:
                    full_response += chunk.content
                    yield full_response   # yield accumulated text, not just the chunk

    # Run the async generator synchronously
    loop = asyncio.new_event_loop()
    try:
        gen = _stream()
        while True:
            try:
                partial = loop.run_until_complete(gen.__anext__())
                yield partial
            except StopAsyncIteration:
                break
    finally:
        loop.close()


# ─── Gradio UI ────────────────────────────────────────────────────────────────

import uuid

with gr.Blocks(title="LangGraph Chat Demo") as demo:
    gr.Markdown("# LangGraph Chat Demo\nPowered by Claude + LangGraph")

    # gr.State stores per-session data on the client (sent with each request)
    # This is how Gradio handles per-user state without a server-side session store
    thread_state = gr.State(value=lambda: str(uuid.uuid4()))

    chatbot = gr.ChatInterface(
        fn=respond,
        additional_inputs=[thread_state],
        type="messages",          # use OpenAI-style message dicts
        title="",
        description="Ask me anything!",
        examples=["What's the weather in Paris?", "Explain quantum entanglement"],
        cache_examples=False,     # don't cache — responses depend on thread state
    )


if __name__ == "__main__":
    demo.launch(
        server_port=7860,
        share=False,    # set True to get a public URL (useful for demos)
        # auth=("username", "password"),   # simple password protection
    )
```

### File Upload + RAG in Gradio

```python
"""
Gradio app with file upload → RAG pipeline.
"""
import gradio as gr
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

_vectorstores: dict[str, Chroma] = {}   # session_id → vectorstore


def ingest_file(file_path: str, session_id: str) -> str:
    """Called when user uploads a file. Returns status message."""
    if file_path.endswith(".pdf"):
        loader = PyPDFLoader(file_path)
    else:
        loader = TextLoader(file_path, encoding="utf-8")

    docs = loader.load()
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
    chunks = splitter.split_documents(docs)

    _vectorstores[session_id] = Chroma.from_documents(
        documents=chunks,
        embedding=OpenAIEmbeddings(model="text-embedding-3-small"),
    )
    return f"Ingested {len(chunks)} chunks. You can now ask questions about the document."


with gr.Blocks() as demo:
    session_id = gr.State(value=lambda: str(__import__("uuid").uuid4()))

    with gr.Row():
        with gr.Column(scale=1):
            file_upload = gr.File(label="Upload PDF or TXT", file_types=[".pdf", ".txt"])
            upload_status = gr.Textbox(label="Status", interactive=False)
            file_upload.upload(
                fn=ingest_file,
                inputs=[file_upload, session_id],
                outputs=upload_status,
            )
        with gr.Column(scale=3):
            chatbot = gr.Chatbot(type="messages")
            msg = gr.Textbox(placeholder="Ask about your document...")
            msg.submit(
                fn=lambda m, h, sid: ("", h),   # TODO: wire RAG respond()
                inputs=[msg, chatbot, session_id],
                outputs=[msg, chatbot],
            )

demo.launch()
```

### Gradio Spaces Deployment

```bash
# 1. Create requirements.txt
echo "gradio langchain-anthropic langgraph langchain-community python-dotenv" > requirements.txt

# 2. Create app.py (Spaces looks for this name specifically)
cp gradio_app.py app.py

# 3. Push to Hugging Face Hub
pip install huggingface_hub
huggingface-cli login
huggingface-cli repo create my-langchain-demo --type space --space_sdk gradio
git init && git add . && git commit -m "Initial commit"
git remote add origin https://huggingface.co/spaces/YOUR_USERNAME/my-langchain-demo
git push

# Secrets: set in Space Settings → Repository secrets
# ANTHROPIC_API_KEY, LANGSMITH_API_KEY, etc.
```

---

## 3. STREAMLIT (DATA APPS)

**Why Streamlit for data apps:**
- Rich layout: charts, dataframes, maps alongside chat
- `@st.cache_resource` prevents expensive objects (models, DBs) from reinitialising on each message
- `st.session_state` is the Streamlit equivalent of per-user storage
- Excellent for internal tools where layout control matters more than chat aesthetics

### CRITICAL: `@st.cache_resource`

Streamlit reruns your entire script on every user interaction (including every
chat message). Without `@st.cache_resource`, you would rebuild the LangGraph
graph and LLM client on every message — 2-3 seconds of wasted overhead.

`@st.cache_resource` caches the return value across reruns and users. Use it
for anything expensive to initialise: models, database connections, vector stores.

### Install

```bash
pip install streamlit langchain-anthropic langgraph
streamlit run streamlit_app.py
```

### Complete Streamlit App — `streamlit_app.py`

```python
"""
Streamlit chat app wired to a LangGraph agent.

Key Streamlit concepts used here:
  st.session_state   — persists data across script reruns for the current user
  @st.cache_resource — initialises expensive objects once, shared across users
  st.chat_message    — renders a chat bubble (role: "user" or "assistant")
  st.chat_input      — the text input box at the bottom of the page
  st.write_stream    — streams a generator into a chat bubble token by token

Run:
    streamlit run streamlit_app.py
"""
import asyncio
import os
import uuid
from typing import Generator

import streamlit as st
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool, ToolException
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

load_dotenv()

st.set_page_config(
    page_title="LangGraph Chat",
    page_icon="🤖",
    layout="centered",
)


# ─── @st.cache_resource — build once, reuse on every rerun ───────────────────

@st.cache_resource
def get_graph():
    """
    Build and cache the LangGraph agent.

    @st.cache_resource caches the return value permanently (until the server
    restarts or the cache is cleared manually). This function runs exactly
    once per server process, no matter how many users are active.

    IMPORTANT: MemorySaver is shared here — for production, use a DB-backed
    checkpointer so each user's thread is isolated. With MemorySaver, all
    users share the same in-process memory store, which is fine for demos.
    """
    @tool
    def search(query: str) -> str:
        """Search the web for information about a topic."""
        return f"[Placeholder search results for: {query}]"

    @tool
    def calculate(expression: str) -> str:
        """Evaluate a mathematical expression."""
        try:
            return str(eval(expression, {"__builtins__": {}}))  # noqa: S307
        except Exception as e:
            raise ToolException(f"Cannot evaluate: {e}") from e

    tools = [search, calculate]
    llm = ChatAnthropic(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        temperature=0,
        streaming=True,
    ).bind_tools(tools)

    graph = StateGraph(MessagesState)
    graph.add_node("agent", lambda s: {"messages": [llm.invoke(s["messages"])]})
    graph.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        lambda s: "tools" if s["messages"][-1].tool_calls else END,
    )
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=MemorySaver())


# ─── Per-user session state ───────────────────────────────────────────────────
# st.session_state is a dict that persists across reruns for ONE user.
# Each browser tab gets its own session_state.

if "thread_id" not in st.session_state:
    # Assign a unique LangGraph thread_id on first load
    st.session_state.thread_id = str(uuid.uuid4())

if "messages" not in st.session_state:
    # Display history — list of {"role": str, "content": str}
    # This is separate from LangGraph's internal history (managed by checkpointer)
    st.session_state.messages = []


# ─── Page layout ──────────────────────────────────────────────────────────────

st.title("LangGraph Chat")
st.caption("Powered by Claude + LangGraph")

# Render existing message history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])


# ─── Handle new input ─────────────────────────────────────────────────────────

if prompt := st.chat_input("Ask me anything..."):
    # Display user message immediately
    with st.chat_message("user"):
        st.markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    # Stream assistant response
    with st.chat_message("assistant"):

        def token_generator() -> Generator[str, None, None]:
            """
            Generator that yields tokens from the LangGraph stream.

            st.write_stream() accepts any generator of strings and renders
            each yielded string as it arrives, creating the streaming effect.

            We use asyncio.run() to bridge Streamlit's sync execution model
            with LangGraph's async API. This blocks the Streamlit thread but
            that is acceptable — Streamlit runs each session in its own thread.
            """
            graph = get_graph()
            config = {
                "configurable": {"thread_id": st.session_state.thread_id},
                "recursion_limit": 25,
            }

            async def _collect():
                tokens = []
                async for event in graph.astream_events(
                    {"messages": [HumanMessage(content=prompt)]},
                    config=config,
                    version="v2",
                ):
                    if event["event"] == "on_chat_model_stream":
                        chunk = event["data"]["chunk"]
                        if chunk.content:
                            tokens.append(chunk.content)
                return tokens

            # asyncio.run() creates a new event loop, runs the coroutine, returns
            all_tokens = asyncio.run(_collect())
            yield from all_tokens

        # st.write_stream renders each yielded token and returns the full text
        full_response = st.write_stream(token_generator())

    # Save to display history
    st.session_state.messages.append(
        {"role": "assistant", "content": full_response}
    )
```

### File Upload in Streamlit

```python
"""
Streamlit file upload — add this block above the chat input.
"""
import streamlit as st
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings


@st.cache_resource
def get_embeddings():
    return OpenAIEmbeddings(model="text-embedding-3-small")


# File uploader in the sidebar
with st.sidebar:
    uploaded_file = st.file_uploader(
        "Upload a document to chat with",
        type=["pdf", "txt"],
        help="Upload a PDF or text file. The assistant will answer questions about it.",
    )

    if uploaded_file is not None:
        # Only process when a new file is uploaded
        file_key = f"vectorstore_{uploaded_file.name}_{uploaded_file.size}"
        if file_key not in st.session_state:
            with st.spinner(f"Processing {uploaded_file.name}..."):
                # Write to temp file (Streamlit gives us bytes, loaders need a path)
                import tempfile, os
                suffix = ".pdf" if uploaded_file.type == "application/pdf" else ".txt"
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(uploaded_file.read())
                    tmp_path = tmp.name

                # Load and split
                loader = PyPDFLoader(tmp_path) if suffix == ".pdf" else TextLoader(tmp_path)
                docs = loader.load()
                chunks = RecursiveCharacterTextSplitter(
                    chunk_size=1000, chunk_overlap=150
                ).split_documents(docs)

                # Embed
                vs = Chroma.from_documents(chunks, embedding=get_embeddings())
                st.session_state[file_key] = vs
                os.unlink(tmp_path)   # clean up temp file

            st.success(f"Loaded {len(chunks)} chunks from {uploaded_file.name}")

        st.session_state.retriever = st.session_state[file_key].as_retriever(
            search_kwargs={"k": 4}
        )
```

### Streamlit Authentication (streamlit-authenticator)

```bash
pip install streamlit-authenticator
```

```python
"""
Password-based authentication for Streamlit apps using streamlit-authenticator.

Create a credentials config file (do NOT commit to git):
    credentials.yaml:
        credentials:
          usernames:
            alice:
              email: alice@example.com
              name: Alice
              password: $2b$12$...   # bcrypt hash — generate with: bcrypt.hashpw(b"password", bcrypt.gensalt())
        cookie:
          name: streamlit_auth
          key: some_random_secret_key
          expiry_days: 30
"""
import streamlit as st
import streamlit_authenticator as stauth
import yaml
from yaml.loader import SafeLoader

with open("credentials.yaml") as f:
    config = yaml.load(f, Loader=SafeLoader)

authenticator = stauth.Authenticate(
    config["credentials"],
    config["cookie"]["name"],
    config["cookie"]["key"],
    config["cookie"]["expiry_days"],
)

name, authentication_status, username = authenticator.login("Login", "main")

if authentication_status is False:
    st.error("Username or password is incorrect")
    st.stop()
elif authentication_status is None:
    st.warning("Please enter your username and password")
    st.stop()

# If we get here, user is authenticated
authenticator.logout("Logout", "sidebar")
st.sidebar.write(f"Welcome, **{name}**!")

# Rest of your app follows here...
```

---

## 4. FASTAPI + HTMX (LIGHTWEIGHT WEB UI)

**Why FastAPI + HTMX:**
- No separate frontend framework (React/Vue/etc.) needed
- SSE (Server-Sent Events) is a native browser API — works everywhere
- HTMX handles DOM updates with minimal JavaScript
- Ideal when you already have a FastAPI backend and want to add a chat page
- Full control over HTML/CSS

**Concept: Server-Sent Events (SSE)**
SSE is a one-way channel: server pushes events to the browser over a long-lived
HTTP connection. The browser's `EventSource` API handles reconnection automatically.
Each event is a line starting with `data:` followed by the payload.

LangGraph sends tokens → FastAPI formats as SSE events → browser appends to chat.

### Install

```bash
pip install fastapi uvicorn[standard] sse-starlette langchain-anthropic langgraph
```

### Complete FastAPI + HTMX App — `server.py` + `templates/index.html`

```python
"""
FastAPI server with SSE streaming endpoint for LangGraph.

Run:
    uvicorn server:app --reload --port 8000
    # Open http://localhost:8000
"""
import os
import uuid
from typing import AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool, ToolException
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from sse_starlette.sse import EventSourceResponse

load_dotenv()

app = FastAPI(title="LangGraph Chat")


# ─── Build graph ──────────────────────────────────────────────────────────────

@tool
def search(query: str) -> str:
    """Search for information about a topic."""
    return f"[Search results for: {query}]"


TOOLS = [search]

_llm = ChatAnthropic(
    model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    temperature=0,
    streaming=True,
).bind_tools(TOOLS)

_graph = (
    StateGraph(MessagesState)
    .add_node("agent", lambda s: {"messages": [_llm.invoke(s["messages"])]})
    .add_node("tools", ToolNode(TOOLS, handle_tool_errors=True))
    .add_edge(START, "agent")
    .add_conditional_edges(
        "agent",
        lambda s: "tools" if s["messages"][-1].tool_calls else END,
    )
    .add_edge("tools", "agent")
    .compile(checkpointer=MemorySaver())
)

# Simple in-memory session store — swap for Redis in production
_sessions: dict[str, str] = {}   # session_cookie → thread_id


# ─── SSE streaming endpoint ───────────────────────────────────────────────────

@app.post("/chat/stream")
async def chat_stream(request: Request):
    """
    SSE endpoint — streams LangGraph tokens to the browser.

    Request body: {"message": str, "session_id": str}

    Each SSE event format:
        data: <token text>\n\n
        data: [DONE]\n\n   (signals end of stream to client)

    The browser's EventSource API will call the onmessage handler for each event.
    """
    body = await request.json()
    message = body.get("message", "").strip()
    session_id = body.get("session_id", str(uuid.uuid4()))

    if not message:
        return {"error": "Message is required"}

    # Get or create a LangGraph thread for this session
    if session_id not in _sessions:
        _sessions[session_id] = str(uuid.uuid4())
    thread_id = _sessions[session_id]

    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 25}

    async def generate() -> AsyncGenerator[dict, None]:
        """
        Async generator that yields SSE events.

        EventSourceResponse (from sse-starlette) consumes this generator and
        formats each yielded dict as an SSE event:
            {"data": "token text"} → "data: token text\n\n"
        """
        try:
            async for event in _graph.astream_events(
                {"messages": [HumanMessage(content=message)]},
                config=config,
                version="v2",
            ):
                if event["event"] == "on_chat_model_stream":
                    chunk = event["data"]["chunk"]
                    if chunk.content:
                        # Yield as SSE event — sse-starlette handles formatting
                        yield {"data": chunk.content}

                elif event["event"] == "on_tool_start":
                    tool_name = event["name"]
                    # Use a custom event type so client can handle it separately
                    yield {"event": "tool_start", "data": tool_name}

        except Exception as e:
            yield {"event": "error", "data": str(e)}

        # Signal stream end — client checks for this sentinel
        yield {"event": "done", "data": "[DONE]"}

    return EventSourceResponse(generate())


# ─── Serve the HTML page ──────────────────────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LangGraph Chat</title>
    <!-- HTMX: handles AJAX requests and DOM swaps declaratively via HTML attributes -->
    <script src="https://unpkg.com/htmx.org@2.0.0"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: system-ui, sans-serif; background: #f5f5f5; height: 100vh; display: flex; flex-direction: column; }
        #chat-container { flex: 1; overflow-y: auto; padding: 20px; max-width: 800px; margin: 0 auto; width: 100%; }
        .message { margin-bottom: 16px; padding: 12px 16px; border-radius: 12px; max-width: 80%; line-height: 1.5; }
        .user-message { background: #0084ff; color: white; margin-left: auto; border-radius: 12px 12px 4px 12px; }
        .assistant-message { background: white; border: 1px solid #e0e0e0; border-radius: 12px 12px 12px 4px; }
        .tool-message { background: #f0f0f0; color: #666; font-size: 0.85em; font-style: italic; border-radius: 8px; }
        #input-area { background: white; border-top: 1px solid #e0e0e0; padding: 16px; max-width: 800px; margin: 0 auto; width: 100%; }
        #message-form { display: flex; gap: 8px; }
        #message-input { flex: 1; padding: 10px 14px; border: 1px solid #ddd; border-radius: 24px; font-size: 16px; outline: none; }
        #message-input:focus { border-color: #0084ff; }
        #send-button { padding: 10px 20px; background: #0084ff; color: white; border: none; border-radius: 24px; cursor: pointer; font-size: 16px; }
        #send-button:disabled { background: #ccc; cursor: not-allowed; }
        #send-button:hover:not(:disabled) { background: #0066cc; }
    </style>
</head>
<body>
    <div id="chat-container">
        <div class="message assistant-message">Hello! How can I help you today?</div>
    </div>

    <div id="input-area">
        <form id="message-form">
            <input
                id="message-input"
                type="text"
                placeholder="Type a message..."
                autocomplete="off"
                autofocus
            />
            <button id="send-button" type="submit">Send</button>
        </form>
    </div>

    <script>
        // Generate a session ID for this browser tab
        const sessionId = crypto.randomUUID();
        const chatContainer = document.getElementById('chat-container');
        const messageInput = document.getElementById('message-input');
        const sendButton = document.getElementById('send-button');
        const messageForm = document.getElementById('message-form');

        function scrollToBottom() {
            chatContainer.scrollTop = chatContainer.scrollHeight;
        }

        function addMessage(content, cssClass) {
            const div = document.createElement('div');
            div.className = `message ${cssClass}`;
            div.textContent = content;
            chatContainer.appendChild(div);
            scrollToBottom();
            return div;
        }

        messageForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const message = messageInput.value.trim();
            if (!message) return;

            // Show user message
            addMessage(message, 'user-message');
            messageInput.value = '';
            sendButton.disabled = true;

            // Create empty assistant message div — we'll stream tokens into it
            const assistantDiv = addMessage('', 'assistant-message');
            // CSS cursor blink animation while streaming
            assistantDiv.style.minHeight = '1.5em';

            // POST to SSE endpoint — the response is a stream of events
            const response = await fetch('/chat/stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message, session_id: sessionId }),
            });

            // Read the SSE stream manually
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\\n');
                buffer = lines.pop();  // keep incomplete line in buffer

                for (const line of lines) {
                    if (line.startsWith('data:')) {
                        const data = line.slice(5).trim();
                        if (data === '[DONE]') {
                            sendButton.disabled = false;
                            messageInput.focus();
                            break;
                        }
                        // Append token to assistant message
                        assistantDiv.textContent += data;
                        scrollToBottom();
                    } else if (line.startsWith('event: tool_start')) {
                        // Next data line will be the tool name
                    } else if (line.startsWith('event: error')) {
                        assistantDiv.textContent = 'Sorry, an error occurred. Please try again.';
                        assistantDiv.style.color = '#cc0000';
                        sendButton.disabled = false;
                    }
                }
            }
        });
    </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the chat page."""
    return HTMLResponse(content=HTML_PAGE)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
```

---

## 5. COMMON UI PATTERNS

### Pattern: Source Citation Display (RAG)

Show users which documents the answer came from — builds trust and enables verification.

```python
"""
Chainlit version — display sources as clickable elements below the answer.
"""
import chainlit as cl

async def send_answer_with_sources(answer: str, source_docs: list) -> None:
    """Send answer text followed by collapsible source citations."""
    await cl.Message(content=answer).send()

    if source_docs:
        # cl.Text creates a collapsible text element
        elements = [
            cl.Text(
                name=f"Source {i+1}: {doc.metadata.get('source', 'Unknown')}",
                content=doc.page_content,
                display="side",   # "inline" | "side" | "page"
            )
            for i, doc in enumerate(source_docs[:3])   # cap at 3 sources
        ]
        await cl.Message(
            content=f"**Sources** ({len(source_docs)} found):",
            elements=elements,
        ).send()
```

```python
"""
Streamlit version — expander shows source text inline.
"""
import streamlit as st

def display_sources(source_docs: list) -> None:
    if not source_docs:
        return
    with st.expander(f"Sources ({len(source_docs)})"):
        for i, doc in enumerate(source_docs):
            source = doc.metadata.get("source", "Unknown")
            page = doc.metadata.get("page", "")
            label = f"Source {i+1}: {source}" + (f" (p.{page})" if page else "")
            st.markdown(f"**{label}**")
            st.text(doc.page_content[:500] + ("..." if len(doc.page_content) > 500 else ""))
            if i < len(source_docs) - 1:
                st.divider()
```

### Pattern: Tool Call Visualization

Show users what tools the agent is using — demystifies the process.

```python
"""
Chainlit: cl.Step renders tool calls as expandable steps in the chat.
"""
import chainlit as cl

async def stream_with_tool_visualization(graph, messages, config):
    """Stream graph with tool call steps shown inline."""
    response_msg = cl.Message(content="")
    await response_msg.send()

    active_steps: dict[str, cl.Step] = {}

    async for event in graph.astream_events(messages, config=config, version="v2"):
        kind = event["event"]

        if kind == "on_chat_model_stream":
            chunk = event["data"]["chunk"]
            if chunk.content:
                await response_msg.stream_token(chunk.content)

        elif kind == "on_tool_start":
            run_id = event["run_id"]
            tool_name = event["name"]
            tool_input = event["data"].get("input", {})
            # cl.Step creates a collapsible section in the Chainlit UI
            step = cl.Step(name=tool_name, type="tool")
            step.input = str(tool_input)
            await step.__aenter__()
            active_steps[run_id] = step

        elif kind == "on_tool_end":
            run_id = event["run_id"]
            if run_id in active_steps:
                step = active_steps.pop(run_id)
                step.output = str(event["data"].get("output", ""))
                await step.__aexit__(None, None, None)

    await response_msg.update()
```

### Pattern: Human-in-the-Loop UI

Show interrupt → user approves or rejects → resume graph.

```python
"""
Chainlit: interrupt the graph before tool execution, ask the user to approve.

Requires the graph to be compiled with interrupt_before=["tools"].
"""
import chainlit as cl
from langgraph.types import Command

# Build graph with interrupt
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

def build_hitl_graph(tools):
    llm = ...  # your LLM with tools bound
    g = StateGraph(MessagesState)
    g.add_node("agent", lambda s: {"messages": [llm.invoke(s["messages"])]})
    g.add_node("tools", ToolNode(tools))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", lambda s: "tools" if s["messages"][-1].tool_calls else END)
    g.add_edge("tools", "agent")
    return g.compile(
        checkpointer=MemorySaver(),
        interrupt_before=["tools"],   # pause BEFORE executing any tool
    )


@cl.on_message
async def handle_with_approval(message: cl.Message):
    graph = cl.user_session.get("graph")
    thread_id = cl.user_session.get("thread_id")
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 25}

    # Run until interrupt
    state = await graph.ainvoke(
        {"messages": [{"role": "user", "content": message.content}]},
        config=config,
    )

    last_msg = state["messages"][-1]
    if not (hasattr(last_msg, "tool_calls") and last_msg.tool_calls):
        # No tool call — just display the response
        await cl.Message(content=last_msg.content).send()
        return

    # There is a pending tool call — ask for approval
    tc = last_msg.tool_calls[0]
    tool_name = tc["name"]
    tool_args = tc["args"]

    # cl.AskActionMessage shows approval buttons in the chat
    action = await cl.AskActionMessage(
        content=f"The assistant wants to call **{tool_name}** with:\n```json\n{tool_args}\n```\n\nApprove?",
        actions=[
            cl.Action(name="approve", label="Approve", payload={"approved": True}),
            cl.Action(name="reject", label="Reject", payload={"approved": False}),
        ],
        timeout=60,
    ).send()

    if action and action.get("payload", {}).get("approved"):
        # Resume the graph — it will execute the tool and continue
        result = await graph.ainvoke(Command(resume=None), config=config)
        await cl.Message(content=result["messages"][-1].content).send()
    else:
        await cl.Message(content="Tool call rejected. I won't take that action.").send()
```

### Pattern: Error Display

Never show raw stack traces in production — display friendly messages and log details.

```python
"""
User-friendly error handling pattern for any framework.
"""
import logging

logger = logging.getLogger(__name__)

# Error category mapping — classify exceptions for user-friendly messages
ERROR_MESSAGES = {
    "RateLimitError": "I'm receiving too many requests right now. Please wait a moment and try again.",
    "AuthenticationError": "There's a configuration issue. Please contact support.",
    "ContextWindowExceededError": "The conversation is too long. Please start a new chat.",
    "ToolException": "One of my tools encountered an error. Let me try a different approach.",
}

def friendly_error(exc: Exception) -> str:
    """Convert a technical exception into a user-friendly message."""
    exc_type = type(exc).__name__
    msg = ERROR_MESSAGES.get(exc_type)
    if msg:
        return msg
    # Generic fallback — never expose internal details
    logger.error("Unhandled exception in chat handler", exc_info=exc)
    return "Something went wrong on my end. Please try again."
```

---

## 6. AUTHENTICATION PATTERNS

### Chainlit Built-in OAuth (Recommended for Chainlit)

```bash
# .env — get client credentials from your OAuth provider's console
OAUTH_GOOGLE_CLIENT_ID=your_client_id
OAUTH_GOOGLE_CLIENT_SECRET=your_client_secret
CHAINLIT_AUTH_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
```

```python
# In app.py — add the oauth_callback (see Authentication section above)
# Chainlit handles the OAuth flow automatically once env vars are set.
# Supported providers: google, github, azure-ad, okta, auth0, cognito, gitlab
```

### FastAPI + JWT

For custom auth in FastAPI, see the `/lc-guardrails` skill which covers JWT
validation, API key gates, and rate limiting in detail.

Quick reference:

```python
"""
FastAPI dependency for JWT authentication.
Reference: /lc-guardrails for full implementation.
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


async def get_current_user(token: str = Depends(oauth2_scheme)):
    """FastAPI dependency — validates JWT and returns user."""
    # TODO: verify token using python-jose or PyJWT
    # See /lc-guardrails for complete implementation
    ...
```

---

## Environment Variables

Add to `.env` alongside your existing LangChain vars:

```dotenv
# Framework-specific installs
# Chainlit
CHAINLIT_AUTH_SECRET=<random-32-char-string>
OAUTH_GOOGLE_CLIENT_ID=...
OAUTH_GOOGLE_CLIENT_SECRET=...

# Gradio (none required — uses ANTHROPIC_API_KEY from LangChain)

# Streamlit
# No additional vars — uses ANTHROPIC_API_KEY

# FastAPI
# No additional vars for basic setup
# See /lc-guardrails for JWT_SECRET_KEY etc.
```

---

## Installation by Framework

```bash
# Chainlit (production chat)
pip install chainlit langchain-anthropic langgraph

# Gradio (demos)
pip install gradio langchain-anthropic langgraph

# Streamlit (data apps)
pip install streamlit langchain-anthropic langgraph

# FastAPI + HTMX (lightweight web)
pip install fastapi "uvicorn[standard]" sse-starlette langchain-anthropic langgraph

# File upload RAG (any framework)
pip install langchain-community langchain-chroma langchain-openai pypdf
```

---

## Concept Summary

| Concept | What it is | Where used |
|---|---|---|
| `streaming=True` on ChatAnthropic | Enables token-by-token generation | All frameworks |
| `astream_events(..., version="v2")` | Fine-grained event stream from LangGraph | Chainlit, FastAPI |
| `cl.user_session` | Per-user dict that persists across messages | Chainlit |
| `@st.cache_resource` | Build expensive objects once, share across reruns | Streamlit |
| `st.session_state` | Per-user dict for Streamlit | Streamlit |
| `gr.State` | Per-session value sent with every Gradio request | Gradio |
| SSE (Server-Sent Events) | Browser API for receiving a stream of events | FastAPI |
| `interrupt_before=["tools"]` | Pause graph at node for human approval | Chainlit HITL |
| `thread_id` in `configurable` | Isolates each user's conversation in LangGraph | All frameworks |
| `recursion_limit` in `config` | Prevents infinite graph loops | All frameworks |

---

## Next Steps by Framework

| Framework | Step 1 | Step 2 | Step 3 |
|---|---|---|---|
| Chainlit | `chainlit run app.py --watch` | Replace `get_time` tool with your real tools | Set `LANGSMITH_TRACING=true` to trace in LangSmith |
| Gradio | `python gradio_app.py` | Add `share=True` to get a public demo URL | Deploy to Hugging Face Spaces |
| Streamlit | `streamlit run streamlit_app.py` | Add file upload block if needed | Deploy to Streamlit Community Cloud |
| FastAPI + HTMX | `uvicorn server:app --reload` | Customise HTML/CSS in `HTML_PAGE` | Add `/lc-guardrails` JWT auth |

---

## Related Skills

- `/lc-agent` — build the LangGraph agent to wire into any of these UIs
- `/lc-tools` — add tools to your agent (search, DB queries, APIs)
- `/lc-memory` — add persistent conversation memory across sessions
- `/lc-guardrails` — add authentication, rate limiting, and input validation
- `/lc-deploy` — containerise and deploy the full stack
- `/rag` — build a RAG pipeline for document Q&A with file upload
