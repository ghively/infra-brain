# lc:start — LangChain Lab Onboarding Skill

## Purpose

This is the entry point for the langchain-lab plugin. It welcomes a complete beginner, discovers what they want to build, explains the relevant framework components in plain English, scaffolds a production-ready project structure, installs dependencies, generates a working hello_world.py, configures LangSmith tracing, verifies the entire setup, and routes the user to the correct domain skill.

Execute every step in order. Do not skip steps. Do not assume the user knows anything about LangChain, LangGraph, or LLM frameworks.

---

## STEP 1 — Welcome and Goal Discovery

Print this welcome message verbatim:

```
Welcome to LangChain Lab.

LangChain is a framework for building applications powered by large language
models (LLMs). Instead of writing raw API calls and stitching them together
yourself, LangChain gives you composable building blocks — prompts, models,
parsers, memory, tools — that snap together like LEGO.

LangGraph extends LangChain with stateful, graph-based workflows. Think of it
as a state machine where each node is an LLM call or a tool. It handles
branching, looping, and multi-agent coordination.

LangSmith is the observability layer. Every LLM call, every tool invocation,
every token gets logged so you can debug, evaluate, and improve your app.

Let's figure out what YOU want to build.
```

Then ask this exact question:

```
What do you want to build? Pick the option that sounds closest:

  1. Chatbot — A conversational assistant that remembers what you said earlier
               in the conversation. Good first project.

  2. RAG system — "Retrieval-Augmented Generation." You have documents (PDFs,
                  web pages, Notion pages) and you want the LLM to answer
                  questions using ONLY that content, not its training data.

  3. Agent — An LLM that can use tools: search the web, run Python, call APIs,
             read files. The LLM decides which tools to call and when.

  4. Pipeline — A multi-step data transformation: extract → transform → summarize
                → classify. No conversation, no tools. Pure ETL with LLMs.

  5. Multi-agent system — Multiple specialized AI agents that collaborate,
                          hand off tasks, and check each other's work.

  6. I'm not sure yet — Walk me through all of them and help me decide.

Enter a number (1-6):
```

Wait for the user's response. Store it as GOAL. If they enter 6, run through a brief description of each use case and ask follow-up questions to determine which is the best fit, then set GOAL to the closest match.

---

## STEP 2 — Explain the Relevant Stack

Based on GOAL, print the corresponding explanation below. Use plain English. Do not use jargon without immediately defining it.

### If GOAL = 1 (Chatbot)

```
Here is what you will use:

  LangChain Core — The foundation. Defines the Runnable interface (explained
  below) and the pipe | operator that chains steps together.

  ChatAnthropic — The connector to Claude. Sends your messages, gets back
  responses. Swap this for ChatOpenAI if you ever switch providers — the
  rest of your code stays identical.

  ChatPromptTemplate — A reusable message template. You define the system
  prompt once and inject variables per request.

  LangGraph (for memory) — Chatbots need memory. LangGraph manages a
  conversation state object that persists between turns. You will use the
  built-in MessageGraph for this.

  LangSmith — Logs every call so you can see exactly what was sent,
  received, and how many tokens were used.

What is a Runnable?
  Everything in LangChain implements the Runnable interface. That means every
  component has .invoke(), .stream(), and .batch() methods. The pipe operator
  | connects Runnables into chains: prompt | model | parser. Each component
  receives output from the one before it. This is called LCEL (LangChain
  Expression Language). It replaced the older LLMChain pattern in 2023 and
  is now the canonical way to compose steps.
```

### If GOAL = 2 (RAG)

```
Here is what you will use:

  LangChain Core + LCEL — Chains your retrieval and generation steps together
  with the pipe | operator.

  ChatAnthropic — The generation model. Answers questions given retrieved
  context.

  A document loader — Reads your source files (PyPDFLoader, WebBaseLoader,
  NotionDBLoader, etc.) and converts them into Document objects.

  A text splitter — Cuts long documents into overlapping chunks so the
  embedding model can handle them.

  An embedding model — Converts text chunks into vectors (lists of numbers
  that capture semantic meaning). Similar chunks get similar vectors.

  A vector store — Stores those vectors and lets you search by semantic
  similarity. Chroma is the easiest local option. Pinecone/Weaviate for
  production.

  A retriever — Wraps the vector store and returns the top-k relevant chunks
  for a given query.

  LangSmith — Critical for RAG. Shows you exactly which chunks were retrieved
  and why the LLM answered the way it did.

What is a Runnable?
  Everything in LangChain implements the Runnable interface. That means every
  component has .invoke(), .stream(), and .batch() methods. The pipe operator
  | connects Runnables into chains: retriever | prompt | model | parser. Each
  component receives output from the one before it. This is LCEL — the
  canonical 2026 way to compose LangChain steps.
```

