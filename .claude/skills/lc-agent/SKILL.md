---
name: lc-agent
description: Use when designing or scaffolding any LangChain/LangGraph agent — ReAct, supervisor multi-agent, plan-and-execute, reflection/self-critique, parallel fan-out, or event-driven agents (webhooks, crons, queues, Slack bots). Triggered by requests to build an AI agent, add tools to an LLM, chain LLM calls, create a multi-agent system, or questions about LangGraph patterns, checkpointing, streaming, human-in-the-loop, webhook triggers, background runs, or idempotent agent execution.
---

# lc-agent — LangChain/LangGraph Agent Designer

## Overview

This skill designs and scaffolds LangGraph agent patterns. It teaches as it builds: every concept is explained the first time it appears. Default model is `claude-sonnet-4-6` via `langchain-anthropic`. All patterns include LangSmith tracing and a test section.

**Core mental model:** A LangGraph agent is a directed graph where **nodes** are Python functions and **edges** are the transitions between them. State flows through the graph and is updated at each node.

---

## Skill Flow

Run these questions before generating code. Skip any that are obviously answered by context.

1. **Which pattern?** (See pattern guide below — present the table, let the user choose)
2. **What tools does the agent need?** (web search, code execution, file ops, database, custom API)
3. **Memory across sessions?** Yes → `PostgresSaver`. No / dev only → `MemorySaver`
4. **Human approval required?** Yes → add `interrupt()` node
5. **Stream output in real time?** Yes → show `astream()` pattern
6. **Event-driven trigger?** Yes → Pattern 6 — ask: webhook / cron / queue / Slack bot?
7. **Scaffold code for the chosen pattern**
8. **Add LangSmith tracing** (always — one env var)
9. **Show how to test it**

---

## Pattern Selection Guide

| Pattern | Use when | Complexity |
|---|---|---|
| ReAct | Single agent with tools, most common starting point | Low |
| Supervisor | Multiple specialized agents, each owns a domain | Medium |
| Plan-and-Execute | Long multi-step tasks needing a plan upfront | Medium |
| Reflection | Output quality matters, needs self-critique loop | Medium |
| Parallel (Send API) | Same operation over many items simultaneously | Medium |
| Event-Driven | Agent triggered by webhook, cron, queue, or Slack — not chat | Medium-High |

**Decision flowchart:**

```
Is the agent triggered by an external event (webhook/cron/queue/Slack)?
  YES → Event-Driven (Pattern 6)
  NO  → Does the task require specialized sub-domains?
          YES → Supervisor
          NO  → Does the task need a multi-step plan made upfront?
                  YES → Plan-and-Execute
                  NO  → Does the output need iterative quality improvement?
                          YES → Reflection
                          NO  → Are you processing many items in parallel?
                                  YES → Parallel (Send API)
                                  NO  → ReAct  ← start here
```

---

## Environment Setup

```bash
pip install langgraph langchain-anthropic langgraph-checkpoint-postgres psycopg
```

```python
# .env
ANTHROPIC_API_KEY="sk-ant-..."
LANGSMITH_API_KEY="ls__..."        # get free key at smith.langchain.com
LANGSMITH_TRACING="true"
LANGSMITH_PROJECT="my-agent"
DATABASE_URL="postgresql://..."    # only needed for PostgresSaver
```

```python
# Always load at top of every agent file
from dotenv import load_dotenv
load_dotenv()
```

---

## Pattern 1 — ReAct Agent

**Best for:** Most agents. A single LLM that decides which tools to call, calls them, observes results, and loops until it has an answer.

**Mental model:** Reason → Act → Observe → Reason → Act → ... → Answer

### Concept: MessagesState

`MessagesState` is a built-in LangGraph state type. Its `messages` field uses the `add_messages` reducer, which **appends** new messages rather than replacing the list. This is the right choice for any conversational or tool-using agent.

```python
# MessagesState is equivalent to:
from typing import Annotated
from langgraph.graph import add_messages
from typing_extensions import TypedDict

class MessagesState(TypedDict):
    messages: Annotated[list, add_messages]
```

### Concept: ToolNode

`ToolNode` is a prebuilt node that inspects the last message for `tool_calls`, executes each tool, and returns `ToolMessage` results. You never write this loop yourself.

### Concept: Checkpointing

A **checkpointer** saves state after every node. This gives you:
- Conversation memory (same `thread_id` → same history)
- Human-in-the-loop (pause and resume)
- Fault tolerance (resume after crash)

Use `MemorySaver` during development (in-process, lost on restart). Use `PostgresSaver` in production (persistent, survives restarts).

### Complete ReAct Implementation

```python
"""
react_agent.py — Complete ReAct agent with tools, memory, and streaming.

Concepts introduced:
  - MessagesState: append-only message list
  - ToolNode: prebuilt tool executor
  - should_continue: conditional routing function
  - MemorySaver: dev checkpointer (swap for PostgresSaver in prod)
  - astream(): async token-level streaming
"""
import asyncio
from typing import Literal

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

load_dotenv()

# --- 1. Define tools ---
# A tool is any Python function decorated with @tool.
# The docstring becomes the tool description the LLM sees — write it clearly.

@tool
def search_web(query: str) -> str:
    """Search the web for current information about a topic."""
    # Replace with real search (e.g. Tavily, SerpAPI)
    return f"Search results for '{query}': [placeholder — wire up real search here]"

@tool
def calculate(expression: str) -> str:
    """Evaluate a mathematical expression. Example: '2 + 2 * 3'"""
    try:
        result = eval(expression, {"__builtins__": {}})  # noqa: S307
        return str(result)
    except Exception as e:
        return f"Error: {e}"

tools = [search_web, calculate]

# --- 2. Create LLM with tools bound ---
# bind_tools() tells the LLM the tool schemas so it can call them.
llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)
llm_with_tools = llm.bind_tools(tools)

# --- 3. Define nodes ---
# Each node is a function: State -> dict of updates.

def agent_node(state: MessagesState) -> dict:
    """The LLM decides: answer directly OR call a tool."""
    response = llm_with_tools.invoke(state["messages"])
    # Returning {"messages": [response]} triggers add_messages reducer,
    # which appends the response to the existing list.
    return {"messages": [response]}

# ToolNode handles all tool execution. Pass it the same tools list.
tool_node = ToolNode(tools)

# --- 4. Routing logic ---
# This function inspects the last message and decides the next step.

def should_continue(state: MessagesState) -> Literal["tools", "__end__"]:
    """Route: if the LLM called a tool → execute it. Otherwise → done."""
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "tools"
    return END

# --- 5. Build the graph ---
graph = StateGraph(MessagesState)

graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)

graph.add_edge(START, "agent")
graph.add_conditional_edges(
    "agent",           # source node
    should_continue,   # routing function
    {
        "tools": "tools",   # tool_calls present → execute tools
        END: END,           # no tool_calls → done
    }
)
graph.add_edge("tools", "agent")   # after tools, always go back to agent

# --- 6. Compile with checkpointer ---
# MemorySaver keeps state in memory. Fine for development.
# For production: from langgraph.checkpoint.postgres import PostgresSaver
checkpointer = MemorySaver()
app = graph.compile(checkpointer=checkpointer)

# --- 7. Run with streaming ---
# thread_id scopes the conversation. Same ID = shared history.

async def run_agent(user_message: str, thread_id: str = "default") -> str:
    config = {"configurable": {"thread_id": thread_id}}
    final_response = ""

    # astream() yields events as they happen. stream_mode="messages" gives
    # token-level output — each chunk is (message_chunk, metadata).
    async for event in app.astream(
        {"messages": [{"role": "user", "content": user_message}]},
        config=config,
        stream_mode="messages",
    ):
        # event is a tuple: (AIMessageChunk or ToolMessage, metadata)
        if hasattr(event, "__iter__") and len(event) == 2:
            chunk, meta = event
            if hasattr(chunk, "content") and chunk.content:
                print(chunk.content, end="", flush=True)
                final_response += str(chunk.content)

    print()  # newline after streaming completes
    return final_response

# --- 8. Human-in-the-loop variant ---
# Add interrupt_before=["tools"] to pause before any tool execution.
# The graph will pause, return control to you, and wait for a Command(resume=...).
app_with_approval = graph.compile(
    checkpointer=checkpointer,
    interrupt_before=["tools"],   # pause before executing any tool
)

async def run_with_approval(user_message: str, thread_id: str = "hitl-demo"):
    from langgraph.types import Command

    config = {"configurable": {"thread_id": thread_id}}
    state = await app_with_approval.ainvoke(
        {"messages": [{"role": "user", "content": user_message}]},
        config=config,
    )

    # Graph paused — show what tool it wants to call
    last = state["messages"][-1]
    if last.tool_calls:
        print(f"\nAgent wants to call: {last.tool_calls[0]['name']}")
        print(f"Arguments: {last.tool_calls[0]['args']}")
        approval = input("Approve? (y/n): ")

        if approval.lower() == "y":
            # Resume — pass None to continue normally
            result = await app_with_approval.ainvoke(
                Command(resume=None), config=config
            )
            return result
        else:
            print("Tool call rejected.")
            return state

# --- 9. Production: PostgresSaver ---
# Uncomment to use persistent storage:
#
# from psycopg import AsyncConnection
# from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
#
# async def create_production_app():
#     conn = await AsyncConnection.connect(os.environ["DATABASE_URL"])
#     checkpointer = AsyncPostgresSaver(conn)
#     await checkpointer.setup()   # creates tables on first run
#     return graph.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    asyncio.run(run_agent("What is 15% of 847, and what's the weather in Paris?"))
```

### Testing a ReAct Agent

```python
# test_react_agent.py
import asyncio
from react_agent import app

def test_tool_calling():
    """Verify the agent calls tools when needed."""
    config = {"configurable": {"thread_id": "test-tools"}}
    result = app.invoke(
        {"messages": [{"role": "user", "content": "What is 42 * 17?"}]},
        config=config,
    )
    messages = result["messages"]
    # Should have: HumanMessage, AIMessage (tool call), ToolMessage, AIMessage (final)
    assert any(m.type == "tool" for m in messages), "Expected a tool message"
    assert "714" in messages[-1].content

def test_conversation_memory():
    """Verify thread_id keeps conversation history."""
    config = {"configurable": {"thread_id": "test-memory"}}
    app.invoke(
        {"messages": [{"role": "user", "content": "My name is Alice."}]},
        config=config,
    )
    result = app.invoke(
        {"messages": [{"role": "user", "content": "What is my name?"}]},
        config=config,
    )
    assert "Alice" in result["messages"][-1].content

def test_no_tools_needed():
    """Verify the agent answers directly when no tools required."""
    config = {"configurable": {"thread_id": "test-direct"}}
    result = app.invoke(
        {"messages": [{"role": "user", "content": "What is the capital of France?"}]},
        config=config,
    )
    assert "Paris" in result["messages"][-1].content

if __name__ == "__main__":
    test_tool_calling()
    test_conversation_memory()
    test_no_tools_needed()
    print("All tests passed.")
```

---

## Pattern 2 — Supervisor Multi-Agent

