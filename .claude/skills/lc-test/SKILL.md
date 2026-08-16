---
name: lc-test
description: Use when writing tests for any LangChain or LangGraph code — chains, agents, graphs, RAG pipelines, tools, output parsers, or prompts. Triggered by requests to test, evaluate, benchmark, or measure quality of LLM-based code, or questions about FakeListChatModel, LangSmith datasets, aevaluate, RAGAS, pytest fixtures for LangChain, or CI/CD evaluation gates.
---

# lc:test — LangChain/LangGraph Testing & Evaluation

## Overview

Testing LLM applications has three distinct layers that serve different purposes and run at different points in the development cycle. This skill scaffolds the right test for the right job.

**Core mental model:**
- **Unit tests** = no LLM calls, run in CI on every commit, test logic and wiring
- **Integration tests** = real (cheap) LLM calls, run nightly or on PR, test end-to-end behavior
- **LangSmith evaluations** = LLM-as-judge over a dataset, run before releases, measure quality

---

## Skill Flow

Ask these questions before generating any test code:

1. **What layer?** Unit (no LLM), integration (real LLM), or quality evaluation (LangSmith)?
2. **What are you testing?** Chain / agent / LangGraph graph / RAG pipeline / single tool?
3. **What matters most?** Correctness / routing logic / latency / cost / output quality?
4. **Do you have a LangSmith account?** (affects evaluation patterns)

Use the answer to select from the patterns below.

---

## Layer 1 — Unit Testing (No LLM Calls)

**When to use:** Every commit. Fast (<1s per test). No API keys needed.

**Key insight:** LangGraph nodes are plain Python functions. Test them like any other function. Replace the LLM with `FakeListChatModel`.

### Setup

```bash
pip install pytest pytest-asyncio langchain-community
```

```python
# conftest.py — shared fixtures for the whole test suite
import pytest
from langchain_community.chat_models.fake import FakeListChatModel, FakeStreamingListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


@pytest.fixture
def fake_llm():
    """Deterministic LLM that cycles through a preset list of responses."""
    return FakeListChatModel(responses=["Paris", "The capital of France is Paris."])


@pytest.fixture
def fake_streaming_llm():
    """Streaming variant — yields tokens one character at a time."""
    return FakeStreamingListChatModel(responses=["Hello world"])


@pytest.fixture
def tool_call_response():
    """Pre-built AIMessage with a tool call — for testing tool-calling nodes."""
    return AIMessage(
        content="",
        tool_calls=[{
            "id": "call_abc123",
            "name": "search_web",
            "args": {"query": "capital of France"},
        }],
    )
```

### Testing Individual Nodes

```python
# test_nodes.py
# Nodes are just Python functions. Pass a state dict, assert the output dict.

from my_agent import agent_node, should_continue
from langchain_community.chat_models.fake import FakeListChatModel
from langchain_core.messages import HumanMessage, AIMessage


def test_agent_node_returns_message():
    """agent_node should append an AIMessage to the messages list."""
    fake = FakeListChatModel(responses=["42"])
    # Patch the LLM — dependency inject it into the node or monkeypatch
    import my_agent
    original_llm = my_agent.llm_with_tools
    my_agent.llm_with_tools = fake

    state = {"messages": [HumanMessage(content="What is 6 * 7?")]}
    result = agent_node(state)

    my_agent.llm_with_tools = original_llm  # restore

    assert "messages" in result
    assert len(result["messages"]) == 1
    assert result["messages"][0].content == "42"


def test_should_continue_routes_to_tools():
    """should_continue must return 'tools' when the last message has tool_calls."""
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"id": "x", "name": "search", "args": {}}],
            )
        ]
    }
    assert should_continue(state) == "tools"


def test_should_continue_routes_to_end():
    """should_continue must return END when there are no tool calls."""
    from langgraph.graph import END
    state = {"messages": [AIMessage(content="The answer is 42.")]}
    assert should_continue(state) == END
```

### Testing Tools Independently

```python
# test_tools.py
from my_agent import calculate, search_web


def test_calculate_addition():
    assert calculate.invoke({"expression": "2 + 2"}) == "4"


def test_calculate_handles_error():
    result = calculate.invoke({"expression": "1 / 0"})
    assert "Error" in result


def test_search_web_returns_string():
    result = search_web.invoke({"query": "test query"})
    assert isinstance(result, str)
    assert len(result) > 0
```

### Testing LCEL Chains with Mocked LLMs

```python
# test_chains.py
from langchain_community.chat_models.fake import FakeListChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser


def test_simple_chain():
    """Test a prompt | llm | parser chain end-to-end with no real LLM call."""
    fake = FakeListChatModel(responses=["Mocked answer"])

    chain = (
        ChatPromptTemplate.from_template("Answer: {question}")
        | fake
        | StrOutputParser()
    )

    result = chain.invoke({"question": "What is 2+2?"})
    assert result == "Mocked answer"


def test_chain_invoked_with_correct_input():
    """Verify the prompt formats correctly before hitting the LLM."""
    received = []

    class CapturingFake(FakeListChatModel):
        def _generate(self, messages, **kwargs):
            received.extend(messages)
            return super()._generate(messages, **kwargs)

    fake = CapturingFake(responses=["ok"])
    chain = ChatPromptTemplate.from_template("Q: {q}") | fake | StrOutputParser()
    chain.invoke({"q": "hello"})

    assert "Q: hello" in received[0].content
```

### Testing Output Parsers

```python
# test_parsers.py
from pydantic import BaseModel, Field
from langchain_core.output_parsers import JsonOutputParser
from langchain_community.chat_models.fake import FakeListChatModel


class SentimentOutput(BaseModel):
    sentiment: str = Field(description="positive, negative, or neutral")
    score: float = Field(description="confidence 0.0-1.0")


def test_json_parser_valid():
    import json
    parser = JsonOutputParser(pydantic_object=SentimentOutput)
    raw = '{"sentiment": "positive", "score": 0.9}'
    result = parser.invoke(raw)
    assert result["sentiment"] == "positive"
    assert result["score"] == 0.9


def test_json_parser_with_fake_llm():
    parser = JsonOutputParser(pydantic_object=SentimentOutput)
    fake = FakeListChatModel(responses=['{"sentiment": "negative", "score": 0.8}'])
    chain = fake | parser
    result = chain.invoke("Analyze this text")
    assert result["sentiment"] == "negative"
```

---

## Layer 2 — LangGraph State Testing

**Key insight:** A graph is deterministic if you control the LLM. Inject state → invoke → assert output state.

### Testing Full Graph with Mocked LLM

```python
# test_graph.py
import pytest
from unittest.mock import patch, MagicMock
from langchain_community.chat_models.fake import FakeListChatModel
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.checkpoint.memory import MemorySaver


def test_full_graph_no_tools(monkeypatch):
    """Graph should go agent → END when LLM gives a direct answer."""
    from my_agent import graph  # the uncompiled StateGraph

    fake = FakeListChatModel(responses=["The capital is Paris."])
    import my_agent
    monkeypatch.setattr(my_agent, "llm_with_tools", fake)

    app = graph.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "test-1"}}
    result = app.invoke(
        {"messages": [HumanMessage(content="What is the capital of France?")]},
        config=config,
    )

    assert "Paris" in result["messages"][-1].content


def test_conditional_edge_routing():
    """Test that the routing function sends tool calls to the tools node."""
    from my_agent import should_continue
    from langgraph.graph import END

    # No tool calls → END
    state_no_tools = {
        "messages": [AIMessage(content="Direct answer.")]
    }
    assert should_continue(state_no_tools) == END

    # Tool calls present → tools
    state_with_tools = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "search", "args": {"query": "x"}}],
            )
        ]
    }
    assert should_continue(state_with_tools) == "tools"


def test_graph_checkpointing():
    """State is persisted between invocations with the same thread_id."""
    from langchain_community.chat_models.fake import FakeListChatModel
    import my_agent

    fake = FakeListChatModel(responses=["I see you said hello.", "Yes, you greeted me."])
    app = my_agent.graph.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "memory-test"}}

    # Turn 1
    app.invoke({"messages": [HumanMessage(content="Hello!")]}, config=config)

    # Turn 2 — graph should have history from turn 1
    result = app.invoke(
        {"messages": [HumanMessage(content="What did I just say?")]},
        config=config,
    )
    # The history is present in the graph state
    all_messages = result["messages"]
    human_messages = [m for m in all_messages if isinstance(m, HumanMessage)]
    assert len(human_messages) == 2  # both turns present


def test_custom_state_node():
    """Test a node that operates on a custom TypedDict state."""
    from my_agent import plan_node  # example: the planner node
    from my_agent import PlanExecuteState

    initial_state: PlanExecuteState = {
        "input": "Research quantum computing",
        "plan": [],
        "past_steps": [],
        "response": None,
    }

    # Mock the planner chain
    from unittest.mock import patch
    with patch("my_agent.planner") as mock_planner:
        mock_planner.invoke.return_value = MagicMock(
            steps=["Search web", "Summarize findings", "Write report"]
        )
        result = plan_node(initial_state)

    assert result["plan"] == ["Search web", "Summarize findings", "Write report"]
```