### If GOAL = 3 (Agent)

```
Here is what you will use:

  LangGraph — Agents are control flow problems. The LLM decides what to do
  next. LangGraph models this as a graph where nodes are steps (call the LLM,
  call a tool) and edges are decisions (did the LLM ask for a tool? yes →
  run the tool; no → return to user). LangGraph gives you checkpointing,
  interrupts, and human-in-the-loop by default.

  ChatAnthropic with tool_calling — Claude can output structured JSON
  requesting a tool call. LangGraph intercepts this, runs the tool, and feeds
  the result back to Claude.

  Tools — Functions decorated with @tool. Any Python function becomes a tool
  the LLM can invoke: web search, database query, calculator, API call.

  LangSmith — Essential for agents. Shows the full reasoning trace: which tools
  were called, in what order, with what inputs and outputs.

What is LCEL vs LangGraph?
  LCEL (the pipe | syntax) is for linear, deterministic pipelines: A → B → C.
  LangGraph is for non-linear, stateful workflows where the path through the
  steps depends on runtime decisions. Agents need LangGraph. Pipelines use
  LCEL. Many real apps use both.
```

### If GOAL = 4 (Pipeline)

```
Here is what you will use:

  LangChain Core + LCEL — The pipe | operator is your entire framework. Chain
  a loader → splitter → extractor → classifier → formatter. Pure functional
  data transformation.

  ChatAnthropic — The transformation engine at each step that requires
  language understanding.

  Output parsers — Convert the model's text output into structured Python
  objects (dicts, Pydantic models, lists). JsonOutputParser,
  PydanticOutputParser, CommaSeparatedListOutputParser.

  LangSmith — Shows you exactly what each step received and returned. Invaluable
  for debugging when a middle step produces unexpected output.

What is LCEL?
  LCEL is LangChain Expression Language — the pipe | syntax for composing
  Runnables. prompt | model | parser is a chain. It replaced the older
  LLMChain and SequentialChain patterns. Every Runnable in LCEL gets
  automatic streaming, async support, batching, and LangSmith tracing.
```

### If GOAL = 5 (Multi-agent)

```
Here is what you will use:

  LangGraph — The entire architecture lives here. Each agent is a node in the
  graph. Edges define how agents hand off work to each other. LangGraph's
  StateGraph manages the shared state all agents read from and write to.

  A supervisor pattern or swarm pattern:
    - Supervisor: one orchestrator agent decides which specialist to call next.
    - Swarm: agents hand off directly to each other based on context.

  ChatAnthropic per agent — Each agent gets its own system prompt, its own
  tool set, its own temperature. They share the same state graph.

  LangSmith — Non-negotiable for multi-agent. Without tracing you cannot
  understand what happened across multiple agents. LangSmith shows the entire
  execution tree with per-agent sub-traces.

What makes LangGraph right for multi-agent?
  State machines (graphs) are the natural model for multi-agent coordination.
  LangGraph gives you: typed shared state, conditional edges, subgraph
  composition, streaming of intermediate results, human-in-the-loop
  interrupts, and persistent checkpointing so agents can pause and resume.
```

---

## STEP 3 — Check Python Version

Run:

```bash
python --version
```

If Python is not found or returns an error, also try:

```bash
python3 --version
```

Rules:
- If version is below 3.9: tell the user they must upgrade to Python 3.11. Link them to https://www.python.org/downloads/ and stop until they confirm upgrade.
- If version is 3.9 or 3.10: warn that 3.11 is recommended for best compatibility with LangGraph, but proceed.
- If version is 3.11 or above: confirm and continue.

Print the Python version found and the recommendation.

---

## STEP 4 — Ask for Project Name and Location

Ask:

```
What should we name your project? (lowercase, hyphens ok, e.g. my-chatbot)
Press Enter to use the default: langchain-app
```

Store the response as PROJECT_NAME. Default to `langchain-app` if empty.

Ask:

```
Where should we create it? Provide an absolute path or press Enter to use
the current directory.
```

Store as PROJECT_DIR. Default to current working directory + "/" + PROJECT_NAME.