**Best for:** Tasks that decompose into distinct domains (research vs. math vs. writing). Each specialist is a full ReAct agent. The supervisor LLM routes to the right specialist.

**When to use over monolithic ReAct:**
- You need more than ~5 tools (tool lists become unwieldy)
- Domain specialists need different system prompts or model settings
- You want each specialist to have its own conversation context
- You need to nest teams (research team + writing team under one supervisor)

**Mental model:** The supervisor is a ReAct agent whose "tools" are other agents.

### Complete Supervisor Implementation

```python
"""
supervisor_agent.py — Multi-agent supervisor pattern.

Uses langgraph-supervisor (pip install langgraph-supervisor).
Each specialist is a create_react_agent with its own tools and prompt.
The supervisor LLM decides which specialist to call.
"""
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from langgraph_supervisor import create_supervisor

load_dotenv()

model = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

# --- 1. Define specialist tools ---

@tool
def web_search(query: str) -> str:
    """Search the web for current news and facts."""
    # Wire up Tavily, SerpAPI, etc.
    return f"[Web results for: {query}]"

@tool
def fetch_url(url: str) -> str:
    """Fetch and return the text content of a web page."""
    import urllib.request
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return resp.read().decode()[:3000]

@tool
def python_repl(code: str) -> str:
    """Execute Python code and return stdout. Use for data analysis and math."""
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, {})  # noqa: S102
    return buf.getvalue() or "(no output)"

@tool
def write_file(filename: str, content: str) -> str:
    """Write content to a file."""
    with open(filename, "w") as f:
        f.write(content)
    return f"Written {len(content)} chars to {filename}"

# --- 2. Create specialist agents ---
# Each agent gets:
#   - name: used by supervisor to route ("call research_agent to...")
#   - prompt: specialization instructions
#   - tools: only the tools relevant to its domain

research_agent = create_react_agent(
    model=model,
    tools=[web_search, fetch_url],
    name="research_agent",
    prompt=(
        "You are an expert researcher. Use web_search and fetch_url to gather "
        "accurate, current information. Always cite your sources. "
        "Do NOT perform calculations — delegate those to the analyst."
    ),
)

analyst_agent = create_react_agent(
    model=model,
    tools=[python_repl],
    name="analyst_agent",
    prompt=(
        "You are a data analyst. Use python_repl to perform calculations, "
        "data processing, and statistical analysis. "
        "Do NOT do web searches — rely on data provided to you."
    ),
)

writer_agent = create_react_agent(
    model=model,
    tools=[write_file],
    name="writer_agent",
    prompt=(
        "You are a professional writer. Synthesize information into clear, "
        "well-structured prose. Use write_file to save deliverables. "
        "Do NOT search the web or run code."
    ),
)

# --- 3. Create supervisor ---
# The supervisor sees all agent names and their descriptions.
# It routes by calling agents as tools: transfer_to_research_agent(task="...")

workflow = create_supervisor(
    agents=[research_agent, analyst_agent, writer_agent],
    model=model,
    prompt=(
        "You are a project supervisor coordinating a research, analysis, and writing team. "
        "Break complex tasks into subtasks and route each to the right specialist:\n"
        "- research_agent: web searches, fact-finding, current events\n"
        "- analyst_agent: math, data processing, Python computation\n"
        "- writer_agent: drafting reports, summaries, structured documents\n"
        "Synthesize specialist outputs into a final coherent answer."
    ),
    # output_mode="last_message" (default) returns only the final message
    # output_mode="full_history" returns all messages including specialist exchanges
    output_mode="last_message",
)

app = workflow.compile(checkpointer=MemorySaver())

# --- 4. Multi-level hierarchy example ---
# Compile sub-teams first, then pass compiled graphs as agents to top-level supervisor.
#
# research_team = create_supervisor(
#     [research_agent, analyst_agent], model=model,
#     supervisor_name="research_supervisor"
# ).compile(name="research_team")
#
# writing_team = create_supervisor(
#     [writer_agent], model=model,
#     supervisor_name="writing_supervisor"
# ).compile(name="writing_team")
#
# top_supervisor = create_supervisor(
#     [research_team, writing_team], model=model
# ).compile()


if __name__ == "__main__":
    config = {"configurable": {"thread_id": "supervisor-demo"}}
    result = app.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Research the current price of gold per ounce, calculate how much "
                        "100 ounces would be worth, then write a brief investment summary."
                    ),
                }
            ]
        },
        config=config,
    )
    print(result["messages"][-1].content)
```

### Testing a Supervisor

```python
# test_supervisor.py
from supervisor_agent import app

def test_routing_to_researcher():
    config = {"configurable": {"thread_id": "test-route-1"}}
    result = app.invoke(
        {"messages": [{"role": "user", "content": "Search for the latest news on quantum computing."}]},
        config=config,
    )
    # Should have invoked research_agent (check message history for tool calls)
    messages = result["messages"]
    agent_calls = [m for m in messages if hasattr(m, "name") and m.name == "research_agent"]
    assert len(agent_calls) > 0, "Expected research_agent to be called"

def test_routing_to_analyst():
    config = {"configurable": {"thread_id": "test-route-2"}}
    result = app.invoke(
        {"messages": [{"role": "user", "content": "Calculate compound interest on $10,000 at 5% for 10 years."}]},
        config=config,
    )
    messages = result["messages"]
    analyst_calls = [m for m in messages if hasattr(m, "name") and m.name == "analyst_agent"]
    assert len(analyst_calls) > 0, "Expected analyst_agent to be called"
```

---

## Pattern 3 — Plan-and-Execute

**Best for:** Long, multi-step tasks where you need to think ahead (e.g., "research a topic and write a 10-page report"). Unlike ReAct which is reactive, Plan-and-Execute creates an explicit plan first.

**Mental model:** Planner → Executor → Replanner → Executor → ... → Done

**Key insight:** The replanner can revise the remaining steps based on what was learned — it handles "I found out X so I no longer need step 4."

### Concept: State with a plan list

The state carries the full plan as a list of steps. The executor marks steps complete. The replanner may rewrite future steps. A `response` field signals when we're done.

```python
"""
plan_execute_agent.py — Plan-and-Execute pattern.

State:
  input: str            — the original user request
  plan: list[str]       — ordered list of steps (mutated by replanner)
  past_steps: list      — (step, result) pairs of completed work
  response: str | None  — set when done, signals termination
"""
import asyncio
from typing import Union

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import Annotated, TypedDict

load_dotenv()

llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)

# --- 1. State definition ---

def add_tuples(a: list, b: list) -> list:
    """Reducer: append new (step, result) pairs."""
    return a + b

class PlanExecuteState(TypedDict):
    input: str
    plan: list[str]                              # remaining steps
    past_steps: Annotated[list, add_tuples]      # completed (step, result) pairs
    response: str | None                         # set when done

# --- 2. Structured output schemas ---
# Using Pydantic models + with_structured_output() ensures the LLM
# returns data we can reliably parse rather than free-form text.

class Plan(BaseModel):
    """An ordered list of steps to complete the task."""
    steps: list[str] = Field(description="Ordered list of steps, each actionable")

class Response(BaseModel):
    """Final response when the task is complete."""
    response: str

class Act(BaseModel):
    """The replanner's decision: continue with a new plan OR respond."""
    action: Union[Response, Plan] = Field(
        description="Either a final Response (if done) or a revised Plan (if more steps needed)"
    )

# --- 3. Tools ---

@tool
def web_search(query: str) -> str:
    """Search the web for information."""
    return f"[Results for: {query}]"

@tool
def write_document(title: str, content: str) -> str:
    """Write a document to disk."""
    filename = title.replace(" ", "_").lower() + ".md"
    with open(filename, "w") as f:
        f.write(f"# {title}\n\n{content}")
    return f"Document saved as {filename}"

tools = [web_search, write_document]
tools_by_name = {t.name: t for t in tools}

# --- 4. Planner node ---

planner_prompt = ChatPromptTemplate.from_messages([
    ("system", (
        "You are an expert planner. Given a task, create an ordered list of concrete steps. "
        "Each step should be independently executable. Be specific about what information "
        "to find and what to produce. Aim for 3-7 steps."
    )),
    ("human", "{input}"),
])

planner = planner_prompt | llm.with_structured_output(Plan)

def plan_node(state: PlanExecuteState) -> dict:
    """Generate the initial plan."""
    plan = planner.invoke({"input": state["input"]})
    return {"plan": plan.steps}

# --- 5. Executor node ---
# Executes only the NEXT step (first item in plan list).

executor_prompt = ChatPromptTemplate.from_messages([
    ("system", (
        "You are an executor. Complete the given step using available tools. "
        "Be thorough and produce a clear result summary.\n\n"
        "Available tools: {tool_descriptions}"
    )),
    ("human", (
        "Task: {input}\n\n"
        "Completed steps:\n{past_steps}\n\n"
        "Current step to execute: {current_step}"
    )),
])

llm_with_tools = llm.bind_tools(tools)

def execute_node(state: PlanExecuteState) -> dict:
    """Execute the next step in the plan."""
    current_step = state["plan"][0]
    tool_descriptions = "\n".join(f"- {t.name}: {t.description}" for t in tools)
    past_summary = "\n".join(f"Step: {s}\nResult: {r}" for s, r in state["past_steps"])

    # Simple execution: invoke LLM, handle one round of tool calls
    messages = executor_prompt.format_messages(
        input=state["input"],
        tool_descriptions=tool_descriptions,
        past_steps=past_summary or "None yet",
        current_step=current_step,
    )
    response = llm_with_tools.invoke(messages)

    result_text = current_step  # default: step description if no tools called
    if response.tool_calls:
        for tc in response.tool_calls:
            tool_result = tools_by_name[tc["name"]].invoke(tc["args"])
            result_text = str(tool_result)
    elif response.content:
        result_text = str(response.content)

    return {
        "past_steps": [(current_step, result_text)],
        "plan": state["plan"][1:],  # remove the completed step
    }

# --- 6. Replanner node ---
# Decides: are we done? If not, revise the remaining plan.

replan_prompt = ChatPromptTemplate.from_messages([
    ("system", (
        "You are a replanner. Review progress and decide:\n"
        "1. If the original task is complete → return a Response with the final answer.\n"
        "2. If more work is needed → return a revised Plan with remaining steps only.\n"
        "   (Do NOT re-list already completed steps.)"
    )),
    ("human", (
        "Original task: {input}\n\n"
        "Completed steps and results:\n{past_steps}\n\n"
        "Remaining plan:\n{plan}"
    )),
])

replanner = replan_prompt | llm.with_structured_output(Act)

def replan_node(state: PlanExecuteState) -> dict:
    """Evaluate progress. Either revise the plan or produce final answer."""
    past_summary = "\n".join(f"- {s}: {r}" for s, r in state["past_steps"])
    plan_summary = "\n".join(f"- {s}" for s in state["plan"])

    result = replanner.invoke({
        "input": state["input"],
        "past_steps": past_summary,
        "plan": plan_summary or "(all steps completed)",
    })

    if isinstance(result.action, Response):
        return {"response": result.action.response}
    else:
        return {"plan": result.action.steps}

# --- 7. Routing ---

def should_end(state: PlanExecuteState) -> str:
    """Stop if we have a response OR no steps remain."""
    if state.get("response") or not state.get("plan"):
        return END
    return "executor"

# --- 8. Build graph ---

graph = StateGraph(PlanExecuteState)
graph.add_node("planner", plan_node)
graph.add_node("executor", execute_node)
graph.add_node("replanner", replan_node)

graph.add_edge(START, "planner")
graph.add_edge("planner", "executor")
graph.add_edge("executor", "replanner")
graph.add_conditional_edges("replanner", should_end, {"executor": "executor", END: END})

app = graph.compile(checkpointer=MemorySaver())


if __name__ == "__main__":
    config = {"configurable": {"thread_id": "plan-execute-demo"}}
    result = app.invoke(
        {"input": "Research recent advances in fusion energy and write a 3-paragraph summary."},
        config=config,
    )
    print(result.get("response") or result["past_steps"][-1][1])
```

