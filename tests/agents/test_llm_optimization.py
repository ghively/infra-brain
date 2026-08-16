"""T6 (rev14): tests pinning the LLM ReAct-lane optimizations.

Kept in their own module rather than appended to ``test_llm_base.py`` purely so
the two concerns stay separable; they exercise ``agents/llm_base.py`` exactly as
that file's tests do.

The failure these pin is a real one, from ``agent_decision_log`` on this
deployment (230 rows, one DiscoveryAgent run): prompt size grew monotonically
17,275 -> 17,926 -> 20,949 -> 23,410 -> 27,159 tokens across iterations 6..10,
the run hit the recursion limit, and the structured-output finalizer had to try
to rescue an answer from mid-exploration text. Two causes, both pinned below:
unbounded tool results entering a transcript that is re-read every turn, and a
loop that was never told a stopping condition anywhere it would still be
reading by turn 10.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from infra_brain.agents.llm_base import LLMAgent


class _FakeToolModel(GenericFakeChatModel):
    """Fake chat model that accepts bind_tools (returns itself)."""

    def bind_tools(self, tools, **kwargs):
        return self


def _agent(fake_llm=None):
    agent = LLMAgent.__new__(LLMAgent)  # skip __init__: no DB / model creds needed
    agent.callbacks = []
    if fake_llm is not None:
        agent.llm = fake_llm
    return agent


def _echo_tool():
    @tool
    def echo(text: str) -> str:
        """Echo the text back."""
        return f"echo:{text}"

    return echo


def _big_project_list(n=200):
    """A realistically-shaped GitLab ``/projects`` payload.

    ``gitlab_projects_tool`` returns the FULL project object for every
    accessible project with no field selection — this mirrors that shape closely
    enough to measure what one such call costs the transcript.
    """
    return [
        {
            "id": i,
            "name": f"project-{i}",
            "path_with_namespace": f"group/sub/project-{i}",
            "description": "x" * 120,
            "web_url": f"https://git.example.test/group/sub/project-{i}",
            "namespace": {"id": i, "name": "group", "full_path": "group/sub", "kind": "group"},
            "_links": {
                k: f"https://git.example.test/api/v4/projects/{i}/{k}"
                for k in ("self", "issues", "merge_requests", "repo_branches", "labels", "events")
            },
            "permissions": {"project_access": None, "group_access": {"access_level": 30}},
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Tool-result truncation
# ---------------------------------------------------------------------------


def test_truncation_bounds_an_oversized_list_result():
    """A large list must become a bounded envelope that still reports the TRUE
    count, so the model can tell "5 of 200" from "5"."""
    from infra_brain.agents.llm_base import _truncate_tool_result_for_transcript

    payload = _big_project_list()
    out = _truncate_tool_result_for_transcript(payload, 8000)

    assert isinstance(out, dict)
    assert out["_truncated"] is True
    assert out["count"] == 200  # true size preserved
    assert 0 < out["shown"] < 200  # genuinely fewer items shown
    assert len(json.dumps(out, default=str)) <= 8500  # envelope overhead only
    # Whole items only — never a half-serialized record.
    assert all(isinstance(item, dict) and "id" in item for item in out["data"])


def test_truncation_bounds_an_oversized_string_result():
    """``gitlab_file_tool`` returns a whole decoded file with no bound at all."""
    from infra_brain.agents.llm_base import _truncate_tool_result_for_transcript

    out = _truncate_tool_result_for_transcript("A" * 50_000, 8000)
    assert isinstance(out, str)
    assert len(out) < 8400  # 8000 + the trailing note
    assert "truncated" in out
    assert "50000" in out  # tells the model how much it did not see


def test_truncation_leaves_small_results_identical():
    """The common case must be completely unchanged — no envelope, no note."""
    from infra_brain.agents.llm_base import _truncate_tool_result_for_transcript

    for value in ("short answer", [{"a": 1}], {"k": "v"}, 42, None, True):
        assert _truncate_tool_result_for_transcript(value, 8000) is value


def test_truncation_survives_a_non_serializable_result():
    """An exotic return value must still be bounded, not passed through raw."""
    from infra_brain.agents.llm_base import _truncate_tool_result_for_transcript

    class _Weird:
        def __repr__(self):
            return "W" * 50_000

    out = _truncate_tool_result_for_transcript({"k": _Weird()}, 8000)
    assert len(json.dumps(out, default=str)) < 20_000


def test_gate_tools_truncates_oversized_tool_output():
    """END TO END: the bound must be applied by the gated wrapper
    ``create_agent`` actually calls, not merely available as a helper."""
    agent = _agent()

    @tool
    def fat_tool(query: str) -> str:
        """Pretend tool returning an unbounded blob."""
        return "B" * 100_000

    with patch("infra_brain.agents.llm_base.enforce_tool_gate"):
        (gated,) = agent._gate_tools([fat_tool])
        result = gated.func(query="everything")
    assert len(result) < 9000
    assert "truncated" in result


def test_gate_tools_truncation_measures_the_real_transcript_saving():
    """Quantify the fix on a realistic payload. This is the number a ReAct loop
    pays again on EVERY remaining turn, which is why it compounds."""
    agent = _agent()
    payload = _big_project_list()

    @tool
    def projects_tool() -> list:
        """Pretend gitlab_projects_tool."""
        return payload

    before = len(json.dumps(payload, default=str))
    with patch("infra_brain.agents.llm_base.enforce_tool_gate"):
        (gated,) = agent._gate_tools([projects_tool])
        after = len(json.dumps(gated.func(), default=str))

    assert before > 50_000  # the real problem is this big
    assert after <= 8500  # and this is what it becomes
    assert after < before / 5  # >5x reduction per occurrence, per turn


def test_gate_tools_does_not_truncate_error_observations():
    """A tool failure returns navigation instructions ("do not repeat the
    identical call"). Cutting those mid-sentence would defeat their purpose."""
    agent = _agent()

    @tool
    def broken_tool(path: str) -> str:
        """Pretend tool that 404s."""
        raise RuntimeError("404 Not Found")

    with patch("infra_brain.agents.llm_base.enforce_tool_gate"):
        (gated,) = agent._gate_tools([broken_tool])
        out = gated.func(path="entities/raw")
    assert out.startswith("ERROR: tool broken_tool failed")
    assert "Do not repeat the identical" in out


def test_truncation_budget_is_configurable_and_rejects_nonpositive():
    """``<= 0`` must fall back to the default rather than disabling the bound —
    unbounded is the bug being fixed, so config must not be able to restore it."""
    from infra_brain.agents.llm_base import (
        _DEFAULT_TOOL_RESULT_MAX_CHARS,
        _tool_result_max_chars,
    )

    assert _tool_result_max_chars(SimpleNamespace(llm_tool_result_max_chars=1234)) == 1234
    for bad in (0, -1, None, "nope"):
        assert (
            _tool_result_max_chars(SimpleNamespace(llm_tool_result_max_chars=bad))
            == _DEFAULT_TOOL_RESULT_MAX_CHARS
        )
    # No settings object at all (tests construct agents via __new__).
    assert _tool_result_max_chars(None) == _DEFAULT_TOOL_RESULT_MAX_CHARS


def test_settings_expose_the_truncation_budget():
    """The knob must be real config, not only a module constant."""
    from infra_brain.config import Settings

    assert Settings.model_fields["llm_tool_result_max_chars"].default == 8000


# ---------------------------------------------------------------------------
# The ReAct lane finally gets a real system prompt
# ---------------------------------------------------------------------------


def test_reason_passes_a_system_prompt_to_create_agent():
    """``reason()`` used to pass none, so its instructions rode in as a
    HumanMessage that sat behind every accumulated tool result."""
    fake_compiled = MagicMock()
    fake_compiled.invoke.return_value = {"messages": [AIMessage(content="ok")]}
    agent = _agent(_FakeToolModel(messages=iter([AIMessage(content="ok")])))
    with (
        patch("infra_brain.agents.llm_base.create_agent", return_value=fake_compiled) as mock_ca,
        patch("infra_brain.agents.llm_base.get_session", side_effect=Exception("no db")),
    ):
        agent.reason("task", tools=[_echo_tool()], max_iters=10)
    system_prompt = mock_ca.call_args.kwargs["system_prompt"]
    assert system_prompt
    assert "READ-ONLY" in system_prompt
    assert "at most 10 model turns" in system_prompt


def test_system_prompt_states_the_budget_and_a_soft_stop_below_it():
    """Non-termination is the failure being fixed: the model must be told both
    the hard limit and a turn to start landing on, with real room to land."""
    from infra_brain.agents.llm_base import _build_react_system_prompt

    prompt = _build_react_system_prompt(max_iters=10, has_tools=True)
    assert "at most 10 model turns" in prompt
    assert "by turn 8" in prompt  # soft stop, two turns of headroom
    assert "MUST write it by turn 10" in prompt
    # The incentive that makes stopping the rational choice.
    assert "NO answer" in prompt
    assert "incomplete answer always beats no answer" in prompt.lower()


def test_system_prompt_soft_stop_never_collapses_below_turn_one():
    """A tiny budget must still name a reachable soft stop, not turn 0/-1."""
    from infra_brain.agents.llm_base import _build_react_system_prompt

    for max_iters in (1, 2, 3, 4, 8):
        prompt = _build_react_system_prompt(max_iters=max_iters, has_tools=True)
        assert "by turn 0" not in prompt
        assert "by turn -" not in prompt


def test_system_prompt_omits_the_turn_budget_when_there_are_no_tools():
    """A tool-less ``reason()`` (Coverage/Remediation) is a single model turn —
    a turn budget it cannot spend would be noise."""
    from infra_brain.agents.llm_base import _build_react_system_prompt

    prompt = _build_react_system_prompt(max_iters=8, has_tools=False)
    assert "model turns" not in prompt
    assert "READ-ONLY" in prompt  # posture still stated


def test_system_prompt_licenses_saying_it_does_not_know():
    """Honesty over plausibility — the alternative to a guessed hostname."""
    from infra_brain.agents.llm_base import _build_react_system_prompt

    prompt = _build_react_system_prompt(max_iters=8, has_tools=True)
    assert "could not determine" in prompt
    assert "Never invent" in prompt


def test_system_prompt_teaches_navigation_around_truncated_and_failed_calls():
    """Both are wasted-iteration sources: re-calling a truncated result returns
    the same truncation; retrying an ERROR returns the same error."""
    from infra_brain.agents.llm_base import _build_react_system_prompt

    prompt = _build_react_system_prompt(max_iters=8, has_tools=True)
    assert "_truncated" in prompt
    assert "ERROR:" in prompt


def test_system_prompt_never_weakens_the_read_only_posture():
    """SAFETY: the prompt may only tighten. It must not grant a capability or
    hint at a way past a guard."""
    from infra_brain.agents.llm_base import _build_react_system_prompt

    for has_tools in (True, False):
        prompt = _build_react_system_prompt(max_iters=8, has_tools=has_tools)
        lowered = prompt.lower()
        assert "read-only" in lowered
        for forbidden in ("bypass", "ignore the", "override", "you may modify"):
            assert forbidden not in lowered, f"{forbidden!r} must not appear in the system prompt"