---

## STEP 5 — Scaffold Project Structure

Create the following directory and file structure. Use the Write tool for each file. Show the user each file as you create it.

### Directory layout

```
PROJECT_DIR/
  pyproject.toml
  .env.example
  .env               (created but empty — user fills this in)
  .gitignore
  README.md
  src/
    __init__.py
    hello_world.py
  tests/
    __init__.py
```

### pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "PROJECT_NAME"
version = "0.1.0"
description = "LangChain application scaffolded by langchain-lab"
requires-python = ">=3.9"
dependencies = [
    "langchain>=0.3.0",
    "langchain-core>=0.3.0",
    "langchain-anthropic>=0.3.0",
    "langgraph>=0.2.0",
    "langsmith>=0.1.0",
    "python-dotenv>=1.0.0",
]

[project.optional-dependencies]
rag = [
    "langchain-community>=0.3.0",
    "chromadb>=0.5.0",
    "pypdf>=4.0.0",
    "langchain-chroma>=0.1.0",
]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "ruff>=0.4.0",
]

[tool.ruff]
line-length = 88
target-version = "py311"
```

Replace PROJECT_NAME with the actual project name.

For GOAL = 2 (RAG), add the rag optional dependencies to the base dependencies list as well.

### .env.example

```bash
# Anthropic API Key — get yours at https://console.anthropic.com
ANTHROPIC_API_KEY=your_anthropic_api_key_here

# LangSmith — get yours at https://smith.langchain.com
# Create a free account, then Settings → API Keys → Create API Key
LANGSMITH_API_KEY=your_langsmith_api_key_here
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=PROJECT_NAME

# Optional: OpenAI (only needed if you switch from Anthropic)
# OPENAI_API_KEY=your_openai_api_key_here
```

Replace PROJECT_NAME with the actual project name.

### .env

Create this file but leave it empty. The user will copy .env.example and fill in their keys.

### .gitignore

```gitignore
# Environment — NEVER commit API keys
.env
.env.local
.env.*.local

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
dist/
*.egg-info/
.eggs/

# Virtual environments
.venv/
venv/
env/
ENV/

# IDEs
.vscode/
.idea/
*.swp
*.swo

# LangChain / LangGraph
*.db
chroma_db/
lancedb/

# OS
.DS_Store
Thumbs.db

# Test artifacts
.pytest_cache/
.coverage
htmlcov/
```

### README.md

```markdown
# PROJECT_NAME

Built with LangChain + LangGraph + LangSmith.

## Quick Start

### 1. Set up your environment

Copy the example env file and fill in your API keys:

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY and LANGSMITH_API_KEY
```

### 2. Install dependencies

Using uv (recommended):
```bash
pip install uv
uv venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
uv pip install -e ".[dev]"
```

Using pip:
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 3. Verify your setup

```bash
python src/hello_world.py
```

You should see a response from Claude and a LangSmith trace URL.

## Project Structure

```
src/           Your application code
tests/         Tests
.env.example   Template for required environment variables
.env           Your actual secrets (never committed)
```
```

Replace PROJECT_NAME with the actual project name.

### src/__init__.py

```python
"""PROJECT_NAME — LangChain application."""
```

### tests/__init__.py

```python
```

(empty file)

---

## STEP 6 — Generate hello_world.py

Write `src/hello_world.py` with content appropriate to the GOAL.

### For GOAL = 1 (Chatbot) or GOAL = 4 (Pipeline) or GOAL = 6 (unsure):