---

## Pattern 4 — Reflection / Self-Critique Agent

**Best for:** When output quality matters and one pass isn't enough. The agent generates, critiques its own work, then revises. Repeat until quality threshold met or max iterations reached.

**When to use:**
- Writing (essays, reports, code)
- Analysis (the first take is often shallow)
- Any output that benefits from a second opinion

**Key design decision:** What is the termination condition?
- Score threshold: critic assigns 1-10, stop when >= 8
- Max iterations: always stop after N cycles (safest)
- Both: stop at score threshold OR max iterations, whichever comes first

```python
"""
reflection_agent.py — Generator → Critic → Reviser loop.

State tracks:
  - draft: current version of the output
  - critique: the critic's feedback
  - score: quality score (1-10)
  - iteration: current loop count
  - final_output: set when done

Termination: score >= SCORE_THRESHOLD or iteration >= MAX_ITERATIONS
"""
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

load_dotenv()

llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0.3)

MAX_ITERATIONS = 4
SCORE_THRESHOLD = 8   # stop when critic gives >= 8/10

# --- 1. State ---

class ReflectionState(TypedDict):
    task: str               # original request
    draft: str              # current draft
    critique: str           # latest critique
    score: int              # quality score (1-10), 0 = not yet scored
    iteration: int          # current cycle
    final_output: str       # set when done

# --- 2. Critic schema ---

class Critique(BaseModel):
    """Structured critique with a quality score."""
    score: int = Field(ge=1, le=10, description="Quality score from 1 (terrible) to 10 (perfect)")
    strengths: list[str] = Field(description="What the draft does well")
    weaknesses: list[str] = Field(description="Specific flaws to fix")
    suggestions: list[str] = Field(description="Concrete improvement instructions")

# --- 3. Nodes ---

generator_prompt = ChatPromptTemplate.from_messages([
    ("system", (
        "You are an expert writer. Produce a high-quality draft for the given task. "
        "Be thorough, specific, and well-structured."
    )),
    ("human", "Task: {task}"),
])

revision_prompt = ChatPromptTemplate.from_messages([
    ("system", (
        "You are a skilled reviser. Improve the draft based on the critique. "
        "Address every weakness and suggestion. Keep the strengths. "
        "Return ONLY the revised draft — no meta-commentary."
    )),
    ("human", (
        "Task: {task}\n\n"
        "Current draft:\n{draft}\n\n"
        "Critique (score {score}/10):\n"
        "Weaknesses: {weaknesses}\n"
        "Suggestions: {suggestions}"
    )),
])

critic_chain = ChatPromptTemplate.from_messages([
    ("system", (
        "You are a rigorous critic. Evaluate the draft against the task requirements. "
        "Be specific about flaws. Score honestly — reserve 9-10 for truly excellent work."
    )),
    ("human", "Task: {task}\n\nDraft to evaluate:\n{draft}"),
]) | llm.with_structured_output(Critique)


def generate_node(state: ReflectionState) -> dict:
    """Produce initial draft (iteration 0) or revise (iterations 1+)."""
    if state["iteration"] == 0:
        # First pass: generate from scratch
        response = (generator_prompt | llm).invoke({"task": state["task"]})
        return {"draft": response.content, "iteration": 1}
    else:
        # Subsequent passes: revise based on critique
        response = (revision_prompt | llm).invoke({
            "task": state["task"],
            "draft": state["draft"],
            "score": state["score"],
            "weaknesses": "\n".join(f"- {w}" for w in state["critique"].split("|weaknesses|")[1].split("|")[0].split("\n") if w.strip()) if "|weaknesses|" in state["critique"] else state["critique"],
            "suggestions": "",
        })
        return {"draft": response.content, "iteration": state["iteration"] + 1}


def critic_node(state: ReflectionState) -> dict:
    """Evaluate the current draft and return structured critique."""
    critique = critic_chain.invoke({
        "task": state["task"],
        "draft": state["draft"],
    })
    # Pack critique into a single string for state storage
    critique_text = (
        f"Score: {critique.score}/10\n"
        f"Strengths: {'; '.join(critique.strengths)}\n"
        f"Weaknesses: {'; '.join(critique.weaknesses)}\n"
        f"Suggestions: {'; '.join(critique.suggestions)}"
    )
    print(f"  [Iteration {state['iteration']}] Critic score: {critique.score}/10")
    return {"critique": critique_text, "score": critique.score}


def revise_node(state: ReflectionState) -> dict:
    """Revise the draft using the critique."""
    response = (revision_prompt | llm).invoke({
        "task": state["task"],
        "draft": state["draft"],
        "score": state["score"],
        "weaknesses": state["critique"],
        "suggestions": "",
    })
    return {"draft": response.content}


def should_continue_reflection(state: ReflectionState) -> str:
    """Terminate if score is high enough OR max iterations reached."""
    if state["score"] >= SCORE_THRESHOLD:
        print(f"  Quality threshold reached ({state['score']}/10). Done.")
        return "done"
    if state["iteration"] >= MAX_ITERATIONS:
        print(f"  Max iterations ({MAX_ITERATIONS}) reached. Done.")
        return "done"
    print(f"  Score {state['score']}/10 below threshold — revising...")
    return "revise"


def finalize_node(state: ReflectionState) -> dict:
    return {"final_output": state["draft"]}


# --- 4. Build graph ---

graph = StateGraph(ReflectionState)
graph.add_node("generate", generate_node)
graph.add_node("critic", critic_node)
graph.add_node("revise", revise_node)
graph.add_node("finalize", finalize_node)

graph.add_edge(START, "generate")
graph.add_edge("generate", "critic")
graph.add_conditional_edges(
    "critic",
    should_continue_reflection,
    {"revise": "revise", "done": "finalize"},
)
graph.add_edge("revise", "critic")   # loop: revise → critic → revise → ...
graph.add_edge("finalize", END)

app = graph.compile(checkpointer=MemorySaver())


if __name__ == "__main__":
    config = {"configurable": {"thread_id": "reflection-demo"}}
    result = app.invoke(
        {
            "task": "Write a compelling introduction paragraph for an essay on climate change adaptation.",
            "draft": "",
            "critique": "",
            "score": 0,
            "iteration": 0,
            "final_output": "",
        },
        config=config,
    )
    print("\n--- Final Output ---")
    print(result["final_output"])
    print(f"\nAchieved score: {result['score']}/10 in {result['iteration']} iteration(s)")
```

---

## Pattern 5 — Parallel Agent (Send API)

**Best for:** Processing many independent items simultaneously. Classic map-reduce. Examples: summarize 50 documents, analyze 100 customer reviews, score 30 job applications.

**Concept: Send API**

The `Send` API lets a routing function dynamically create multiple graph invocations — one per item — that run in parallel. Each invocation gets its own isolated state. Results are aggregated with a reducer (usually `operator.add`).

```
generate_items → [Send("worker", item_1), Send("worker", item_2), ...] → worker (x N, parallel) → aggregate
```

```python
"""
parallel_agent.py — Send API fan-out / map-reduce pattern.

Use case: given a list of topics, generate a summary for each in parallel,
then pick the best one.

The Send API creates N simultaneous invocations of the worker node.
Each worker gets its own WorkerState. Results aggregate via operator.add.
"""
import operator
from typing import Any

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from typing_extensions import Annotated, TypedDict

load_dotenv()

llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0.5)

# --- 1. State types ---
# OverallState: the main graph's state
# WorkerState: each parallel worker's isolated state

class OverallState(TypedDict):
    topics: list[str]                              # input: list of topics to process
    summaries: Annotated[list[dict], operator.add] # output: aggregated results
    best_summary: dict                             # final selected result

class WorkerState(TypedDict):
    topic: str
    summary: str
    word_count: int

# --- 2. Nodes ---

def prepare_topics(state: OverallState) -> dict:
    """Validate and normalize input topics. Could also fetch them from a DB."""
    topics = [t.strip() for t in state["topics"] if t.strip()]
    return {"topics": topics}

worker_prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a concise summarizer. Write a 2-3 sentence summary."),
    ("human", "Topic: {topic}"),
])

def summarize_topic(state: WorkerState) -> dict:
    """Worker node: runs in parallel, one instance per topic."""
    response = (worker_prompt | llm).invoke({"topic": state["topic"]})
    summary = response.content
    return {
        # Workers write to OverallState.summaries via operator.add reducer
        "summaries": [{"topic": state["topic"], "summary": summary, "length": len(summary)}]
    }

def select_best(state: OverallState) -> dict:
    """Reduce step: pick the most informative summary from all results."""
    if not state["summaries"]:
        return {"best_summary": {}}

    # Simple heuristic: longest summary is most detailed
    # Replace with LLM-based selection for quality-sensitive use cases
    best = max(state["summaries"], key=lambda x: x["length"])

    # LLM-based selection (uncomment for quality-sensitive use):
    # selection_prompt = "Given these summaries, pick the most informative:\n" + \
    #     "\n".join(f"{i}. {s['topic']}: {s['summary']}" for i, s in enumerate(state["summaries"]))
    # response = llm.invoke(selection_prompt)
    # ... parse index from response

    return {"best_summary": best}

# --- 3. Fan-out routing function ---
# This is the key: return a list of Send() objects, one per item.
# Each Send("node_name", state_dict) creates an independent parallel execution.

def fan_out_to_workers(state: OverallState) -> list[Send]:
    """Create one worker invocation per topic. All run simultaneously."""
    return [
        Send(
            "summarize_topic",           # target node name
            {"topic": topic}             # WorkerState for this worker
        )
        for topic in state["topics"]
    ]

# --- 4. Build graph ---

graph = StateGraph(OverallState)
graph.add_node("prepare_topics", prepare_topics)
graph.add_node("summarize_topic", summarize_topic)  # will be called N times in parallel
graph.add_node("select_best", select_best)

graph.add_edge(START, "prepare_topics")
graph.add_conditional_edges(
    "prepare_topics",
    fan_out_to_workers,       # returns list[Send] → parallel execution
    ["summarize_topic"],      # declare possible target nodes
)
graph.add_edge("summarize_topic", "select_best")
graph.add_edge("select_best", END)

app = graph.compile()   # no checkpointer needed for stateless parallel work


if __name__ == "__main__":
    result = app.invoke({
        "topics": [
            "quantum computing",
            "CRISPR gene editing",
            "nuclear fusion energy",
            "large language models",
            "climate adaptation strategies",
        ],
        "summaries": [],
        "best_summary": {},
    })

    print(f"Processed {len(result['summaries'])} topics in parallel.\n")
    for item in result["summaries"]:
        print(f"[{item['topic']}]\n{item['summary']}\n")

    print("--- Best Summary ---")
    print(f"Topic: {result['best_summary']['topic']}")
    print(f"Summary: {result['best_summary']['summary']}")
```