---

## Layer 3 — Integration Testing (Real LLM Calls)

**When to use:** PR merges, nightly CI. Use cheap models. Keep inputs small.

```python
# test_integration.py
import pytest

# Mark ALL integration tests so they can be skipped in fast CI
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def cheap_llm():
    """Use the cheapest model to control costs in integration tests."""
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(model="claude-haiku-4-5", temperature=0)


@pytest.mark.integration
def test_agent_gives_correct_answer(cheap_llm):
    """Real LLM call: verify the agent answers a factual question correctly."""
    from langgraph.checkpoint.memory import MemorySaver
    import my_agent

    app = my_agent.graph.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "integration-1"}}

    result = app.invoke(
        {"messages": [{"role": "user", "content": "What is 12 * 12? Answer with just the number."}]},
        config=config,
    )
    assert "144" in result["messages"][-1].content


@pytest.mark.integration
def test_tool_is_called_when_needed(cheap_llm):
    """Real LLM: verify the agent calls the calculate tool for math questions."""
    from langgraph.checkpoint.memory import MemorySaver
    from langchain_core.messages import ToolMessage
    import my_agent

    app = my_agent.graph.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "integration-tool"}}

    result = app.invoke(
        {"messages": [{"role": "user", "content": "Calculate 47 * 83 using your calculator tool."}]},
        config=config,
    )
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) > 0, "Expected the agent to call a tool"
```

**Run unit tests only (CI fast path):**
```bash
pytest -m "not integration"
```

**Run all tests including integration:**
```bash
pytest -m "integration" --timeout=60
```

**pytest.ini configuration:**
```ini
[pytest]
markers =
    integration: marks tests as requiring a real LLM API key (deselect with -m "not integration")
asyncio_mode = auto
```

---

## Layer 4 — LangSmith Evaluation

**When to use:** Before releases. Measures output quality over a curated dataset. Runs LLM-as-judge to score responses.

### Install

```bash
pip install langsmith
```

```bash
# .env
LANGSMITH_API_KEY="ls__..."
LANGSMITH_TRACING="true"
```

### Creating a Dataset

```python
# create_dataset.py — run once to populate LangSmith with test examples
from langsmith import Client

client = Client()

# Create the dataset
dataset = client.create_dataset(
    "my-agent-qa-v1",
    description="Q&A pairs for evaluating the customer support agent",
)

# Add examples — inputs match what your target function receives
examples = [
    {
        "inputs":  {"question": "What is your return policy?"},
        "outputs": {"answer": "Returns are accepted within 30 days with receipt."},
    },
    {
        "inputs":  {"question": "How do I track my order?"},
        "outputs": {"answer": "Log into your account and visit the Orders page."},
    },
    {
        "inputs":  {"question": "Do you ship internationally?"},
        "outputs": {"answer": "Yes, we ship to over 50 countries."},
    },
]

client.create_examples(
    inputs=[e["inputs"] for e in examples],
    outputs=[e["outputs"] for e in examples],
    dataset_id=dataset.id,
)

print(f"Dataset created: {dataset.id}")
print(f"View at: https://smith.langchain.com/datasets/{dataset.id}")
```

### Running an Evaluation

```python
# eval_agent.py — run before a release to get quality scores
import asyncio
from langsmith import Client
from langsmith.evaluation import aevaluate, EvaluationResult
from langsmith.schemas import Run, Example


# ── 1. Target function ────────────────────────────────────────────────────────
# Must be async. Receives the example inputs dict. Returns a dict.

async def target(inputs: dict) -> dict:
    """The system under test. Called once per dataset example."""
    from langgraph.checkpoint.memory import MemorySaver
    import my_agent

    app = my_agent.graph.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": f"eval-{inputs['question'][:20]}"}}

    result = await app.ainvoke(
        {"messages": [{"role": "user", "content": inputs["question"]}]},
        config=config,
    )
    return {"answer": result["messages"][-1].content}


# ── 2. Evaluators ─────────────────────────────────────────────────────────────
# Each evaluator receives (run: Run, example: Example) and returns EvaluationResult.

def correctness_evaluator(run: Run, example: Example) -> EvaluationResult:
    """LLM-as-judge: is the answer factually correct vs. the reference?"""
    from langchain_anthropic import ChatAnthropic
    from langchain_core.prompts import ChatPromptTemplate

    llm = ChatAnthropic(model="claude-haiku-4-5", temperature=0)
    prompt = ChatPromptTemplate.from_template("""
You are an evaluator. Score whether the AI answer is correct compared to the reference answer.

Reference answer: {reference}
AI answer: {prediction}

Respond with ONLY a JSON object: {{"score": 0.0-1.0, "reasoning": "one sentence"}}
""")

    import json
    response = (prompt | llm).invoke({
        "reference":  example.outputs["answer"],
        "prediction": run.outputs["answer"],
    })
    try:
        data = json.loads(response.content)
        score = float(data["score"])
        comment = data.get("reasoning", "")
    except Exception:
        score = 0.0
        comment = "Parse error"

    return EvaluationResult(key="correctness", score=score, comment=comment)


def length_evaluator(run: Run, example: Example) -> EvaluationResult:
    """Business logic: answers over 500 characters are penalized."""
    answer = run.outputs.get("answer", "")
    score = 1.0 if len(answer) <= 500 else max(0.0, 1.0 - (len(answer) - 500) / 1000)
    return EvaluationResult(key="conciseness", score=score)


# ── 3. Run evaluation ─────────────────────────────────────────────────────────

async def run_eval():
    results = await aevaluate(
        target,
        data="my-agent-qa-v1",          # dataset name or ID
        evaluators=[correctness_evaluator, length_evaluator],
        experiment_prefix="agent-v2",   # shows in LangSmith UI as "agent-v2-<timestamp>"
        max_concurrency=4,              # parallel evaluation runs
        num_repetitions=1,              # run each example N times (use >1 for variance)
    )

    # Print summary
    print(f"\nExperiment URL: {results.experiment_url}")
    print("\nAggregated scores:")
    summary = results.to_pandas().groupby("feedback.key")["feedback.score"].mean()
    print(summary.to_string())

    return results


if __name__ == "__main__":
    asyncio.run(run_eval())
```

### Comparing Two Versions (Regression Testing)

```python
# compare_versions.py — run both v1 and v2 targets against the same dataset
import asyncio
from langsmith.evaluation import aevaluate


async def target_v1(inputs: dict) -> dict:
    """The current production version."""
    # ... invoke old version ...
    return {"answer": "old answer"}


async def target_v2(inputs: dict) -> dict:
    """The new candidate version."""
    # ... invoke new version ...
    return {"answer": "new answer"}


async def compare():
    evaluators = [correctness_evaluator, length_evaluator]  # from eval_agent.py

    # Run both versions against the same dataset
    results_v1, results_v2 = await asyncio.gather(
        aevaluate(target_v1, data="my-agent-qa-v1", evaluators=evaluators,
                  experiment_prefix="v1-baseline"),
        aevaluate(target_v2, data="my-agent-qa-v1", evaluators=evaluators,
                  experiment_prefix="v2-candidate"),
    )

    # Compare in LangSmith UI by navigating to the dataset and selecting both experiments
    print(f"v1: {results_v1.experiment_url}")
    print(f"v2: {results_v2.experiment_url}")


asyncio.run(compare())
```

---

## Built-In LangSmith Evaluators

```python
from langsmith.evaluation import LangChainStringEvaluator

# Criteria evaluator — LLM grades output on named criteria
criteria_eval = LangChainStringEvaluator(
    "criteria",
    config={
        "criteria": {
            "correctness": "Is the answer factually accurate?",
            "conciseness": "Is the answer concise without missing key points?",
            "harmlessness": "Does the answer avoid harmful content?",
        }
    },
)

# Embedding distance — semantic similarity to reference (no LLM needed)
from langsmith.evaluation import EmbeddingDistanceEvaluator
embedding_eval = EmbeddingDistanceEvaluator()  # uses cosine distance by default

# String distance — character-level similarity (Levenshtein, Jaro-Winkler)
from langsmith.evaluation import StringDistanceEvaluator
string_eval = StringDistanceEvaluator()

# QA relevance — is the answer relevant to the question?
qa_eval = LangChainStringEvaluator("qa")   # uses LLM internally

# JSON validity — does the output parse as valid JSON?
json_eval = LangChainStringEvaluator("json_validity")

# Use any combination in aevaluate:
results = await aevaluate(
    target,
    data="my-dataset",
    evaluators=[criteria_eval, embedding_eval, string_eval],
)
```