```python
"""
hello_world.py — Verify your LangChain setup.

This script:
1. Loads environment variables from .env
2. Sends a single message to Claude using LCEL (the | pipe syntax)
3. Prints the response
4. Prints your LangSmith trace URL so you can inspect the call

Run with:
    python src/hello_world.py
"""

import os
from dotenv import load_dotenv

# Load .env before importing LangChain so LANGSMITH_TRACING is set
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser


def main() -> None:
    # Validate required environment variables
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise EnvironmentError(
            "ANTHROPIC_API_KEY is not set. "
            "Copy .env.example to .env and add your key."
        )

    # --- Build the chain using LCEL ---
    #
    # LCEL (LangChain Expression Language) uses the | operator to compose
    # Runnables. Each component receives the output of the previous one.
    #
    #   prompt         → formats your variables into a list of messages
    #   model          → sends those messages to Claude, returns an AIMessage
    #   output_parser  → extracts the text content from AIMessage
    #
    # This is the canonical 2026 pattern. It replaced LLMChain.

    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            "You are a helpful assistant. Answer clearly and concisely.",
        ),
        (
            "human",
            "{question}",
        ),
    ])

    model = ChatAnthropic(
        model="claude-sonnet-4-6",
        temperature=0,
        max_tokens=1024,
    )

    output_parser = StrOutputParser()

    # The pipe | operator connects Runnables into a chain.
    # chain.invoke() runs the full pipeline synchronously.
    chain = prompt | model | output_parser

    # --- Invoke the chain ---
    question = "What is LangChain in one sentence?"
    print(f"\nQuestion: {question}\n")

    response = chain.invoke({"question": question})
    print(f"Answer: {response}\n")

    # --- LangSmith trace ---
    if os.getenv("LANGSMITH_TRACING") == "true":
        project = os.getenv("LANGSMITH_PROJECT", "default")
        print(
            f"LangSmith trace: https://smith.langchain.com/projects/{project}\n"
            "Open that URL to inspect the full call — tokens, latency, inputs, outputs."
        )
    else:
        print(
            "Tip: Set LANGSMITH_TRACING=true and LANGSMITH_API_KEY in .env "
            "to see full traces in LangSmith."
        )


if __name__ == "__main__":
    main()
```

### For GOAL = 2 (RAG):

```python
"""
hello_world.py — Verify your LangChain RAG setup.

This script:
1. Loads environment variables from .env
2. Creates a tiny in-memory vector store from sample text
3. Builds a RAG chain: retriever | prompt | model | parser
4. Answers a question using retrieved context (not model training data)
5. Prints your LangSmith trace URL

Run with:
    python src/hello_world.py
"""

import os
from dotenv import load_dotenv

load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from langchain_core.documents import Document
from langchain_community.vectorstores import Chroma
from langchain_anthropic import AnthropicEmbeddings


def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise EnvironmentError(
            "ANTHROPIC_API_KEY is not set. "
            "Copy .env.example to .env and add your key."
        )

    # --- Sample documents (replace with your real loader later) ---
    docs = [
        Document(
            page_content=(
                "LangChain is a framework for building LLM-powered applications. "
                "It provides composable building blocks for prompts, models, "
                "retrievers, and output parsers."
            ),
            metadata={"source": "sample"},
        ),
        Document(
            page_content=(
                "LangGraph is a library for building stateful, multi-actor "
                "applications with LLMs. It models workflows as graphs where "
                "nodes are computation steps and edges define control flow."
            ),
            metadata={"source": "sample"},
        ),
        Document(
            page_content=(
                "LangSmith is an observability platform for LLM applications. "
                "It logs every LLM call, tool invocation, and retrieval step "
                "so you can debug and evaluate your application."
            ),
            metadata={"source": "sample"},
        ),
    ]

    # --- Build vector store (in-memory Chroma for local dev) ---
    # In production you would persist this: Chroma(persist_directory="./chroma_db")
    embeddings = AnthropicEmbeddings(model="voyage-3")
    vector_store = Chroma.from_documents(docs, embeddings)
    retriever = vector_store.as_retriever(search_kwargs={"k": 2})

    # --- RAG prompt ---
    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            "You are a helpful assistant. Answer the question using ONLY the "
            "provided context. If the context does not contain the answer, "
            "say 'I don't have that information in my context.'\n\n"
            "Context:\n{context}",
        ),
        ("human", "{question}"),
    ])

    model = ChatAnthropic(
        model="claude-sonnet-4-6",
        temperature=0,
        max_tokens=1024,
    )

    def format_docs(docs: list[Document]) -> str:
        return "\n\n".join(doc.page_content for doc in docs)

    # --- RAG chain using LCEL ---
    # RunnablePassthrough passes the original question through unchanged
    # while the retriever fetches relevant docs in parallel.
    chain = (
        {"context": retriever | format_docs, "question": RunnablePassthrough()}
        | prompt
        | model
        | StrOutputParser()
    )

    question = "What is LangGraph used for?"
    print(f"\nQuestion: {question}\n")

    response = chain.invoke(question)
    print(f"Answer: {response}\n")

    if os.getenv("LANGSMITH_TRACING") == "true":
        project = os.getenv("LANGSMITH_PROJECT", "default")
        print(
            f"LangSmith trace: https://smith.langchain.com/projects/{project}\n"
            "Open that URL to see which documents were retrieved and why."
        )


if __name__ == "__main__":
    main()
```