### Testing Parallel Agent

```python
# test_parallel_agent.py
from parallel_agent import app

def test_all_topics_processed():
    topics = ["quantum computing", "CRISPR", "fusion energy"]
    result = app.invoke({"topics": topics, "summaries": [], "best_summary": {}})
    assert len(result["summaries"]) == len(topics), "Expected one summary per topic"

def test_best_summary_selected():
    topics = ["AI", "robotics"]
    result = app.invoke({"topics": topics, "summaries": [], "best_summary": {}})
    assert result["best_summary"].get("topic") in topics
    assert len(result["best_summary"].get("summary", "")) > 0

def test_empty_topics():
    result = app.invoke({"topics": [], "summaries": [], "best_summary": {}})
    assert result["summaries"] == []
```

---

## Pattern 6 — Event-Driven Agents

**Best for:** Production agents that are triggered by the outside world rather than a human typing in a chat box. Webhooks, scheduled crons, message queues, and Slack bots are the most common entry points. Chat-based patterns (Patterns 1–5) assume a human is waiting for a synchronous reply. Event-driven patterns assume no one is watching — the work happens in the background, potentially over hours or days.

**Core shift in thinking:**

| Chat agent | Event-driven agent |
|---|---|
| Caller waits for response | Caller gets `202 Accepted`, work happens async |
| Single `thread_id` per session | `thread_id` derived from the event source (user ID, issue ID, etc.) |
| No idempotency needed | Same event may arrive twice — must be safe to ignore duplicates |
| Seconds-level latency | Minutes-to-days latency acceptable |
| Human types resume input | API call resumes a suspended graph |

**Required extras (beyond base LangGraph):**

```bash
pip install fastapi uvicorn httpx python-dotenv langgraph-sdk slack-bolt
# For LangGraph Platform background runs:
pip install langgraph-sdk
```

```python
# Additional .env keys for event-driven patterns
WEBHOOK_SECRET="your-hmac-secret"          # HMAC signature verification
LANGSMITH_API_KEY="ls__..."                # same as always
LANGGRAPH_API_URL="http://localhost:2024"  # LangGraph Platform server URL
SLACK_BOT_TOKEN="xoxb-..."                 # Slack bolt
SLACK_SIGNING_SECRET="..."                 # Slack request verification
```

---

### Concept: Idempotency via run_id

**The problem:** Webhooks are delivered at-least-once. GitHub, Stripe, and most SaaS platforms will retry a webhook if your server returns 5xx or times out. Your agent might process the same event twice, sending duplicate emails, double-charging customers, or filing duplicate tickets.

**The fix:** Set `run_id` in `RunnableConfig` to a stable identifier derived from the event itself — typically the webhook event ID. LangGraph's checkpointer uses `run_id` to detect whether this exact run already completed. If it did, it returns the cached result without re-executing nodes.

```python
# The run_id must be a valid UUID string.
# Derive it deterministically from the event ID so it is stable across retries.
import uuid

def event_id_to_run_id(event_id: str) -> str:
    """Convert any string event ID to a stable UUID (namespace-based, deterministic)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"webhook-event:{event_id}"))

# Usage in RunnableConfig:
config = {
    "configurable": {"thread_id": f"event-{event_id}"},
    "run_id": event_id_to_run_id(event_id),  # prevents double-processing
}
```

**Why `thread_id` too?** `run_id` is per-run idempotency (was this exact invocation already done?). `thread_id` is per-conversation continuity (what is the ongoing state for this entity?). For events tied to a specific entity — a GitHub issue, a Stripe customer, a Slack user — derive `thread_id` from the entity ID so the agent accumulates context across multiple events for the same entity.

---

### Sub-pattern A — Webhook Trigger (FastAPI + HMAC)

**Use when:** A SaaS service (GitHub, Stripe, Linear, etc.) pushes events to your server via HTTP POST. The agent must process the event asynchronously, and the HTTP response must return within the provider's timeout (usually 5–30 seconds).

**Flow:**
```
Provider → POST /webhook → verify HMAC → enqueue → 202 Accepted
                                                ↓ (background)
                                         LangGraph invoke → result stored
```

```python
"""
webhook_agent.py — FastAPI webhook endpoint with HMAC verification,
BackgroundTasks async execution, and idempotent run_id.

Covers:
  - HMAC-SHA256 signature verification (constant-time comparison)
  - FastAPI BackgroundTasks for fire-and-forget execution
  - Idempotent run_id derived from webhook event ID
  - Per-entity thread_id for accumulated context
  - Result storage and status polling endpoint
"""
import asyncio
import hashlib
import hmac
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

load_dotenv()

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

# ---------------------------------------------------------------------------
# 1. Agent graph — same pattern as ReAct, but invoked by webhook not chat
# ---------------------------------------------------------------------------

@tool
def triage_issue(title: str, body: str) -> str:
    """Classify a GitHub issue by severity and suggest labels."""
    # Replace with real logic — call an internal API, check a database, etc.
    keywords = {"crash": "bug", "error": "bug", "feature": "enhancement", "request": "enhancement"}
    for kw, label in keywords.items():
        if kw.lower() in (title + body).lower():
            return f"Suggested label: {label}. Priority: high if 'crash' in title else medium."
    return "Suggested label: needs-triage. Priority: low."

@tool
def post_github_comment(repo: str, issue_number: int, comment: str) -> str:
    """Post a comment on a GitHub issue."""
    # Replace with real GitHub API call via httpx or PyGithub
    print(f"[MOCK] Posting to {repo}#{issue_number}: {comment[:80]}...")
    return f"Comment posted to {repo}#{issue_number}"

tools = [triage_issue, post_github_comment]
llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0).bind_tools(tools)
tool_node = ToolNode(tools)
checkpointer = MemorySaver()  # use PostgresSaver in production

def agent_node(state: MessagesState) -> dict:
    return {"messages": [llm.invoke(state["messages"])]}

def should_continue(state: MessagesState):
    return "tools" if state["messages"][-1].tool_calls else END

graph = StateGraph(MessagesState)
graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", should_continue)
graph.add_edge("tools", "agent")
agent_app = graph.compile(checkpointer=checkpointer)

# ---------------------------------------------------------------------------
# 2. Idempotency helpers
# ---------------------------------------------------------------------------

def event_to_run_id(event_id: str) -> str:
    """Deterministic UUID from event ID — safe to call multiple times for same event."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"webhook-event:{event_id}"))

def event_to_thread_id(repo: str, issue_number: int) -> str:
    """Stable thread_id per GitHub issue — agent accumulates context across events."""
    return f"github:{repo}:issue:{issue_number}"

# ---------------------------------------------------------------------------
# 3. Result store — replace with Redis or database in production
# ---------------------------------------------------------------------------

run_results: dict[str, dict] = {}  # run_id → {status, result, started_at, finished_at}

# ---------------------------------------------------------------------------
# 4. Background runner
# ---------------------------------------------------------------------------

async def run_agent_background(
    event_id: str,
    repo: str,
    issue_number: int,
    title: str,
    body: str,
) -> None:
    """
    Executes the agent for one webhook event.

    Called via FastAPI BackgroundTasks — runs after the HTTP response is sent.
    The run_id is derived from event_id, making this function idempotent:
    if called twice with the same event_id, the second call finds status='completed'
    in the result store and exits without re-running the agent.
    """
    run_id = event_to_run_id(event_id)
    thread_id = event_to_thread_id(repo, issue_number)

    # Idempotency guard: skip if already processed
    if run_id in run_results and run_results[run_id]["status"] == "completed":
        print(f"[idempotent] run_id {run_id} already completed — skipping duplicate event")
        return

    run_results[run_id] = {"status": "running", "started_at": datetime.utcnow().isoformat()}

    config = {
        "configurable": {"thread_id": thread_id},
        "run_id": run_id,  # LangGraph checkpointer uses this for run-level dedup
        "run_name": f"github-issue-{repo}-{issue_number}",  # visible in LangSmith
    }

    prompt = (
        f"A new GitHub issue was opened in repository '{repo}'.\n"
        f"Issue #{issue_number}: {title}\n\n"
        f"Body:\n{body}\n\n"
        f"Please triage this issue and post an appropriate comment."
    )

    try:
        result = await agent_app.ainvoke(
            {"messages": [HumanMessage(content=prompt)]},
            config=config,
        )
        final_message = result["messages"][-1].content
        run_results[run_id] = {
            "status": "completed",
            "result": final_message,
            "thread_id": thread_id,
            "started_at": run_results[run_id]["started_at"],
            "finished_at": datetime.utcnow().isoformat(),
        }
        print(f"[run {run_id[:8]}] Completed: {final_message[:120]}")
    except Exception as exc:
        run_results[run_id] = {
            "status": "failed",
            "error": str(exc),
            "started_at": run_results[run_id]["started_at"],
            "finished_at": datetime.utcnow().isoformat(),
        }
        print(f"[run {run_id[:8]}] Failed: {exc}")

# ---------------------------------------------------------------------------
# 5. FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Webhook agent server started.")
    yield
    print("Webhook agent server stopped.")

app = FastAPI(title="LangGraph Webhook Agent", lifespan=lifespan)

def verify_github_signature(payload: bytes, signature_header: str | None) -> None:
    """
    Verify GitHub webhook HMAC-SHA256 signature.

    GitHub sends: X-Hub-Signature-256: sha256=<hex-digest>
    We recompute the HMAC over the raw request body and compare using
    hmac.compare_digest() to prevent timing attacks.

    Raises HTTPException(401) if signature is missing or invalid.
    """
    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")

    expected_sig = hmac.new(
        WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    expected = f"sha256={expected_sig}"

    # compare_digest prevents timing attacks — always use this, not ==
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


@app.post("/webhook/github", status_code=202)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),  # GitHub's unique event ID
) -> dict:
    """
    Receive GitHub webhook events.

    Returns 202 immediately — agent runs in background.
    Use GET /runs/{run_id} to poll for completion.

    GitHub requires a response within 10 seconds. BackgroundTasks fires
    after the response is sent, so the HTTP layer is never blocked.
    """
    # 1. Read raw body BEFORE parsing JSON (signature is over raw bytes)
    raw_body = await request.body()

    # 2. Verify signature
    verify_github_signature(raw_body, x_hub_signature_256)

    # 3. Parse event
    import json
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # 4. Only handle issue-opened events
    action = payload.get("action")
    if action != "opened" or "issue" not in payload:
        return {"accepted": False, "reason": f"Ignoring action={action}"}

    issue = payload["issue"]
    repo = payload.get("repository", {}).get("full_name", "unknown/repo")
    issue_number = issue.get("number", 0)
    title = issue.get("title", "")
    body = issue.get("body", "")

    # Use GitHub's delivery ID as event_id for idempotency
    event_id = x_github_delivery or str(uuid.uuid4())
    run_id = event_to_run_id(event_id)

    # 5. Enqueue background work — returns immediately to GitHub
    background_tasks.add_task(
        run_agent_background,
        event_id=event_id,
        repo=repo,
        issue_number=issue_number,
        title=title,
        body=body,
    )

    return {
        "accepted": True,
        "run_id": run_id,
        "thread_id": event_to_thread_id(repo, issue_number),
        "poll_url": f"/runs/{run_id}",
    }


@app.get("/runs/{run_id}")
async def get_run_status(run_id: str) -> dict:
    """Poll for agent run completion. Status: 'running' | 'completed' | 'failed'."""
    if run_id not in run_results:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return run_results[run_id]


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "active_runs": sum(1 for r in run_results.values() if r["status"] == "running")}


# Run with: uvicorn webhook_agent:app --port 8000 --reload
# Expose locally: ngrok http 8000
```