---

## Custom Evaluators — Full Patterns

### LLM-as-Judge Pattern

```python
# evaluators/llm_judge.py
from langsmith.schemas import Run, Example
from langsmith.evaluation import EvaluationResult
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
import json


def make_llm_judge(criteria: str, name: str):
    """
    Factory that creates an LLM-as-judge evaluator for any criteria.

    Usage:
        faithfulness = make_llm_judge(
            "Is the answer fully supported by the provided context with no fabricated facts?",
            name="faithfulness",
        )
    """
    llm = ChatAnthropic(model="claude-haiku-4-5", temperature=0)
    prompt = ChatPromptTemplate.from_template("""
Evaluate the following criterion:
{criteria}

Question: {question}
Answer: {answer}
Reference (if available): {reference}

Respond ONLY with JSON: {{"score": 0.0-1.0, "reasoning": "one sentence explaining the score"}}
""")

    def evaluator(run: Run, example: Example) -> EvaluationResult:
        response = (prompt | llm).invoke({
            "criteria":  criteria,
            "question":  example.inputs.get("question", ""),
            "answer":    run.outputs.get("answer", ""),
            "reference": example.outputs.get("answer", "N/A"),
        })
        try:
            data = json.loads(response.content)
            return EvaluationResult(
                key=name,
                score=float(data["score"]),
                comment=data.get("reasoning", ""),
            )
        except Exception as e:
            return EvaluationResult(key=name, score=0.0, comment=f"Parse error: {e}")

    evaluator.__name__ = name
    return evaluator


# Pre-built evaluators using the factory
faithfulness_eval = make_llm_judge(
    "Is every claim in the answer directly supported by the reference? No hallucinations?",
    name="faithfulness",
)

relevance_eval = make_llm_judge(
    "Does the answer directly address what was asked? Is it on-topic?",
    name="relevance",
)

completeness_eval = make_llm_judge(
    "Does the answer cover all important aspects of the question, or is it incomplete?",
    name="completeness",
)
```

### Business Logic Evaluator

```python
# evaluators/business.py
from langsmith.schemas import Run, Example
from langsmith.evaluation import EvaluationResult
import re


def no_pii_evaluator(run: Run, example: Example) -> EvaluationResult:
    """Verify the answer contains no personally identifiable information."""
    answer = run.outputs.get("answer", "")

    # Patterns to check
    patterns = {
        "email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        "phone": r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b",
        "ssn":   r"\b\d{3}-\d{2}-\d{4}\b",
        "credit_card": r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
    }

    found = [name for name, pattern in patterns.items() if re.search(pattern, answer)]

    return EvaluationResult(
        key="no_pii",
        score=0.0 if found else 1.0,
        comment=f"Found PII: {found}" if found else "No PII detected",
    )


def citation_evaluator(run: Run, example: Example) -> EvaluationResult:
    """For RAG: verify answer cites at least one source."""
    answer = run.outputs.get("answer", "")
    has_citation = bool(
        re.search(r"\[Source:", answer) or
        re.search(r"\[\d+\]", answer) or
        "according to" in answer.lower() or
        "source:" in answer.lower()
    )
    return EvaluationResult(
        key="has_citation",
        score=1.0 if has_citation else 0.0,
        comment="Citation found" if has_citation else "No citation in answer",
    )
```

---

## RAG Evaluation

### Retrieval Quality (Are Retrieved Docs Relevant?)

```python
# eval_rag.py
from langsmith.schemas import Run, Example
from langsmith.evaluation import EvaluationResult, aevaluate


async def rag_target(inputs: dict) -> dict:
    """RAG pipeline under test. Returns answer AND retrieved docs."""
    from chain import build_chain  # your RAG chain from lc:rag skill

    chain = build_chain()
    # Return both answer and docs for evaluators to inspect
    retriever = chain.steps[0]["context"].steps[0]  # adjust to your chain structure
    docs = retriever.invoke(inputs["question"])
    answer = chain.invoke(inputs["question"])

    return {
        "answer": answer,
        "retrieved_docs": [d.page_content for d in docs],
    }


def retrieval_relevance_evaluator(run: Run, example: Example) -> EvaluationResult:
    """Are the retrieved documents relevant to the question?"""
    from langchain_anthropic import ChatAnthropic
    from langchain_core.prompts import ChatPromptTemplate
    import json

    docs = run.outputs.get("retrieved_docs", [])
    if not docs:
        return EvaluationResult(key="retrieval_relevance", score=0.0,
                                comment="No documents retrieved")

    llm = ChatAnthropic(model="claude-haiku-4-5", temperature=0)
    prompt = ChatPromptTemplate.from_template("""
Score how relevant these retrieved documents are to the question.
Question: {question}
Documents: {docs}
Score 0.0 (completely irrelevant) to 1.0 (perfectly relevant).
Respond ONLY with JSON: {{"score": 0.0-1.0, "reasoning": "one sentence"}}
""")
    response = (prompt | llm).invoke({
        "question": example.inputs["question"],
        "docs": "\n\n---\n\n".join(docs[:3]),  # limit to first 3 for cost
    })
    try:
        data = json.loads(response.content)
        return EvaluationResult(key="retrieval_relevance", score=float(data["score"]),
                                comment=data.get("reasoning", ""))
    except Exception:
        return EvaluationResult(key="retrieval_relevance", score=0.0, comment="Parse error")


def answer_faithfulness_evaluator(run: Run, example: Example) -> EvaluationResult:
    """Is the answer grounded in the retrieved docs (no hallucinations)?"""
    from langchain_anthropic import ChatAnthropic
    import json

    llm = ChatAnthropic(model="claude-haiku-4-5", temperature=0)
    from langchain_core.prompts import ChatPromptTemplate

    prompt = ChatPromptTemplate.from_template("""
Is every claim in the answer supported by the provided documents?
Score 1.0 if fully grounded, 0.5 if partially, 0.0 if hallucinated.

Answer: {answer}
Documents: {docs}

Respond ONLY with JSON: {{"score": 0.0-1.0, "reasoning": "one sentence"}}
""")
    docs = run.outputs.get("retrieved_docs", ["No documents available"])
    response = (prompt | llm).invoke({
        "answer": run.outputs.get("answer", ""),
        "docs": "\n\n".join(docs[:3]),
    })
    try:
        data = json.loads(response.content)
        return EvaluationResult(key="faithfulness", score=float(data["score"]),
                                comment=data.get("reasoning", ""))
    except Exception:
        return EvaluationResult(key="faithfulness", score=0.0, comment="Parse error")


# Run the RAG evaluation
async def run_rag_eval():
    results = await aevaluate(
        rag_target,
        data="rag-qa-dataset",
        evaluators=[retrieval_relevance_evaluator, answer_faithfulness_evaluator],
        experiment_prefix="rag-v1",
        max_concurrency=3,
    )
    print(f"Results: {results.experiment_url}")
```

### RAGAS Integration

```bash
pip install ragas langchain-openai
```

```python
# eval_ragas.py — RAGAS provides a full suite of RAG metrics
from datasets import Dataset
from ragas import evaluate
from ragas.metrics import (
    faithfulness,          # answer grounded in docs?
    answer_relevancy,      # answer relevant to question?
    context_recall,        # docs cover the reference answer?
    context_precision,     # docs are precise (no noise)?
)
from langchain_openai import ChatOpenAI, OpenAIEmbeddings


# Collect data from your RAG pipeline
questions = ["What is the return policy?", "How do I track my order?"]
answers    = []  # your RAG pipeline outputs
contexts   = []  # retrieved docs for each question
ground_truths = ["30 days with receipt.", "Log into Orders page."]

# Build RAGAS dataset
data = {
    "question":    questions,
    "answer":      answers,
    "contexts":    contexts,      # list of lists of strings
    "ground_truth": ground_truths,
}
dataset = Dataset.from_dict(data)

# Run evaluation
result = evaluate(
    dataset=dataset,
    metrics=[faithfulness, answer_relevancy, context_recall, context_precision],
    llm=ChatOpenAI(model="gpt-4o-mini"),
    embeddings=OpenAIEmbeddings(model="text-embedding-3-small"),
)

print(result.to_pandas()[["question", "faithfulness", "answer_relevancy"]].to_string())
```

