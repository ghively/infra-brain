---
name: lc-explain
description: >
  Explains any LangChain or LangGraph concept in plain English tailored for a
  beginner. Triggered by /lc-explain [concept], or whenever the user asks "what
  is X", "explain X", or "I don't understand X" where X is a LangChain/LangGraph
  term. Covers concepts including but not limited to: StateGraph, checkpointing,
  LCEL, supervisors, RAG, ToolNode, MessagesState, reducers, Send API, interrupt,
  RunnablePassthrough, RunnableParallel, embeddings, vector stores, chains,
  agents, memory, streaming, LangSmith, and all pattern names from lc-agent and
  lc-lcel skills.
---

# lc-explain — LangChain/LangGraph Concept Explainer

## Overview

`/lc-explain [concept]` is a beginner-first command that explains any
LangChain or LangGraph concept in exactly six structured sections, always in
plain English with a real-world analogy, a minimal working code example, and
clear pointers to what to learn next.

This skill also scans the current project files to detect whether the concept
is already in use and, if so, points the user directly to it.

**Default model:** `claude-sonnet-4-6` via `langchain-anthropic`.

---

## Invocation

```
/lc-explain [concept]
```

**Examples:**
```
/lc-explain StateGraph
/lc-explain checkpointing
/lc-explain LCEL
/lc-explain supervisors
/lc-explain RAG
/lc-explain ToolNode
/lc-explain MessagesState
/lc-explain reducers
/lc-explain Send API
/lc-explain interrupt
/lc-explain RunnablePassthrough
/lc-explain embeddings
/lc-explain streaming
```

If the user invokes the skill with no argument (`/lc-explain` alone), ask:

```
Which LangChain or LangGraph concept would you like explained?
Some popular choices: StateGraph, LCEL, checkpointing, RAG, ToolNode,
supervisors, reducers, Send API, interrupt, RunnablePassthrough, embeddings,
streaming, LangSmith
```

---

## Execution Flow

Run every step in this order. Do not skip steps.

### STEP 1 — Resolve the concept

Normalize the input: trim whitespace, lowercase for lookup, but display in the
user's original casing.

Check whether the concept is in the **Known Concepts Catalog** at the bottom of
this skill. If it is, use the entry there as the authoritative source for
sections 1–4 of the explanation output.

If it is **not** in the catalog:
- Attempt a fuzzy match against all catalog entries (e.g. "stategraph" →
  `StateGraph`, "memory" → `checkpointing + MemorySaver`, "pipe" → `LCEL`).
- If a match is found with high confidence, explain it but add a note:
  "I'm interpreting '[user input]' as [matched concept]. Let me know if you
  meant something else."
- If no match is found, run the **Unknown Concept Handler** (see end of skill).

### STEP 2 — Scan current project files

Before generating the explanation, scan the open project for uses of the
concept using these patterns:

| Concept | Scan pattern |
|---|---|
| StateGraph | `StateGraph` in any `.py` file |
| LCEL | `\|` chain operator or `RunnableSequence` in any `.py` |
| checkpointing | `MemorySaver`, `PostgresSaver`, `checkpointer=` in any `.py` |
| ToolNode | `ToolNode` in any `.py` |
| MessagesState | `MessagesState` in any `.py` |
| RAG | `retriever`, `vectorstore`, `Chroma`, `Pinecone` in any `.py` |
| supervisors | `create_supervisor`, `langgraph_supervisor` in any `.py` |
| Send API | `Send(` in any `.py` |
| interrupt | `interrupt(`, `interrupt_before` in any `.py` |
| RunnablePassthrough | `RunnablePassthrough` in any `.py` |
| RunnableParallel | `RunnableParallel` in any `.py` |
| embeddings | `Embeddings`, `embed_` in any `.py` |
| reducers | `Annotated[list, operator.add]`, `add_messages` in any `.py` |
| streaming | `astream(`, `.stream(` in any `.py` |
| LangSmith | `LANGSMITH_TRACING`, `langsmith` in any `.py` or `.env` |

Store results as PROJECT_HITS: a list of `(file_path, line_number, line_text)`.

### STEP 3 — Generate the explanation

Output the explanation in exactly this format. Use plain English throughout.
Do not use jargon without defining it inline.

---

## Output Format

```
## [Concept Name]

### What it is

[One sentence. Subject-verb-object. No buzzwords. If the concept is part of a
larger framework, name the framework. Example: "StateGraph is a LangGraph class
that lets you define a workflow as a directed graph, where each node is a
Python function and each edge is a transition between nodes."]

---

### The analogy

[One short paragraph using a concrete real-world analogy. The analogy must be
something a non-programmer would immediately recognise. Start with:
"[Concept] is like [familiar thing]..."

Examples of good analogies:
- StateGraph is like a flowchart on a whiteboard where each box is a task and
  each arrow shows which task comes next.
- Checkpointing is like a video game save point: if your agent crashes or you
  close the app, you can reload from the last save instead of starting over.
- LCEL (the | operator) is like a kitchen assembly line, where each station
  receives the output of the previous one and passes its result to the next.
- RAG is like giving someone a research packet before asking them to answer an
  exam question: you supply the relevant facts so they don't have to guess.]

---

### When you need it

- [Bullet 1: the most common real-world scenario that requires this concept]
- [Bullet 2: a second, distinct scenario]
- [Bullet 3: a third scenario, often a "you'll know you need it when..." case]

---

### Minimal code example

```python
# [concept_name]_example.py
#
# [One-line description of what this example demonstrates]
#
# Requirements:
#   pip install [only the packages this specific example needs]

[10–20 lines of working Python. Rules:
  - load_dotenv() must be the first call if any API key is needed
  - Use claude-sonnet-4-6 as the model
  - Include exactly one inline comment per non-obvious line
  - The example must be self-contained and runnable as-is
  - Show the concept in isolation — do not combine with unrelated concepts
  - End with a print() call so the user sees output when they run it]
```

---

### Common beginner mistakes

| Mistake | What goes wrong | Fix |
|---|---|---|
| [Mistake 1 — short phrase] | [What the error looks like or what breaks] | [One-sentence fix] |
| [Mistake 2] | [Effect] | [Fix] |
| [Mistake 3] | [Effect] | [Fix] |

---

### What to learn next

You understand [concept]. The natural next steps are:

1. **[Next concept A]** — [one sentence on why it builds on this concept]
   → Run `/lc-explain [Next concept A]` to learn it now.

2. **[Next concept B]** — [one sentence]
   → Run `/lc-explain [Next concept B]`

3. **[Related skill or pattern]** — [one sentence]
   → Run `/[skill-name]` to scaffold a full working example.

---
```