---

### Sub-pattern B — LangGraph Platform Background Run

**Use when:** You are deploying to LangGraph Platform (the managed cloud service) and want fire-and-forget execution with built-in status polling, persistence, and observability — without writing your own BackgroundTasks or result store.

**Concept: Runs API**

The LangGraph Platform exposes a REST API (and Python SDK) for managing runs. A run is one invocation of a compiled graph. You `POST /runs` and immediately get a `run_id` back. The graph executes server-side. You poll `GET /runs/{run_id}` for status.

```python
"""
platform_background_run.py — Fire-and-forget agent execution via LangGraph Platform SDK.

Requires:
  - A deployed LangGraph Platform server (local dev: langgraph dev)
  - LANGGRAPH_API_URL in .env pointing at that server
  - pip install langgraph-sdk

The SDK client wraps the REST API. Key methods:
  client.runs.create()   → POST /threads/{thread_id}/runs
  client.runs.get()      → GET  /threads/{thread_id}/runs/{run_id}
  client.runs.join()     → long-poll until run finishes
  client.threads.get_state() → read final graph state
"""
import asyncio
import os
import uuid

from dotenv import load_dotenv
from langgraph_sdk import get_client

load_dotenv()

LANGGRAPH_API_URL = os.environ.get("LANGGRAPH_API_URL", "http://localhost:2024")
ASSISTANT_ID = "agent"  # matches the graph name in your langgraph.json

client = get_client(url=LANGGRAPH_API_URL)


def event_to_run_id(event_id: str) -> str:
    """Deterministic run ID from event ID — idempotent across retries."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"platform-event:{event_id}"))


async def fire_and_forget(
    event_id: str,
    thread_id: str,
    input_data: dict,
    metadata: dict | None = None,
) -> str:
    """
    Submit an agent run to LangGraph Platform and return immediately.

    The run executes on the Platform server — this process is not blocked.
    Returns the run_id for later polling.

    Idempotency: if a run with this run_id already exists, the Platform
    returns the existing run rather than creating a duplicate.
    """
    run_id = event_to_run_id(event_id)

    run = await client.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
        input=input_data,
        config={
            "configurable": {},
            "run_id": run_id,              # idempotency key
            "run_name": f"event-{event_id[:12]}",
            "tags": ["event-driven"],
        },
        metadata=metadata or {},
        # multitask_strategy controls what happens if this thread already has a running run:
        #   "reject"    → raise error (safest for idempotent workflows)
        #   "interrupt" → cancel current run, start new one
        #   "rollback"  → cancel and rollback state, start new one
        #   "enqueue"   → queue behind current run
        multitask_strategy="reject",
    )

    print(f"Run submitted: {run['run_id']} (thread: {thread_id})")
    return run["run_id"]


async def poll_until_done(
    thread_id: str,
    run_id: str,
    poll_interval_secs: float = 2.0,
    timeout_secs: float = 300.0,
) -> dict:
    """
    Poll a run until it reaches a terminal state.

    Terminal states: 'success' | 'error' | 'timeout' | 'interrupted'

    For most event-driven use cases you do NOT need to poll — the agent
    runs to completion autonomously. Poll only when the caller needs the result
    (e.g., a webhook handler that wants to write the result to a database).
    """
    elapsed = 0.0
    while elapsed < timeout_secs:
        run = await client.runs.get(thread_id=thread_id, run_id=run_id)
        status = run["status"]

        if status in ("success", "error", "timeout", "interrupted"):
            if status == "success":
                # Read final graph state from the thread
                state = await client.threads.get_state(thread_id=thread_id)
                return {
                    "status": "success",
                    "run_id": run_id,
                    "output": state.values,
                }
            else:
                return {"status": status, "run_id": run_id, "error": run.get("error")}

        print(f"  [poll] status={status}, elapsed={elapsed:.0f}s — waiting...")
        await asyncio.sleep(poll_interval_secs)
        elapsed += poll_interval_secs

    return {"status": "poll_timeout", "run_id": run_id}


async def wait_for_run(thread_id: str, run_id: str) -> dict:
    """
    Preferred alternative to manual polling: join() long-polls server-side.
    More efficient than repeated GET requests.
    """
    await client.runs.join(thread_id=thread_id, run_id=run_id)
    state = await client.threads.get_state(thread_id=thread_id)
    return state.values


async def demo():
    """Demonstrate fire-and-forget then result retrieval."""
    event_id = "evt_demo_001"
    thread_id = f"demo-thread-{event_id}"

    # Fire and forget — returns immediately
    run_id = await fire_and_forget(
        event_id=event_id,
        thread_id=thread_id,
        input_data={"messages": [{"role": "user", "content": "Summarize the LangGraph docs."}]},
        metadata={"source": "demo", "event_id": event_id},
    )
    print(f"Submitted. Polling run_id={run_id}...")

    # Poll for completion (or use wait_for_run for simpler code)
    result = await poll_until_done(thread_id=thread_id, run_id=run_id)
    print(f"Result: {result}")


if __name__ == "__main__":
    asyncio.run(demo())
```

---

### Sub-pattern C — Cron Scheduling

**Use when:** An agent should run on a fixed schedule — daily digests, weekly reports, hourly data syncs. LangGraph Platform's `crons.create()` API manages this server-side. You register the schedule once; the Platform fires the agent run automatically.

**Concept: Platform Crons**

`client.crons.create()` registers a cron expression with a specific assistant and input payload. The Platform creates a new run on schedule. No external job scheduler (Celery, APScheduler, cron daemon) is needed.

```python
"""
cron_agent.py — Register and manage scheduled agent runs via LangGraph Platform.

Use cases:
  - Daily briefing agent (sends summary email every morning)
  - Hourly data sync agent
  - Weekly report generator

crons.create() arguments:
  assistant_id   : which graph to run
  schedule       : standard cron expression (UTC)
  input          : fixed input payload for every run
  config         : RunnableConfig (thread_id, etc.)
  metadata       : arbitrary labels for LangSmith filtering
"""
import asyncio
import os

from dotenv import load_dotenv
from langgraph_sdk import get_client

load_dotenv()

LANGGRAPH_API_URL = os.environ.get("LANGGRAPH_API_URL", "http://localhost:2024")
client = get_client(url=LANGGRAPH_API_URL)


async def register_daily_digest_cron() -> dict:
    """
    Register an agent to run every day at 07:00 UTC.

    The agent receives the same input payload each time. To pass dynamic
    data, inject it via a tool (e.g., a tool that fetches today's headlines).
    """
    cron = await client.crons.create(
        assistant_id="agent",
        schedule="0 7 * * *",   # cron: minute hour day month weekday (UTC)
        input={
            "messages": [{
                "role": "user",
                "content": (
                    "You are a daily briefing agent. "
                    "Fetch today's top AI news headlines, summarize them in 5 bullet points, "
                    "and send a digest email to the team."
                ),
            }]
        },
        config={
            "configurable": {
                # Use a stable thread_id so the agent accumulates context across days.
                # Or use a dynamic thread_id per run by omitting this — each run starts fresh.
                "thread_id": "daily-digest-main",
            }
        },
        metadata={
            "schedule_name": "daily-ai-digest",
            "owner": "platform-team",
        },
    )
    print(f"Cron registered: id={cron['cron_id']}, schedule={cron['schedule']}")
    return cron


async def register_hourly_sync_cron(repository: str) -> dict:
    """Register an hourly GitHub issue sync for a specific repository."""
    cron = await client.crons.create(
        assistant_id="agent",
        schedule="0 * * * *",  # every hour at :00
        input={
            "messages": [{
                "role": "user",
                "content": f"Check repository {repository} for new untraiged issues and label them.",
            }]
        },
        config={"configurable": {"thread_id": f"hourly-sync-{repository.replace('/', '-')}"}},
        metadata={"repository": repository, "schedule_type": "issue-sync"},
    )
    return cron


async def list_active_crons() -> list:
    """List all registered crons. Returns cron_id, schedule, and last run status."""
    crons = await client.crons.search()
    for c in crons:
        print(f"  [{c['cron_id'][:8]}] {c['schedule']:15s}  assistant={c['assistant_id']}")
    return crons


async def delete_cron(cron_id: str) -> None:
    """Deregister a cron. The agent will stop running on schedule."""
    await client.crons.delete(cron_id=cron_id)
    print(f"Cron {cron_id} deleted.")


async def demo():
    print("Registering crons...")
    daily = await register_daily_digest_cron()
    hourly = await register_hourly_sync_cron("my-org/my-repo")

    print("\nActive crons:")
    await list_active_crons()

    # To clean up:
    # await delete_cron(daily["cron_id"])
    # await delete_cron(hourly["cron_id"])


if __name__ == "__main__":
    asyncio.run(demo())
```

**Common cron expressions:**

| Expression | Meaning |
|---|---|
| `0 7 * * *` | Every day at 07:00 UTC |
| `0 * * * *` | Every hour at :00 |
| `*/15 * * * *` | Every 15 minutes |
| `0 9 * * 1` | Every Monday at 09:00 UTC |
| `0 0 1 * *` | First day of every month at midnight |

---

### Sub-pattern D — Local Dev Queue (asyncio.Queue)

**Use when:** You want to test event-driven patterns locally without standing up a webhook server or a message broker. An `asyncio.Queue` acts as a lightweight in-process substitute for SQS, Kafka, or RabbitMQ. Replace `asyncio.Queue` with your real queue client when deploying.

**Mental model:** A producer coroutine pushes events onto the queue. A consumer coroutine pulls events and invokes the agent. Multiple consumers run concurrently for throughput.