---

## CI/CD Integration

### GitHub Actions Workflow

```yaml
# .github/workflows/test.yml
name: Tests

on:
  push:
    branches: [main, develop]
  pull_request:

jobs:
  # ── Fast unit tests on every push ─────────────────────────────────────────
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt pytest pytest-asyncio

      - name: Run unit tests (no LLM calls)
        run: pytest -m "not integration" --timeout=30 -v

  # ── Integration tests on PR merge to main ─────────────────────────────────
  integration-tests:
    runs-on: ubuntu-latest
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    env:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      LANGSMITH_API_KEY: ${{ secrets.LANGSMITH_API_KEY }}
      LANGSMITH_TRACING: "true"
      LANGSMITH_PROJECT: "ci-integration"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
      - run: pip install -r requirements.txt pytest pytest-asyncio
      - name: Run integration tests
        run: pytest -m "integration" --timeout=120 -v

  # ── LangSmith evaluation gate on release ──────────────────────────────────
  eval-gate:
    runs-on: ubuntu-latest
    if: startsWith(github.ref, 'refs/tags/v')
    env:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      LANGSMITH_API_KEY: ${{ secrets.LANGSMITH_API_KEY }}
      LANGSMITH_TRACING: "true"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
      - run: pip install -r requirements.txt langsmith

      - name: Run LangSmith evaluation and check score gate
        run: python ci/eval_gate.py
```

### Evaluation Gate Script

```python
# ci/eval_gate.py — fails CI if quality drops below threshold
import asyncio
import sys
from langsmith.evaluation import aevaluate

# Import your target and evaluators
from eval_agent import target, correctness_evaluator, length_evaluator

# ── Thresholds — adjust to your quality bar ──────────────────────────────────
THRESHOLDS = {
    "correctness": 0.80,   # 80% of answers must be correct
    "conciseness": 0.70,   # 70% of answers must be concise
}

# ── Token budget: limit to 20 examples max to control CI cost ────────────────
MAX_EXAMPLES = 20


async def main():
    print("Running evaluation gate...")
    results = await aevaluate(
        target,
        data="my-agent-qa-v1",
        evaluators=[correctness_evaluator, length_evaluator],
        experiment_prefix="ci-eval",
        max_concurrency=4,
        # Limit examples to control cost
        # (LangSmith picks a random sample if dataset > MAX_EXAMPLES)
    )

    print(f"\nExperiment: {results.experiment_url}")

    # Compute per-metric averages
    df = results.to_pandas()
    scores = {}
    failed = []

    for metric, threshold in THRESHOLDS.items():
        col = f"feedback.{metric}"
        if col in df.columns:
            avg = df[col].dropna().mean()
            scores[metric] = avg
            print(f"  {metric}: {avg:.3f} (threshold: {threshold})")
            if avg < threshold:
                failed.append(f"{metric}={avg:.3f} < {threshold}")

    if failed:
        print(f"\nEVAL GATE FAILED: {', '.join(failed)}")
        print("Investigate at:", results.experiment_url)
        sys.exit(1)
    else:
        print("\nAll thresholds passed. Proceeding with release.")
        sys.exit(0)


asyncio.run(main())
```

---

---

## Section 4 — Prompt Regression Testing

**The problem:** You reword a prompt, eyeball a few outputs, it looks better. You ship it. Three days later support tickets spike. You just changed behaviour on thousands of examples you never checked.

Prompt regression testing makes "does this prompt change help or hurt?" a statistical question with a yes/no answer that CI can enforce.

### Mental model

```
current prompt  ──push to Hub as "baseline"──► LangSmith Prompt Hub
                                                      │
                                     run evaluate() on shared dataset
                                                      │
                              baseline scores   candidate scores
                                       │               │
                                  delta per metric ◄──┘
                                  bootstrap CI
                                  paired t-test
                                       │
                              p < 0.05 AND delta > -0.05?
                              YES → merge    NO → block
```

### Install

```bash
pip install langsmith scipy numpy
```

### Step 1 — Push current prompt to LangSmith Prompt Hub as baseline

```python
# push_baseline.py
# Run this BEFORE you edit the prompt. It snapshots the current version.

from langsmith import Client
from langchain_core.prompts import ChatPromptTemplate

client = Client()

# Your production prompt — pull it however you normally build it
current_prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful customer support agent for Acme Corp. "
               "Answer questions accurately and concisely. "
               "If you don't know the answer, say so."),
    ("human", "{question}"),
])

# Push to Prompt Hub under a versioned name
# The commit hash / date is appended automatically by LangSmith
prompt_url = client.push_prompt(
    "customer-support-baseline",   # repo name in Prompt Hub
    object=current_prompt,
    description="Baseline snapshot before prompt refactor — June 2026",
    tags=["baseline", "production"],
)
print(f"Baseline stored at: {prompt_url}")
# e.g. https://smith.langchain.com/hub/your-org/customer-support-baseline
```

### Step 2 — Target functions for baseline and candidate

```python
# regression_targets.py
import asyncio
from langsmith import Client
from langchain_core.prompts import ChatPromptTemplate
from langchain_anthropic import ChatAnthropic
from langchain_core.output_parsers import StrOutputParser

client = Client()
llm = ChatAnthropic(model="claude-haiku-4-5", temperature=0)


async def target_baseline(inputs: dict) -> dict:
    """Runs the STORED baseline prompt from LangSmith Prompt Hub."""
    # pull_prompt fetches the version you pushed earlier
    baseline_prompt = client.pull_prompt("customer-support-baseline")
    chain = baseline_prompt | llm | StrOutputParser()
    answer = await chain.ainvoke({"question": inputs["question"]})
    return {"answer": answer}


async def target_candidate(inputs: dict) -> dict:
    """Runs the NEW candidate prompt you want to ship."""
    candidate_prompt = ChatPromptTemplate.from_messages([
        ("system", "You are an expert customer support agent for Acme Corp. "
                   "Be direct, accurate, and empathetic. "
                   "Acknowledge the customer's frustration when relevant. "
                   "If you don't know, say so clearly and offer next steps."),
        ("human", "{question}"),
    ])
    chain = candidate_prompt | llm | StrOutputParser()
    answer = await chain.ainvoke({"question": inputs["question"]})
    return {"answer": answer}
```

### Step 3 — Shared evaluators

```python
# regression_evaluators.py
import json
from langsmith.schemas import Run, Example
from langsmith.evaluation import EvaluationResult
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate

_judge_llm = ChatAnthropic(model="claude-haiku-4-5", temperature=0)

_JUDGE_PROMPT = ChatPromptTemplate.from_template("""
You are a strict evaluator. Score the AI answer on the criterion below.

Criterion: {criterion}
Question: {question}
Reference answer: {reference}
AI answer: {answer}

Respond ONLY with JSON: {{"score": 0.0-1.0, "reasoning": "one sentence"}}
""")


def _judge(criterion: str, key: str):
    def evaluator(run: Run, example: Example) -> EvaluationResult:
        try:
            resp = (_JUDGE_PROMPT | _judge_llm).invoke({
                "criterion": criterion,
                "question":  example.inputs.get("question", ""),
                "reference": example.outputs.get("answer", "N/A"),
                "answer":    run.outputs.get("answer", ""),
            })
            data = json.loads(resp.content)
            return EvaluationResult(key=key, score=float(data["score"]),
                                    comment=data.get("reasoning", ""))
        except Exception as e:
            return EvaluationResult(key=key, score=0.0, comment=f"Parse error: {e}")
    evaluator.__name__ = key
    return evaluator


correctness_eval  = _judge("Is the answer factually correct vs. the reference?", "correctness")
empathy_eval      = _judge("Does the answer acknowledge user frustration appropriately?", "empathy")
conciseness_eval  = _judge("Is the answer concise — no filler, no repetition?", "conciseness")

ALL_EVALUATORS = [correctness_eval, empathy_eval, conciseness_eval]
```

### Step 4 — Delta calculation with bootstrap confidence intervals

Small datasets are noisy. 20 examples is not enough to declare a winner without quantifying your uncertainty. Bootstrap CIs tell you the plausible range of the true delta.

