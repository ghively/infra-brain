"""T7 (rev14): reasoning capture for AgentDecisionLog.

``_log_decisions`` used to read a turn's narration as
``msg.content if isinstance(msg.content, str) else str(msg.content)``, which

  * stored a Python *repr* when ``content`` was a list of structured content
    blocks (Anthropic native, and every extended-thinking / reasoning model),
    and
  * dropped ``additional_kwargs["reasoning_content"]`` /
    ``additional_kwargs["reasoning"]`` — where the OpenAI-compatible reasoning
    shims (DeepSeek-R1, Ollama, vLLM, OpenRouter) actually put the narration —
    entirely.

The counterpart requirement is that nothing is INVENTED: an assistant message
carrying tool_calls with ``content == ""`` is a model that chose to act without
narrating, and must still be recorded as empty so the UI can say so honestly.
"""

from langchain_core.messages import AIMessage

from infra_brain.agents.llm_base import extract_reasoning_text


class _Msg:
    """Minimal AIMessage stand-in for shapes langchain_core would normalize."""

    def __init__(self, content, additional_kwargs=None):
        self.content = content
        self.additional_kwargs = additional_kwargs or {}


def test_plain_string_content_is_returned_unchanged():
    assert extract_reasoning_text(AIMessage(content="I will list the repos.")) == (
        "I will list the repos."
    )


def test_structured_text_blocks_are_joined_not_reprd():
    msg = _Msg(
        [
            {"type": "text", "text": "First I check the tree."},
            {"type": "text", "text": "Then I read the file."},
        ]
    )
    out = extract_reasoning_text(msg)
    assert out == "First I check the tree.\n\nThen I read the file."
    assert not out.startswith("["), "must never store a Python repr of the block list"
    assert "'type':" not in out


def test_thinking_blocks_are_captured():
    msg = _Msg(
        [
            {"type": "thinking", "thinking": "The repo layout is unusual."},
            {"type": "text", "text": "Listing entities/."},
        ]
    )
    assert extract_reasoning_text(msg) == "The repo layout is unusual.\n\nListing entities/."


def test_tool_use_blocks_are_skipped_so_args_do_not_leak_into_reasoning():
    msg = _Msg(
        [
            {"type": "text", "text": "Fetching."},
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "gitlab_file_tool",
                "input": {"path": "secret/creds.yml"},
            },
        ]
    )
    out = extract_reasoning_text(msg)
    assert out == "Fetching."
    assert "creds.yml" not in out


def test_reasoning_content_side_channel_is_used_when_content_is_empty():
    msg = _Msg("", additional_kwargs={"reasoning_content": "Let me enumerate the projects."})
    assert extract_reasoning_text(msg) == "Let me enumerate the projects."


def test_openrouter_style_reasoning_key_is_also_read():
    msg = _Msg("", additional_kwargs={"reasoning": "Checking the inventory first."})
    assert extract_reasoning_text(msg) == "Checking the inventory first."


def test_content_wins_over_the_side_channel_when_both_are_present():
    msg = _Msg("Visible answer.", additional_kwargs={"reasoning_content": "hidden"})
    assert extract_reasoning_text(msg) == "Visible answer."


def test_silent_tool_call_turn_stays_empty_nothing_is_fabricated():
    """The live failure mode: an OpenAI-compatible tool-call turn has
    ``content == ""`` and no reasoning channel. That is a real fact about the
    model, not a capture bug — it must be recorded as empty, not filled in."""
    msg = AIMessage(
        content="",
        tool_calls=[{"name": "gitlab_file_tool", "args": {"path": "a"}, "id": "1"}],
    )
    assert extract_reasoning_text(msg) == ""


def test_whitespace_only_content_is_treated_as_no_narration():
    assert extract_reasoning_text(_Msg("   \n  ")) == ""
    assert extract_reasoning_text(_Msg([{"type": "text", "text": "  "}])) == ""


def test_unknown_and_malformed_shapes_degrade_to_empty_without_raising():
    assert extract_reasoning_text(_Msg(None)) == ""
    assert extract_reasoning_text(_Msg(42)) == ""
    assert extract_reasoning_text(_Msg([{"type": "mystery"}, None, 7])) == ""
    assert extract_reasoning_text(object()) == ""


def test_log_decisions_persists_block_content_as_prose(monkeypatch):
    """End-to-end through ``_log_decisions``: a block-shaped turn must land in
    ``reasoning_text`` as prose, and its ``decision_summary`` must be derived
    from that same prose."""
    from infra_brain.agents.llm_base import LLMAgent

    class _Probe(LLMAgent):
        domain = "probe"

    rows = []

    class _Session:
        def add(self, row):
            rows.append(row)

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("infra_brain.agents.llm_base.get_session", lambda: _Session())

    agent = _Probe.__new__(_Probe)
    msg = AIMessage(content="")
    msg.content = [{"type": "text", "text": "Reading the inventory file."}]
    LLMAgent._log_decisions(agent, __import__("uuid").uuid4(), [msg])

    assert len(rows) == 1
    assert rows[0].reasoning_text == "Reading the inventory file."
    assert rows[0].decision_summary == "Reading the inventory file."