```python
"""
queue_consumer.py — asyncio.Queue event consumer for local development.

Production swap-in: replace asyncio.Queue with your real queue:
  - AWS SQS:    aiobotocore SQS client
  - RabbitMQ:   aio-pika
  - Redis:      aioredis BLPOP / Streams
  - Kafka:      aiokafka

The agent and config logic is identical regardless of queue backend.
"""
import asyncio
import uuid
from datetime import datetime

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

load_dotenv()

# ---------------------------------------------------------------------------
# 1. Agent (identical to any other pattern — queue is just the trigger)
# ---------------------------------------------------------------------------

@tool
def process_order(order_id: str, amount: float) -> str:
    """Process a payment order and return confirmation."""
    return f"Order {order_id} processed for ${amount:.2f}. Confirmation: CNF-{order_id[-4:]}"

tools = [process_order]
llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0).bind_tools(tools)
tool_node = ToolNode(tools)
checkpointer = MemorySaver()

def agent_node(state: MessagesState) -> dict:
    return {"messages": [llm.invoke(state["messages"])]}

def should_continue(state: MessagesState):
    return "tools" if state["messages"][-1].tool_calls else END

graph = StateGraph(MessagesState)
graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", should_continue)
graph.add_edge("tools", "agent")
agent_app = graph.compile(checkpointer=checkpointer)

# ---------------------------------------------------------------------------
# 2. Event model
# ---------------------------------------------------------------------------

class QueueEvent:
    def __init__(self, event_id: str, event_type: str, payload: dict):
        self.event_id = event_id
        self.event_type = event_type
        self.payload = payload
        self.received_at = datetime.utcnow().isoformat()

# ---------------------------------------------------------------------------
# 3. Consumer worker
# ---------------------------------------------------------------------------

processed_events: set[str] = set()  # idempotency guard — use Redis SET in prod

async def process_event(event: QueueEvent, worker_id: int) -> None:
    """
    Process one queue event by invoking the agent.

    Idempotency: skip if event_id already in processed_events.
    In production, use a distributed SET (Redis SETNX) so multiple
    worker instances are also deduplicated.
    """
    if event.event_id in processed_events:
        print(f"[worker-{worker_id}] Skipping duplicate event {event.event_id}")
        return

    print(f"[worker-{worker_id}] Processing {event.event_type}: {event.event_id}")
    processed_events.add(event.event_id)

    # Derive stable identifiers from event content
    entity_id = event.payload.get("customer_id", event.event_id)
    thread_id = f"{event.event_type}:{entity_id}"
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"queue-event:{event.event_id}"))

    config = {
        "configurable": {"thread_id": thread_id},
        "run_id": run_id,
        "run_name": f"{event.event_type}-{event.event_id[:8]}",
    }

    prompt = (
        f"Event type: {event.event_type}\n"
        f"Event ID: {event.event_id}\n"
        f"Payload: {event.payload}\n\n"
        f"Please handle this event appropriately."
    )

    try:
        result = await agent_app.ainvoke(
            {"messages": [HumanMessage(content=prompt)]},
            config=config,
        )
        print(f"[worker-{worker_id}] Done: {result['messages'][-1].content[:80]}")
    except Exception as exc:
        print(f"[worker-{worker_id}] Error processing {event.event_id}: {exc}")
        # In production: push to a dead-letter queue for manual inspection


async def consumer_worker(queue: asyncio.Queue, worker_id: int) -> None:
    """
    Long-running consumer coroutine. Pulls events from the queue and processes them.

    Multiple consumer_worker coroutines can run concurrently for throughput.
    Each processes events independently.
    """
    print(f"[worker-{worker_id}] Started.")
    while True:
        try:
            event: QueueEvent = await asyncio.wait_for(queue.get(), timeout=5.0)
            await process_event(event, worker_id)
            queue.task_done()   # signal queue that item is processed
        except asyncio.TimeoutError:
            continue  # no events — loop back and wait
        except asyncio.CancelledError:
            print(f"[worker-{worker_id}] Shutting down.")
            break


# ---------------------------------------------------------------------------
# 4. Producer (simulates incoming events — replace with real source)
# ---------------------------------------------------------------------------

async def event_producer(queue: asyncio.Queue) -> None:
    """Simulate incoming events. In production, this reads from SQS/Kafka/etc."""
    sample_events = [
        QueueEvent("evt-001", "order.placed", {"customer_id": "cust-42", "order_id": "ord-001", "amount": 149.99}),
        QueueEvent("evt-002", "order.placed", {"customer_id": "cust-77", "order_id": "ord-002", "amount": 29.99}),
        QueueEvent("evt-001", "order.placed", {"customer_id": "cust-42", "order_id": "ord-001", "amount": 149.99}),  # duplicate
        QueueEvent("evt-003", "order.refunded", {"customer_id": "cust-42", "order_id": "ord-001", "amount": 149.99}),
    ]
    for event in sample_events:
        await queue.put(event)
        await asyncio.sleep(0.5)   # simulate events arriving over time
    print("[producer] All events enqueued.")


# ---------------------------------------------------------------------------
# 5. Main entrypoint
# ---------------------------------------------------------------------------

async def main():
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)  # backpressure at 100 items
    num_workers = 2  # concurrent agent executions

    # Start consumer workers
    workers = [
        asyncio.create_task(consumer_worker(queue, worker_id=i))
        for i in range(num_workers)
    ]

    # Run producer
    await event_producer(queue)

    # Wait for all events to be processed
    await queue.join()

    # Shut down workers
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)
    print(f"Done. Processed {len(processed_events)} unique events.")


if __name__ == "__main__":
    asyncio.run(main())
```

---

### Sub-pattern E — Slack Bot Trigger (slack_bolt)

**Use when:** Users interact with the agent via Slack — slash commands, mentions in channels, or direct messages. Each Slack user gets their own `thread_id`, so the agent remembers context across a conversation thread.

**Key design:** Slack's event API requires a response within 3 seconds. Use `bolt`'s `async` mode with `asyncio` so the agent runs in the background while Slack receives an immediate acknowledgement.

```python
"""
slack_bot_agent.py — Slack bot with per-user thread_id and async agent execution.

Setup:
  1. Create a Slack app at api.slack.com/apps
  2. Enable Socket Mode (Settings > Socket Mode)
  3. Add event subscriptions: app_mention, message.im
  4. Install app to your workspace — copy Bot Token and Signing Secret
  5. pip install slack-bolt

The bot:
  - Responds to direct messages and @mentions
  - Each (user_id, channel_id) pair gets its own thread_id → persistent memory
  - A Slack thread reply uses the same thread_id as the parent message
  - Typing indicator shown while agent runs
"""
import asyncio
import os
import uuid

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

load_dotenv()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]   # xapp-... token for Socket Mode

# ---------------------------------------------------------------------------
# 1. Agent
# ---------------------------------------------------------------------------

@tool
def lookup_employee(name: str) -> str:
    """Look up an employee's contact info and team in the directory."""
    # Replace with real HR system call
    return f"{name}: Senior Engineer, Platform Team. Slack: @{name.lower().replace(' ', '.')}"

@tool
def create_jira_ticket(title: str, description: str, priority: str = "medium") -> str:
    """Create a Jira ticket and return its URL."""
    ticket_id = f"ENG-{abs(hash(title)) % 9000 + 1000}"
    return f"Created {ticket_id}: {title} (priority: {priority}) — https://jira.example.com/{ticket_id}"

tools = [lookup_employee, create_jira_ticket]
llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0).bind_tools(tools)
tool_node = ToolNode(tools)
checkpointer = MemorySaver()  # use PostgresSaver in production for persistence across restarts

def agent_node(state: MessagesState) -> dict:
    return {"messages": [llm.invoke(state["messages"])]}

def should_continue(state: MessagesState):
    return "tools" if state["messages"][-1].tool_calls else END

graph = StateGraph(MessagesState)
graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", should_continue)
graph.add_edge("tools", "agent")
agent_app = graph.compile(checkpointer=checkpointer)

# ---------------------------------------------------------------------------
# 2. Thread ID strategy
# ---------------------------------------------------------------------------

def slack_thread_id(user_id: str, channel_id: str, slack_thread_ts: str | None = None) -> str:
    """
    Derive a stable thread_id for the LangGraph agent.

    If the Slack message is part of a thread (slack_thread_ts is set), all
    messages in that thread share the same agent thread_id — the agent
    sees the full conversation within the Slack thread.

    If it's a top-level DM or channel message, scope by user+channel so
    the agent remembers context across separate messages from the same user.
    """
    if slack_thread_ts:
        # Scope to the specific Slack thread
        return f"slack:{channel_id}:{slack_thread_ts}"
    else:
        # Scope to the user within the channel (persistent memory across DMs)
        return f"slack:{user_id}:{channel_id}"

# ---------------------------------------------------------------------------
# 3. Slack app
# ---------------------------------------------------------------------------

bolt_app = AsyncApp(token=SLACK_BOT_TOKEN)

async def run_agent_and_reply(
    user_id: str,
    channel_id: str,
    user_message: str,
    say,
    thread_ts: str | None = None,
    slack_thread_ts: str | None = None,
) -> None:
    """
    Run the agent and post its response back to Slack.

    This runs in a background task so Slack's 3-second timeout is never hit.
    The caller has already sent an acknowledgement (implicit in bolt async handlers).
    """
    thread_id = slack_thread_id(user_id, channel_id, slack_thread_ts)
    run_id = str(uuid.uuid4())  # each Slack message is a fresh run (no dedup needed)

    config = {
        "configurable": {"thread_id": thread_id},
        "run_id": run_id,
        "run_name": f"slack-{user_id[:6]}",
    }

    try:
        result = await agent_app.ainvoke(
            {"messages": [HumanMessage(content=user_message)]},
            config=config,
        )
        response_text = result["messages"][-1].content

        # Reply in thread if the original message was in a thread, otherwise start one
        await say(
            text=response_text,
            thread_ts=thread_ts or slack_thread_ts,
        )
    except Exception as exc:
        await say(
            text=f"Sorry, I encountered an error: {str(exc)[:200]}",
            thread_ts=thread_ts,
        )


@bolt_app.event("app_mention")
async def handle_mention(event: dict, say) -> None:
    """Handle @bot mentions in channels."""
    user_id = event["user"]
    channel_id = event["channel"]
    text = event.get("text", "").strip()
    thread_ts = event.get("thread_ts")   # set if mention is inside a thread

    # Remove the bot mention prefix (<@UXXXXXX> ...)
    import re
    clean_text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    if not clean_text:
        await say("Hi! How can I help?", thread_ts=event["ts"])
        return

    # Run agent in background — bolt handler returns immediately
    asyncio.create_task(
        run_agent_and_reply(
            user_id=user_id,
            channel_id=channel_id,
            user_message=clean_text,
            say=say,
            thread_ts=event["ts"],          # reply in same thread
            slack_thread_ts=thread_ts,       # use existing thread context if in a thread
        )
    )


@bolt_app.event("message")
async def handle_dm(event: dict, say) -> None:
    """Handle direct messages to the bot."""
    # Ignore messages from bots (including self) and message_changed/deleted subtypes
    if event.get("bot_id") or event.get("subtype"):
        return

    user_id = event["user"]
    channel_id = event["channel"]
    text = event.get("text", "").strip()
    if not text:
        return

    asyncio.create_task(
        run_agent_and_reply(
            user_id=user_id,
            channel_id=channel_id,
            user_message=text,
            say=say,
            thread_ts=event["ts"],
            slack_thread_ts=event.get("thread_ts"),
        )
    )


async def main():
    handler = AsyncSocketModeHandler(bolt_app, SLACK_APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
```

---