```python
# regression_stats.py
import numpy as np
from scipy import stats
from dataclasses import dataclass


@dataclass
class MetricDelta:
    metric: str
    baseline_mean: float
    candidate_mean: float
    delta: float               # candidate - baseline (positive = better)
    ci_lower: float            # 95% bootstrap CI lower bound on delta
    ci_upper: float            # 95% bootstrap CI upper bound on delta
    p_value: float             # paired t-test p-value
    significant: bool          # p_value < 0.05
    regression: bool           # delta < -0.05 (meaningful drop)

    def __str__(self) -> str:
        direction = "IMPROVED" if self.delta > 0.02 else ("REGRESSED" if self.regression else "NEUTRAL")
        sig = "significant" if self.significant else "not significant"
        return (
            f"{self.metric}: {self.baseline_mean:.3f} -> {self.candidate_mean:.3f} "
            f"(delta={self.delta:+.3f}, 95%CI=[{self.ci_lower:+.3f},{self.ci_upper:+.3f}], "
            f"p={self.p_value:.3f}, {sig}) [{direction}]"
        )


def bootstrap_ci(
    baseline_scores: list[float],
    candidate_scores: list[float],
    n_bootstrap: int = 2000,
    confidence: float = 0.95,
    seed: int = 42,
) -> tuple[float, float]:
    """
    Non-parametric bootstrap confidence interval for the mean delta.

    Why bootstrap instead of a closed-form CI?
    LLM eval scores are bounded [0,1], often bimodal (0 or 1),
    and with n<50 the normality assumption for a standard t-interval is shaky.
    Bootstrap makes no distributional assumptions.
    """
    rng = np.random.default_rng(seed)
    baseline = np.array(baseline_scores)
    candidate = np.array(candidate_scores)
    n = len(baseline)

    deltas = []
    for _ in range(n_bootstrap):
        idx = rng.integers(0, n, size=n)
        deltas.append(candidate[idx].mean() - baseline[idx].mean())

    alpha = 1 - confidence
    lo = float(np.percentile(deltas, 100 * alpha / 2))
    hi = float(np.percentile(deltas, 100 * (1 - alpha / 2)))
    return lo, hi


def compute_metric_delta(
    metric: str,
    baseline_scores: list[float],
    candidate_scores: list[float],
) -> MetricDelta:
    """
    Compute delta + CI + paired t-test for a single metric.

    Paired t-test (not independent): we compare SAME examples across prompts,
    so the natural pairing removes between-example variance and gives us more power.
    """
    b = np.array(baseline_scores)
    c = np.array(candidate_scores)

    delta = float(c.mean() - b.mean())
    ci_lo, ci_hi = bootstrap_ci(baseline_scores, candidate_scores)

    # Paired t-test: H0 = no difference in per-example scores
    t_stat, p_value = stats.ttest_rel(c, b)

    return MetricDelta(
        metric=metric,
        baseline_mean=float(b.mean()),
        candidate_mean=float(c.mean()),
        delta=delta,
        ci_lower=ci_lo,
        ci_upper=ci_hi,
        p_value=float(p_value),
        significant=bool(p_value < 0.05),
        regression=bool(delta < -0.05),
    )
```

### Step 5 — Minimum sample size calculator

Before running, check if your dataset is big enough to detect meaningful drops.

```python
# sample_size.py
import math
from scipy import stats


def min_sample_size(
    effect_size: float = 0.05,    # smallest delta you care about
    alpha: float = 0.05,          # false positive rate (Type I)
    power: float = 0.80,          # probability of detecting a real effect (1 - Type II)
    score_std: float = 0.25,      # estimated std dev of per-example scores (0.25 is typical for 0/1 eval)
) -> int:
    """
    Returns the minimum number of examples needed to detect `effect_size`
    with the given statistical power using a paired t-test.

    Rule of thumb: with binary (0/1) LLM judge scores, std dev is ~0.25-0.45.
    For more continuous scores (0.0-1.0), std dev is ~0.15-0.30.

    Example:
        >>> min_sample_size(effect_size=0.05, power=0.80)
        99
        >>> min_sample_size(effect_size=0.10, power=0.80)
        25
    """
    # Standardised effect size = raw effect / std dev of differences
    # For paired design, std of differences ≈ std of individual scores * sqrt(2) * (1-r)^0.5
    # Conservative approximation: treat as independent (slightly overestimates n)
    cohen_d = effect_size / score_std
    # From power analysis formula for one-sample t-test on differences
    z_alpha = stats.norm.ppf(1 - alpha / 2)
    z_beta  = stats.norm.ppf(power)
    n = math.ceil(((z_alpha + z_beta) / cohen_d) ** 2)
    return n


# Print a quick reference table
if __name__ == "__main__":
    print("Minimum sample sizes for prompt regression testing\n")
    print(f"{'Effect size':>12} | {'n (power=0.80)':>14} | {'n (power=0.90)':>14}")
    print("-" * 46)
    for effect in [0.03, 0.05, 0.08, 0.10, 0.15, 0.20]:
        n80 = min_sample_size(effect_size=effect, power=0.80)
        n90 = min_sample_size(effect_size=effect, power=0.90)
        print(f"{effect:>12.2f} | {n80:>14d} | {n90:>14d}")
    # Output:
    # Effect size | n (power=0.80) | n (power=0.90)
    # ----------------------------------------------
    #        0.03 |            549 |            735
    #        0.05 |            198 |            265
    #        0.08 |             78 |            104
    #        0.10 |             50 |             67
    #        0.15 |             23 |             30
    #        0.20 |             13 |             17
```

### Step 6 — Complete `regression_test()` function

```python
# prompt_regression.py — the full regression test pipeline
import asyncio
import sys
from typing import NamedTuple

import numpy as np
import pandas as pd
from langsmith.evaluation import aevaluate

from regression_targets    import target_baseline, target_candidate
from regression_evaluators import ALL_EVALUATORS
from regression_stats      import compute_metric_delta, MetricDelta, min_sample_size


# ── Configuration ─────────────────────────────────────────────────────────────
DATASET_NAME = "customer-support-qa-v1"   # LangSmith dataset to evaluate against
REGRESSION_THRESHOLD = -0.05              # fail if any metric drops more than this
P_VALUE_THRESHOLD    = 0.05              # fail if change is not statistically significant


class RegressionResult(NamedTuple):
    passed: bool
    deltas: list[MetricDelta]
    baseline_url: str
    candidate_url: str
    summary: str


async def regression_test(
    dataset: str = DATASET_NAME,
    regression_threshold: float = REGRESSION_THRESHOLD,
    p_value_threshold: float = P_VALUE_THRESHOLD,
) -> RegressionResult:
    """
    Run baseline and candidate against the same dataset, compute per-metric
    deltas with bootstrap CIs and paired t-tests, and return pass/fail.

    CI gate logic (fail if EITHER condition):
      1. delta < regression_threshold  (actual performance drop > threshold)
      2. delta is negative AND p_value > p_value_threshold
         (we can't confirm the drop is real — but we're not confident it isn't)

    Note on condition 2: we do NOT require p < 0.05 for improvements (that would
    penalise small positive changes). We only require significance when the delta
    is negative, so we don't block on noise that looks bad.
    """
    print(f"Running regression test against dataset: {dataset}")
    print(f"Minimum recommended sample size: {min_sample_size()} examples\n")

    # Run both targets in parallel to save time
    baseline_results, candidate_results = await asyncio.gather(
        aevaluate(
            target_baseline,
            data=dataset,
            evaluators=ALL_EVALUATORS,
            experiment_prefix="regression-baseline",
            max_concurrency=4,
        ),
        aevaluate(
            target_candidate,
            data=dataset,
            evaluators=ALL_EVALUATORS,
            experiment_prefix="regression-candidate",
            max_concurrency=4,
        ),
    )

    # Extract per-example scores from both experiments
    baseline_df  = baseline_results.to_pandas()
    candidate_df = candidate_results.to_pandas()

    # Align on example_id so we have matched pairs
    metric_keys = [col.replace("feedback.", "") for col in baseline_df.columns
                   if col.startswith("feedback.")]

    deltas: list[MetricDelta] = []
    failures: list[str] = []

    for metric in metric_keys:
        col = f"feedback.{metric}"
        if col not in baseline_df.columns or col not in candidate_df.columns:
            continue

        # Inner join on example ID to ensure same examples
        merged = baseline_df[["example_id", col]].merge(
            candidate_df[["example_id", col]],
            on="example_id",
            suffixes=("_base", "_cand"),
        ).dropna()

        if len(merged) < 10:
            print(f"  WARNING: only {len(merged)} matched pairs for {metric} — CIs will be wide")

        b_scores = merged[f"{col}_base"].tolist()
        c_scores = merged[f"{col}_cand"].tolist()

        delta = compute_metric_delta(metric, b_scores, c_scores)
        deltas.append(delta)

        # Gate check
        if delta.delta < regression_threshold:
            failures.append(
                f"{metric}: delta={delta.delta:+.3f} < threshold={regression_threshold}"
            )
        elif delta.delta < 0 and delta.p_value > p_value_threshold:
            failures.append(
                f"{metric}: negative delta ({delta.delta:+.3f}) but p={delta.p_value:.3f} "
                f"> {p_value_threshold} — cannot confirm change is noise"
            )

    passed = len(failures) == 0

    summary_lines = ["", "=" * 65, "PROMPT REGRESSION REPORT", "=" * 65]
    for d in deltas:
        summary_lines.append(str(d))
    summary_lines.append("")
    if passed:
        summary_lines.append("RESULT: PASSED — candidate prompt is safe to ship")
    else:
        summary_lines.append("RESULT: FAILED")
        for f in failures:
            summary_lines.append(f"  FAIL: {f}")
    summary_lines += [
        "",
        f"Baseline experiment:  {baseline_results.experiment_url}",
        f"Candidate experiment: {candidate_results.experiment_url}",
        "=" * 65,
    ]
    summary = "\n".join(summary_lines)
    print(summary)

    return RegressionResult(
        passed=passed,
        deltas=deltas,
        baseline_url=baseline_results.experiment_url,
        candidate_url=candidate_results.experiment_url,
        summary=summary,
    )


if __name__ == "__main__":
    result = asyncio.run(regression_test())
    sys.exit(0 if result.passed else 1)
```

