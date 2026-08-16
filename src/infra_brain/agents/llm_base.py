"""
LLMAgent — base for agents that reason with the model and call tools.

Wave 4 item 4.2 (F-009): the tool-calling loop is the framework's
``create_agent`` (LangGraph prebuilt ReAct successor), not a hand-rolled loop —
so every LLM agent inherits checkpointing (Postgres-backed via
``infra_brain.checkpointer``), ``astream_events`` token streaming, and
framework tool dispatch. Every invocation carries ``self.callbacks``, so
ReadOnlyToolValidator / DLP / audit enforcement is real.
"""

import json
import logging
import random
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

import httpx
from anthropic import APIConnectionError as AnthropicAPIConnectionError
from anthropic import InternalServerError as AnthropicInternalServerError
from langchain.agents import create_agent
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphRecursionError
from openai import APIConnectionError as OpenAIAPIConnectionError
from openai import InternalServerError as OpenAIInternalServerError
from pydantic import ValidationError

from infra_brain.agents.base import BaseAgent
from infra_brain.callbacks.boundary import dedup_gate_callbacks, enforce_tool_gate
from infra_brain.callbacks.dlp import redact_pans_preserving_uuids
from infra_brain.checkpointer import get_checkpointer
from infra_brain.db.models import ZONE_CORPORATE, AgentDecisionLog
from infra_brain.db.session import get_session
from infra_brain.learning import build_context

logger = logging.getLogger(__name__)

# NEW (item 9): a queryable marker distinguishing a "hit the recursion limit"
# AgentDecisionLog row from an ordinary reasoning-turn row. anomaly.py's
# check_agent_anomalies() counts these per-agent over a rolling window to
# flag a possible runaway reasoning loop.
RECURSION_LIMIT_MARKER = "__RECURSION_LIMIT_HIT__"


# --------------------------------------------------------------------------- #
# TRK-119: bounded retry for structured-output calls                          #
# --------------------------------------------------------------------------- #
#
# A ``with_structured_output(...).invoke(...)`` call occasionally fails
# transiently: the model returns something that doesn't parse/validate into the
# target schema — a well-known LLM flakiness mode that a single retry usually
# clears. These are the exception types worth a bounded retry. A GENUINE config
# error (no model configured) is deliberately NOT here: it can never succeed on
# retry, so it must propagate immediately (fail fast) rather than burn retries.
STRUCTURED_OUTPUT_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    OutputParserException,
    ValidationError,
)
STRUCTURED_OUTPUT_MAX_ATTEMPTS = 2


# --------------------------------------------------------------------------- #
# T3 (rev12): markdown-fence tolerance for structured-output calls            #
# --------------------------------------------------------------------------- #
#
# Live production failure (discovery run 2026-08-09, collection_runs
# error_message): the Ollama endpoint behind the OpenAI-compat ChatOpenAI
# client returned a COMPLETE, valid JSON answer wrapped in a ```json markdown
# fence, and the structured-output path threw the real answer away as a
# pydantic.ValidationError (type=json_invalid).
#
# Root cause, traced to the exact layer: ``ChatOpenAI.with_structured_output``
# defaults to ``method="json_schema"`` (langchain_openai's ChatOpenAI override,
# NOT the base class's "function_calling" default). That path funnels the raw
# completion text through the underlying OPENAI SDK's own ``.parse()`` client
# helper (``self.root_client.chat.completions.with_raw_response.parse(...)``,
# langchain_openai/chat_models/base.py:_generate) which calls
# ``openai/lib/_parsing/_completions.py:_parse_content`` ->
# ``openai/_compat.py:model_parse_json`` -> ``schema.model_validate_json(text)``
# directly on the model's raw ``message.content`` — with NO markdown-fence
# handling whatsoever. That is a DIFFERENT code path from
# ``method="json_mode"``'s ``langchain_core`` ``JsonOutputParser``, which
# already strips ```/```json fences via ``parse_json_markdown`` before
# validating — so this gap is specific to the json_schema (default) method,
# which is exactly what every ``get_chat_model(...).with_structured_output(...)``
# call in this repo uses.
#
# The recovered pydantic.ValidationError carries the FULL raw text that failed
# to parse as JSON in its ``.errors()[0]["input"]`` (type == "json_invalid") —
# verified against a real ``model_validate_json`` call, not inferred from docs.
# That lets us recover WITHOUT re-invoking the model: strip the fence, and
# re-validate the same schema against the stripped text.
_MARKDOWN_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)


def _raw_text_from_structured_output_error(exc: Exception) -> str | None:
    """Extract the raw model-output text that failed to parse, if any.

    Deliberately narrow: for a :class:`pydantic.ValidationError`, only the
    whole-document ``type == "json_invalid"`` case carries the full raw text
    as its ``input`` (every other error type's ``input`` is a single FIELD's
    value, e.g. ``"not-an-int"`` for an ``int`` field — extracting fences out
    of those would be nonsensical and could mask a genuine validation bug).
    For an :class:`OutputParserException`, ``llm_output`` (when set) is the
    text the parser was working from.
    """
    if isinstance(exc, ValidationError):
        for err in exc.errors():
            if err.get("type") == "json_invalid" and isinstance(err.get("input"), str):
                return err["input"]
        return None
    if isinstance(exc, OutputParserException):
        llm_output = getattr(exc, "llm_output", None)
        return llm_output if isinstance(llm_output, str) else None
    return None


def _extract_fenced_json_text(raw: str) -> str | None:
    """Return the contents of the first ``` / ```json fence in *raw*, or
    ``None`` if no fence is present.

    Tolerates leading/trailing prose around the fence (e.g. "Here is the
    JSON:\\n```json\\n{...}\\n```\\nHope that helps!") since the fence
    delimiters themselves mark the payload boundary regardless of what
    surrounds them. Returns ``None`` (not the original text) when there is no
    fence at all — callers must NOT treat "no fence found" as "the whole
    string is JSON already"; that guess is exactly how genuinely non-JSON
    prose (e.g. the mid-exploration recursion-limit text) could get
    incorrectly fed to ``model_validate_json`` a second time.
    """
    match = _MARKDOWN_FENCE_RE.search(raw)
    if match is None:
        return None
    candidate = match.group(1).strip()
    return candidate or None