### For GOAL = 3 (Agent):

```python
"""
hello_world.py — Verify your LangGraph agent setup.

This script:
1. Loads environment variables from .env
2. Defines two simple tools (get_weather, calculate)
3. Builds a ReAct agent using LangGraph's prebuilt pattern
4. Runs a query that requires tool use
5. Prints your LangSmith trace URL

Run with:
    python src/hello_world.py
"""

import os
from dotenv import load_dotenv

load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent


@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city. Returns a weather description."""
    # Stub — replace with a real weather API call
    weather_data = {
        "london": "Overcast, 12°C, light rain",
        "new york": "Sunny, 22°C, clear skies",
        "tokyo": "Partly cloudy, 18°C, humid",
    }
    return weather_data.get(city.lower(), f"Weather data not available for {city}")


@tool
def calculate(expression: str) -> str:
    """Evaluate a mathematical expression. Example: '2 + 2' returns '4'."""
    try:
        # Safe eval for simple arithmetic only
        allowed = set("0123456789+-*/(). ")
        if not all(c in allowed for c in expression):
            return "Error: only basic arithmetic is allowed"
        result = eval(expression)  # noqa: S307
        return str(result)
    except Exception as e:
        return f"Error: {e}"


def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise EnvironmentError(
            "ANTHROPIC_API_KEY is not set. "
            "Copy .env.example to .env and add your key."
        )

    model = ChatAnthropic(
        model="claude-sonnet-4-6",
        temperature=0,
        max_tokens=4096,
    )

    tools = [get_weather, calculate]

    # create_react_agent builds a LangGraph graph that implements the
    # ReAct pattern: Reason → Act → Observe → Reason → ...
    # The graph runs until the model produces a final answer (no tool call).
    agent = create_react_agent(model, tools)

    query = "What is the weather in London, and what is 15% of 250?"
    print(f"\nQuery: {query}\n")

    # stream_mode="values" yields the full state at each graph step
    for step in agent.stream(
        {"messages": [("human", query)]},
        stream_mode="values",
    ):
        last_message = step["messages"][-1]
        last_message.pretty_print()

    print()

    if os.getenv("LANGSMITH_TRACING") == "true":
        project = os.getenv("LANGSMITH_PROJECT", "default")
        print(
            f"LangSmith trace: https://smith.langchain.com/projects/{project}\n"
            "Open that URL to see the full ReAct loop — every tool call and response."
        )


if __name__ == "__main__":
    main()
```

### For GOAL = 5 (Multi-agent):

```python
"""
hello_world.py — Verify your LangGraph multi-agent setup.

This script builds a minimal two-agent supervisor system:
  - Researcher: looks up information (stubbed)
  - Writer: turns research into prose

The Supervisor decides which agent to call and when to finish.

Run with:
    python src/hello_world.py
"""

import os
from typing import Literal
from dotenv import load_dotenv

load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, MessagesState, START, END


def make_agent_node(name: str, system_prompt: str, model: ChatAnthropic):
    """Factory: returns a LangGraph node function for an agent."""

    def node(state: MessagesState):
        messages = [SystemMessage(content=system_prompt)] + state["messages"]
        response = model.invoke(messages)
        # Tag the message with the agent name so the supervisor knows who spoke
        response.name = name
        return {"messages": [response]}

    node.__name__ = name
    return node


def make_supervisor(members: list[str], model: ChatAnthropic):
    """Returns a supervisor node that routes to agents or FINISH."""
    options = members + ["FINISH"]
    system_prompt = (
        f"You are a supervisor coordinating: {', '.join(members)}.\n"
        f"Given the conversation, decide who should act next: {options}.\n"
        "Reply with ONLY the name of the next worker, or FINISH when done.\n"
        "Do not add any explanation."
    )

    def node(state: MessagesState) -> dict:
        messages = [SystemMessage(content=system_prompt)] + state["messages"]
        response = model.invoke(messages)
        next_worker = response.content.strip()
        if next_worker not in options:
            next_worker = "FINISH"
        return {"messages": [response], "next": next_worker}

    return node


def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise EnvironmentError(
            "ANTHROPIC_API_KEY is not set. "
            "Copy .env.example to .env and add your key."
        )

    model = ChatAnthropic(
        model="claude-sonnet-4-6",
        temperature=0,
        max_tokens=2048,
    )

    # Build agent nodes
    researcher = make_agent_node(
        "Researcher",
        "You are a researcher. Given a topic, provide 3 key facts about it. "
        "Be concise and factual.",
        model,
    )
    writer = make_agent_node(
        "Writer",
        "You are a writer. Given research facts, write a short, engaging "
        "paragraph (3-4 sentences) for a general audience.",
        model,
    )
    supervisor = make_supervisor(["Researcher", "Writer"], model)

    # Build the graph
    # MessagesState is a built-in state type with a 'messages' list
    class SupervisorState(MessagesState):
        next: str

    graph = StateGraph(SupervisorState)
    graph.add_node("Supervisor", supervisor)
    graph.add_node("Researcher", researcher)
    graph.add_node("Writer", writer)

    graph.add_edge(START, "Supervisor")
    graph.add_conditional_edges(
        "Supervisor",
        lambda state: state["next"],
        {"Researcher": "Researcher", "Writer": "Writer", "FINISH": END},
    )
    graph.add_edge("Researcher", "Supervisor")
    graph.add_edge("Writer", "Supervisor")

    app = graph.compile()

    task = "Write a short paragraph about how LangGraph enables multi-agent systems."
    print(f"\nTask: {task}\n")
    print("-" * 60)

    for step in app.stream(
        {"messages": [HumanMessage(content=task)]},
        stream_mode="values",
    ):
        last = step["messages"][-1]
        name = getattr(last, "name", "Supervisor")
        print(f"\n[{name}]")
        print(last.content)

    print("\n" + "-" * 60)

    if os.getenv("LANGSMITH_TRACING") == "true":
        project = os.getenv("LANGSMITH_PROJECT", "default")
        print(
            f"\nLangSmith trace: https://smith.langchain.com/projects/{project}\n"
            "Open that URL to see the full multi-agent execution tree."
        )


if __name__ == "__main__":
    main()
```