### Sub-pattern F — Long-Horizon Async Interrupts

**Use when:** An agent needs a human decision that may not come for hours or days — a legal approval, a manager sign-off, a multi-step compliance review. Unlike the synchronous `interrupt_before=["tools"]` pattern (which blocks the caller), long-horizon interrupts store the paused state durably and resume via an API call later.

**Critical requirement:** Long-horizon interrupts MUST use a persistent checkpointer (`PostgresSaver` or LangGraph Platform). `MemorySaver` loses state on process restart — a 3-day approval would be lost.

**Flow:**
```
Agent runs → hits interrupt() → state persisted to DB → HTTP 202 returned
                                       ↓
              (days later) Approver calls POST /resume/{thread_id}
                                       ↓
              Agent resumes from checkpoint → continues execution
```

```python
"""
long_horizon_interrupt.py — Multi-day human approval via interrupt() + resume API.

Covers:
  - interrupt() inside a node (pauses graph, stores value for the caller)
  - Persistent state in PostgresSaver (required — MemorySaver won't survive restarts)
  - Resume endpoint that feeds the human's decision back into the graph
  - State inspection so approvers can see what they are approving
  - Timeout handling — what to do if approval never comes

Setup:
  pip install langgraph-checkpoint-postgres psycopg fastapi uvicorn
"""
import asyncio
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt
from psycopg import AsyncConnection
from pydantic import BaseModel

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

# ---------------------------------------------------------------------------
# 1. Agent with interrupt() node
# ---------------------------------------------------------------------------

@tool
def draft_contract(parties: str, terms: str) -> str:
    """Draft a legal contract between parties with given terms."""
    return (
        f"CONTRACT DRAFT\n"
        f"Parties: {parties}\n"
        f"Terms: {terms}\n"
        f"Date: {datetime.utcnow().date()}\n"
        f"Status: PENDING APPROVAL"
    )

@tool
def send_signed_contract(contract_text: str, recipients: str) -> str:
    """Send the approved and signed contract to all parties."""
    return f"Contract sent to {recipients}. Reference: CTR-{abs(hash(contract_text)) % 90000 + 10000}"

tools = [draft_contract, send_signed_contract]
llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0).bind_tools(tools)
tool_node = ToolNode(tools)


def agent_node(state: MessagesState) -> dict:
    return {"messages": [llm.invoke(state["messages"])]}


def human_approval_node(state: MessagesState) -> dict:
    """
    Pause execution and wait for human approval.

    interrupt() does two things:
      1. Serializes the current graph state to the checkpointer.
      2. Raises a special exception that halts execution and returns control
         to the caller with the interrupt value as metadata.

    The graph will not advance past this node until the caller resumes it
    with Command(resume=<decision>). This can be hours or days later.

    The interrupt value (the dict passed to interrupt()) is visible to the
    caller via graph.get_state(config).tasks[0].interrupts[0].value
    """
    # Find the most recent draft in the message history
    draft_content = None
    for msg in reversed(state["messages"]):
        if hasattr(msg, "content") and "CONTRACT DRAFT" in str(msg.content):
            draft_content = msg.content
            break

    # interrupt() pauses here. The value is the "question" presented to the human.
    # When resumed, interrupt() returns whatever the human passed to Command(resume=...).
    human_decision = interrupt({
        "action": "approve_contract",
        "contract_preview": draft_content or "(see previous messages)",
        "instructions": (
            "Review the contract above. Resume this workflow with:\n"
            "  {'approved': true, 'notes': '...'}  — to approve and send\n"
            "  {'approved': false, 'reason': '...'} — to reject and revise"
        ),
        "expires_at": (datetime.utcnow() + timedelta(days=3)).isoformat(),
    })

    # Code below interrupt() only runs after resume
    if human_decision.get("approved"):
        notes = human_decision.get("notes", "")
        return {"messages": [HumanMessage(
            content=f"Contract approved. Approver notes: {notes}. Proceed to send."
        )]}
    else:
        reason = human_decision.get("reason", "No reason given.")
        return {"messages": [HumanMessage(
            content=f"Contract rejected. Reason: {reason}. Please revise the contract."
        )]}


def should_continue(state: MessagesState):
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    # Check if the last tool result contains a draft needing approval
    for msg in reversed(state["messages"]):
        if hasattr(msg, "content") and "CONTRACT DRAFT" in str(msg.content):
            if not any(
                "approved" in str(m.content).lower()
                for m in state["messages"]
                if hasattr(m, "content")
            ):
                return "human_approval"
    return END


graph = StateGraph(MessagesState)
graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)
graph.add_node("human_approval", human_approval_node)

graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", should_continue, {
    "tools": "tools",
    "human_approval": "human_approval",
    END: END,
})
graph.add_edge("tools", "agent")
graph.add_edge("human_approval", "agent")  # after approval, agent continues


# ---------------------------------------------------------------------------
# 2. App factory — must be async because AsyncPostgresSaver requires async setup
# ---------------------------------------------------------------------------

_agent_app = None  # module-level singleton

async def get_agent_app():
    global _agent_app
    if _agent_app is None:
        conn = await AsyncConnection.connect(DATABASE_URL)
        checkpointer = AsyncPostgresSaver(conn)
        await checkpointer.setup()  # creates tables on first run (idempotent)
        _agent_app = graph.compile(checkpointer=checkpointer)
    return _agent_app


# ---------------------------------------------------------------------------
# 3. FastAPI resume endpoint
# ---------------------------------------------------------------------------

fastapi_app = FastAPI(title="Long-Horizon Interrupt Agent")


class StartRequest(BaseModel):
    thread_id: str
    user_message: str


class ResumeRequest(BaseModel):
    thread_id: str
    decision: dict  # e.g. {"approved": True, "notes": "Looks good"}


@fastapi_app.post("/agent/start", status_code=202)
async def start_agent(req: StartRequest) -> dict:
    """Start an agent run. Returns immediately if the agent hits an interrupt."""
    app = await get_agent_app()
    config = {"configurable": {"thread_id": req.thread_id}}

    result = await app.ainvoke(
        {"messages": [HumanMessage(content=req.user_message)]},
        config=config,
    )

    # Check if we paused at an interrupt
    state = await app.aget_state(config)
    pending_interrupts = [
        t.interrupts for t in (state.tasks or []) if t.interrupts
    ]

    if pending_interrupts:
        interrupt_value = pending_interrupts[0][0].value
        return {
            "status": "awaiting_approval",
            "thread_id": req.thread_id,
            "interrupt": interrupt_value,
            "resume_url": f"/agent/resume",
        }

    return {
        "status": "completed",
        "thread_id": req.thread_id,
        "result": result["messages"][-1].content,
    }


@fastapi_app.post("/agent/resume")
async def resume_agent(req: ResumeRequest) -> dict:
    """
    Resume a paused agent with a human decision.

    Call this endpoint when the approver has made their decision.
    The agent continues from exactly where it was interrupted.
    """
    app = await get_agent_app()
    config = {"configurable": {"thread_id": req.thread_id}}

    # Verify the thread is actually paused
    state = await app.aget_state(config)
    if not state or not any(t.interrupts for t in (state.tasks or [])):
        raise HTTPException(
            status_code=400,
            detail=f"Thread {req.thread_id} has no pending interrupt to resume.",
        )

    # Command(resume=value) feeds the decision back into interrupt()
    # The agent continues from the line after interrupt() in human_approval_node
    result = await app.ainvoke(
        Command(resume=req.decision),
        config=config,
    )

    # Check if there's another interrupt (multi-step approval workflows)
    new_state = await app.aget_state(config)
    new_interrupts = [t.interrupts for t in (new_state.tasks or []) if t.interrupts]

    if new_interrupts:
        return {
            "status": "awaiting_next_approval",
            "thread_id": req.thread_id,
            "interrupt": new_interrupts[0][0].value,
        }

    return {
        "status": "completed",
        "thread_id": req.thread_id,
        "result": result["messages"][-1].content,
    }


@fastapi_app.get("/agent/state/{thread_id}")
async def inspect_state(thread_id: str) -> dict:
    """
    Inspect the current state of a thread.

    Use this to show approvers what they are reviewing:
    the full message history, any pending interrupt value, and run status.
    """
    app = await get_agent_app()
    config = {"configurable": {"thread_id": thread_id}}
    state = await app.aget_state(config)

    if not state:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")

    pending_interrupts = [
        t.interrupts[0].value
        for t in (state.tasks or [])
        if t.interrupts
    ]

    return {
        "thread_id": thread_id,
        "status": "awaiting_approval" if pending_interrupts else "running_or_complete",
        "pending_interrupt": pending_interrupts[0] if pending_interrupts else None,
        "message_count": len(state.values.get("messages", [])),
        "last_message": state.values.get("messages", [{}])[-1].content
            if state.values.get("messages") else None,
        "checkpoint_ts": state.created_at if hasattr(state, "created_at") else None,
    }


# Run with: uvicorn long_horizon_interrupt:fastapi_app --port 8001
```

**Example API conversation:**

```bash
# 1. Start the workflow
curl -X POST http://localhost:8001/agent/start \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "contract-acme-2024", "user_message": "Draft a contract between Acme Corp and Widget Inc for a 12-month software license at $50k/year."}'

# Response: {"status": "awaiting_approval", "interrupt": {"action": "approve_contract", ...}}

# --- (days pass, approver reviews) ---

# 2. Approve
curl -X POST http://localhost:8001/agent/resume \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "contract-acme-2024", "decision": {"approved": true, "notes": "Approved by legal team on 2024-06-18"}}'

# 3. Reject and revise
curl -X POST http://localhost:8001/agent/resume \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "contract-acme-2024", "decision": {"approved": false, "reason": "Payment terms need to be quarterly, not annual."}}'
```

---

### Event-Driven Patterns: Testing