def _try_recover_fenced_json(exc: Exception, schema, label: str):
    """On a structured-output failure, attempt fence-stripped recovery
    WITHOUT re-invoking the model.

    Returns the validated *schema* instance on success. Returns ``None`` in
    every case where recovery does not apply — no *schema* supplied, no raw
    text recoverable from *exc*, no markdown fence found, or the
    fence-stripped text STILL fails to validate against the full *schema*
    (never a partial/lenient re-parse) — so the caller's existing
    retry/error path proceeds completely unchanged in all of those cases.
    """
    if schema is None:
        return None
    raw = _raw_text_from_structured_output_error(exc)
    if raw is None:
        return None
    fenced_text = _extract_fenced_json_text(raw)
    if fenced_text is None:
        return None
    try:
        recovered = schema.model_validate_json(fenced_text)
    except (ValidationError, ValueError):
        return None
    except (AttributeError, TypeError):
        # ``with_structured_output`` also accepts TypedDicts / raw JSON-schema
        # dicts; a future call site passing one of those as ``schema=`` must
        # degrade to "no recovery" (the unchanged retry path), not replace the
        # original transient error with an AttributeError that skips the retry
        # and defeats callers' except-based fallbacks (rev12 safety review).
        return None
    logger.warning(
        "%s: model wrapped structured output in a markdown fence — recovered "
        "by stripping the fence and re-validating against %s (no re-invoke)",
        label,
        getattr(schema, "__name__", schema),
    )
    return recovered