---

## STEP 7 — Install Dependencies

Ask the user:

```
How would you like to install dependencies?

  1. uv (recommended — fast Rust-based installer, creates .venv automatically)
  2. pip (standard Python package installer)

Enter 1 or 2 (default: 1):
```

### If uv selected or default:

Check if uv is installed:

```bash
uv --version
```

If not installed, run:

```bash
pip install uv
```

Then:

```bash
cd PROJECT_DIR
uv venv
```

For Windows:
```bash
.venv\Scripts\activate
```

For Mac/Linux:
```bash
source .venv/bin/activate
```

Then install:

```bash
uv pip install -e ".[dev]"
```

For GOAL = 2 (RAG), also run:

```bash
uv pip install -e ".[rag,dev]"
```

### If pip selected:

```bash
cd PROJECT_DIR
python -m venv .venv
```

Windows activation:
```bash
.venv\Scripts\activate
```

Mac/Linux activation:
```bash
source .venv/bin/activate
```

Install:

```bash
pip install -e ".[dev]"
```

For GOAL = 2 (RAG):

```bash
pip install -e ".[rag,dev]"
```

After installation, verify:

```bash
python -c "import langchain; import langgraph; import langsmith; import langchain_anthropic; print('All packages imported successfully')"
```

If this fails, show the exact error and ask the user to paste it.

---

## STEP 8 — Configure API Keys

Print:

```
Now let's set up your API keys.

You need two keys to run hello_world.py:

  1. Anthropic API Key (required)
     Get it at: https://console.anthropic.com
     → Click "Get API Keys" → Create a new key
     Copy the key — it starts with sk-ant-

  2. LangSmith API Key (strongly recommended)
     Get it free at: https://smith.langchain.com
     → Sign up → Settings → API Keys → Create API Key
     Copy the key — it starts with lsv2_

Once you have both keys, open .env in your editor and fill them in.
The file already has the right variable names — just replace the placeholder values.

Have you added your keys to .env? (yes/no)
```

Wait for confirmation. If no, wait and ask again. Do not proceed until they confirm.

After they confirm, verify the keys are loaded:

```bash
python -c "
from dotenv import load_dotenv
import os
load_dotenv()
anthropic = os.getenv('ANTHROPIC_API_KEY', '')
langsmith = os.getenv('LANGSMITH_API_KEY', '')
print(f'ANTHROPIC_API_KEY: {\"set (\" + anthropic[:8] + \"...)\" if anthropic else \"NOT SET\"}')
print(f'LANGSMITH_API_KEY: {\"set (\" + langsmith[:8] + \"...)\" if langsmith else \"NOT SET — tracing disabled\"}')
print(f'LANGSMITH_TRACING: {os.getenv(\"LANGSMITH_TRACING\", \"not set\")}')
"
```