### GitHub Actions job

Add this job to your `.github/workflows/test.yml` alongside the existing jobs:

```yaml
  # ── Prompt regression gate on PRs that touch prompts ─────────────────────
  prompt-regression:
    runs-on: ubuntu-latest
    # Only run when prompt files change — saves money on unrelated PRs
    if: |
      github.event_name == 'pull_request' &&
      contains(github.event.pull_request.labels.*.name, 'prompt-change')
    env:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      LANGSMITH_API_KEY: ${{ secrets.LANGSMITH_API_KEY }}
      LANGSMITH_TRACING: "true"
      LANGSMITH_PROJECT: "prompt-regression"
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt langsmith scipy numpy

      - name: Push candidate prompt baseline (first run only — idempotent)
        run: python push_baseline.py
        continue-on-error: true   # don't fail if baseline already exists

      - name: Run prompt regression test
        run: python prompt_regression.py
        # Script exits 1 on failure — CI will block the PR

      - name: Upload regression report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: regression-report
          path: regression_report.txt
          retention-days: 30
```

**Workflow:** when a developer changes a prompt, they add the `prompt-change` label to their PR. CI runs the regression test. If any metric drops > 5% with statistical significance, the PR is blocked. The LangSmith experiment URLs appear in the job logs so the developer can dig into individual failures.

### Common regression testing mistakes

| Mistake | Fix |
|---|---|
| Dataset too small | Run `min_sample_size()` first. Need ≥50 examples to detect a 0.10 delta at 80% power |
| Requiring p<0.05 for improvements | Only gate on p-value for *negative* deltas — improvements don't need to be significant to ship |
| Using the same dataset for every change | Curate a domain-specific dataset; a generic Q&A dataset won't catch domain regressions |
| Forgetting to push baseline before editing | Run `push_baseline.py` as the first step, before touching any prompt file |
| Evaluator LLM is too cheap | Claude Haiku is fine for binary correct/incorrect; for nuanced criteria use Sonnet |
| Not pairing scores on `example_id` | If you use `.mean()` without alignment, you're comparing apples to oranges |

---

## Section 5 — RAGAS Evaluation (First-Class RAG Eval)

RAGAS (Retrieval-Augmented Generation Assessment) is a purpose-built framework for measuring RAG pipeline quality. It covers the two axes that matter: **retrieval quality** (did we fetch the right context?) and **generation quality** (did the LLM use that context faithfully?).

The generic LLM-as-judge approach from Section 3 requires you to write and maintain your own judge prompts. RAGAS gives you battle-tested, research-backed metrics out of the box.

### Install

```bash
pip install ragas datasets langchain-anthropic
# ragas uses HuggingFace datasets as its data format
# langchain-anthropic for the judge LLM (any LangChain LLM works)
```

### The 4 core metrics — what they measure

| Metric | What it measures | Requires ground truth? | Score of 0.5 means... | Score of 0.8 means... |
|---|---|---|---|---|
| `faithfulness` | Are all claims in the answer directly supported by the retrieved context? Catches hallucination. | No | Half the claims are made up — serious hallucination problem | Almost all claims are grounded; minor unsupported details |
| `answer_relevancy` | Does the answer actually address the question asked? Penalises off-topic, verbose, or evasive answers. | No | Answer is tangentially related — probably grabbed wrong docs | Answer is focused and on-topic |
| `context_precision` | Of the retrieved chunks, what fraction are actually relevant to answering the question? Measures retrieval precision (signal-to-noise). | Yes | Half the retrieved context is noise — retriever is too broad | Retrieved context is mostly useful |
| `context_recall` | Does the retrieved context contain everything needed to answer the question (per the ground truth)? Measures retrieval recall. | Yes | Half the reference answer can't be derived from retrieved context — retriever is missing key docs | Most of the answer is inferable from retrieved context |

**Which metrics to use when:**
- No ground truth labels → use `faithfulness` + `answer_relevancy` (both are reference-free)
- Have ground truth → add `context_precision` + `context_recall` for the full picture
- Optimising retriever → focus on `context_precision` + `context_recall`
- Optimising generator prompt → focus on `faithfulness` + `answer_relevancy`

### Collecting data from your RAG pipeline

```python
# collect_ragas_data.py
# Run your RAG pipeline on a set of questions, capturing everything RAGAS needs.

import asyncio
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

# --- Replace with your actual RAG components ---
from chain import build_retriever  # returns a LangChain retriever


async def run_rag_pipeline(question: str) -> dict:
    """
    Run one question through the RAG pipeline and return everything RAGAS needs.
    Returns: question, answer, contexts (list of strings), ground_truth (optional).
    """
    retriever = build_retriever()
    llm = ChatAnthropic(model="claude-haiku-4-5", temperature=0)

    # Step 1: retrieve
    docs = await retriever.ainvoke(question)
    contexts = [doc.page_content for doc in docs]

    # Step 2: generate
    prompt = ChatPromptTemplate.from_messages([
        ("system", "Answer the question using ONLY the provided context. "
                   "If the context doesn't contain the answer, say 'I don't know based on the provided information.'"),
        ("human", "Context:\n{context}\n\nQuestion: {question}"),
    ])
    chain = prompt | llm | StrOutputParser()
    answer = await chain.ainvoke({
        "context": "\n\n".join(contexts),
        "question": question,
    })

    return {
        "question": question,
        "answer":   answer,
        "contexts": contexts,   # RAGAS expects a list of strings per question
    }


async def collect_dataset(
    questions: list[str],
    ground_truths: list[str] | None = None,
) -> dict:
    """Collect RAGAS-format data for a list of questions."""
    results = await asyncio.gather(*[run_rag_pipeline(q) for q in questions])

    data = {
        "question": [r["question"] for r in results],
        "answer":   [r["answer"]   for r in results],
        "contexts": [r["contexts"] for r in results],  # list of lists
    }
    if ground_truths:
        data["ground_truth"] = ground_truths

    return data
```

### Synchronous evaluation (simple)