def invoke_structured_with_retry(
    runnable,
    prompt,
    *,
    config=None,
    attempts: int = STRUCTURED_OUTPUT_MAX_ATTEMPTS,
    label: str = "structured-output",
    schema=None,
):
    """Invoke a ``with_structured_output(...)`` runnable, retrying ONLY transient
    parse/validation failures up to ``attempts`` total.

    Non-transient errors (e.g. a real model/config error) propagate immediately
    — retrying them is pointless and the caller's own fail-open/fail-fast
    handling deals with them. On exhausting the retries, the last transient
    error is re-raised so the caller's graceful fallback still fires. This
    hardens against a single malformed-output hiccup silently discarding a whole
    run's work, without changing any feature's fail-open safety posture.

    ``schema`` (T3, rev12): the SAME pydantic model passed to
    ``model.with_structured_output(schema)`` that produced *runnable*. When
    supplied, a transient failure is first checked for the markdown-fence
    production failure mode (see the module-level comment above
    ``_MARKDOWN_FENCE_RE``) — if the raw output is fenced JSON, it is stripped
    and re-validated against ``schema`` directly, with NO re-invocation of the
    model. Only when that recovery does not apply does the existing
    retry-then-raise path run, byte-identical to before. ``schema=None`` (the
    default, and every pre-T3 call site before this change) skips the
    recovery attempt entirely.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return runnable.invoke(prompt, config=config)
        except STRUCTURED_OUTPUT_RETRYABLE_ERRORS as exc:
            recovered = _try_recover_fenced_json(exc, schema, label)
            if recovered is not None:
                return recovered
            last_exc = exc
            logger.warning(
                "%s: transient structured-output failure (attempt %d/%d): %s",
                label,
                attempt,
                attempts,
                exc,
            )
    # Retries exhausted — re-raise the last transient error for the caller's
    # fallback. (last_exc is always set here: the loop ran at least once and
    # only a retryable exception reaches this point.)
    raise last_exc  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# TRK-146: bounded retry for reason()'s agent.invoke() connection errors      #
# --------------------------------------------------------------------------- #
#
# The reasoning collectors (discovery/coverage/remediation/vuln) intermittently
# fail with "Connection error." — the Ollama endpoint behind the OpenAI-
# compatible ``ChatOpenAI`` client (see ``llm.py``) has occasional transient
# connection blips, and until now ``reason()`` had zero retry/backoff for that
# failure mode (only ``GraphRecursionError`` was handled). These are the
# exception types worth a bounded retry — transport-level connection/timeout
# failures from the two providers with a real HTTP client (``httpx`` directly,
# plus each provider SDK's own connection-error type; ``APITimeoutError`` on
# both SDKs subclasses ``APIConnectionError`` so it's covered without a
# separate entry). A genuine non-transient error (auth failure, bad request,
# unknown model, etc.) is deliberately NOT here: retrying it can never
# succeed, so it must propagate immediately (fail fast) rather than burn
# retries — same fail-fast principle as ``invoke_structured_with_retry``
# (TRK-119) above. ``GraphRecursionError`` is untouched by this tuple; it is
# handled by ``reason()``'s own except clause exactly as before.
#
# Also includes each SDK's InternalServerError (5xx) -- verified live against
# the real omniroute gateway: "503 - Chat admission capacity is temporarily
# unavailable. Retry shortly." killed a DiscoveryAgent run outright with no
# retry at all, even though the error message itself says to retry. A 5xx is
# the same class of transient, provider-side issue as a connection drop, not
# a "will never succeed" error like a bad request or auth failure.
REASON_RETRYABLE_CONNECTION_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.TimeoutException,
    OpenAIAPIConnectionError,
    AnthropicAPIConnectionError,
    OpenAIInternalServerError,
    AnthropicInternalServerError,
)
REASON_MAX_ATTEMPTS = 3
REASON_RETRY_BASE_DELAY_SECONDS = 0.5


def _reason_retry_backoff_seconds(attempt: int) -> float:
    """Jittered exponential backoff: base * 2**(attempt-1), plus up to 50%
    jitter, so repeated retries don't hammer an already-struggling endpoint
    in lockstep."""
    base = REASON_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
    return base + random.uniform(0, base * 0.5)


def _tool_failure_observation(tool_name: str, exc: Exception) -> str:
    """Render a failed tool call as an OBSERVATION the model can react to.

    A tool raising is a fact about the world ("that path does not exist"), not
    a reason to abandon the collection. LangGraph's ToolNode disagrees by
    default: ``_default_handle_tool_errors`` returns a message only for
    ``ToolInvocationError`` and re-raises everything else, so any real
    exception from a tool body escapes the graph, escapes ``reason()``, and the
    calling agent records status="failed".

    That is how DiscoveryAgent had never once completed a run: the model made
    an exploratory ``gitlab_repository_tree_tool`` call for ``entities/raw``
    (the repo has ``entities/`` and ``raw/`` as separate top-level trees),
    httpx raised 404, and one wrong guess *during exploration* killed the whole
    run. A ReAct agent is supposed to see that, adjust, and try the real path.

    Returning a string rather than raising deliberately avoids depending on
    framework error-handling behaviour. The gated copies produced by
    ``_gate_tools`` are used ONLY inside ``create_agent``'s loop — never by
    direct ``_run_tool``/``tool.invoke`` callers — so no non-LLM code path can
    receive this string where it expected structured data.

    PermissionError is NOT routed here: the read-only boundary refusing a call
    must abort the run, not become text the model can paraphrase around and
    retry. See the note in ``_gate_tools``. Runaway loops stay bounded by
    ``recursion_limit``/``max_iters``.
    """
    logger.warning(
        "tool %s failed during reason() — returning it to the model as an "
        "observation instead of aborting the run: %s",
        tool_name,
        exc,
    )
    return (
        f"ERROR: tool {tool_name} failed: {type(exc).__name__}: {exc}. "
        "This is a tool failure, not a result. Do not repeat the identical "
        "call — either try a different approach or continue without this data."
    )


# --------------------------------------------------------------------------- #
# T6 (rev14): bound tool-result size BEFORE it enters the ReAct transcript     #
# --------------------------------------------------------------------------- #
#
# A ReAct loop replays the WHOLE transcript on every model turn, so one
# oversized tool result is not paid once — it is paid again on every remaining
# turn of the run. Nothing in this lane bounded that. Measured on a real
# DiscoveryAgent run (agent_decision_log, 230 rows): prompt size grew
# monotonically 17,275 -> 17,926 -> 20,949 -> 23,410 -> 27,159 tokens across
# iterations 6..10, and the run then died on the recursion limit without ever
# producing an answer. The tools in that trace are the reason:
# ``gitlab_projects_tool`` returns the FULL project JSON for every accessible
# project (GitLab project objects carry ~100 fields each, including nested
# namespace/owner/permissions/_links), ``gitlab_file_tool`` returns an entire
# file's decoded contents with no bound at all, and iteration 9 alone batched
# six of them.
#
# The chat lane already solved exactly this, and has for some time:
# ``chat/tools.py::_truncate_tool_result`` is applied to every chat tool
# result. The domain-agent ReAct lane simply never got the same treatment.
# This is that treatment, applied at the one place every ReAct tool result
# must pass through.
#
# Placement is deliberate: ``_gate_tools`` has exactly ONE caller —
# ``reason()``, feeding ``create_agent`` — so truncation applies to the model
# transcript and NOWHERE else. Direct ``_run_tool``/``tool.invoke`` callers and
# every deterministic (non-LLM) collector keep receiving full, unmodified tool
# output; a truncated value can never reach the database or a drift comparison.
_DEFAULT_TOOL_RESULT_MAX_CHARS = 8000

# Deliberately phrased as navigation guidance, not just a notice. A model shown
# "[truncated]" with no instruction tends to re-issue the identical call hoping
# for the rest — which spends another turn and appends another truncated copy.
_TRUNCATION_HINT = (
    "Narrow the call (add a path/filter/limit or request a more specific item) "
    "instead of repeating it — repeating it returns this same truncated result."
)


def _tool_result_max_chars(settings) -> int:
    """Resolve the per-result character budget, tolerating a missing/partial
    settings object (tests construct agents via ``__new__``)."""
    raw = getattr(settings, "llm_tool_result_max_chars", None)
    if not isinstance(raw, int) or raw <= 0:
        return _DEFAULT_TOOL_RESULT_MAX_CHARS
    return raw


def _truncate_tool_result_for_transcript(result, max_chars: int):
    """Bound *result* to roughly *max_chars* characters before it becomes a
    ToolMessage in the ReAct transcript.

    Mirrors the shape of ``chat/tools.py::_truncate_tool_result`` (the same fix,
    already proven in the chat lane) with a hint tuned for the tool-loop:

    * ``str`` — cut to *max_chars* plus a trailing note.
    * ``list`` — whole items are kept until the budget is spent, then the list
      is replaced by an envelope recording the true count and how many are
      shown, so the model can tell "5 of 128" from "5".
    * ``dict`` — replaced by an envelope carrying a prefix of the serialized
      form, so the model still sees representative structure.
    * anything else (int/bool/None/…) — returned unchanged; these cannot bloat.

    A value already inside the budget is returned UNCHANGED and identical, so
    the common small-result case is byte-for-byte what it was before.
    """
    if isinstance(result, str):
        if len(result) <= max_chars:
            return result
        return (
            result[:max_chars] + f"\n\n[... truncated — the full result was {len(result)} chars, "
            f"limit {max_chars}. {_TRUNCATION_HINT}]"
        )
    if isinstance(result, (dict, list)):
        try:
            serialized = json.dumps(result, default=str)
        except (TypeError, ValueError):
            # Not JSON-serializable even with default=str — fall back to repr so
            # an exotic return value is still bounded rather than passed through.
            text = repr(result)
            if len(text) <= max_chars:
                return result
            return text[:max_chars] + f"\n\n[... truncated. {_TRUNCATION_HINT}]"
        if len(serialized) <= max_chars:
            return result
        if isinstance(result, list):
            kept: list = []
            size = 0
            for item in result:
                item_len = len(json.dumps(item, default=str))
                if size + item_len > max_chars:
                    break
                kept.append(item)
                size += item_len
            return {
                "_truncated": True,
                "count": len(result),
                "shown": len(kept),
                "data": kept,
                "hint": _TRUNCATION_HINT,
            }
        return {
            "_truncated": True,
            "hint": _TRUNCATION_HINT,
            "partial_json": serialized[:max_chars],
        }
    return result


# --------------------------------------------------------------------------- #
# T6 (rev14): a real SYSTEM prompt for the ReAct lane                          #
# --------------------------------------------------------------------------- #
#
# ``create_agent`` accepts ``system_prompt=``, and until now ``reason()`` passed
# nothing — every instruction rode in as a HumanMessage at position 0 of the
# transcript. The chat lane, by contrast, has always had a real system prompt
# (``chat/agent.py::SYSTEM_PROMPT``, re-prepended on every turn in
# ``agent_node``). That asymmetry is the second half of the recursion-limit
# story: by iteration 10 of the measured DiscoveryAgent run the task text was
# sitting behind ~27k tokens of tool output, in the position an instruction-
# tuned model weights LEAST. A system message is re-anchored at position 0 of
# every turn instead.
#
# This matters because the previous wave already tried the obvious things IN
# THE TASK PROMPT and measured that they did not work: discovery.py's own
# comments record that path-guessing rules, a larger iteration budget
# (max_iters 18), and an explicit tool-call budget were each tried against the
# live model and none produced a conclusion. What was never tried is stating
# the budget and the stopping condition somewhere that does not get buried.
#
# SAFETY: this prompt only ever TIGHTENS the read-only posture. It grants no
# capability, names no tool, and cannot instruct the model past a guard — the
# structural gate (``_gate_tools`` -> ``enforce_tool_gate``) runs regardless of
# anything the model was told or decides.
_REACT_SYSTEM_PROMPT_BASE = """You are an autonomous, READ-ONLY infrastructure agent \
running as an unattended batch job.