If ANTHROPIC_API_KEY is not set, stop and help the user fix it before proceeding.

If LANGSMITH_API_KEY is not set, print a warning but allow proceeding.

---

## STEP 9 — Run hello_world.py and Verify

Print:

```
Let's run hello_world.py to verify everything works.
```

Run:

```bash
cd PROJECT_DIR
python src/hello_world.py
```

### Success criteria:

- Exit code 0
- A response from Claude is printed to stdout
- If LANGSMITH_TRACING=true, a LangSmith URL is printed

### If it succeeds:

Print:

```
Your setup is working correctly.

  Claude responded to your message.
  LangSmith is recording your traces (if you configured the key).

What just happened under the hood:

  1. load_dotenv() read your .env file and set environment variables.
  2. ChatAnthropic connected to the Anthropic API using your key.
  3. ChatPromptTemplate formatted your question into a message list.
  4. The | pipe operator created a chain: prompt | model | parser
  5. chain.invoke() ran the full pipeline synchronously.
  6. LangSmith intercepted every step and logged it to your project.

This is the foundation every LangChain app builds on.
```

### If it fails with an authentication error:

Print the exact error and say:

```
This is an API key error. Check that:
  - Your ANTHROPIC_API_KEY in .env starts with sk-ant-
  - There are no extra spaces or quotes around the value
  - The key is active at https://console.anthropic.com
```

### If it fails with an import error:

```
This is a dependency error. Run:
  pip install -e ".[dev]"
and try again. If it still fails, paste the full error message here.
```

### If it fails with any other error:

Show the full traceback, identify the likely cause, and provide a fix.

---

## STEP 10 — LangSmith Deep Dive (if key is configured)

If LANGSMITH_API_KEY is set:

Print:

```
Let's look at your first LangSmith trace.

Open: https://smith.langchain.com

You should see a project called "PROJECT_NAME" with one run.

Click on it. You will see:

  Inputs tab:
    The exact messages sent to Claude, including the system prompt and
    your question formatted by ChatPromptTemplate.

  Outputs tab:
    Claude's raw response as an AIMessage object, then the parsed
    string output.

  Metadata tab:
    - Model: claude-sonnet-4-6
    - Total tokens: input tokens + output tokens
    - Latency: how long Claude took to respond
    - Cost: estimated cost in USD

Why this matters from day 1:
  When your app is broken, LangSmith shows you EXACTLY what was sent
  to the model and what came back. This is the difference between
  "it's not working" and "ah, the prompt is missing the context block."

Press Enter to continue.
```

Wait for Enter.

---

## STEP 11 — Teaching Moment: Why LCEL

Print this teaching moment:

```
Before we go further, here is the most important concept in modern LangChain.

THE OLD WAY (pre-2023, still works but avoid it):

    chain = LLMChain(llm=model, prompt=prompt)
    result = chain.run(question="What is LangChain?")

THE NEW WAY (LCEL — what you just used):

    chain = prompt | model | output_parser
    result = chain.invoke({"question": "What is LangChain?"})

Why LCEL is better:

  1. Composable — chain components with | just like Unix pipes.
     prompt | model | parser | validator | formatter

  2. Streaming is automatic — chain.stream() works on any chain.
     for chunk in chain.stream(input): print(chunk, end="")

  3. Async is automatic — await chain.ainvoke(input)

  4. Batch is automatic — chain.batch([input1, input2, input3])
     Runs in parallel, respects rate limits.

  5. Every step is a Runnable — custom functions work too:
     prompt | model | (lambda x: x.upper()) | parser

  6. LangSmith traces every step automatically — no instrumentation needed.

What is a Runnable?
  A Runnable is any object with .invoke(), .stream(), .batch(), and
  .ainvoke() methods. ChatAnthropic is a Runnable. ChatPromptTemplate
  is a Runnable. StrOutputParser is a Runnable. Your own Python function
  wrapped with RunnableLambda is a Runnable. The | operator between two
  Runnables creates a RunnableSequence — also a Runnable.

  This uniformity means the entire framework is composable by default.
```

---

## STEP 12 — Route to Domain Skill

Based on GOAL, print the routing message and invoke the appropriate skill.

### GOAL = 1 (Chatbot):