### STEP 4 — Project file reference (conditional)

If PROJECT_HITS is non-empty, append this section AFTER the explanation:

```
### You're already using this

[Concept] appears in your project:

[For each hit, one line:]
- `[relative_file_path]` line [N]: `[line_text trimmed to 80 chars]`

[If more than 5 hits exist, show only the first 5 and add:]
…and [N] more occurrences. Run a project-wide search for "[scan pattern]"
to see all of them.
```

If PROJECT_HITS is empty, do not output this section at all.

---

## Known Concepts Catalog

Use these entries as the authoritative source for each concept's explanation.
Fill in the six-section template using the data below.

---

### StateGraph

**One-line definition:**
`StateGraph` is a LangGraph class that lets you build a workflow as a directed
graph, where each node is a Python function that reads from and writes to a
shared state object, and each edge defines which node runs next.

**Analogy:**
StateGraph is like a whiteboard flowchart that actually runs: each box is a
task your program performs, each arrow shows which task comes next, and a
sticky note in the corner holds all the information that gets passed from box
to box.

**When you need it:**
- You need an LLM loop that runs until a condition is met (e.g. "keep trying
  until the answer is good enough").
- You need branching: "if the LLM called a tool, go to the tool node; otherwise
  return to the user."
- You need to add checkpointing, human-in-the-loop interrupts, or streaming to
  a multi-step workflow.

**Minimal code:**
```python
# stategraph_example.py
# Build a two-node graph: generate a joke, then improve it.
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langgraph.graph import StateGraph, START, END
from typing_extensions import TypedDict

class JokeState(TypedDict):
    topic: str       # input: what the joke is about
    joke: str        # the current joke draft

llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0.7)

def write_joke(state: JokeState) -> dict:
    """Node 1: write a first-draft joke."""
    response = llm.invoke(f"Write a short one-liner joke about {state['topic']}.")
    return {"joke": response.content}

def improve_joke(state: JokeState) -> dict:
    """Node 2: make the joke punchier."""
    response = llm.invoke(f"Make this joke punchier in one sentence: {state['joke']}")
    return {"joke": response.content}

graph = StateGraph(JokeState)
graph.add_node("write", write_joke)      # register nodes by name
graph.add_node("improve", improve_joke)
graph.add_edge(START, "write")           # START → write → improve → END
graph.add_edge("write", "improve")
graph.add_edge("improve", END)

app = graph.compile()
result = app.invoke({"topic": "Python programming", "joke": ""})
print(result["joke"])
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Returning the full state dict instead of only changed fields | LangGraph sees unexpected keys and may raise a validation error | Return only the keys you changed: `{"joke": new_joke}` not `{**state, "joke": new_joke}` |
| Forgetting `START` and `END` imports | `NameError: name 'START' is not defined` | `from langgraph.graph import StateGraph, START, END` |
| Not compiling before invoking | `AttributeError` — `StateGraph` has no `invoke` method | Call `app = graph.compile()` before `app.invoke(...)` |

**What to learn next:**
1. `checkpointing` — adds memory and human-in-the-loop to a StateGraph
2. `MessagesState` — a built-in state type designed for conversational agents
3. `/lc-agent` — scaffold a full ReAct agent using StateGraph

---

### checkpointing

**One-line definition:**
Checkpointing is LangGraph's mechanism for saving the state of a graph after
every node execution, so the graph can be paused, resumed, or replayed from any
point.

**Analogy:**
Checkpointing is like a video game save point: every time your agent completes a
step, the game saves automatically. If your server crashes, the user closes the
app, or you want to pause and ask a human for approval, you reload from the last
save instead of starting over from the beginning.

**When you need it:**
- Your agent has conversation history that must survive server restarts
  (production chatbots, support systems).
- You want human-in-the-loop: pause the agent before it executes a risky tool
  call, get approval, then resume.
- You're building a long-running task (report generation, research) that might
  fail mid-way and needs to be recoverable.

**Minimal code:**
```python
# checkpointing_example.py
# Show how the same thread_id produces conversation memory.
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.memory import MemorySaver   # dev only — lost on restart
from langgraph.graph import MessagesState, StateGraph, START, END

llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

def chat_node(state: MessagesState) -> dict:
    response = llm.invoke(state["messages"])
    return {"messages": [response]}

graph = StateGraph(MessagesState)
graph.add_node("chat", chat_node)
graph.add_edge(START, "chat")
graph.add_edge("chat", END)

checkpointer = MemorySaver()                   # swap for PostgresSaver in prod
app = graph.compile(checkpointer=checkpointer)

# thread_id scopes the conversation — same ID = same history
config = {"configurable": {"thread_id": "user-alice"}}

app.invoke({"messages": [("user", "My name is Alice.")]}, config=config)
result = app.invoke({"messages": [("user", "What is my name?")]}, config=config)
print(result["messages"][-1].content)   # "Your name is Alice."
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Using `MemorySaver` in production | State is lost every time the process restarts | Use `PostgresSaver` with a real database URL for production |
| Forgetting `thread_id` in config | Each call starts a fresh conversation with no memory | Always pass `config={"configurable": {"thread_id": "some-id"}}` |
| Using `interrupt()` without a checkpointer | `RuntimeError` — interrupt requires a checkpointer to save state | Add `checkpointer=MemorySaver()` to `graph.compile()` |

**What to learn next:**
1. `interrupt` — how to pause mid-graph for human approval using checkpointing
2. `StateGraph` — the graph that checkpointing runs inside
3. `/lc-agent` — scaffold a full agent with checkpointing and memory

---

### LCEL

**One-line definition:**
LCEL (LangChain Expression Language) is the `|` pipe syntax for chaining
LangChain components together: `prompt | model | parser` connects three
Runnables so each one receives the output of the previous one.

**Analogy:**
LCEL is like a kitchen assembly line: the first station preps the ingredients
(the prompt template formats your variables), the next station does the cooking
(the model generates a response), and the last station plates the dish (the
output parser extracts the text). Each station receives the output of the
previous one, and the customer only sees the finished plate.

**When you need it:**
- You want to combine a prompt, a model, and a parser into a single callable
  chain without writing the glue code yourself.
- You need automatic streaming, async support, or batch processing on a
  multi-step pipeline.
- You want every step traced in LangSmith without adding any instrumentation.

**Minimal code:**
```python
# lcel_example.py
# Build a minimal chain: format a prompt, call Claude, parse the result.
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

prompt = ChatPromptTemplate.from_template(
    "Give me one interesting fact about {topic}."
)
model = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)
parser = StrOutputParser()   # converts AIMessage → plain string

# The | operator creates a RunnableSequence: prompt → model → parser
chain = prompt | model | parser

# .invoke() runs the full pipeline synchronously
result = chain.invoke({"topic": "black holes"})
print(result)

# .stream() yields tokens as they arrive
for chunk in chain.stream({"topic": "origami"}):
    print(chunk, end="", flush=True)
print()
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Forgetting `StrOutputParser()` at the end | `chain.invoke()` returns an `AIMessage` object, not a string | Add `| StrOutputParser()` as the last step |
| Calling `.run()` instead of `.invoke()` | `AttributeError` — `.run()` was removed in LangChain 0.2 | Use `.invoke({"key": "value"})` |
| Breaking streaming by using `return` in a transform | Streaming stops at that step; downstream gets nothing until the whole result is ready | Use `yield` inside any custom transform function, not `return` |

**What to learn next:**
1. `RunnablePassthrough` — carry the original input forward while adding computed context
2. `RunnableParallel` — run multiple chains on the same input simultaneously
3. `/lc-lcel` — a full masterclass on every LCEL component

---

### RAG

**One-line definition:**
RAG (Retrieval-Augmented Generation) is a pattern where an LLM's answer is
grounded in documents you provide at query time, rather than relying on what
the model learned during training.

**Analogy:**
RAG is like an open-book exam: instead of relying purely on what the student
memorised, you hand them a curated research packet before they answer. The
student (the LLM) reads the packet (retrieved chunks) and uses it to give a
more accurate, specific answer — and can even cite the source.

**When you need it:**
- You need the LLM to answer questions about your private data (internal docs,
  product manuals, support tickets) that it has never seen before.
- You need answers grounded in current information: training data has a cutoff
  date, but your vector store can be updated daily.
- You need citations or source attribution: the LLM can reference the exact
  document chunks that informed its answer.

**Minimal code:**
```python
# rag_example.py
# Index two documents, then answer a question using them.
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_openai import OpenAIEmbeddings   # or AnthropicEmbeddings

# Step 1: Create documents (replace with a real loader in production)
docs = [
    Document(page_content="LangGraph is used to build stateful agent workflows."),
    Document(page_content="LangSmith provides observability for LLM applications."),
]

# Step 2: Embed and index — converts text to vectors for semantic search
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
vectorstore = Chroma.from_documents(docs, embeddings)
retriever = vectorstore.as_retriever(search_kwargs={"k": 2})

# Step 3: Build the RAG chain
prompt = ChatPromptTemplate.from_template(
    "Answer using ONLY the context below.\nContext: {context}\nQuestion: {question}"
)
model = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

def format_docs(docs):
    return "\n\n".join(d.page_content for d in docs)

chain = (
    {"context": retriever | format_docs, "question": RunnablePassthrough()}
    | prompt
    | model
    | StrOutputParser()
)

result = chain.invoke("What does LangGraph do?")
print(result)
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Asking about topics not in your documents | LLM hallucinates or says "I don't know" | Add more relevant documents, or use the CRAG pattern to fall back to web search |
| Chunk size too large | Retrieved chunks contain mostly irrelevant text, diluting the answer | Use `RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)` |
| Not running `ingest.py` before querying | `CollectionNotFoundError` or empty retrieval results | Always run your ingestion script once before querying |

**What to learn next:**
1. `embeddings` — how text is converted to vectors for similarity search
2. `checkpointing` — add conversation memory to a RAG chatbot
3. `/rag` — scaffold the full RAG pattern from naive to agentic

---

### ToolNode

**One-line definition:**
`ToolNode` is a prebuilt LangGraph node that inspects the last AI message for
tool call requests, executes each tool, and returns the results as
`ToolMessage` objects — so you never have to write the tool-execution loop
yourself.

**Analogy:**
ToolNode is like the mail room in a large company: the LLM (an executive)
writes a memo saying "I need these three reports", puts it in the out tray, and
the mail room (ToolNode) automatically fetches all three reports and puts them
in the in tray, ready for the executive to read.

**When you need it:**
- You have a ReAct agent that needs to call tools and you don't want to write
  the dispatch loop yourself.
- You need all tool calls in a single LLM message executed in one batch.
- You want correct error handling and `ToolMessage` formatting without manual
  plumbing.

**Minimal code:**
```python
# toolnode_example.py
# A two-node agent: LLM decides which tools to call, ToolNode executes them.
from dotenv import load_dotenv
load_dotenv()

from typing import Literal
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langgraph.graph import MessagesState, StateGraph, START, END
from langgraph.prebuilt import ToolNode

@tool
def get_word_count(text: str) -> str:
    """Count the number of words in the given text."""
    return str(len(text.split()))

@tool
def reverse_string(text: str) -> str:
    """Reverse the characters in the given string."""
    return text[::-1]

tools = [get_word_count, reverse_string]

llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)
llm_with_tools = llm.bind_tools(tools)   # tell the LLM about the tools

def agent_node(state: MessagesState) -> dict:
    return {"messages": [llm_with_tools.invoke(state["messages"])]}

def route(state: MessagesState) -> Literal["tools", "__end__"]:
    """If the LLM requested a tool call, go to tools; else finish."""
    return "tools" if state["messages"][-1].tool_calls else END

graph = StateGraph(MessagesState)
graph.add_node("agent", agent_node)
graph.add_node("tools", ToolNode(tools))   # ToolNode handles all execution
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", route)
graph.add_edge("tools", "agent")           # after tools, back to agent

app = graph.compile()
result = app.invoke({"messages": [("user", "How many words in 'hello world'?")]})
print(result["messages"][-1].content)
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Passing tools to `ToolNode` but not to `bind_tools()` | LLM never requests a tool call because it doesn't know the tools exist | Pass the same tools list to both `llm.bind_tools(tools)` and `ToolNode(tools)` |
| `@tool` function has no docstring | LLM never calls the tool because it doesn't understand what it does | Every `@tool` function must have a docstring — it becomes the tool description |
| Routing to `"tools"` even when `tool_calls` is empty | Infinite loop or empty `ToolMessage` error | Check `state["messages"][-1].tool_calls` — it's an empty list, not `None`, when there are no calls |

**What to learn next:**
1. `MessagesState` — the state type designed for tool-calling agents
2. `interrupt` — pause before ToolNode executes to get human approval
3. `/lc-agent` — scaffold a complete ReAct agent with ToolNode

---

### MessagesState

**One-line definition:**
`MessagesState` is a built-in LangGraph state type whose `messages` field
automatically appends new messages to the existing list rather than replacing
it, which is the correct behaviour for any conversational or tool-using agent.

**Analogy:**
MessagesState is like a conversation transcript that only allows you to add new
lines — you can never erase what was said before. Every new message goes at the
bottom of the transcript. This way the agent always has the full history of what
was said and what tools returned.

**When you need it:**
- Building any agent that has a conversation history (chatbots, support agents).
- Any agent that uses tools: the tool results need to be appended to the message
  list so the LLM sees them on the next iteration.
- Whenever you want the standard LangGraph message accumulation behaviour
  without writing a custom reducer.

**Minimal code:**
```python
# messagesstate_example.py
# Show how MessagesState appends messages rather than replacing them.
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langgraph.graph import MessagesState, StateGraph, START, END

llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

def respond(state: MessagesState) -> dict:
    # state["messages"] contains ALL previous messages
    response = llm.invoke(state["messages"])
    # Returning {"messages": [response]} APPENDS — does not replace
    return {"messages": [response]}

graph = StateGraph(MessagesState)
graph.add_node("respond", respond)
graph.add_edge(START, "respond")
graph.add_edge("respond", END)
app = graph.compile()

# MessagesState is equivalent to defining:
#   from typing import Annotated
#   from langgraph.graph.message import add_messages
#   class MyState(TypedDict):
#       messages: Annotated[list, add_messages]

result = app.invoke({"messages": [("user", "Hello, my name is Bob.")]})
print(result["messages"][-1].content)
# "Hello Bob! Nice to meet you."
print(f"Total messages in state: {len(result['messages'])}")
# 2: the HumanMessage and the AIMessage
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Using a plain `list` field instead of `Annotated[list, add_messages]` | Each node invocation replaces the message list — all history is lost | Use `MessagesState` or annotate your field: `messages: Annotated[list, add_messages]` |
| Returning `{"messages": state["messages"] + [response]}` | Messages are doubled — both the existing list and the new one are merged by the reducer | Return only the new messages: `{"messages": [response]}` |
| Expecting `state["messages"]` to be a list of strings | It is a list of `BaseMessage` objects (`HumanMessage`, `AIMessage`, `ToolMessage`) | Access content with `state["messages"][-1].content` |

**What to learn next:**
1. `reducers` — how `add_messages` works and how to write custom reducers
2. `ToolNode` — the prebuilt node that uses `MessagesState` for tool calling
3. `checkpointing` — persist `MessagesState` across sessions

---

### reducers

**One-line definition:**
A reducer is a function that tells LangGraph how to merge a new value into an
existing state field when two or more nodes update the same field — instead of
one update silently overwriting another.

**Analogy:**
A reducer is like a meeting notes policy: if two people are taking notes at the
same meeting, you need a rule for combining them. "Append to the list" is one
rule. "Keep the highest score" is another. Without a rule, only one person's
notes survive and the other's are lost.

**When you need it:**
- Multiple nodes write to the same state field and you need both updates
  preserved (e.g. a list of results from parallel workers).
- You are using the Send API for fan-out: each worker writes a result, and you
  need them all collected into one list.
- You want conversation history to accumulate rather than be overwritten
  (the `add_messages` reducer is the canonical example).

**Minimal code:**
```python
# reducers_example.py
# Custom reducer: collect scores from parallel nodes without overwriting.
from dotenv import load_dotenv
load_dotenv()

import operator
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

# operator.add is the standard "append list" reducer
class EvalState(TypedDict):
    topic: str
    scores: Annotated[list[int], operator.add]   # each node appends, not overwrites

def score_clarity(state: dict) -> dict:
    """Simulates a clarity-scoring node."""
    return {"scores": [8]}   # returns a list to be appended

def score_depth(state: dict) -> dict:
    """Simulates a depth-scoring node."""
    return {"scores": [6]}   # appended alongside clarity score

def fan_out(state: EvalState):
    """Send the same state to both scorer nodes in parallel."""
    return [Send("clarity", state), Send("depth", state)]

def summarise(state: EvalState) -> dict:
    avg = sum(state["scores"]) / len(state["scores"])
    print(f"Scores: {state['scores']}, Average: {avg:.1f}")
    return {}

graph = StateGraph(EvalState)
graph.add_node("clarity", score_clarity)
graph.add_node("depth", score_depth)
graph.add_node("summarise", summarise)
graph.add_conditional_edges(START, fan_out, ["clarity", "depth"])
graph.add_edge("clarity", "summarise")
graph.add_edge("depth", "summarise")
graph.add_edge("summarise", END)

app = graph.compile()
app.invoke({"topic": "LangGraph reducers", "scores": []})
# Scores: [8, 6], Average: 7.0
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Using a plain `list` field in parallel workflows | Later node overwrites earlier node's result — you lose data | Annotate with `Annotated[list, operator.add]` so each write appends |
| Returning a scalar instead of a list with `operator.add` | `TypeError: can only concatenate list to list` | Wrap the value in a list: `{"scores": [my_score]}` not `{"scores": my_score}` |
| Writing a reducer that mutates the existing list in place | Subtle bugs: the graph sees side effects across runs | Reducers must return a new value, not mutate the old one |

**What to learn next:**
1. `Send API` — the fan-out pattern that makes reducers essential
2. `MessagesState` — uses the `add_messages` reducer under the hood
3. `/lc-agent` — parallel patterns that rely on reducers

---

### Send API

**One-line definition:**
The Send API is a LangGraph feature that lets a routing function dynamically
spawn multiple parallel graph invocations — one per item in a list — each with
its own isolated state, with results aggregated back via a reducer.

**Analogy:**
The Send API is like a manager distributing envelopes to a team: each envelope
contains one task and goes to one worker. All workers open their envelopes and
start working simultaneously. When everyone is done, the manager collects all
the results. You decide how many envelopes to send based on runtime data, not
at design time.

**When you need it:**
- You need to process N items in parallel (summarise 50 documents, score 100
  applicants, translate 30 files) where N is not known until runtime.
- You want true fan-out: each item gets its own isolated state so workers
  cannot interfere with each other.
- You need map-reduce: fan out to workers, then aggregate all results.

**Minimal code:**
```python
# send_api_example.py
# Fan out: summarise each topic in parallel, collect all summaries.
from dotenv import load_dotenv
load_dotenv()

import operator
from typing import Annotated
from typing_extensions import TypedDict
from langchain_anthropic import ChatAnthropic
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

class OverallState(TypedDict):
    topics: list[str]
    summaries: Annotated[list[str], operator.add]   # reducer collects all results

class WorkerState(TypedDict):
    topic: str          # each worker gets its own isolated state
    summaries: Annotated[list[str], operator.add]

llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

def summarise_topic(state: WorkerState) -> dict:
    """Worker node: runs once per topic, in parallel."""
    response = llm.invoke(f"In one sentence, summarise: {state['topic']}")
    return {"summaries": [response.content]}   # appended to OverallState.summaries

def fan_out(state: OverallState) -> list[Send]:
    """Create one Send per topic — all run simultaneously."""
    return [Send("summarise_topic", {"topic": t}) for t in state["topics"]]

def print_results(state: OverallState) -> dict:
    for s in state["summaries"]:
        print(f"- {s}")
    return {}

graph = StateGraph(OverallState)
graph.add_node("summarise_topic", summarise_topic)
graph.add_node("print_results", print_results)
graph.add_conditional_edges(START, fan_out, ["summarise_topic"])
graph.add_edge("summarise_topic", "print_results")
graph.add_edge("print_results", END)

app = graph.compile()
app.invoke({"topics": ["quantum computing", "CRISPR", "fusion energy"], "summaries": []})
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Not using `Annotated[list, operator.add]` on the aggregation field | Workers overwrite each other's results — only the last one survives | Always use a reducer on any field that multiple parallel nodes write to |
| Using `Send` without declaring target nodes in `add_conditional_edges` | `ValueError: node not found` | Pass the list of possible target nodes as the third argument: `add_conditional_edges(src, fn, ["node_a", "node_b"])` |
| Passing the full `OverallState` as worker state | Workers see and can accidentally mutate fields they shouldn't | Define a separate `WorkerState` TypedDict with only the fields the worker needs |

**What to learn next:**
1. `reducers` — essential for collecting Send API results
2. `StateGraph` — the graph that the Send API runs inside
3. `/lc-agent` — the parallel (Send API) pattern scaffold

---

### interrupt

**One-line definition:**
`interrupt()` is a LangGraph function that pauses a running graph at a specific
node, returns control to the calling code, and waits for a `Command(resume=...)`
call before continuing — enabling human-in-the-loop approval workflows.

**Analogy:**
`interrupt()` is like a "Pending Approval" status in an expense report system:
the workflow pauses, sends a notification to a manager, and does nothing more
until the manager clicks Approve or Reject. The workflow then resumes exactly
where it left off, with the manager's decision available to the next step.

**When you need it:**
- You want a human to review and approve a tool call before it executes (e.g.
  "the agent wants to delete a file — is that OK?").
- You need a human to provide missing information mid-workflow (e.g. "I need
  your password to log in to that service").
- You are building an autonomous agent where certain high-stakes actions must
  always be confirmed.

**Minimal code:**
```python
# interrupt_example.py
# Pause before a "dangerous" action and ask for human approval.
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import MessagesState, StateGraph, START, END
from langgraph.types import Command, interrupt

llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

def agent_node(state: MessagesState) -> dict:
    response = llm.invoke(state["messages"])
    return {"messages": [response]}

def approval_node(state: MessagesState) -> dict:
    """Pause here and wait for human input."""
    # interrupt() pauses the graph and surfaces 'value' to the caller
    human_decision = interrupt({"question": "Proceed with this action?", "state": state})
    if human_decision != "yes":
        return {"messages": [{"role": "assistant", "content": "Action cancelled by user."}]}
    return {}   # no change — graph continues to next node

def execute_node(state: MessagesState) -> dict:
    return {"messages": [{"role": "assistant", "content": "Action executed successfully."}]}

graph = StateGraph(MessagesState)
graph.add_node("agent", agent_node)
graph.add_node("approval", approval_node)   # interrupt lives here
graph.add_node("execute", execute_node)
graph.add_edge(START, "agent")
graph.add_edge("agent", "approval")
graph.add_edge("approval", "execute")
graph.add_edge("execute", END)

app = graph.compile(checkpointer=MemorySaver())   # checkpointer is REQUIRED for interrupt
config = {"configurable": {"thread_id": "hitl-demo"}}

# First call — graph pauses at approval_node
state = app.invoke({"messages": [("user", "Please do the thing.")]}, config=config)
print("Graph paused. Pending approval.")

# Second call — resume with the human's decision
result = app.invoke(Command(resume="yes"), config=config)
print(result["messages"][-1].content)
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Using `interrupt()` without a checkpointer | `RuntimeError: interrupt requires a checkpointer` | Always compile with `checkpointer=MemorySaver()` (dev) or `PostgresSaver` (prod) |
| Resuming with `app.invoke(input, config)` instead of `app.invoke(Command(resume=...), config)` | Graph restarts from the beginning instead of resuming | Use `Command(resume=value)` as the input, not a new messages dict |
| Not using the same `thread_id` when resuming | Graph cannot find the saved state — starts fresh | Use identical `config = {"configurable": {"thread_id": "same-id"}}` for both calls |

**What to learn next:**
1. `checkpointing` — the mechanism that makes interrupt possible
2. `Command` — the LangGraph type used to resume or redirect after an interrupt
3. `/lc-agent` — scaffold a full HITL agent with interrupt

---

### RunnablePassthrough

**One-line definition:**
`RunnablePassthrough` is an LCEL component that passes its input through
unchanged, and `RunnablePassthrough.assign(key=fn)` adds a new computed key
to the input dict without removing the original keys.

**Analogy:**
`RunnablePassthrough` is like a relay runner who carries the baton without
dropping it: the original question stays in the dict the whole way through
the chain. `assign()` is like that runner also picking up a second baton
(the retrieved context) mid-race so both can be handed to the next runner.

**When you need it:**
- Building a RAG chain where the prompt needs both `{question}` and `{context}`
  but the chain starts with only the question.
- You want to thread the original input through a transformation step to make
  it available further down the chain.
- You need to add a computed field (e.g. retrieved documents, user metadata)
  to the dict flowing through the chain.

**Minimal code:**
```python
# runnablepassthrough_example.py
# Use assign() to add retrieved context while keeping the original question.
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough

def fake_retriever(inputs: dict) -> str:
    """Simulate context retrieval."""
    return f"[Context about: {inputs['question']}]"

prompt = ChatPromptTemplate.from_template(
    "Context: {context}\n\nAnswer this: {question}"
)
model = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

# assign() adds "context" to the dict without removing "question"
chain = (
    RunnablePassthrough.assign(context=fake_retriever)
    # After assign: {"question": "...", "context": "[Context about: ...]"}
    | prompt
    | model
    | StrOutputParser()
)

result = chain.invoke({"question": "What is LangGraph?"})
print(result)
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Using `RunnablePassthrough()` alone when you need `assign()` | The input dict is passed through but no new keys are added | Use `RunnablePassthrough.assign(key=fn)` to add computed keys |
| `assign()` lambda receives the whole input dict, not just one field | `KeyError` if you try to use `x` as a string | Always access the key you need: `lambda x: do_thing(x["question"])` |
| Using `assign()` with a key name that already exists in the input | The original value is silently overwritten | Choose a different key name, or intentionally use this behaviour to update a field |

**What to learn next:**
1. `RunnableParallel` — run multiple chains on the same input simultaneously
2. `LCEL` — the full pipe composition model
3. `/lc-lcel` — a complete LCEL masterclass

---

### RunnableParallel

**One-line definition:**
`RunnableParallel` runs multiple Runnables with the same input simultaneously
and returns a dict mapping each key to its corresponding output — all branches
run concurrently in a thread pool.

**Analogy:**
`RunnableParallel` is like asking three analysts to review the same document at
the same time: one writes a summary, one extracts action items, one checks for
risks. They all start simultaneously, work independently, and you get all three
results in a single envelope — taking only as long as the slowest one.

**When you need it:**
- You want multiple perspectives on the same input (pros, cons, summary) without
  waiting for each one sequentially.
- You need to run retrieval and rephrase a query simultaneously before feeding
  both results to a prompt.
- You want to reduce total latency by parallelising independent LLM calls.

**Minimal code:**
```python
# runnableparallel_example.py
# Generate pros and cons of a topic simultaneously.
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableParallel

model = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

pros_chain = (
    ChatPromptTemplate.from_template("List 2 pros of {topic} in bullet points.")
    | model | StrOutputParser()
)
cons_chain = (
    ChatPromptTemplate.from_template("List 2 cons of {topic} in bullet points.")
    | model | StrOutputParser()
)

# Both chains receive {"topic": "..."} simultaneously
analysis = RunnableParallel(pros=pros_chain, cons=cons_chain)

result = analysis.invoke({"topic": "remote work"})
print("Pros:\n", result["pros"])
print("Cons:\n", result["cons"])

# Dict shorthand — LCEL auto-wraps a dict of Runnables as RunnableParallel
analysis_shorthand = {"pros": pros_chain, "cons": cons_chain}
result2 = (analysis_shorthand | ChatPromptTemplate.from_template(
    "Given pros: {pros} and cons: {cons}, give a verdict on: {topic}"
) | model | StrOutputParser()).invoke({"topic": "remote work"})
print(result2)
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Expecting the dict shorthand to work mid-chain | `TypeError` — dicts are only auto-wrapped as `RunnableParallel` at the chain start | Wrap explicitly with `RunnableParallel(...)` when using it as a non-first step |
| Running independent LLM calls sequentially with `|` | Total latency = sum of all calls instead of max of all calls | Use `RunnableParallel` so they run concurrently |
| Assuming order of keys is preserved | Dict order is preserved in Python 3.7+ but result order is non-deterministic in some async paths | Always access results by key name, not by index |

**What to learn next:**
1. `RunnablePassthrough` — carry the original input alongside parallel results
2. `LCEL` — the full composition model
3. `/lc-lcel` — a complete LCEL masterclass

---

### embeddings

**One-line definition:**
An embedding is a list of numbers (a vector) that represents the meaning of a
piece of text so that semantically similar texts produce mathematically similar
vectors, enabling semantic search without keyword matching.

**Analogy:**
Embeddings are like GPS coordinates for meaning: just as two nearby cities have
similar coordinates, two sentences that mean the same thing have similar
embeddings. "The dog ran fast" and "The canine sprinted quickly" are far apart
alphabetically but close together in embedding space because they convey the
same idea.

**When you need it:**
- Building RAG: you embed your documents once and embed each query at search
  time, then retrieve the documents whose embeddings are closest to the query
  embedding.
- Semantic search: find documents by meaning, not just by matching keywords.
- Clustering or classification: group similar texts together without writing
  explicit rules.

**Minimal code:**
```python
# embeddings_example.py
# Embed two sentences and compare their similarity.
from dotenv import load_dotenv
load_dotenv()

import math
from langchain_openai import OpenAIEmbeddings   # or AnthropicEmbeddings(model="voyage-3")

embeddings_model = OpenAIEmbeddings(model="text-embedding-3-small")

# embed_query: single string → list of floats
vec_a = embeddings_model.embed_query("The dog ran fast.")
vec_b = embeddings_model.embed_query("The canine sprinted quickly.")
vec_c = embeddings_model.embed_query("I love pizza.")

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    return dot / (mag_a * mag_b)

print(f"dog vs canine: {cosine_similarity(vec_a, vec_b):.3f}")  # high: ~0.95
print(f"dog vs pizza:  {cosine_similarity(vec_a, vec_c):.3f}")  # low:  ~0.70

# embed_documents: list of strings → list of vectors (batch, more efficient)
docs = ["LangChain is a framework.", "LangGraph builds agent workflows."]
doc_vecs = embeddings_model.embed_documents(docs)
print(f"Embedded {len(doc_vecs)} documents, each with {len(doc_vecs[0])} dimensions.")
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Using `embed_documents()` for a query | Works but is slightly inefficient — `embed_query()` may use a different (optimised) prompt for queries | Use `embed_query()` for queries, `embed_documents()` for documents |
| Re-embedding documents every time the app starts | Slow startup and unnecessary API cost | Store embeddings in a persistent vector store (Chroma, Pinecone) and load at startup |
| Using different embedding models for ingestion and query time | Vectors are in different spaces — semantic similarity breaks completely | Always use the same embedding model at ingest time and query time |

**What to learn next:**
1. `RAG` — how embeddings power semantic retrieval
2. `/rag` — scaffold a complete RAG pipeline using embeddings

---

### streaming

**One-line definition:**
Streaming is the ability to receive LLM output token by token as it is generated,
rather than waiting for the entire response to be ready, using `.stream()` for
sync contexts or `.astream()` for async contexts.

**Analogy:**
Streaming is like watching a live sports broadcast instead of waiting for the
highlight reel: you see each event as it happens. Without streaming, you stare
at a spinner for 5 seconds and then the full response appears at once. With
streaming, you see each word as Claude writes it — dramatically improving the
perceived speed of your app.

**When you need it:**
- Any user-facing interface where you want responses to feel instant instead of
  making the user wait for the full completion.
- Long responses (summaries, essays, code) where displaying partial output is
  better than showing nothing.
- Real-time dashboards or CLI tools that update as the agent reasons.

**Minimal code:**
```python
# streaming_example.py
# Show sync and async streaming for an LCEL chain.
import asyncio
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

chain = (
    ChatPromptTemplate.from_template("Write a short poem about {topic}.")
    | ChatAnthropic(model="claude-sonnet-4-6", temperature=0.7)
    | StrOutputParser()
)

# Synchronous streaming — works anywhere
print("=== sync stream ===")
for chunk in chain.stream({"topic": "LangGraph"}):
    print(chunk, end="", flush=True)   # each chunk is a string fragment
print()

# Async streaming — use in FastAPI, Jupyter, or any async context
async def async_example():
    print("\n=== async stream ===")
    async for chunk in chain.astream({"topic": "state machines"}):
        print(chunk, end="", flush=True)
    print()

asyncio.run(async_example())

# LangGraph: stream_mode="messages" for token-level output from agents
# from langgraph.graph import MessagesState
# async for chunk, meta in app.astream(input, config, stream_mode="messages"):
#     if hasattr(chunk, "content") and chunk.content:
#         print(chunk.content, end="", flush=True)
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Using a `return` statement in a custom transform mid-chain | Streaming breaks at that step — the transform accumulates the full input before passing it on | Replace `return` with `yield` to create a generator that streams through |
| Calling `chain.stream()` inside an async function | Event loop may block | Use `chain.astream()` in async contexts |
| Using `stream_mode="updates"` when you want token-level output in LangGraph | You get node-level dicts, not tokens | Use `stream_mode="messages"` for token-level streaming in LangGraph |

**What to learn next:**
1. `LCEL` — the full pipe model that makes `.stream()` automatic
2. `StateGraph` — streaming through LangGraph agents
3. `/lc-lcel` — streaming patterns section (Part 6)

---

### LangSmith

**One-line definition:**
LangSmith is Anthropic-agnostic observability platform for LangChain
applications that automatically records every LLM call, tool invocation, and
retrieval step — their inputs, outputs, token counts, latency, and cost — so
you can debug, evaluate, and improve your app.

**Analogy:**
LangSmith is like a flight data recorder ("black box") for your LLM app: every
time Claude makes a decision, calls a tool, or retrieves a document, LangSmith
records exactly what went in and what came out. When something goes wrong you
don't have to guess — you open LangSmith and see the exact prompt, the exact
response, and the exact chain of events that led to the bad output.

**When you need it:**
- Any time you build with LangChain — LangSmith is free and takes 2 lines to
  enable. The cost of not having it is debugging blind.
- Debugging: "why did the agent give that wrong answer?" — trace shows you the
  exact prompt and context it received.
- Evaluation: compare two versions of a prompt by running them over a test
  dataset and measuring quality metrics.

**Minimal code:**
```python
# langsmith_example.py
# LangSmith traces automatically — zero code required beyond env vars.
from dotenv import load_dotenv
load_dotenv()
# That's all the code you need for LangSmith. The rest just verifies it works.

import os
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

# LANGSMITH_TRACING and LANGSMITH_API_KEY must be in your .env
if os.getenv("LANGSMITH_TRACING") != "true":
    print("Warning: LANGSMITH_TRACING is not set to 'true' — tracing disabled.")
    print("Add these to your .env:")
    print("  LANGSMITH_TRACING=true")
    print("  LANGSMITH_API_KEY=your_key_from_smith.langchain.com")
    print("  LANGSMITH_PROJECT=my-project")

chain = (
    ChatPromptTemplate.from_template("What is {topic} in one sentence?")
    | ChatAnthropic(model="claude-sonnet-4-6", temperature=0)
    | StrOutputParser()
)

# This call is automatically traced — no additional instrumentation needed
result = chain.invoke(
    {"topic": "LangSmith"},
    # Optional: add metadata for filtering in the UI
    config={"run_name": "lc-explain demo", "tags": ["demo"]},
)
print(result)

project = os.getenv("LANGSMITH_PROJECT", "default")
print(f"\nView trace at: https://smith.langchain.com/projects/{project}")
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Setting `LANGSMITH_TRACING` after importing LangChain | Tracing does not activate — LangChain reads this env var at import time | Call `load_dotenv()` BEFORE any `from langchain...` import |
| Not setting `LANGSMITH_PROJECT` | All traces go to the "default" project — hard to find your runs | Set `LANGSMITH_PROJECT=your-project-name` in `.env` |
| Confusing LangSmith with logging | LangSmith is not a log aggregator — it is a structured trace store with evaluation tools | Use LangSmith for LLM-specific observability; use your regular logger for app-level events |

**What to learn next:**
1. `LCEL` — every LCEL chain is automatically traced by LangSmith
2. `StateGraph` — every LangGraph node execution is traced
3. `/lc-monitor` — the langchain-lab monitoring and evaluation skill

---

### supervisors

**One-line definition:**
A supervisor is a LangGraph pattern where one orchestrator agent routes tasks
to specialised sub-agents (researcher, analyst, writer) by calling them as
tools — each specialist has its own tools, system prompt, and context.

**Analogy:**
A supervisor is like a project manager who never does the work themselves: they
receive the task, decide which team member (specialist agent) is best suited
for the next step, hand it off, receive the result, then decide who should act
next. They keep doing this until the project is complete and they can deliver
the final output to the client.

**When you need it:**
- Your task requires more than ~5 tools and a single agent becomes unwieldy.
- Different sub-tasks need different system prompts (a researcher needs web
  access; a writer needs writing guidelines; an analyst needs data tools).
- You want specialists to be testable and replaceable independently of the
  supervisor.

**Minimal code:**
```python
# supervisors_example.py
# A supervisor that routes between a researcher and a writer.
# pip install langgraph-supervisor
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from langgraph_supervisor import create_supervisor

model = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

@tool
def search(query: str) -> str:
    """Search the web for information."""
    return f"[Search results for: {query}]"   # replace with real search

@tool
def draft_document(title: str, content: str) -> str:
    """Save a drafted document."""
    return f"Document '{title}' saved ({len(content)} chars)."

# Each agent has its own name, tools, and system prompt
researcher = create_react_agent(
    model, tools=[search], name="researcher",
    prompt="You are a researcher. Use search to find accurate information.",
)
writer = create_react_agent(
    model, tools=[draft_document], name="writer",
    prompt="You are a writer. Turn research into clear, well-structured documents.",
)

# create_supervisor wraps both agents and adds a routing LLM
workflow = create_supervisor(
    agents=[researcher, writer], model=model,
    prompt="Route tasks: use researcher for facts, writer for final documents.",
)

app = workflow.compile(checkpointer=MemorySaver())
config = {"configurable": {"thread_id": "supervisor-demo"}}
result = app.invoke(
    {"messages": [("user", "Research LangGraph and write a 2-sentence summary.")]},
    config=config,
)
print(result["messages"][-1].content)
```

**Mistakes:**
| Mistake | What goes wrong | Fix |
|---|---|---|
| Agent `name` parameter doesn't match the noun the supervisor prompt uses | Supervisor never routes to that agent | Make sure `name="researcher"` matches "use researcher for..." in the supervisor prompt |
| Giving a specialist tools that belong to another specialist | Specialists call the wrong tools; results are inconsistent | Each specialist should have only the tools relevant to their domain |
| Not compiling with a checkpointer | Conversation history is lost between turns | Add `checkpointer=MemorySaver()` to `.compile()` |

**What to learn next:**
1. `StateGraph` — the underlying graph that supervisors are built on
2. `checkpointing` — add memory to a supervisor workflow
3. `/lc-agent` — the supervisor pattern scaffold (Pattern 2)

---

## Unknown Concept Handler

If the concept cannot be matched to any catalog entry or close fuzzy match:

```
I don't recognise "[concept]" as a LangChain or LangGraph concept.

Here are some possibilities:

1. You may have a typo. Did you mean one of these?
   [List up to 3 closest matches from the catalog, e.g. if "stategaph" →
   "StateGraph", "stateful graph" → "StateGraph"]

2. It may be a concept from a specific integration (e.g. a vector store,
   a specific model provider, a third-party library). If so, tell me more
   and I can explain the LangChain wrapper for it.

3. It may be a general LLM concept (e.g. "temperature", "tokens",
   "context window") not specific to LangChain. Type the concept and I
   will explain it in the context of how LangChain uses it.

Which of these is closest to what you meant?
Or type a different concept to search for.
```

If the user clarifies and the concept is identifiable, generate a best-effort
explanation using the six-section template even if the concept is not in the
catalog. Use:
- Knowledge of the LangChain/LangGraph ecosystem
- Patterns from similar catalog entries as structural templates
- The same "plain English, analogy, when to use, code, mistakes, next steps"
  format without exception

---

## Format Rules

1. Always output all six sections in the exact order defined above.
2. Never skip a section. If a section has less to say for a simple concept,
   keep it brief but do not omit it.
3. Code examples must be 10–20 lines (not counting blank lines and comments).
4. Code examples must be runnable: correct imports, `load_dotenv()` first,
   working `print()` at the end.
5. Use `claude-sonnet-4-6` as the model in all code examples.
6. The analogy paragraph must start with "[Concept] is like...".
7. The "What to learn next" section must include at least one `/lc-...` or
   `/rag` skill reference.
8. The project file reference section is only shown if `PROJECT_HITS` is
   non-empty.
9. Never use jargon in sections 1 and 2 without defining it inline.
10. Tables in the "Common beginner mistakes" section must have exactly three
    columns: Mistake, What goes wrong, Fix.

---

## Concept Index (for fuzzy matching)

Use this index to resolve alternate spellings, abbreviations, and synonyms.

| User input | Canonical concept |
|---|---|
| stategraph, state graph, state-graph, graph | StateGraph |
| checkpoint, checkpoints, memory, persistence, save state, thread memory | checkpointing |
| lcel, pipe, \|, chain, runnable, expression language | LCEL |
| rag, retrieval, retrieval augmented, question answering over docs, chat with pdf | RAG |
| toolnode, tool node, tool executor, tool dispatch | ToolNode |
| messagesstate, messages state, message state, conversation state | MessagesState |
| reducer, reducers, annotated, operator.add, add_messages, state merge | reducers |
| send, send api, fan-out, fanout, parallel send, map reduce | Send API |
| interrupt, hitl, human in the loop, human approval, pause graph | interrupt |
| runnablepassthrough, passthrough, pass through, assign | RunnablePassthrough |
| runnableparallel, parallel, run in parallel, concurrent chains | RunnableParallel |
| embedding, embeddings, vectors, vector, semantic search, embed | embeddings |
| stream, streaming, astream, token by token, real time output | streaming |
| langsmith, tracing, traces, observability, monitoring, trace | LangSmith |
| supervisor, supervisors, multi agent, multiagent, orchestrator | supervisors |