There is no human in this loop. Nobody will answer a question, supply a value you \
are missing, or approve a next step. Never ask for one: find it with a tool, or \
report plainly that you could not determine it.

Every tool you have is read-only. Never claim, plan, or describe having made a \
change to any system — you cannot, and saying you did would be false.

Be honest about the limits of what you saw. Never invent a hostname, IP address, \
file path, version, identifier, or count that did not appear in a tool result. \
"I could not determine X" is a correct and useful answer; a plausible guess is \
neither. Report only what your tool results actually support."""

_REACT_SYSTEM_PROMPT_TOOL_LOOP = """
BUDGET. You have at most {max_iters} model turns for this task. Each turn may \
batch several tool calls, and the entire conversation so far is re-read on every \
turn, so every result you collect makes each remaining turn more expensive. Before \
each tool call, ask: will this change what I report? If not, do not make it.

STOPPING. Finishing is mandatory; exploring is not. Aim to write your final answer \
by turn {soft_stop}, and you MUST write it by turn {max_iters}. If you reach turn \
{soft_stop} without complete information, STOP exploring and answer with what you \
have, stating explicitly what you could not determine and why. Running out of turns \
does not produce a partial answer — it produces NO answer, and the whole task is \
recorded as a failure. An incomplete answer always beats no answer.

TRUNCATED RESULTS. A result containing "_truncated" or "[... truncated" means you \
were shown only part of it. Repeating the identical call returns the identical \
truncated result and wastes a turn. Either narrow the call's arguments or continue \
with what you have.