```python
# eval_ragas_sync.py
from datasets import Dataset
from ragas import evaluate
from ragas.metrics import (
    faithfulness,
    answer_relevancy,
    context_precision,
    context_recall,
)
from langchain_anthropic import ChatAnthropic
from langchain_community.embeddings import HuggingFaceEmbeddings  # free, no API key


# ── Sample data — replace with output of collect_dataset() ───────────────────
questions = [
    "What is the return policy for electronics?",
    "How long does standard shipping take?",
    "Can I return a opened software product?",
]
answers = [
    "Electronics can be returned within 30 days with original packaging.",
    "Standard shipping takes 5-7 business days.",
    "Opened software cannot be returned unless defective.",
]
contexts = [
    ["Our electronics return policy: 30 days from purchase date. "
     "Items must be in original packaging with all accessories."],
    ["Shipping options: Standard (5-7 days), Express (2-3 days), Overnight (next day)."],
    ["Software return policy: Unopened software may be returned within 15 days. "
     "Opened software is non-returnable except in cases of manufacturing defects."],
]
ground_truths = [
    "Electronics can be returned within 30 days with original packaging.",
    "Standard shipping takes 5-7 business days.",
    "Opened software cannot be returned unless defective.",
]

# ── Build dataset ─────────────────────────────────────────────────────────────
dataset = Dataset.from_dict({
    "question":     questions,
    "answer":       answers,
    "contexts":     contexts,       # MUST be list[list[str]]
    "ground_truth": ground_truths,  # omit if you don't have labels
})

# ── Configure judge LLM and embeddings ───────────────────────────────────────
# RAGAS uses LLM for faithfulness + relevancy, embeddings for answer_relevancy
judge_llm  = ChatAnthropic(model="claude-haiku-4-5", temperature=0)
embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
# Alternative: use OpenAI embeddings if you have a key
# from langchain_openai import OpenAIEmbeddings
# embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

# ── Run evaluation ────────────────────────────────────────────────────────────
result = evaluate(
    dataset=dataset,
    metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
    llm=judge_llm,
    embeddings=embeddings,
    raise_exceptions=False,   # don't crash on single-example failures
)

# ── Print results ─────────────────────────────────────────────────────────────
df = result.to_pandas()
print("\nRAGAS Evaluation Results")
print("=" * 50)
print(f"faithfulness:      {df['faithfulness'].mean():.3f}")
print(f"answer_relevancy:  {df['answer_relevancy'].mean():.3f}")
print(f"context_precision: {df['context_precision'].mean():.3f}")
print(f"context_recall:    {df['context_recall'].mean():.3f}")
print("\nPer-question breakdown:")
print(df[["question", "faithfulness", "answer_relevancy",
          "context_precision", "context_recall"]].to_string(index=False))
```

### Async evaluation for speed

RAGAS evaluates each example independently. Running them in parallel is 4-8x faster for datasets > 20 examples.

```python
# eval_ragas_async.py
import asyncio
from datasets import Dataset
from ragas import EvaluationDataset, SingleTurnSample
from ragas.metrics import faithfulness, answer_relevancy, context_precision, context_recall
from ragas import evaluate
from langchain_anthropic import ChatAnthropic
from langchain_community.embeddings import HuggingFaceEmbeddings


async def evaluate_ragas_async(
    questions:     list[str],
    answers:       list[str],
    contexts:      list[list[str]],
    ground_truths: list[str] | None = None,
    max_workers:   int = 8,
) -> dict[str, float]:
    """
    Async RAGAS evaluation. Uses RAGAS's built-in async support.

    Returns a dict of metric name -> mean score.
    """
    # Build RAGAS dataset
    samples = []
    for i, (q, a, c) in enumerate(zip(questions, answers, contexts)):
        sample = SingleTurnSample(
            user_input=q,
            response=a,
            retrieved_contexts=c,
            reference=ground_truths[i] if ground_truths else None,
        )
        samples.append(sample)

    eval_dataset = EvaluationDataset(samples=samples)

    metrics = [faithfulness, answer_relevancy]
    if ground_truths:
        metrics += [context_precision, context_recall]

    judge_llm  = ChatAnthropic(model="claude-haiku-4-5", temperature=0)
    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2"
    )

    # RAGAS evaluate() is synchronous but internally manages async LLM calls.
    # Run in a thread pool to avoid blocking the event loop if called from async code.
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: evaluate(
            dataset=eval_dataset,
            metrics=metrics,
            llm=judge_llm,
            embeddings=embeddings,
            raise_exceptions=False,
        ),
    )

    df = result.to_pandas()
    return {
        metric_name: float(df[metric_name].mean())
        for metric_name in ["faithfulness", "answer_relevancy",
                             "context_precision", "context_recall"]
        if metric_name in df.columns
    }
```

### LangSmith integration — log RAGAS scores as run feedback

This connects RAGAS scores to your LangSmith traces so you can see per-run quality scores in the UI alongside the traces themselves.

```python
# ragas_langsmith.py
import asyncio
import uuid
from langsmith import Client
from langsmith.run_helpers import traceable
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from datasets import Dataset
from ragas import evaluate
from ragas.metrics import faithfulness, answer_relevancy

langsmith_client = Client()


@traceable(name="rag_pipeline", run_type="chain")
async def traced_rag_pipeline(question: str, retriever) -> dict:
    """
    RAG pipeline wrapped with @traceable.
    The run_id is accessible so we can attach RAGAS feedback to this specific run.
    """
    docs    = await retriever.ainvoke(question)
    contexts = [d.page_content for d in docs]

    llm    = ChatAnthropic(model="claude-haiku-4-5", temperature=0)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "Answer using only the provided context."),
        ("human",  "Context:\n{context}\n\nQuestion: {question}"),
    ])
    answer = await (prompt | llm | StrOutputParser()).ainvoke({
        "context":  "\n\n".join(contexts),
        "question": question,
    })

    return {"answer": answer, "contexts": contexts}


async def evaluate_and_log_to_langsmith(
    questions:     list[str],
    ground_truths: list[str] | None = None,
    retriever=None,
) -> None:
    """
    Run the RAG pipeline, evaluate with RAGAS, and log scores back to LangSmith
    as feedback on each run so scores appear in the trace UI.
    """
    run_ids      = []
    answers      = []
    all_contexts = []

    # Run pipeline, capture LangSmith run IDs
    for question in questions:
        # LangSmith assigns a run_id to each @traceable invocation.
        # We generate it manually so we can reference it after the call.
        run_id = str(uuid.uuid4())
        result = await traced_rag_pipeline(
            question, retriever,
            langsmith_extra={"run_id": run_id},  # inject run_id into trace
        )
        run_ids.append(run_id)
        answers.append(result["answer"])
        all_contexts.append(result["contexts"])

    # Run RAGAS
    data: dict = {
        "question": questions,
        "answer":   answers,
        "contexts": all_contexts,
    }
    if ground_truths:
        data["ground_truth"] = ground_truths

    from langchain_community.embeddings import HuggingFaceEmbeddings
    result = evaluate(
        dataset=Dataset.from_dict(data),
        metrics=[faithfulness, answer_relevancy],
        llm=ChatAnthropic(model="claude-haiku-4-5", temperature=0),
        embeddings=HuggingFaceEmbeddings(
            model_name="sentence-transformers/all-MiniLM-L6-v2"
        ),
        raise_exceptions=False,
    )
    df = result.to_pandas()

    # Log RAGAS scores back to each LangSmith run as structured feedback
    for i, run_id in enumerate(run_ids):
        row = df.iloc[i]

        for metric in ["faithfulness", "answer_relevancy"]:
            if metric in row and not __import__("math").isnan(row[metric]):
                langsmith_client.create_feedback(
                    run_id=run_id,
                    key=f"ragas.{metric}",
                    score=float(row[metric]),
                    comment=f"RAGAS {metric} score for this run",
                )
        print(f"Run {run_id[:8]}... "
              f"faithfulness={row.get('faithfulness', 'N/A'):.2f}  "
              f"answer_relevancy={row.get('answer_relevancy', 'N/A'):.2f}")
```

### Interpreting RAGAS scores

**faithfulness** — "does the answer stick to the context?"

| Score | What it means | Action |
|---|---|---|
| 0.9 - 1.0 | Virtually all claims are grounded in retrieved context | Nothing — this is excellent |
| 0.7 - 0.9 | Mostly grounded; occasional extrapolations | Review low-scoring examples; tighten system prompt |
| 0.5 - 0.7 | Half the claims are unsupported — significant hallucination | Rewrite system prompt to require context citation; check if context is actually relevant |
| < 0.5 | Model is mostly ignoring context and answering from parametric memory | The retriever or chunking is likely broken; context isn't making it into the prompt |

**answer_relevancy** — "does the answer address the question?"

| Score | What it means | Action |
|---|---|---|
| 0.85+ | Answer is tightly focused on the question | Good |
| 0.65 - 0.85 | Mostly relevant with some padding | Tighten system prompt: "be concise, don't add unrequested information" |
| < 0.65 | Answer is off-topic or evasive | Check if retriever is fetching wrong documents; check for prompt injection |

**context_precision** — "is the retrieved context signal or noise?"

| Score | Interpretation |
|---|---|
| 0.8+ | Retriever is precise — what it fetches is actually needed |
| 0.5 - 0.8 | Half the context is noise — try smaller `k`, MMR, or reranking |
| < 0.5 | Retriever is too broad; consider filtering by metadata or semantic threshold |

**context_recall** — "did the retriever find everything needed?"

| Score | Interpretation |
|---|---|
| 0.8+ | Retriever finds most of the relevant information |
| 0.5 - 0.8 | Missing relevant chunks — try larger `k`, hybrid search, or re-chunking |
| < 0.5 | Retriever is missing critical information — check embedding model, chunk size |

