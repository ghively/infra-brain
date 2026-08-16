"""Tests for the framework-based LLMAgent.reason() (Wave 4 item 4.2).

The hand-rolled tool loop is gone; reason() now drives langchain's
create_agent (the LangGraph prebuilt ReAct successor) with the shared
checkpointer and the safety callbacks in the runtime config.
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError
from openai import APIConnectionError as OpenAIAPIConnectionError
from openai import InternalServerError as OpenAIInternalServerError
from pydantic import BaseModel, ValidationError

from infra_brain.agents.llm_base import (
    LLMAgent,
    ReasonDeadlineExceededError,
    invoke_reason_with_retry,
)


class _FakeToolModel(GenericFakeChatModel):
    """Fake chat model that accepts bind_tools (returns itself)."""

    def bind_tools(self, tools, **kwargs):
        return self


def _agent(fake_llm):
    agent = LLMAgent.__new__(LLMAgent)  # skip __init__: no DB / model creds needed
    agent.callbacks = []
    agent.llm = fake_llm
    return agent


def _echo_tool():
    @tool
    def echo(text: str) -> str:
        """Echo the text back."""
        return f"echo:{text}"

    return echo


def test_reason_runs_framework_react_loop():
    """A scripted tool-call turn followed by a final answer completes the loop."""
    msgs = iter(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "echo", "args": {"text": "hi"}, "id": "c1"}],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = _agent(_FakeToolModel(messages=msgs))
    with patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")):
        result = agent.reason("do the thing", tools=[_echo_tool()], max_iters=3)
    assert result == "done"


def test_reason_passes_callbacks_and_thread_id():
    """The runtime config must carry self.callbacks and a per-run thread_id."""
    fake_compiled = MagicMock()
    fake_compiled.invoke.return_value = {"messages": [AIMessage(content="ok")]}
    agent = _agent(_FakeToolModel(messages=iter([AIMessage(content="ok")])))
    agent.callbacks = ["sentinel-callback"]
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")),
    ):
        result = agent.reason("task", tools=[], max_iters=2)
    assert result == "ok"
    _, kwargs = fake_compiled.invoke.call_args
    config = kwargs["config"]
    assert config["callbacks"] == ["sentinel-callback"]
    assert config["recursion_limit"] == 5  # 2 * max_iters + 1
    assert config["configurable"]["thread_id"].startswith("LLMAgent:")


def test_reason_recursion_limit_returns_last_text():
    """Parity with the old loop: hitting the iteration bound returns last text."""
    fake_compiled = MagicMock()
    fake_compiled.invoke.side_effect = GraphRecursionError("limit")
    state = MagicMock()
    state.values = {"messages": [AIMessage(content="partial progress")]}
    fake_compiled.get_state.return_value = state
    agent = _agent(_FakeToolModel(messages=iter([])))
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")),
    ):
        result = agent.reason("loop forever", tools=[], max_iters=2)
    assert result == "partial progress"


def test_reason_writes_decision_log_per_model_turn():
    added = []

    class _FakeSession:
        def add(self, row):
            added.append(row)

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_compiled = MagicMock()
    fake_compiled.invoke.return_value = {
        "messages": [
            HumanMessage(content="task"),
            AIMessage(content="thinking"),
            AIMessage(content="final"),
        ]
    }
    agent = _agent(_FakeToolModel(messages=iter([])))
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", return_value=_FakeSession()),
    ):
        result = agent.reason("task", tools=[], max_iters=2)
    assert result == "final"
    assert len(added) == 2  # one AgentDecisionLog row per AIMessage


def test_reason_recursion_limit_writes_marker_decision_log_row():
    """Item 9: hitting the recursion limit must write a queryable marker row
    (decision_summary == RECURSION_LIMIT_MARKER) so check_agent_anomalies()
    can detect a runaway reasoning loop — not just a log.warning() no one watches."""
    from infra_brain.agents.llm_base import RECURSION_LIMIT_MARKER

    added = []

    class _FakeSession:
        def add(self, row):
            added.append(row)

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_compiled = MagicMock()
    fake_compiled.invoke.side_effect = GraphRecursionError("limit")
    state = MagicMock()
    state.values = {"messages": [AIMessage(content="partial progress")]}
    fake_compiled.get_state.return_value = state
    agent = _agent(_FakeToolModel(messages=iter([])))
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", return_value=_FakeSession()),
    ):
        result = agent.reason("loop forever", tools=[], max_iters=2)

    assert result == "partial progress"
    marker_rows = [r for r in added if r.decision_summary == RECURSION_LIMIT_MARKER]
    assert len(marker_rows) == 1
    assert marker_rows[0].agent == "LLMAgent"


def test_reason_decision_log_failure_does_not_break_reasoning(caplog):
    """OB-3 sliver: a persist failure never raises, but must be logged loud
    enough (warning, with agent/domain/run_id context) to triage — not
    silently swallowed at debug."""
    import logging

    fake_compiled = MagicMock()
    fake_compiled.invoke.return_value = {"messages": [AIMessage(content="fine")]}
    agent = _agent(_FakeToolModel(messages=iter([])))
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("db down")),
        caplog.at_level(logging.WARNING, logger="infra_brain.agents.llm_base"),
    ):
        assert agent.reason("task", tools=[], max_iters=2) == "fine"

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("AgentDecisionLog persist failed" in r.message for r in warning_records)


def test_react_agent_streams_tokens():
    """Acceptance (ROADMAP 4.2): an LLM agent streams tokens via astream_events."""
    from langchain.agents import create_agent

    model = _FakeToolModel(messages=iter([AIMessage(content="hello world tokens")]))
    graph = create_agent(model, [_echo_tool()])

    async def run():
        out = []
        async for ev in graph.astream_events(
            {"messages": [HumanMessage(content="go")]}, version="v2"
        ):
            if ev["event"] == "on_chat_model_stream":
                out.append(ev["data"]["chunk"].content)
        return out

    tokens = asyncio.run(run())
    assert "".join(tokens) == "hello world tokens"
    assert len(tokens) > 1  # actually streamed, not one blob


def test_reason_resumes_from_checkpoint_thread():
    """Acceptance (ROADMAP 4.2): the checkpointer persists thread state — a
    second invoke on the same thread_id sees the prior messages (resume)."""
    from langchain.agents import create_agent
    from langgraph.checkpoint.memory import MemorySaver

    model = _FakeToolModel(messages=iter([AIMessage(content="first"), AIMessage(content="second")]))
    graph = create_agent(model, [_echo_tool()], checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "resume-test"}}
    graph.invoke({"messages": [HumanMessage(content="one")]}, config=cfg)
    result = graph.invoke({"messages": [HumanMessage(content="two")]}, config=cfg)
    contents = [m.content for m in result["messages"]]
    assert "first" in contents and "second" in contents
    assert len(result["messages"]) == 4  # 2 human + 2 ai accumulated on the thread


def test_run_tool_passes_callbacks():
    agent = LLMAgent.__new__(LLMAgent)
    agent.callbacks = ["sentinel-callback"]
    fake_tool = MagicMock()
    agent._run_tool(fake_tool, {"a": 1})
    _, kwargs = fake_tool.invoke.call_args
    assert kwargs["config"]["callbacks"] == ["sentinel-callback"]


def test_collect_returns_empty_list():
    agent = LLMAgent.__new__(LLMAgent)
    assert agent.collect() == []


def test_gate_tools_blocks_denied_call_before_it_runs():
    """The R2 boundary gate must fire on create_agent's real dispatch path.

    reason() hands tools to create_agent, whose framework ToolNode calls
    tool.func()/tool.coroutine() directly — never LLMAgent._run_tool(). If
    _gate_tools() didn't wrap the tool, enforce_tool_gate would never run
    for a framework-dispatched call and a denied tool body would execute.
    """
    agent = LLMAgent.__new__(LLMAgent)
    agent.callbacks = []

    body_ran = False

    @tool
    def mutating_tool(query: str) -> str:
        """Pretend mutating tool for the gate test."""
        nonlocal body_ran
        body_ran = True
        return "should never get here"

    with patch(
        "infra_brain.agents.llm_base.enforce_tool_gate",
        side_effect=PermissionError("blocked"),
    ) as mock_gate:
        (gated,) = agent._gate_tools([mutating_tool])
        try:
            gated.func(query="DROP TABLE resources")
        except PermissionError:
            pass
        assert mock_gate.called
    assert body_ran is False


def test_gate_tools_allows_call_through_on_no_denial():
    agent = LLMAgent.__new__(LLMAgent)
    agent.callbacks = []

    @tool
    def read_tool(query: str) -> str:
        """Pretend read-only tool for the gate test."""
        return f"ok:{query}"

    (gated,) = agent._gate_tools([read_tool])
    assert gated.func(query="select 1") == "ok:select 1"


def test_gate_tools_raises_for_tool_with_neither_func_nor_coroutine():
    """N-6: a BaseTool subclass overriding _run() (no .func/.coroutine) must
    fail loudly at wiring time instead of being silently model_copy'd into a
    no-op that also bypasses the boundary gate."""
    from langchain_core.tools import BaseTool

    class _OverridesRunDirectly(BaseTool):
        name: str = "weird_tool"
        description: str = "overrides _run, exposes no .func/.coroutine"

        def _run(self, query: str) -> str:
            return f"ran:{query}"

    agent = LLMAgent.__new__(LLMAgent)
    agent.callbacks = []
    weird = _OverridesRunDirectly()
    assert getattr(weird, "func", None) is None
    assert getattr(weird, "coroutine", None) is None
    with pytest.raises(RuntimeError, match="neither .func nor .coroutine"):
        agent._gate_tools([weird])


def test_gate_tools_scans_positional_args_for_dlp():
    """AA-R-20: a positional arg alongside a kwarg must still reach the DLP
    scan. The old ``kwargs or {"args": args}`` dropped positional args
    whenever any kwarg was present."""
    agent = LLMAgent.__new__(LLMAgent)
    agent.callbacks = []

    @tool
    def two_arg_tool(query: str, extra: str = "") -> str:
        """Tool callable with a positional arg."""
        return f"{query}:{extra}"

    seen_inputs = []
    with patch(
        "infra_brain.agents.llm_base.enforce_tool_gate",
        side_effect=lambda name, tool_input, agent_name: seen_inputs.append(tool_input),
    ):
        (gated,) = agent._gate_tools([two_arg_tool])
        gated.func("4111111111111111", extra="kw")
    assert seen_inputs, "enforce_tool_gate was not called"
    scanned = seen_inputs[0]
    assert "4111111111111111" in scanned["args"], (
        f"positional arg missing from DLP scan input: {scanned!r}"
    )


def test_reason_redacts_pan_in_reasoning_text():
    """N-2: a Luhn-valid PAN in the model's raw output must be scrubbed BEFORE
    it is persisted to AgentDecisionLog.reasoning_text."""
    added = []

    class _FakeSession:
        def add(self, row):
            added.append(row)

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_compiled = MagicMock()
    fake_compiled.invoke.return_value = {
        "messages": [AIMessage(content="card is 4111111111111111, treat carefully")]
    }
    agent = _agent(_FakeToolModel(messages=iter([])))
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", return_value=_FakeSession()),
    ):
        agent.reason("task", tools=[], max_iters=2)
    assert added, "no AgentDecisionLog row was written"
    assert "4111111111111111" not in added[0].reasoning_text
    assert "REDACTED" in added[0].reasoning_text


def test_reason_captures_token_count_when_usage_metadata_present():
    """OB-6: token_count is populated from AIMessage.usage_metadata when the
    framework supplies it."""
    added = []

    class _FakeSession:
        def add(self, row):
            added.append(row)

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    ai = AIMessage(content="ok")
    ai.usage_metadata = {"total_tokens": 42}
    fake_compiled = MagicMock()
    fake_compiled.invoke.return_value = {"messages": [ai]}
    agent = _agent(_FakeToolModel(messages=iter([])))
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", return_value=_FakeSession()),
    ):
        agent.reason("task", tools=[], max_iters=2)
    assert added
    assert added[0].token_count == 42


def test_reason_skips_checkpointer_when_no_tools():
    """AA-R-10: a tool-less reason() call mints a checkpoint thread that
    nothing ever resumes — skip the checkpointer entirely rather than writing
    write-only garbage no retention job prunes."""
    fake_compiled = MagicMock()
    fake_compiled.invoke.return_value = {"messages": [AIMessage(content="ok")]}
    agent = _agent(_FakeToolModel(messages=iter([])))
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled) as mock_ca,
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")),
        patch("infra_brain.agents.llm_base.get_checkpointer") as mock_gc,
    ):
        agent.reason("task", tools=[], max_iters=2)
    assert not mock_gc.called, "get_checkpointer() must not be called for a tool-less reason()"
    _, kwargs = mock_ca.call_args
    assert kwargs["checkpointer"] is None


def test_reason_uses_checkpointer_when_tools_present():
    """Complement to AA-R-10: a tool-using reason() call still gets the shared
    checkpointer (the recursion-limit handler reads it back via get_state())."""
    fake_compiled = MagicMock()
    fake_compiled.invoke.return_value = {"messages": [AIMessage(content="ok")]}
    agent = _agent(_FakeToolModel(messages=iter([])))
    sentinel_checkpointer = object()
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled) as mock_ca,
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")),
        patch(
            "infra_brain.agents.llm_base.get_checkpointer",
            return_value=sentinel_checkpointer,
        ) as mock_gc,
    ):
        agent.reason("task", tools=[_echo_tool()], max_iters=2)
    assert mock_gc.called
    _, kwargs = mock_ca.call_args
    assert kwargs["checkpointer"] is sentinel_checkpointer


def test_subclass_must_set_a_real_domain():
    """TRK-060: `domain = "llm_base"` is a placeholder — a concrete subclass
    that forgets to override it must fail loudly at class-definition time,
    not silently tag its rows/schedule lookups as "llm_base"."""
    with pytest.raises(TypeError, match="llm_base"):

        class ForgotDomain(LLMAgent):
            pass


def test_subclass_with_real_domain_is_fine():
    class RealAgent(LLMAgent):
        domain = "real_domain"

    assert RealAgent.domain == "real_domain"


def test_llm_base_itself_is_unaffected():
    """The base class keeps its placeholder — the check only fires on subclasses."""
    assert LLMAgent.domain == "llm_base"


# ---------------------------------------------------------------------------
# TRK-119: invoke_structured_with_retry — bounded retry for structured output
# ---------------------------------------------------------------------------


def test_invoke_structured_retry_retries_transient_then_succeeds():
    from langchain_core.exceptions import OutputParserException

    from infra_brain.agents.llm_base import invoke_structured_with_retry

    runnable = MagicMock()
    runnable.invoke.side_effect = [OutputParserException("bad json"), "parsed-ok"]
    out = invoke_structured_with_retry(runnable, "prompt", attempts=2, label="t")
    assert out == "parsed-ok"
    assert runnable.invoke.call_count == 2


def test_invoke_structured_retry_does_not_retry_non_transient():
    """A genuine config/model error (not parse/validation) must fail fast —
    propagate on the first attempt, no pointless retries."""
    from infra_brain.agents.llm_base import invoke_structured_with_retry

    runnable = MagicMock()
    runnable.invoke.side_effect = RuntimeError("no model configured")
    with pytest.raises(RuntimeError, match="no model configured"):
        invoke_structured_with_retry(runnable, "prompt", attempts=2, label="t")
    assert runnable.invoke.call_count == 1


def test_invoke_structured_retry_reraises_last_transient_after_exhaustion():
    from pydantic import BaseModel, ValidationError

    from infra_brain.agents.llm_base import invoke_structured_with_retry

    class _M(BaseModel):
        x: int

    def _always_validation_error(*args, **kwargs):
        return _M(x="not-an-int")  # raises pydantic ValidationError

    runnable = MagicMock()
    runnable.invoke.side_effect = _always_validation_error
    with pytest.raises(ValidationError):
        invoke_structured_with_retry(runnable, "prompt", attempts=2, label="t")
    assert runnable.invoke.call_count == 2


def test_invoke_structured_retry_passes_config_through():
    from infra_brain.agents.llm_base import invoke_structured_with_retry

    runnable = MagicMock()
    runnable.invoke.return_value = "ok"
    cfg = {"callbacks": ["cb"]}
    invoke_structured_with_retry(runnable, "prompt", config=cfg, label="t")
    runnable.invoke.assert_called_once_with("prompt", config=cfg)


# ---------------------------------------------------------------------------
# T3 (rev12): invoke_structured_with_retry tolerates markdown-fenced JSON
# ---------------------------------------------------------------------------
#
# Live production failure (discovery run 2026-08-09, collection_runs
# error_message): the Ollama endpoint behind the OpenAI-compat ChatOpenAI
# client returned a COMPLETE, valid JSON answer wrapped in a ```json fence.
# `with_structured_output`'s default method="json_schema" path funnels the
# raw completion text through the OPENAI SDK's own `.parse()` client helper
# (openai/lib/_parsing/_completions.py:_parse_content ->
# openai/_compat.py:model_parse_json -> `schema.model_validate_json(text)`)
# with NO markdown-fence handling at all — unlike the method="json_mode" path
# (langchain_core's JsonOutputParser), which already strips fences via
# parse_json_markdown. The unhandled path raises `pydantic.ValidationError`
# with `type=json_invalid` and the ENTIRE fenced string as the error's
# `input` value. Each helper below reproduces that EXACT exception by
# actually calling `model_validate_json` on fenced text against a real
# pydantic model — not a lookalike/mocked exception.


class _FindingSchema(BaseModel):
    hostname: str
    confidence: float


class _FindingListSchema(BaseModel):
    findings: list[_FindingSchema]


def _fenced_json_validation_error(schema, text: str) -> ValidationError:
    """Return the real `pydantic.ValidationError` raised by validating
    *text* against *schema* via `model_validate_json` — the exact exception
    type/shape the openai SDK's structured-output parser raises in
    production for a markdown-fenced (or otherwise non-JSON) response."""
    try:
        schema.model_validate_json(text)
    except ValidationError as exc:
        return exc
    raise AssertionError(f"expected {text!r} to fail validation against {schema}")


def test_invoke_structured_retry_recovers_fenced_json_complete():
    """A complete, valid JSON answer wrapped in a ```json fence is recovered
    by stripping the fence and re-validating against the SAME schema —
    WITHOUT re-invoking the model. This is the exact production failure: a
    real answer must not be thrown away."""
    from infra_brain.agents.llm_base import invoke_structured_with_retry

    payload = '{"findings": [{"hostname": "h1", "confidence": 0.7}]}'
    fenced = f"```json\n{payload}\n```"
    exc = _fenced_json_validation_error(_FindingListSchema, fenced)

    runnable = MagicMock()
    runnable.invoke.side_effect = [exc]
    out = invoke_structured_with_retry(
        runnable, "prompt", attempts=2, label="t", schema=_FindingListSchema
    )
    assert isinstance(out, _FindingListSchema)
    assert out.findings[0].hostname == "h1"
    assert out.findings[0].confidence == 0.7
    # Recovered from the SAME failed response — the model was never
    # re-invoked for this case (attempts budget untouched).
    assert runnable.invoke.call_count == 1


def test_invoke_structured_retry_recovers_fenced_json_with_surrounding_prose():
    """A fence with leading/trailing prose ("Here is the JSON: ... Hope
    that helps!") is still recovered — the fence delimits the payload even
    when the fence isn't the entire message."""
    from infra_brain.agents.llm_base import invoke_structured_with_retry

    payload = '{"findings": [{"hostname": "h2", "confidence": 0.9}]}'
    fenced = f"Here is the JSON you asked for:\n```json\n{payload}\n```\nLet me know if that helps!"
    exc = _fenced_json_validation_error(_FindingListSchema, fenced)

    runnable = MagicMock()
    runnable.invoke.side_effect = [exc]
    out = invoke_structured_with_retry(
        runnable, "prompt", attempts=2, label="t", schema=_FindingListSchema
    )
    assert isinstance(out, _FindingListSchema)
    assert out.findings[0].hostname == "h2"
    assert runnable.invoke.call_count == 1


def test_invoke_structured_retry_bare_valid_json_unchanged():
    """No fence, no failure at all: the runnable's own successful parse is
    returned untouched on the first attempt — the new schema= param and
    recovery path are a no-op when nothing failed."""
    from infra_brain.agents.llm_base import invoke_structured_with_retry

    expected = _FindingListSchema(findings=[_FindingSchema(hostname="h3", confidence=0.5)])
    runnable = MagicMock()
    runnable.invoke.return_value = expected
    out = invoke_structured_with_retry(
        runnable, "prompt", attempts=2, label="t", schema=_FindingListSchema
    )
    assert out is expected
    assert runnable.invoke.call_count == 1


def test_invoke_structured_retry_non_json_prose_still_fails():
    """The mid-exploration recursion-limit case (reason()'s
    GraphRecursionError branch, ~llm_base.py line 640): text that is
    genuinely NOT a final JSON answer — no fence anywhere — must keep
    failing exactly as before. Fence-stripping must never rescue plain
    non-JSON prose into a fabricated schema instance."""
    from infra_brain.agents.llm_base import invoke_structured_with_retry

    prose = "I still need to check the gitlab_repository_tree_tool before I can answer."
    exc = _fenced_json_validation_error(_FindingListSchema, prose)

    runnable = MagicMock()
    runnable.invoke.side_effect = exc  # same exception raised every attempt
    with pytest.raises(ValidationError):
        invoke_structured_with_retry(
            runnable, "prompt", attempts=2, label="t", schema=_FindingListSchema
        )
    # No fence found -> old retry-then-raise behavior: both attempts spent.
    assert runnable.invoke.call_count == 2


def test_invoke_structured_retry_fence_recovery_requires_schema_param():
    """Existing callers that don't pass `schema=` (none of the pre-T3 call
    sites did before this change) get byte-identical old behavior: no fence
    recovery is even attempted, so a fenced-JSON failure still exhausts
    retries and raises exactly as it did before this fix existed."""
    from infra_brain.agents.llm_base import invoke_structured_with_retry

    payload = '{"findings": []}'
    fenced = f"```json\n{payload}\n```"
    exc = _fenced_json_validation_error(_FindingListSchema, fenced)

    runnable = MagicMock()
    runnable.invoke.side_effect = exc
    with pytest.raises(ValidationError):
        invoke_structured_with_retry(runnable, "prompt", attempts=2, label="t")
    assert runnable.invoke.call_count == 2


def test_invoke_structured_retry_fence_recovery_still_validates_full_schema():
    """A fenced payload missing a REQUIRED field must still fail — recovery
    re-validates the stripped text against the FULL schema, it never
    partially/leniently parses (no schema weakening)."""
    from infra_brain.agents.llm_base import invoke_structured_with_retry

    # Missing the required `confidence` field inside the nested finding.
    payload = '{"findings": [{"hostname": "h4"}]}'
    fenced = f"```json\n{payload}\n```"
    exc = _fenced_json_validation_error(_FindingListSchema, fenced)

    runnable = MagicMock()
    runnable.invoke.side_effect = exc  # same malformed-but-fenced text throughout
    with pytest.raises(ValidationError):
        invoke_structured_with_retry(
            runnable, "prompt", attempts=2, label="t", schema=_FindingListSchema
        )
    assert runnable.invoke.call_count == 2


def test_invoke_structured_retry_classification_unchanged_for_other_errors():
    """T3 must not touch the existing transient-vs-permanent classification:
    a non-retryable error still fails fast on the first attempt even when a
    `schema=` is now supplied."""
    from infra_brain.agents.llm_base import invoke_structured_with_retry

    runnable = MagicMock()
    runnable.invoke.side_effect = RuntimeError("no model configured")
    with pytest.raises(RuntimeError, match="no model configured"):
        invoke_structured_with_retry(
            runnable, "prompt", attempts=2, label="t", schema=_FindingListSchema
        )
    assert runnable.invoke.call_count == 1


# ---------------------------------------------------------------------------
# TRK-120: opt-in per-run LLM token ceiling
# ---------------------------------------------------------------------------


def _agent_with_settings(**settings_kwargs):
    from types import SimpleNamespace

    agent = _agent(_FakeToolModel(messages=iter([])))
    agent.settings = SimpleNamespace(**settings_kwargs)
    return agent


def _msg_with_tokens(n):
    ai = AIMessage(content="ok")
    ai.usage_metadata = {"total_tokens": n}
    return ai


def test_token_accumulator_sums_usage_metadata():
    agent = _agent(_FakeToolModel(messages=iter([])))
    agent._accumulate_run_tokens(
        [_msg_with_tokens(30), _msg_with_tokens(12), AIMessage(content="no-usage")]
    )
    assert agent._llm_run_tokens_used == 42


def test_token_ceiling_disabled_never_raises():
    agent = _agent_with_settings(llm_run_token_ceiling_enabled=False, llm_run_token_ceiling=1)
    agent._llm_run_tokens_used = 10_000  # far over, but the feature is off
    agent._enforce_token_ceiling()  # must not raise


def test_token_ceiling_absent_settings_is_noop():
    agent = _agent(_FakeToolModel(messages=iter([])))  # no `.settings` attribute
    agent._enforce_token_ceiling()  # must not raise


def test_token_ceiling_stops_further_calls_once_exceeded():
    from infra_brain.agents.llm_base import LLMBudgetExceededError

    agent = _agent_with_settings(llm_run_token_ceiling_enabled=True, llm_run_token_ceiling=100)
    fake_compiled = MagicMock()
    fake_compiled.invoke.return_value = {"messages": [_msg_with_tokens(150)]}
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")),
    ):
        # First call: used=0 < 100 → runs, accumulates 150 tokens.
        assert agent.reason("t1", tools=[]) == "ok"
        assert agent._llm_run_tokens_used == 150
        # Second call: used=150 >= 100 → raises BEFORE invoking the model.
        with pytest.raises(LLMBudgetExceededError):
            agent.reason("t2", tools=[])
    assert fake_compiled.invoke.call_count == 1  # the model was never called again


def test_token_ceiling_first_call_always_runs():
    """Even a single call that will itself blow the ceiling must still run — the
    check is 'stop FURTHER calls', so the first call of a run is never blocked."""
    agent = _agent_with_settings(llm_run_token_ceiling_enabled=True, llm_run_token_ceiling=10)
    fake_compiled = MagicMock()
    fake_compiled.invoke.return_value = {"messages": [_msg_with_tokens(9999)]}
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")),
    ):
        assert agent.reason("t1", tools=[]) == "ok"  # ran despite huge usage
    assert fake_compiled.invoke.call_count == 1


# ---------------------------------------------------------------------------
# TRK-146: invoke_reason_with_retry — bounded retry for reason()'s connection
# errors (GitLab issue #40)
# ---------------------------------------------------------------------------


def _openai_connection_error(message: str = "Connection error.") -> OpenAIAPIConnectionError:
    return OpenAIAPIConnectionError(
        message=message, request=httpx.Request("POST", "http://ollama.local/v1")
    )


def _openai_internal_server_error(message: str = "server error") -> OpenAIInternalServerError:
    request = httpx.Request("POST", "http://ollama.local/v1")
    response = httpx.Response(
        status_code=503, request=request, json={"error": {"message": message}}
    )
    return OpenAIInternalServerError(message=message, response=response, body=None)


def test_invoke_reason_retry_retries_transient_then_succeeds():
    """A connection blip on the first attempt clears on a later attempt."""
    from infra_brain.agents.llm_base import invoke_reason_with_retry

    agent = MagicMock()
    agent.invoke.side_effect = [_openai_connection_error(), {"messages": [AIMessage(content="ok")]}]
    with patch("infra_brain.agents.llm_base.time.sleep") as mock_sleep:
        out = invoke_reason_with_retry(agent, {"messages": []}, config={}, label="t")
    assert out == {"messages": [AIMessage(content="ok")]}
    assert agent.invoke.call_count == 2
    mock_sleep.assert_called_once()  # backoff between attempt 1 and attempt 2


def test_invoke_reason_retry_does_not_retry_non_transient():
    """A genuine non-connection error (e.g. auth failure) must fail fast —
    propagate on the first attempt, no pointless retries."""
    from infra_brain.agents.llm_base import invoke_reason_with_retry

    agent = MagicMock()
    agent.invoke.side_effect = RuntimeError("invalid api key")
    with (
        patch("infra_brain.agents.llm_base.time.sleep") as mock_sleep,
        pytest.raises(RuntimeError, match="invalid api key"),
    ):
        invoke_reason_with_retry(agent, {"messages": []}, config={}, label="t")
    assert agent.invoke.call_count == 1
    mock_sleep.assert_not_called()


def test_invoke_reason_retry_does_not_retry_graph_recursion_error():
    """GraphRecursionError must propagate unchanged on the first attempt so
    reason()'s own except clause still handles it exactly as before —
    it must NOT be swallowed/retried by the connection-retry wrapper."""
    from infra_brain.agents.llm_base import invoke_reason_with_retry

    agent = MagicMock()
    agent.invoke.side_effect = GraphRecursionError("limit")
    with (
        patch("infra_brain.agents.llm_base.time.sleep") as mock_sleep,
        pytest.raises(GraphRecursionError),
    ):
        invoke_reason_with_retry(agent, {"messages": []}, config={}, label="t")
    assert agent.invoke.call_count == 1
    mock_sleep.assert_not_called()


def test_invoke_reason_retry_reraises_last_transient_after_exhaustion():
    """All attempts fail with a transient connection error — the last one is
    what's raised, and no more than `attempts` calls are made."""
    from infra_brain.agents.llm_base import invoke_reason_with_retry

    agent = MagicMock()
    agent.invoke.side_effect = [
        _openai_connection_error("first blip"),
        _openai_connection_error("second blip"),
        _openai_connection_error("third blip"),
    ]
    with (
        patch("infra_brain.agents.llm_base.time.sleep"),
        pytest.raises(OpenAIAPIConnectionError, match="third blip"),
    ):
        invoke_reason_with_retry(agent, {"messages": []}, config={}, attempts=3, label="t")
    assert agent.invoke.call_count == 3


def test_invoke_reason_retry_retries_httpx_connect_error():
    """httpx.ConnectError (a lower-level transport error than the SDK-wrapped
    APIConnectionError) is also retryable."""
    from infra_brain.agents.llm_base import invoke_reason_with_retry

    agent = MagicMock()
    agent.invoke.side_effect = [
        httpx.ConnectError("connection refused"),
        {"messages": [AIMessage(content="recovered")]},
    ]
    with patch("infra_brain.agents.llm_base.time.sleep"):
        out = invoke_reason_with_retry(agent, {"messages": []}, config={}, label="t")
    assert out == {"messages": [AIMessage(content="recovered")]}
    assert agent.invoke.call_count == 2


def test_invoke_reason_retry_retries_internal_server_error():
    """A 5xx (InternalServerError) is the same class of transient, provider-
    side issue as a connection drop -- verified live against the real
    omniroute gateway: a 503 "Chat admission capacity is temporarily
    unavailable. Retry shortly." previously killed a DiscoveryAgent run
    outright with zero retries, despite the message itself saying to retry."""
    from infra_brain.agents.llm_base import invoke_reason_with_retry

    agent = MagicMock()
    agent.invoke.side_effect = [
        _openai_internal_server_error("Chat admission capacity is temporarily unavailable"),
        {"messages": [AIMessage(content="recovered")]},
    ]
    with patch("infra_brain.agents.llm_base.time.sleep") as mock_sleep:
        out = invoke_reason_with_retry(agent, {"messages": []}, config={}, label="t")
    assert out == {"messages": [AIMessage(content="recovered")]}
    assert agent.invoke.call_count == 2
    mock_sleep.assert_called_once()


def test_reason_retries_connection_error_then_succeeds():
    """End-to-end: LLMAgent.reason() itself recovers from a transient
    connection error on the underlying agent.invoke() without the caller
    seeing any exception."""
    fake_compiled = MagicMock()
    fake_compiled.invoke.side_effect = [
        _openai_connection_error(),
        {"messages": [AIMessage(content="final answer")]},
    ]
    agent = _agent(_FakeToolModel(messages=iter([])))
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")),
        patch("infra_brain.agents.llm_base.time.sleep"),
    ):
        result = agent.reason("task", tools=[], max_iters=2)
    assert result == "final answer"
    assert fake_compiled.invoke.call_count == 2


def test_reason_recursion_limit_still_handled_through_retry_wrapper():
    """Regression guard: routing agent.invoke() through invoke_reason_with_retry
    must not change the existing GraphRecursionError → 'return last text'
    behavior (test_reason_recursion_limit_returns_last_text covers the
    pre-existing case; this confirms it still holds with the wrapper in
    place, including when time.sleep is unmocked-but-unused since
    GraphRecursionError never reaches the retry/backoff branch)."""
    fake_compiled = MagicMock()
    fake_compiled.invoke.side_effect = GraphRecursionError("limit")
    state = MagicMock()
    state.values = {"messages": [AIMessage(content="partial progress")]}
    fake_compiled.get_state.return_value = state
    agent = _agent(_FakeToolModel(messages=iter([])))
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled),
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")),
    ):
        result = agent.reason("loop forever", tools=[], max_iters=2)
    assert result == "partial progress"
    assert fake_compiled.invoke.call_count == 1  # not retried


# ---------------------------------------------------------------------------
# GitLab #159/#165 — reason()'s total wall-clock deadline
#
# reason() had no wall-clock bound: only a per-CALL timeout, times max_iters
# model turns, times REASON_MAX_ATTEMPTS retries. For DiscoveryAgent
# (max_iters=10) that is 10*120*3 = 3600s, 12x its own 300s
# collect_timeout_seconds — so ETLConnector._call_with_timeout killed it
# mid-flight from OUTSIDE, with no diagnostic signal (#159's mysterious
# 0-resource failed run). deadline_seconds makes the timeout reason()'s OWN,
# with reason()'s own message.
# ---------------------------------------------------------------------------


class _SleepyToolModel(GenericFakeChatModel):
    """Fake chat model whose every turn blocks for *delay* seconds."""

    delay: float = 5.0

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        time.sleep(self.delay)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_reason_raises_within_deadline_instead_of_being_externally_killed():
    """A model turn that outlives the budget is preempted BY reason(), on time.

    The elapsed-time assertion is the real test. Without deadline_seconds this
    call blocks the full 5s and only an external guard could stop it; with it,
    reason() returns control at ~1s and names itself in the error.
    """
    agent = _agent(_SleepyToolModel(messages=iter([AIMessage(content="too late")]), delay=5.0))
    agent.settings = None

    started = time.monotonic()
    with (
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")),
        pytest.raises(ReasonDeadlineExceededError) as excinfo,
    ):
        agent.reason("slow task", tools=[_echo_tool()], max_iters=3, deadline_seconds=1)
    elapsed = time.monotonic() - started

    assert 0.5 < elapsed < 4.0, f"reason() took {elapsed:.2f}s; deadline was 1s"
    # The message must be actionable — this is the diagnostic signal #159 lacked.
    assert "LLMAgent.reason" in str(excinfo.value)
    assert "1" in str(excinfo.value)


def test_reason_without_deadline_is_unbounded_as_before():
    """deadline_seconds=None (the default) must not change behavior at all."""
    agent = _agent(_SleepyToolModel(messages=iter([AIMessage(content="finished")]), delay=0.2))
    agent.settings = None
    with patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")):
        result = agent.reason("slow-ish task", tools=[_echo_tool()], max_iters=3)
    assert result == "finished"


def test_reason_deadline_not_exceeded_returns_normally():
    """A call that finishes inside its budget is untouched by the deadline."""
    agent = _agent(_SleepyToolModel(messages=iter([AIMessage(content="in time")]), delay=0.1))
    agent.settings = None
    with patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")):
        result = agent.reason("quick task", tools=[_echo_tool()], max_iters=3, deadline_seconds=10)
    assert result == "in time"


def test_reason_deadline_exceeded_still_logs_decisions_and_tokens_before_reraising():
    """GitLab #170: a preempted call must not vanish from the audit/cost trail.

    Before this fix, ReasonDeadlineExceededError propagated straight out of
    reason() and skipped _log_decisions()/_accumulate_run_tokens() entirely —
    a call that made real (billed) model turns left zero AgentDecisionLog rows
    and never counted against the TRK-120 token ceiling. The checkpointer
    persists partial state after each completed graph step even when the
    *next* turn gets preempted, so a real message list should be recoverable
    via agent.get_state() and passed to both — while the exception itself
    still propagates unchanged (the caller's contract).
    """
    agent = _agent(_SleepyToolModel(messages=iter([AIMessage(content="too late")]), delay=5.0))
    agent.settings = None
    logged_messages = []
    accumulated_messages = []
    agent._log_decisions = lambda run_id, messages: logged_messages.append(messages)
    agent._accumulate_run_tokens = lambda messages: accumulated_messages.append(messages)

    with (
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")),
        pytest.raises(ReasonDeadlineExceededError),
    ):
        agent.reason("slow task", tools=[_echo_tool()], max_iters=3, deadline_seconds=1)

    # Both best-effort hooks ran exactly once on the deadline-preemption path
    # (not zero times, as before this fix) — regardless of whether the
    # checkpointer had anything to recover, they must still be invoked so a
    # future real run's decision/token accounting isn't silently skipped.
    assert len(logged_messages) == 1
    assert len(accumulated_messages) == 1


def test_invoke_reason_with_retry_stops_retrying_once_deadline_passes():
    """The budget spans RETRIES too — a connection blip cannot outlive it.

    Three attempts each burning 0.4s against a 0.5s budget must NOT run all
    three: the deadline has to cut the retry loop short.
    """
    calls: list = []

    class _Flaky:
        def invoke(self, input_, config=None):
            calls.append(1)
            time.sleep(0.4)
            raise OpenAIAPIConnectionError(request=httpx.Request("POST", "http://x"))

    started = time.monotonic()
    with pytest.raises(ReasonDeadlineExceededError):
        invoke_reason_with_retry(
            _Flaky(), {"messages": []}, config={}, attempts=3, deadline_seconds=0.5
        )
    elapsed = time.monotonic() - started

    assert len(calls) < 3, f"retried {len(calls)}x despite an exhausted deadline"
    assert elapsed < 2.0, f"retry loop ran {elapsed:.2f}s past a 0.5s deadline"


def test_invoke_reason_with_retry_unbounded_when_deadline_is_none():
    """No deadline => the pre-#165 code path, retries and all."""
    calls: list = []

    class _Flaky:
        def invoke(self, input_, config=None):
            calls.append(1)
            if len(calls) < 2:
                raise OpenAIAPIConnectionError(request=httpx.Request("POST", "http://x"))
            return {"messages": [AIMessage(content="recovered")]}

    result = invoke_reason_with_retry(_Flaky(), {"messages": []}, config={}, attempts=3)
    assert result["messages"][0].content == "recovered"
    assert len(calls) == 2


def test_gate_tools_returns_tool_failure_as_observation():
    """A raising tool body becomes model-visible text, not a run-killing raise.

    LangGraph's _default_handle_tool_errors returns a message only for
    ToolInvocationError and re-raises everything else, so a real exception from
    a tool body escaped create_agent's graph, escaped reason(), and the calling
    agent recorded status="failed". DiscoveryAgent had therefore never once
    completed: one exploratory gitlab_repository_tree_tool call for a path that
    does not exist (404) killed the entire collection.
    """
    agent = LLMAgent.__new__(LLMAgent)
    agent.callbacks = []

    @tool
    def flaky_tool(path: str) -> str:
        """Pretend tool that fails the way an HTTP 404 does."""
        raise RuntimeError("Client error '404 Not Found' for url '/tree?path=entities%2Fraw'")

    (gated,) = agent._gate_tools([flaky_tool])
    out = gated.func(path="entities/raw")

    assert isinstance(out, str)
    assert "ERROR" in out
    assert "flaky_tool" in out
    assert "404" in out
    # The model must be told not to simply retry the same call verbatim.
    assert "Do not repeat the identical" in out


def test_gate_tools_still_raises_permission_error_from_tool_body():
    """SAFETY: a read-only denial raised INSIDE the tool body must still abort.

    The failure-to-observation path above must never absorb a PermissionError —
    that is the read-only boundary refusing the call (e.g. tools/http_readonly
    rejecting a non-GET). Turning it into text would hand the model a denial it
    could paraphrase around and retry.
    """
    agent = LLMAgent.__new__(LLMAgent)
    agent.callbacks = []

    @tool
    def denied_tool(payload: str) -> str:
        """Pretend tool whose body hits the read-only boundary."""
        raise PermissionError("read-only boundary: POST is not permitted")

    (gated,) = agent._gate_tools([denied_tool])
    with pytest.raises(PermissionError, match="read-only boundary"):
        gated.func(payload="x")