```python
# test_event_driven.py
"""
Tests for event-driven agent patterns.

Strategy:
  - Webhook: use FastAPI TestClient (no real HTTP server needed)
  - Idempotency: invoke twice with same run_id, assert only one result
  - Interrupt: invoke, assert status=paused, resume, assert completed
  - Queue: push 3 events including 1 duplicate, assert 2 processed
"""
import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient


# --- Test: HMAC verification ---

def test_webhook_rejects_missing_signature():
    from webhook_agent import app
    client = TestClient(app)
    resp = client.post("/webhook/github", json={"action": "opened"})
    assert resp.status_code == 401

def test_webhook_rejects_bad_signature():
    import hashlib, hmac as hmac_lib
    from webhook_agent import app, WEBHOOK_SECRET
    client = TestClient(app)
    body = b'{"action": "opened", "issue": {"number": 1, "title": "Test", "body": ""}, "repository": {"full_name": "a/b"}}'
    bad_sig = "sha256=0000000000000000000000000000000000000000000000000000000000000000"
    resp = client.post(
        "/webhook/github",
        content=body,
        headers={"X-Hub-Signature-256": bad_sig, "X-GitHub-Delivery": "evt-test"},
    )
    assert resp.status_code == 401

def test_webhook_accepts_valid_signature():
    import hashlib, hmac as hmac_lib, json
    from webhook_agent import app, WEBHOOK_SECRET
    client = TestClient(app)
    payload = {"action": "opened", "issue": {"number": 1, "title": "Bug", "body": "It crashes."}, "repository": {"full_name": "org/repo"}}
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac_lib.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/webhook/github",
        content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "evt-valid-001"},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["accepted"] is True
    assert "run_id" in data


# --- Test: Idempotency ---

def test_idempotent_run_id_is_deterministic():
    from webhook_agent import event_to_run_id
    id1 = event_to_run_id("evt-abc-123")
    id2 = event_to_run_id("evt-abc-123")
    id3 = event_to_run_id("evt-abc-124")
    assert id1 == id2, "Same event ID must produce same run_id"
    assert id1 != id3, "Different event IDs must produce different run_ids"
    # Must be a valid UUID format
    uuid.UUID(id1)  # raises ValueError if not valid UUID

@pytest.mark.asyncio
async def test_duplicate_event_not_reprocessed():
    """Submitting the same event twice should only invoke the agent once."""
    from webhook_agent import run_agent_background, run_results

    event_id = "dedup-test-001"
    call_count = 0
    original_invoke = None

    async def counting_invoke(input_, config):
        nonlocal call_count
        call_count += 1
        from langchain_core.messages import AIMessage
        return {"messages": [AIMessage(content="done")]}

    with patch("webhook_agent.agent_app") as mock_app:
        mock_app.ainvoke = counting_invoke
        await run_agent_background(event_id, "org/repo", 1, "Title", "Body")
        await run_agent_background(event_id, "org/repo", 1, "Title", "Body")  # duplicate

    assert call_count == 1, "Agent should only be invoked once for duplicate events"


# --- Test: Queue consumer deduplication ---

@pytest.mark.asyncio
async def test_queue_deduplication():
    from queue_consumer import QueueEvent, process_event, processed_events

    processed_events.clear()
    call_count = 0

    async def mock_invoke(input_, config):
        nonlocal call_count
        call_count += 1
        from langchain_core.messages import AIMessage
        return {"messages": [AIMessage(content="processed")]}

    with patch("queue_consumer.agent_app") as mock_app:
        mock_app.ainvoke = mock_invoke
        evt = QueueEvent("q-evt-001", "order.placed", {"customer_id": "c1", "order_id": "o1", "amount": 10.0})
        await process_event(evt, worker_id=0)
        await process_event(evt, worker_id=0)  # duplicate

    assert call_count == 1, "Duplicate queue event must not invoke agent twice"


# --- Test: Long-horizon interrupt flow ---

@pytest.mark.asyncio
async def test_interrupt_pause_and_resume():
    """
    Verify the interrupt/resume lifecycle:
      1. Agent runs, hits interrupt(), returns status=awaiting_approval
      2. Resume with approval → agent completes
    """
    # This test requires a real or mocked PostgresSaver.
    # For unit testing, substitute MemorySaver.
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    # Simplified graph that always interrupts then completes
    from langgraph.graph import StateGraph, MessagesState, START, END
    from langgraph.types import interrupt

    def interruptible_node(state):
        decision = interrupt({"question": "Approve?"})
        return {"messages": [AIMessage(content=f"Decision was: {decision}")]}

    g = StateGraph(MessagesState)
    g.add_node("ask", interruptible_node)
    g.add_edge(START, "ask")
    g.add_edge("ask", END)
    test_app = g.compile(checkpointer=MemorySaver())

    config = {"configurable": {"thread_id": "test-interrupt-resume"}}

    # First invoke — should pause at interrupt
    from langchain_core.messages import HumanMessage
    state1 = await test_app.ainvoke(
        {"messages": [HumanMessage(content="start")]},
        config=config,
    )
    graph_state = await test_app.aget_state(config)
    interrupts = [t.interrupts for t in (graph_state.tasks or []) if t.interrupts]
    assert interrupts, "Expected graph to be paused at interrupt()"
    assert interrupts[0][0].value == {"question": "Approve?"}

    # Resume with approval
    state2 = await test_app.ainvoke(Command(resume={"approved": True}), config=config)
    assert "Decision was:" in state2["messages"][-1].content


if __name__ == "__main__":
    # Run sync tests directly
    test_webhook_rejects_missing_signature()
    test_webhook_rejects_bad_signature()
    test_idempotent_run_id_is_deterministic()
    print("Sync tests passed. Run async tests with: pytest test_event_driven.py -v")
```

---

### Event-Driven Patterns: LangSmith Tracing Notes

Event-driven runs are traced identically to chat runs — same `.env` variables. Two additional practices for event-driven workloads:

```python
# 1. Tag runs by trigger source for easy filtering in LangSmith UI
config = {
    "configurable": {"thread_id": thread_id},
    "run_id": run_id,
    "run_name": f"webhook-github-{repo}",
    "tags": ["webhook", "github", "issue-triage"],  # filter in LangSmith
    "metadata": {
        "event_id": event_id,
        "trigger": "github_webhook",
        "repo": repo,
    },
}

# 2. For cron runs, add the schedule cadence so you can distinguish
# "this is a scheduled run" from "this is a manually triggered run"
cron_config = {
    "configurable": {"thread_id": "daily-digest"},
    "tags": ["cron", "daily"],
    "metadata": {"schedule": "0 7 * * *", "trigger": "cron"},
}
```

---

### Common Mistakes (Event-Driven)

| Mistake | Fix |
|---|---|
| Returning 200 and running agent synchronously | Return 202 immediately; run agent via `BackgroundTasks` or async task |
| Using `==` for HMAC comparison | Always use `hmac.compare_digest()` — `==` is vulnerable to timing attacks |
| Skipping HMAC verification | Any public webhook endpoint without signature verification is a security hole |
| Using `MemorySaver` with long-horizon interrupts | State is lost on restart — use `PostgresSaver` or LangGraph Platform |
| Hardcoding `thread_id` for all events | Use entity-scoped `thread_id` (user ID, issue ID) — shared `thread_id` conflates unrelated conversations |
| Not setting `run_id` for idempotency | Webhook retries will double-process — always set `run_id` from event ID |
| Blocking in Slack handler | Slack requires response within 3s — always `asyncio.create_task()` before the handler returns |
| Polling every 100ms for run completion | Use `client.runs.join()` (long-poll) or exponential backoff — tight polling wastes resources |
| Single consumer worker for high-volume queues | Instantiate N `consumer_worker` coroutines; each processes independently |
| `interrupt()` without a resume endpoint | Define a `/resume` API route before deploying long-horizon interrupt workflows |

---

Add these to your `.env` — no code changes needed. Every `invoke()` and `astream()` call is automatically traced.

```bash
LANGSMITH_API_KEY="ls__your_key_here"
LANGSMITH_TRACING="true"
LANGSMITH_PROJECT="my-agent-project"   # all runs grouped under this project
```

View traces at [smith.langchain.com](https://smith.langchain.com). Each trace shows:
- Every node execution with inputs and outputs
- Every LLM call with prompt, response, token counts, latency
- Every tool call with arguments and results
- Full state at each checkpoint

To add a run name for easier filtering:

```python
config = {
    "configurable": {"thread_id": "user-123"},
    "run_name": "customer-support-query",   # shows in LangSmith UI
}
app.invoke({"messages": [...]}, config=config)
```

---

## Common Mistakes

| Mistake | Fix |
|---|---|
| State field replaced instead of appended | Use `Annotated[list, operator.add]` or `add_messages` as reducer |
| Streaming shows nothing | Use `stream_mode="messages"` for token streaming, `"updates"` for node-level |
| MemorySaver loses history on restart | Use `PostgresSaver` for persistence across process restarts |
| `interrupt()` with no checkpointer | `interrupt()` requires a checkpointer — add `MemorySaver` at minimum |
| Worker state leaks into OverallState | Worker nodes should only write to fields with `operator.add` reducers |
| Reflection loop never terminates | Always set `MAX_ITERATIONS` as a hard cap alongside any score threshold |
| Supervisor ignores a specialist | Specialist `name` parameter must match the noun the supervisor prompt uses |
| `with_structured_output` fails silently | Pin `langchain-anthropic >= 0.1.23`; check model supports tool use |
| Tool description missing | Every `@tool` function must have a docstring — it becomes the LLM-visible description |
| Thread ID not set | Without `thread_id` in config, checkpointing and memory do not work |
| Webhook handler blocks on agent | Return 202 immediately; run agent in `BackgroundTasks` — see Pattern 6 |
| No HMAC verification on webhook | Any public endpoint without signature verification is a security hole |
| `MemorySaver` with long-horizon interrupt | State is lost on restart — use `PostgresSaver` for multi-day approvals |
| No `run_id` on event-triggered runs | Webhook retries cause double-processing — derive `run_id` from event ID |

---

## Quick Reference

```python
# Imports cheat sheet
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode, create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Send, interrupt, Command
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool

# Minimal ReAct in 15 lines
llm = ChatAnthropic(model="claude-sonnet-4-6").bind_tools(tools)
g = StateGraph(MessagesState)
g.add_node("agent", lambda s: {"messages": [llm.invoke(s["messages"])]})
g.add_node("tools", ToolNode(tools))
g.add_edge(START, "agent")
g.add_conditional_edges("agent",
    lambda s: "tools" if s["messages"][-1].tool_calls else END)
g.add_edge("tools", "agent")
app = g.compile(checkpointer=MemorySaver())

# Stream tokens
async for chunk, meta in app.astream(
    {"messages": [("user", "hello")]},
    {"configurable": {"thread_id": "1"}},
    stream_mode="messages",
):
    print(chunk.content, end="", flush=True)

# Human-in-the-loop: pause before tools, resume after approval
app_hitl = g.compile(checkpointer=MemorySaver(), interrupt_before=["tools"])
state = app_hitl.invoke(input, config)
# ... show pending tool call to user ...
result = app_hitl.invoke(Command(resume=None), config)  # approve
result = app_hitl.invoke(Command(resume="cancel"), config)  # reject

# Event-driven: idempotent config from webhook event ID
import uuid, hmac, hashlib
run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"webhook-event:{event_id}"))
config = {"configurable": {"thread_id": f"entity:{entity_id}"}, "run_id": run_id}

# HMAC verification (webhook security)
sig = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
hmac.compare_digest(f"sha256={sig}", header_value)  # constant-time — never use ==

# LangGraph Platform: fire-and-forget + poll
from langgraph_sdk import get_client
client = get_client(url=os.environ["LANGGRAPH_API_URL"])
run = await client.runs.create(thread_id=tid, assistant_id="agent", input=inp)
await client.runs.join(thread_id=tid, run_id=run["run_id"])  # blocks until done

# LangGraph Platform: register a cron
cron = await client.crons.create(
    assistant_id="agent",
    schedule="0 7 * * *",    # every day at 07:00 UTC
    input={"messages": [{"role": "user", "content": "Run daily digest."}]},
    config={"configurable": {"thread_id": "daily-digest"}},
)

# Long-horizon interrupt inside a node
from langgraph.types import interrupt, Command
decision = interrupt({"question": "Approve contract?", "expires_at": "2024-06-21"})
# ... resumes here after: app.ainvoke(Command(resume={"approved": True}), config)
```