```
Your setup is complete. Let's build your chatbot.

The next skill will teach you:
  - Conversation memory using LangGraph MessageGraph
  - Session persistence with checkpointers
  - Streaming responses token by token
  - System prompt management
  - Adding a simple CLI interface

Routing to: lc:chatbot
```

Invoke skill: `lc:chatbot`

### GOAL = 2 (RAG):

```
Your setup is complete. Let's build your RAG system.

The next skill will teach you:
  - Document loading (PDF, web, Notion, plain text)
  - Text splitting strategies (RecursiveCharacterTextSplitter)
  - Embedding models and when to use which
  - Vector store setup (Chroma locally, Pinecone for production)
  - Building a retrieval chain with LCEL
  - Evaluating retrieval quality in LangSmith

Routing to: lc:rag
```

Invoke skill: `lc:rag`

### GOAL = 3 (Agent):

```
Your setup is complete. Let's build your agent.

The next skill will teach you:
  - Defining tools with @tool decorator
  - The ReAct reasoning pattern
  - LangGraph StateGraph vs prebuilt create_react_agent
  - Human-in-the-loop interrupts
  - Tool error handling and retries
  - Streaming agent steps to the user

Routing to: lc:agent
```

Invoke skill: `lc:agent`

### GOAL = 4 (Pipeline):

```
Your setup is complete. Let's build your pipeline.

The next skill will teach you:
  - Output parsers (JSON, Pydantic, CSV, XML)
  - Parallel branches with RunnableParallel
  - Conditional routing with RunnableBranch
  - Batch processing with .batch()
  - Error handling with RunnableWithFallbacks
  - Evaluation in LangSmith

Routing to: lc:pipeline
```

Invoke skill: `lc:pipeline`

### GOAL = 5 (Multi-agent):

```
Your setup is complete. Let's build your multi-agent system.

The next skill will teach you:
  - StateGraph architecture for multi-agent systems
  - The supervisor pattern vs the swarm pattern
  - Shared state design
  - Subgraphs for agent encapsulation
  - Streaming across agent boundaries
  - LangSmith trace visualization for multi-agent runs

Routing to: lc:multiagent
```

Invoke skill: `lc:multiagent`

---

## Error Handling

### Python not found:
```
Python is not installed or not in PATH.
Install Python 3.11 from https://www.python.org/downloads/
On Windows, check "Add Python to PATH" during installation.
After installation, open a new terminal and run this skill again.
```

### pip not found:
```
pip is not available. This is unusual if Python is installed.
Try: python -m pip --version
If that works, use python -m pip instead of pip throughout.
```

### Virtual environment activation fails on Windows:
```
On Windows, you may need to allow script execution.
Run in PowerShell as Administrator:
  Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
Then try activating again: .venv\Scripts\activate
```

### Package install fails due to C compiler (chromadb, etc.):
```
Some packages require build tools.
On Windows: install Visual Studio Build Tools from
  https://visualstudio.microsoft.com/visual-cpp-build-tools/
On Mac: run: xcode-select --install
On Linux: run: sudo apt install build-essential
```

---

## Conventions This Skill Enforces

1. Always use `load_dotenv()` as the FIRST import call, before any LangChain imports.
   This ensures LANGSMITH_TRACING is set before LangChain initializes its tracer.

2. Always use `claude-sonnet-4-6` as the default model unless the user specifies otherwise.

3. Never hardcode API keys. Always use os.getenv().

4. LCEL (pipe syntax) is the only chain pattern taught. Do not show LLMChain,
   SequentialChain, or any other legacy pattern.

5. LangSmith tracing is configured from step 1, not added later.

6. Project structure follows src/ layout for proper Python packaging.

7. .env is in .gitignore by default. Warn loudly if the user tries to commit it.

---

## Summary of Files Created

After this skill completes, the project contains:

| File | Purpose |
|------|---------|
| pyproject.toml | Package definition, dependencies, tool config |
| .env.example | Template showing all required env vars |
| .env | Actual secrets (git-ignored) |
| .gitignore | Excludes .env, .venv, __pycache__ |
| README.md | Quick-start instructions |
| src/__init__.py | Makes src/ a Python package |
| src/hello_world.py | Working verification script |
| tests/__init__.py | Empty test package init |

The user leaves this skill with:
- A working Python project
- Claude responding to LLM calls
- LangSmith tracing their runs
- A conceptual understanding of LCEL, Runnables, and LangGraph
- A clear path to their specific domain skill