### When to run RAGAS in CI

```
Every commit             → Skip RAGAS (too slow, too expensive)
Every PR to main         → Run faithfulness + answer_relevancy (reference-free, cheap)
On RAG-related changes   → Run all 4 metrics (add context_precision + context_recall)
Before releases          → Full RAGAS on the complete evaluation dataset
After re-indexing corpus → Full RAGAS to confirm quality didn't drop
```

**What counts as a "RAG-related change":** changes to retriever config (k, score threshold), embedding model, chunk size/overlap, system prompt, document preprocessing, or vector store.

### Complete RAGAS CI pipeline

```python
# ci/ragas_gate.py — run as part of CI on RAG changes, exit 1 if quality drops
import asyncio
import sys
from datasets import Dataset
from ragas import evaluate
from ragas.metrics import (
    faithfulness,
    answer_relevancy,
    context_precision,
    context_recall,
)
from langchain_anthropic import ChatAnthropic
from langchain_community.embeddings import HuggingFaceEmbeddings

# ── Thresholds ────────────────────────────────────────────────────────────────
THRESHOLDS = {
    "faithfulness":      0.75,   # < 0.75 means too much hallucination
    "answer_relevancy":  0.70,   # < 0.70 means answers are off-topic
    "context_precision": 0.60,   # < 0.60 means too much retrieval noise
    "context_recall":    0.70,   # < 0.70 means retriever is missing key docs
}


def load_eval_dataset() -> Dataset:
    """
    Load your RAG evaluation dataset.
    In practice: load from a JSON file checked into the repo, or from LangSmith.
    """
    # Example: load from a JSON file
    import json
    from pathlib import Path

    raw = json.loads(Path("ci/rag_eval_data.json").read_text())
    return Dataset.from_dict({
        "question":     [r["question"]     for r in raw],
        "answer":       [r["answer"]       for r in raw],
        "contexts":     [r["contexts"]     for r in raw],
        "ground_truth": [r["ground_truth"] for r in raw],
    })


async def run_ragas_gate() -> None:
    """Evaluate the RAG pipeline and exit 1 if any metric is below threshold."""
    print("Loading evaluation dataset...")
    dataset = load_eval_dataset()
    n = len(dataset)
    print(f"Loaded {n} examples")

    # Run your actual RAG pipeline to collect answers + contexts
    # (replace this with your real pipeline)
    from collect_ragas_data import collect_dataset
    questions     = dataset["question"]
    ground_truths = dataset["ground_truth"]
    live_data     = await collect_dataset(questions, ground_truths)

    live_dataset = Dataset.from_dict(live_data)

    print("\nRunning RAGAS evaluation...")
    judge_llm  = ChatAnthropic(model="claude-haiku-4-5", temperature=0)
    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2"
    )

    result = evaluate(
        dataset=live_dataset,
        metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
        llm=judge_llm,
        embeddings=embeddings,
        raise_exceptions=False,
    )

    df = result.to_pandas()

    # ── Report and gate ───────────────────────────────────────────────────────
    print("\n" + "=" * 55)
    print("RAGAS EVALUATION REPORT")
    print("=" * 55)

    failures = []
    for metric, threshold in THRESHOLDS.items():
        if metric not in df.columns:
            continue
        score = float(df[metric].mean())
        status = "PASS" if score >= threshold else "FAIL"
        print(f"  {metric:<22} {score:.3f}  (threshold: {threshold})  [{status}]")
        if score < threshold:
            failures.append(f"{metric}={score:.3f} < {threshold}")

    print("=" * 55)

    if failures:
        print("\nRAGAS GATE FAILED:")
        for f in failures:
            print(f"  {f}")
        print("\nSuggested actions:")
        for f in failures:
            metric = f.split("=")[0]
            suggestions = {
                "faithfulness":      "Tighten system prompt: 'answer ONLY from the provided context'",
                "answer_relevancy":  "Check if retriever fetches off-topic documents; add metadata filters",
                "context_precision": "Reduce k, add reranker, or raise similarity threshold",
                "context_recall":    "Increase k, try hybrid search, or reduce chunk size",
            }
            print(f"  {metric}: {suggestions.get(metric, 'investigate low-scoring examples')}")
        sys.exit(1)
    else:
        print("\nAll RAGAS thresholds passed.")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(run_ragas_gate())
```

Add the RAGAS gate to your workflow:

```yaml
  # ── RAGAS quality gate on RAG-related changes ─────────────────────────────
  ragas-eval:
    runs-on: ubuntu-latest
    # Trigger on PRs labelled 'rag-change', or on changes to specific paths
    if: |
      github.event_name == 'pull_request' &&
      (contains(github.event.pull_request.labels.*.name, 'rag-change') ||
       contains(github.event.pull_request.changed_files, 'chain.py') ||
       contains(github.event.pull_request.changed_files, 'retriever.py'))
    env:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      LANGSMITH_API_KEY: ${{ secrets.LANGSMITH_API_KEY }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt ragas datasets sentence-transformers

      - name: Run RAGAS evaluation gate
        run: python ci/ragas_gate.py
```

### Common RAGAS mistakes

| Mistake | Fix |
|---|---|
| `contexts` is `list[str]` instead of `list[list[str]]` | Each example must have a list of doc strings: `[["doc1", "doc2"], ["doc3"]]` |
| Using `faithfulness` alone | Always pair it with `answer_relevancy` — faithful but irrelevant answers also fail users |
| Setting thresholds before seeing your baseline | Run RAGAS on your current pipeline first; set thresholds at baseline - 0.05 |
| Running RAGAS on every commit | Gate it behind a path filter or PR label — it costs ~$0.01/example with Haiku |
| Using GPT-4o as judge LLM | `claude-haiku-4-5` or `gpt-4o-mini` give similar RAGAS scores at 10x lower cost |
| Ignoring `context_recall` | High `faithfulness` + low `context_recall` = answers that are correct but incomplete |
| Not versioning the eval dataset | Check `ci/rag_eval_data.json` into git so scores are comparable across commits |

---

## Quick Reference

| Goal | Pattern | Speed | Cost |
|---|---|---|---|
| Test routing logic | Unit test node directly | Fast | Free |
| Mock LLM in a chain | `FakeListChatModel` | Fast | Free |
| Test graph end-to-end | Compile with MemorySaver + FakeListChatModel | Fast | Free |
| Verify tool behavior | Call `tool.invoke({...})` directly | Fast | Free |
| Test against real LLM | `@pytest.mark.integration` + `claude-haiku-4-5` | Medium | Low |
| Measure output quality | `aevaluate()` + LLM-as-judge evaluators | Slow | Medium |
| Regression comparison | Run two targets against same dataset | Slow | Medium |
| RAG retrieval quality | retrieval\_relevance + faithfulness evaluators | Slow | Medium |
| CI quality gate | `eval_gate.py` with score thresholds | Slow | Medium |
| Prompt regression test | `regression_test()` with paired t-test + bootstrap CI | Slow | Medium |
| RAG quality (all metrics) | RAGAS `evaluate()` with 4 core metrics | Slow | Low-Medium |
| Log RAGAS to LangSmith | `create_feedback()` per run with RAGAS scores | Slow | Low-Medium |

## Common Mistakes

| Mistake | Fix |
|---|---|
| Making real LLM calls in unit tests | Use `FakeListChatModel` — deterministic, free, instant |
| `FakeListChatModel` runs out of responses | Add more items to the `responses=[]` list |
| Forgetting `thread_id` in integration tests | Always set `configurable: {thread_id: "..."}` |
| LangSmith evaluator crashes on missing key | Guard with `.get("key", "")` before accessing `run.outputs` |
| Eval costs spiraling in CI | Use `max_concurrency=4` and limit dataset size to ≤20 examples in CI |
| Using GPT-4o in evaluators | Use `claude-haiku-4-5` or `gpt-4o-mini` for judge LLM — 10x cheaper |
| Dataset has no reference outputs | Add `outputs` when creating examples — required for correctness eval |
| Comparing experiments by memory | Use LangSmith UI comparison view — pass both experiment names |
| pytest-asyncio not configured | Add `asyncio_mode = auto` to `pytest.ini` |
| Prompt change with no regression test | Push baseline to Hub first; run `regression_test()` before merging |
| RAGAS `contexts` wrong type | Must be `list[list[str]]` — one inner list of doc strings per question |
| Regression test dataset too small | Run `min_sample_size()` — need ≥50 examples to detect 0.10 delta at 80% power |