FAILED CALLS. A result starting with "ERROR:" means that call returned no data. \
Treat it as evidence the thing does not exist. Do not retry it and do not try \
near-miss variants."""


def _build_react_system_prompt(max_iters: int, has_tools: bool) -> str:
    """Compose the system prompt for one ``reason()`` call.

    The budget/stopping section is included ONLY when tools were supplied. A
    tool-less ``reason()`` (CoverageAgent, RemediationAgent) is a single model
    turn with nothing to iterate on, so telling it about a turn budget it cannot
    spend would be noise at best and confusing at worst.
    """
    if not has_tools:
        return _REACT_SYSTEM_PROMPT_BASE
    # Leave real room to land: at 8+ turns reserve two, below that reserve one,
    # and never advertise a soft stop below turn 1.
    reserve = 2 if max_iters >= 8 else 1
    soft_stop = max(1, max_iters - reserve)
    return (
        _REACT_SYSTEM_PROMPT_BASE
        + "\n"
        + _REACT_SYSTEM_PROMPT_TOOL_LOOP.format(max_iters=max_iters, soft_stop=soft_stop)
    )

# T7 (rev14): reasoning capture for AgentDecisionLog                          #
# --------------------------------------------------------------------------- #
#
# ``_log_decisions`` used to read a model turn's narration as
# ``msg.content if isinstance(msg.content, str) else str(msg.content)``. That
# handles exactly one of the three shapes a modern chat model actually returns:
#
#   1. ``content`` is a plain string       — handled (and by far the common case
#      on the OpenAI-compatible gateway this deployment uses today).
#   2. ``content`` is a LIST of content blocks (Anthropic native, and every
#      provider that emits extended-thinking / reasoning blocks) — ``str(list)``
#      stored the Python *repr* (``[{'type': 'text', 'text': '...'}]``) into
#      ``reasoning_text``. Nothing in the live table starts with ``[`` today, so
#      this has not bitten yet — it is a latent trap that fires the moment
#      ``LLM_PROVIDER`` is switched to ``anthropic``/``bedrock`` or a thinking
#      model is routed in.
#   3. ``content`` is ``""`` and the narration lives in
#      ``additional_kwargs["reasoning_content"]`` (DeepSeek-R1 / Ollama / vLLM
#      OpenAI-compat) or ``additional_kwargs["reasoning"]`` (OpenRouter). This
#      was dropped entirely and stored as an empty string.
#
# What this does NOT do: invent narration. An OpenAI-compatible assistant
# message that carries ``tool_calls`` legitimately has ``content == ""`` — the
# model chose to act without narrating. In that case every extraction path
# below finds nothing and the column stays empty, which the API layer reports
# honestly as "tool-call turn, no narration" rather than papering over.
#
# Tool-use blocks are deliberately skipped: the tool names are already the
# ``tools_chosen`` column, and splicing serialized tool arguments into
# ``reasoning_text`` would both duplicate that column and widen the DLP surface
# of a field that is meant to hold prose.
_REASONING_TEXT_BLOCK_TYPES = ("text",)
_REASONING_THINKING_BLOCK_TYPES = ("thinking", "reasoning", "reasoning_content")
_REASONING_ADDITIONAL_KWARG_KEYS = ("reasoning_content", "reasoning")


def _block_text(block: dict) -> str:
    """Pull the prose out of one structured content block, or "" if it carries
    none (a ``tool_use``/``tool_call`` block, or an unrecognized type)."""
    btype = block.get("type")
    if btype in _REASONING_TEXT_BLOCK_TYPES:
        value = block.get("text")
        return value if isinstance(value, str) else ""
    if btype in _REASONING_THINKING_BLOCK_TYPES:
        # Providers disagree on the payload key: Anthropic puts extended
        # thinking under "thinking", the OpenAI-compat reasoning shims under
        # "reasoning_content"/"reasoning"/"text". Try the type-named key first,
        # then the siblings, so a new-but-similar shape still lands.
        for key in (btype, "thinking", "reasoning_content", "reasoning", "text"):
            value = block.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def extract_reasoning_text(msg) -> str:
    """Best-effort narration for one model turn, across every content shape.

    Returns ``""`` when the turn genuinely carries no prose (e.g. a pure
    tool-call turn). Callers must treat ``""`` as "the model did not narrate",
    NOT as "capture failed" — see the module comment above. Never raises, and
    never returns a Python repr of a structure.
    """
    content = getattr(msg, "content", None)
    parts: list[str] = []
    if isinstance(content, str):
        if content.strip():
            parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                if block.strip():
                    parts.append(block)
            elif isinstance(block, dict):
                text = _block_text(block)
                if text.strip():
                    parts.append(text)
    if parts:
        return "\n\n".join(parts).strip()

    # Nothing in ``content`` — fall back to the provider-specific reasoning
    # side-channel before concluding the turn was silent.
    extra = getattr(msg, "additional_kwargs", None)
    if isinstance(extra, dict):
        for key in _REASONING_ADDITIONAL_KWARG_KEYS:
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


class ReasonDeadlineExceededError(Exception):
    """Raised by ``LLMAgent.reason()`` when its TOTAL wall-clock budget
    (``deadline_seconds``) is exhausted — either between retries or while a
    single ``agent.invoke()`` is still in flight.

    GitLab #159/#165: before this existed, ``reason()`` had no wall-clock bound
    at all. Its only bounds were per-CALL (``llm_request_timeout_seconds``),
    multiplied by ``max_iters`` model turns, multiplied by
    ``REASON_MAX_ATTEMPTS`` retries — 10 * 120 * 3 = 3600s worst case for
    DiscoveryAgent, 12x its own 300s ``collect_timeout_seconds``. The collector's
    ``ETLConnector._call_with_timeout`` guard then killed it mid-flight with zero
    diagnostic signal (#159: a mysterious 0-resource failed run), and the
    ``query_nl`` MCP path — which calls ``reason()`` directly, bypassing
    ``ETLConnector.run()`` entirely — had no guard whatsoever (#165: a silent
    hang until the MCP client's own transport timeout fired).

    Raising this instead means the timeout is OUR error, with our message,
    attributable to the agent that blew its budget.
    """


def invoke_reason_with_retry(
    agent,
    input_,
    *,
    config,
    attempts: int = REASON_MAX_ATTEMPTS,
    label: str = "reason",
    deadline_seconds: float | None = None,
):
    """Invoke the framework ReAct ``agent`` (``create_agent(...)``'s compiled
    graph), retrying ONLY transient CONNECTION-class failures up to
    ``attempts`` total, with jittered backoff between tries.

    Mirrors ``invoke_structured_with_retry``'s transient-vs-permanent
    classification shape (TRK-119), for a different failure mode: TRK-146's
    "Connection error." from transient LLM-endpoint connection blips, not
    malformed structured output.

    Non-transient errors (auth failures, bad requests, ``GraphRecursionError``)
    are not in :data:`REASON_RETRYABLE_CONNECTION_ERRORS` and so propagate on
    the first attempt — retrying them is pointless, and ``GraphRecursionError``
    is already handled by the caller's own except clause. On exhausting the
    retries, the last connection error is re-raised so the caller's existing
    (lack of) fallback behavior is unchanged, just delayed until genuinely
    out of retries.

    ``deadline_seconds`` (GitLab #159/#165) is a TOTAL wall-clock budget across
    every attempt AND every model turn within an attempt. ``None`` or ``<= 0``
    means unbounded — byte-identical to the pre-#165 behavior, so every existing
    caller is untouched. When set, the budget is enforced three ways:
    checked before each attempt starts, used to PREEMPT an in-flight
    ``agent.invoke()`` (which internally runs up to ``max_iters`` model turns
    that this function cannot see between), and used to cap the retry backoff
    sleep. Exhausting it raises :class:`ReasonDeadlineExceededError`.

    Preemption uses a single-worker ``ThreadPoolExecutor`` with
    ``shutdown(wait=False)`` — the same shape as
    ``ETLConnector._call_with_timeout``. The abandoned thread runs to completion
    in the background rather than being killed (Python cannot kill a thread);
    what matters is that the CALLER is released on time with a real error.
    """
    deadline = (
        time.monotonic() + deadline_seconds
        if deadline_seconds is not None and deadline_seconds > 0
        else None
    )

    def _remaining() -> float | None:
        return None if deadline is None else deadline - time.monotonic()

    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        remaining = _remaining()
        if remaining is not None and remaining <= 0:
            raise ReasonDeadlineExceededError(
                f"{label}: wall-clock deadline of {deadline_seconds}s exhausted "
                f"before attempt {attempt}/{attempts}"
            )
        try:
            if remaining is None:
                return agent.invoke(input_, config=config)
            # Bounded path: preempt an invoke that outlives the budget.
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                fut = pool.submit(agent.invoke, input_, config=config)
                try:
                    return fut.result(timeout=remaining)
                except FuturesTimeout:
                    raise ReasonDeadlineExceededError(
                        f"{label}: exceeded its {deadline_seconds}s wall-clock deadline "
                        f"mid-invoke (attempt {attempt}/{attempts}) — the reasoning loop "
                        "was still running and has been abandoned"
                    ) from None
            finally:
                # Never block on the abandoned thread.
                pool.shutdown(wait=False)
        except REASON_RETRYABLE_CONNECTION_ERRORS as exc:
            last_exc = exc
            logger.warning(
                "%s: transient connection failure (attempt %d/%d): %s",
                label,
                attempt,
                attempts,
                exc,
            )
            if attempt < attempts:
                backoff = _reason_retry_backoff_seconds(attempt)
                remaining = _remaining()
                if remaining is not None:
                    if remaining <= 0:
                        raise ReasonDeadlineExceededError(
                            f"{label}: wall-clock deadline of {deadline_seconds}s exhausted "
                            f"after attempt {attempt}/{attempts}"
                        ) from exc
                    # Never sleep past the deadline — the next loop's
                    # pre-attempt check then raises immediately.
                    backoff = min(backoff, remaining)
                time.sleep(backoff)
    # Retries exhausted — re-raise the last transient error. (last_exc is
    # always set here: the loop ran at least once and only a retryable
    # exception reaches this point.)
    raise last_exc  # type: ignore[misc]


class LLMBudgetExceededError(Exception):
    """Raised by ``LLMAgent.reason()`` when the opt-in per-run LLM token ceiling
    (``settings.llm_run_token_ceiling``) has already been reached — a signal to
    stop making further LLM calls this run and degrade gracefully to
    deterministic/templated output. NEVER raised when the ceiling feature is
    disabled (the default), so it is invisible to today's behavior (TRK-120)."""


class LLMAgent(BaseAgent):
    domain = "llm_base"

    def __init_subclass__(cls, **kwargs) -> None:
        """TRK-060: ``domain = "llm_base"`` is a placeholder, not a real domain.

        Every concrete LLMAgent subclass must set its own ``domain`` — leaving
        the placeholder in place used to be silently wrong (schedule lookups,
        AgentActionLog rows, and CollectionRun rows would all be tagged
        "llm_base" if a subclass forgot). Fail loudly at class-definition time
        instead: any subclass that does not override ``domain`` raises
        immediately on import.
        """
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("domain", "llm_base") == "llm_base":
            # cls.__dict__ (not getattr) so an intermediate subclass that
            # itself leaves `domain` unset inherits — and re-triggers — the
            # check rather than silently passing via the parent's attribute.
            raise TypeError(
                f"{cls.__name__} must set a real `domain` — "
                '`domain = "llm_base"` is a placeholder, not a valid domain (TRK-060)'
            )

    def collect(self, scope: str = "all") -> list[dict]:
        return []  # Subclasses override either collect() or call reason() directly.

    def _run_tool(self, tool_obj, args: dict):
        # R2 boundary gate: blocking read-only/DLP enforcement BEFORE execution,
        # outside the callback machinery (F-004.1). Raises PermissionError on deny.
        enforce_tool_gate(tool_obj.name, args, agent_name=type(self).__name__)
        # Callbacks stay attached for the AUDIT trail (observation-only layer).
        # B1: strip ReadOnlyToolValidator/DLPCallbackHandler here — they already
        # ran (fail-closed) via enforce_tool_gate() above; forwarding them again
        # would fire them a second, redundant time through the callback chain.
        return tool_obj.invoke(args, config={"callbacks": dedup_gate_callbacks(self.callbacks)})

    def _gate_tools(self, tools: list) -> list:
        """Wrap each tool so the R2 boundary gate runs before its body executes.

        ``create_agent``'s framework tool node calls a tool's ``func``/
        ``coroutine`` directly — it never goes through ``_run_tool`` — so the
        gate has to be applied to the tool itself here, or the F-004.1 boundary
        is only real for the old hand-rolled loop, not the framework one.
        """
        agent_name = type(self).__name__
        # T6: resolved ONCE per reason() call, not per tool call. See
        # _truncate_tool_result_for_transcript for why this lives here.
        max_chars = _tool_result_max_chars(getattr(self, "settings", None))
        gated = []
        for t in tools:
            original_func = getattr(t, "func", None)
            original_coroutine = getattr(t, "coroutine", None)
            if original_func is None and original_coroutine is None:
                # N-6: a StructuredTool/BaseTool subclass that overrides _run()
                # (and so exposes neither .func nor .coroutine) would be
                # model_copy'd with both set to None below — silently turning the
                # tool into a no-op AND bypassing the boundary gate. Fail loudly
                # at wiring time instead of shipping a confusingly dead tool.
                raise RuntimeError(
                    f"_gate_tools: tool {t.name!r} exposes neither .func nor "
                    ".coroutine; the read-only boundary gate cannot wrap it. "
                    "Provide it as a StructuredTool with func=/coroutine= so the "
                    "gate can intercept execution."
                )

            def _sync(*args, __t=t, __func=original_func, __max=max_chars, **kwargs):
                # AA-R-20: DLP-scan BOTH positional and keyword args. The old
                # ``kwargs or {"args": args}`` dropped positional args whenever any
                # kwarg was present, so a PAN passed positionally alongside a kwarg
                # slipped past the gate's scan. Always forward both.
                #
                # NOTE: enforce_tool_gate stays OUTSIDE the try below. Its
                # PermissionError is the read-only boundary refusing the call and
                # must abort the run — turning it into an observation would hand
                # the model a denial it could paraphrase around and retry.
                enforce_tool_gate(
                    __t.name, {"args": list(args), "kwargs": kwargs}, agent_name=agent_name
                )
                try:
                    # T6: bound the result before it becomes a transcript entry
                    # that every remaining model turn re-reads. Applied ONLY to
                    # the success path — a _tool_failure_observation string is
                    # short and carries navigation instructions that must not be
                    # cut off mid-sentence.
                    return _truncate_tool_result_for_transcript(__func(*args, **kwargs), __max)
                except PermissionError:
                    # Defense in depth: a denial raised from INSIDE the tool body
                    # (e.g. tools/http_readonly refusing a non-GET) is the same
                    # read-only boundary as the pre-flight gate above and must
                    # abort the run, never become model-visible text.
                    raise
                except Exception as exc:  # noqa: BLE001 - see _tool_failure_observation
                    return _tool_failure_observation(__t.name, exc)

            async def _async(*args, __t=t, __coro=original_coroutine, __max=max_chars, **kwargs):
                enforce_tool_gate(
                    __t.name, {"args": list(args), "kwargs": kwargs}, agent_name=agent_name
                )
                try:
                    # T6: same bound as the sync wrapper above.
                    return _truncate_tool_result_for_transcript(
                        await __coro(*args, **kwargs), __max
                    )
                except PermissionError:
                    # Same defense-in-depth as the sync wrapper above.
                    raise
                except Exception as exc:  # noqa: BLE001 - see _tool_failure_observation
                    return _tool_failure_observation(__t.name, exc)

            gated.append(
                t.model_copy(
                    update={
                        "func": _sync if original_func is not None else None,
                        "coroutine": _async if original_coroutine is not None else None,
                    }
                )
            )
        return gated

    def _learning_preamble(self) -> str:
        # F4: inject prior learning preamble (best-effort; failure → plain task)
        try:
            settings = getattr(self, "settings", None)
            default_zone = getattr(settings, "default_zone", None) or ZONE_CORPORATE
            return build_context(
                self.__class__.__name__,
                getattr(self, "domain", ""),
                zone=getattr(self, "zone", None) or default_zone,
            )
        except Exception:  # noqa: BLE001
            return ""

    def _log_decisions(self, run_id: uuid.UUID, messages: list) -> None:
        """Best-effort AgentDecisionLog rows, one per model turn — never raises."""
        iteration = 0
        for msg in messages:
            if not isinstance(msg, AIMessage):
                continue
            # T7 (rev14): was `msg.content if isinstance(...) else str(msg.content)`,
            # which stored a Python repr for list-shaped content and dropped the
            # `additional_kwargs` reasoning channel entirely. See
            # `extract_reasoning_text` above. An empty result still means the
            # model did not narrate this turn — nothing is fabricated to fill it.
            text = extract_reasoning_text(msg)
            tool_calls = getattr(msg, "tool_calls", None) or []
            # N-2: scrub Luhn-valid PANs out of the raw model output BEFORE it is
            # persisted. Previously reasoning_text stored un-redacted model text,
            # and redact_pans() was applied only on the chat SSE stream — so the
            # domain-agent reasoning lane leaked candidate PANs into the DB.
            scrubbed = redact_pans_preserving_uuids(text)
            # OB-6: capture token usage + parent run id when the framework provides
            # them, so the decision log is queryable for cost/latency and can be
            # linked into a LangSmith run tree. Both columns are nullable.
            usage = getattr(msg, "usage_metadata", None)
            token_count = usage.get("total_tokens") if isinstance(usage, dict) else None
            resp_meta = getattr(msg, "response_metadata", None)
            parent_run_id = None
            if isinstance(resp_meta, dict):
                parent_run_id = resp_meta.get("parent_run_id") or resp_meta.get("parent_run")
            try:
                summary = (scrubbed[:120] + "…") if len(scrubbed) > 120 else scrubbed
                row = AgentDecisionLog(
                    run_id=run_id,
                    agent=self.__class__.__name__,
                    domain=getattr(self, "domain", ""),
                    iteration=iteration,
                    reasoning_text=scrubbed,
                    tools_chosen=[c["name"] for c in tool_calls],
                    decision_summary=summary,
                    token_count=token_count,
                    parent_run_id=str(parent_run_id) if parent_run_id else None,
                )
                with get_session() as session:
                    session.add(row)
                    session.commit()
            except Exception:
                # OB-3 sliver: this was previously logger.debug, which meant a
                # persistent failure here (e.g. schema drift, DB outage) was
                # invisible unless debug logging happened to be on. Raise to
                # warning with enough context (agent/domain/run_id) to triage
                # without leaking reasoning_text/scrubbed content.
                logger.warning(
                    "AgentDecisionLog persist failed (best-effort, not raised): "
                    "agent=%s domain=%s run_id=%s iteration=%d",
                    self.__class__.__name__,
                    getattr(self, "domain", ""),
                    run_id,
                    iteration,
                    exc_info=True,
                )
            iteration += 1

    def _log_recursion_limit_hit(self, run_id: uuid.UUID, max_iters: int) -> None:
        """Best-effort marker row (item 9) so anomaly detection can count how
        often this agent runs away instead of converging — never raises."""
        try:
            row = AgentDecisionLog(
                run_id=run_id,
                agent=self.__class__.__name__,
                domain=getattr(self, "domain", ""),
                iteration=-1,
                reasoning_text="",
                tools_chosen=[],
                decision_summary=RECURSION_LIMIT_MARKER,
            )
            with get_session() as session:
                session.add(row)
                session.commit()
        except Exception:
            logger.debug("AgentDecisionLog recursion-marker persist failed", exc_info=True)

    # ------------------------------------------------------------------
    # TRK-120: opt-in per-run LLM token ceiling (in-process cost safety net)
    # ------------------------------------------------------------------

    def _accumulate_run_tokens(self, messages: list) -> None:
        """Add this turn's ``total_tokens`` (from each AIMessage.usage_metadata)
        to the per-run accumulator. Best-effort: a message without usage
        metadata contributes 0. The accumulator lives on the instance, and each
        run constructs a fresh agent instance (ETLConnector.run / supervisor
        dispatch), so it is naturally scoped to one run."""
        total = getattr(self, "_llm_run_tokens_used", 0)
        for msg in messages:
            usage = getattr(msg, "usage_metadata", None)
            if isinstance(usage, dict):
                total += usage.get("total_tokens") or 0
        self._llm_run_tokens_used = total

    def _enforce_token_ceiling(self) -> None:
        """Raise :class:`LLMBudgetExceededError` when the opt-in per-run token
        ceiling is enabled and already reached.

        Default-off: a settings object without the flag (or with it False) — or
        no settings at all — is a no-op, byte-identical to today. Checked
        BEFORE each reason() model call, so the first call of a run always runs
        (used starts at 0) and only *subsequent* calls stop once cumulative
        usage crosses the ceiling — "stop making FURTHER LLM calls this run"."""
        settings = getattr(self, "settings", None)
        if not getattr(settings, "llm_run_token_ceiling_enabled", False):
            return
        ceiling = getattr(settings, "llm_run_token_ceiling", 0) or 0
        if ceiling <= 0:
            return
        used = getattr(self, "_llm_run_tokens_used", 0)
        if used >= ceiling:
            logger.warning(
                "%s: per-run LLM token ceiling reached (used=%d >= ceiling=%d) — "
                "stopping further LLM calls this run; remaining work degrades to "
                "deterministic/templated output",
                self.__class__.__name__,
                used,
                ceiling,
            )
            raise LLMBudgetExceededError(
                f"{self.__class__.__name__} per-run token ceiling {ceiling} reached (used={used})"
            )

    def reason(
        self,
        task: str,
        tools: list,
        max_iters: int = 8,
        *,
        deadline_seconds: float | None = None,
        attempts: int = REASON_MAX_ATTEMPTS,
    ) -> str:
        """Run the framework ReAct loop over *tools*, return the model's final text.

        ``max_iters`` bounds model turns: one ReAct iteration is one model call
        plus one tool batch (two graph steps), so the LangGraph
        ``recursion_limit`` is ``2 * max_iters + 1``. On hitting the limit the
        last model text is returned — parity with the old hand-rolled loop,
        which returned ``last_text`` after ``max_iters`` iterations.

        Raises :class:`LLMBudgetExceededError` before making the model call when
        the opt-in per-run token ceiling has already been reached (default-off:
        never raised unless ``llm_run_token_ceiling_enabled``).

        ``deadline_seconds`` (GitLab #159/#165) is the TOTAL wall-clock budget for
        this whole call — every model turn and every retry combined. It defaults
        to ``None`` (unbounded), which is exactly the pre-#165 behavior, so no
        existing caller changes. Exceeding it raises
        :class:`ReasonDeadlineExceededError` with a message naming the agent and
        the budget, rather than letting an EXTERNAL guard (the collector's
        ``_call_with_timeout``) kill the run with no diagnostic signal — or, on
        the ``query_nl`` MCP path where no external guard exists at all, letting
        it hang until the client gives up. Interactive callers should pass
        ``settings.mcp_tool_timeout_seconds``; collector callers should pass a
        value comfortably under their ``collect_timeout_seconds``.

        ``attempts`` exposes the retry count (default
        :data:`REASON_MAX_ATTEMPTS`) so an interactive path can trade retry
        resilience for latency without changing the global default that every
        batch collector relies on.
        """
        # TRK-120: stop before spending more once the per-run ceiling is hit.
        self._enforce_token_ceiling()
        ctx = self._learning_preamble()
        effective_task = f"{ctx}\n\n{task}" if ctx else task
        run_id = uuid.uuid4()
        # AA-R-10: a fresh thread_id is minted every reason() call and nothing ever
        # resumes it, so persisting a checkpoint is write-only garbage that no
        # retention job prunes. A tool-less reason() is a single model turn — no
        # interrupt, no tool round-trip, so it never needs checkpoint resumption
        # nor the recursion-recovery snapshot below — so skip the checkpointer
        # entirely for it. Tool-using calls keep the checkpointer, which the
        # recursion-limit handler reads back via agent.get_state(config).
        # Reset per call — a previous call's limit hit must never be read as
        # this one's outcome.
        self._last_reason_hit_iteration_limit = False
        checkpointer = get_checkpointer() if tools else None
        # T6: give the loop a real SYSTEM prompt. Until now this call passed
        # none, so the stopping condition and read-only posture rode in as a
        # HumanMessage that sat behind every accumulated tool result by the time
        # the model most needed them. See _build_react_system_prompt.
        agent = create_agent(
            self.llm,
            self._gate_tools(tools),
            system_prompt=_build_react_system_prompt(max_iters, bool(tools)),
            checkpointer=checkpointer,
        )
        # B1: tools passed to create_agent are already wrapped by _gate_tools()
        # above, which calls enforce_tool_gate() directly (fail-closed) before
        # the tool body runs. Forwarding ReadOnlyToolValidator/DLPCallbackHandler
        # in this config would let the framework's callback machinery fire them
        # again for the same tool call — strip the duplicates here too.
        config = {
            "callbacks": dedup_gate_callbacks(self.callbacks),
            "recursion_limit": 2 * max_iters + 1,
            "configurable": {"thread_id": f"{self.__class__.__name__}:{run_id}"},
        }
        try:
            # TRK-146: bounded retry for transient connection errors (the
            # underlying Ollama endpoint has occasional connection blips) —
            # GraphRecursionError is not in the retryable set, so it still
            # propagates straight through to the except clause below exactly
            # as before this change.
            result = invoke_reason_with_retry(
                agent,
                {"messages": [HumanMessage(content=effective_task)]},
                config=config,
                attempts=attempts,
                label=f"{self.__class__.__name__}.reason",
                deadline_seconds=deadline_seconds,
            )
            messages = result["messages"]
        except GraphRecursionError:
            # Flag it for the caller. The "last text" recovered below is a
            # mid-exploration message, NOT the final answer the caller asked
            # for, so any strict post-processing of it (e.g. DiscoveryAgent's
            # structured-output finalization) will fail — and did, reporting the
            # useless "Invalid JSON: expected value at line 1 column 1" instead
            # of the actual condition, which is that the agent ran out of
            # reasoning steps before concluding.
            self._last_reason_hit_iteration_limit = True
            logger.warning(
                "%s.reason() hit the recursion limit (max_iters=%s) — returning last text",
                self.__class__.__name__,
                max_iters,
            )
            try:
                snapshot = agent.get_state(config)
                messages = snapshot.values.get("messages", [])
            except Exception:  # noqa: BLE001
                messages = []
            self._log_recursion_limit_hit(run_id, max_iters)
        except ReasonDeadlineExceededError:
            # GitLab #170 (follow-up to #159/#165): a deadline preemption used
            # to skip _log_decisions()/_accumulate_run_tokens() entirely for a
            # call that may have made real (billed) model turns before being
            # abandoned — leaving no AgentDecisionLog rows and under-counting
            # the TRK-120 per-run token ceiling on exactly the runs that spent
            # the most. Recover whatever partial state the checkpointer
            # captured (same mechanism the GraphRecursionError branch above
            # uses — LangGraph checkpoints after each completed graph step,
            # so a mid-invoke preemption still leaves a readable snapshot when
            # tools were passed) before re-raising, so the caller's contract
            # (deadline exceeded -> exception) is unchanged.
            try:
                snapshot = agent.get_state(config)
                messages = snapshot.values.get("messages", [])
            except Exception:  # noqa: BLE001
                messages = []
            self._log_decisions(run_id, messages)
            self._accumulate_run_tokens(messages)
            raise
        self._log_decisions(run_id, messages)
        # TRK-120: tally this call's token usage into the per-run accumulator so
        # a subsequent reason() call in the same run can enforce the ceiling.
        self._accumulate_run_tokens(messages)
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                text = msg.content if isinstance(msg.content, str) else str(msg.content)
                if text:
                    return text
        return ""
