"""Tool-execution boundary gate — R2 layer 2 (ARCHITECTURE-RECOMMENDATIONS.md §2).

Blocking read-only + DLP enforcement that runs BEFORE a tool executes and raises
OUTSIDE the LangChain callback machinery. The gate calls the validator handler
methods DIRECTLY (plain Python calls), so nothing can swallow the raise — unlike
callbacks routed through a CallbackManager (F-004.1).

Division of labor:
- This module BLOCKS (fail-closed, pre-execution).
- The callback chain from ``build_callbacks()`` stays attached for AUDIT
  (AuditCallbackHandler / ObservationCallbackHandler) — observation only.
"""

from __future__ import annotations

import json
import logging
import uuid

from langchain_core.messages import ToolMessage
from langchain_core.runnables import Runnable, RunnableConfig

from infra_brain.callbacks.dlp import DLPCallbackHandler
from infra_brain.callbacks.readonly import ReadOnlyToolValidator

logger = logging.getLogger(__name__)


def enforce_tool_gate(
    tool_name: str,
    tool_input,
    agent_name: str,
    whitelisted_post: list[str] | None = None,
) -> None:
    """Raise PermissionError BEFORE a tool executes if it violates read-only/DLP.

    ``tool_input`` may be a dict (LangChain tool args) or a raw string. The
    validators' own denial paths write the AuditLog row, so a blocked call is
    both refused and recorded.
    """
    if isinstance(tool_input, str):
        input_str = tool_input
    else:
        try:
            input_str = json.dumps(tool_input, default=str)
        except (TypeError, ValueError):
            input_str = str(tool_input)
    run_id = uuid.uuid4()
    # Direct handler-method calls: any PermissionError propagates to the caller.
    # These are NOT routed through a CallbackManager, so nothing swallows them.
    ReadOnlyToolValidator(
        agent_name=agent_name, whitelisted_post=whitelisted_post or []
    ).on_tool_start({"name": tool_name}, input_str, run_id=run_id)
    DLPCallbackHandler(agent_name=agent_name).on_tool_start(
        {"name": tool_name}, input_str, run_id=run_id
    )


def dedup_gate_callbacks(callbacks):
    """Strip ``ReadOnlyToolValidator``/``DLPCallbackHandler`` out of *callbacks*.

    B1 (de-dup safety chain): the two call sites that invoke
    ``enforce_tool_gate()`` directly BEFORE executing a tool (this module's
    ``guarded_tools_node`` and ``agents/llm_base.py``'s ReAct loop) already get
    fail-closed read-only/DLP enforcement from that direct call. If the same
    ``ReadOnlyToolValidator``/``DLPCallbackHandler`` instances are also present
    in the ``config["callbacks"]`` forwarded into the tool invocation, the
    LangChain callback machinery fires them a SECOND, redundant time.

    This is ONLY safe to use on those two gated paths. Every other call site in
    the codebase (the ~30+ collector/ETL agents that invoke tools directly with
    ``config={"callbacks": self.callbacks}`` and no ``enforce_tool_gate`` call
    anywhere in the path) relies on the callback-chain validators as their ONLY
    enforcement — do NOT apply this helper there, and do NOT change
    ``build_callbacks()`` to stop including them. ``AuditCallbackHandler`` /
    ``ObservationCallbackHandler`` are deliberately kept so audit logging still
    fires via the callback chain.
    """
    if not callbacks:
        return callbacks
    if isinstance(callbacks, list):
        return [
            cb
            for cb in callbacks
            if not isinstance(cb, (ReadOnlyToolValidator, DLPCallbackHandler))
        ]
    # BaseCallbackManager (or subclass) — LangGraph/LangChain often hand nodes an
    # already-merged manager rather than a plain list. Copy it and drop the
    # handlers we already ran directly via enforce_tool_gate(), by identity/type,
    # from both the regular and inheritable handler lists.
    handlers = getattr(callbacks, "handlers", None)
    if handlers is None:
        return callbacks
    manager = callbacks.copy()
    for handler in list(getattr(manager, "handlers", [])):
        if isinstance(handler, (ReadOnlyToolValidator, DLPCallbackHandler)):
            manager.remove_handler(handler)
    for handler in list(getattr(manager, "inheritable_handlers", [])):
        if (
            isinstance(handler, (ReadOnlyToolValidator, DLPCallbackHandler))
            and handler not in manager.handlers
        ):
            try:
                manager.inheritable_handlers.remove(handler)
            except ValueError:
                pass
    return manager


def scan_tool_output(tool_name: str, output, agent_name: str) -> None:
    """Raise PermissionError if the tool OUTPUT trips DLP (e.g. a Luhn-valid PAN)."""
    run_id = uuid.uuid4()
    DLPCallbackHandler(agent_name=agent_name).on_tool_end(str(output), run_id=run_id)


class _GuardedToolsNode(Runnable[dict, dict]):
    """LangGraph node that executes tool calls behind the read-only/DLP gate.

    Drop-in replacement for ``langgraph.prebuilt.ToolNode``: reads tool_calls off
    the last message, gates each call with ``enforce_tool_gate`` BEFORE invoking,
    DLP-scans each output, and returns ToolMessages. A blocked call produces a
    "BLOCKED by read-only boundary gate" ToolMessage — the tool function is never
    entered — instead of crashing the whole graph run.

    **Why a Runnable with BOTH ``invoke`` and ``ainvoke`` (T-runner-async-tools).**
    This used to be a plain sync closure. LangGraph coerces a plain sync callable
    into a node whose async path is just ``run_in_executor(sync_callable)``, so a
    graph driven with ``ainvoke()``/``astream_events()`` still reached
    ``tool_obj.invoke(...)`` — the SYNC tool entrypoint. Any tool declared
    ``@tool async def`` is an async-only ``StructuredTool`` with no sync ``_run``,
    so ``.invoke()`` on it raises
    ``NotImplementedError: StructuredTool does not support sync invocation.``,
    which the generic ``except Exception`` below turned into a
    ``"tool 'X' failed: ..."`` ToolMessage. That silently killed the headless
    runner's four ``RUNNER_ACTION_TOOLS`` (runner/tools.py) — none of them could
    ever execute. Implementing the node as a ``Runnable`` means LangGraph uses it
    as-is (``coerce_to_runnable`` returns any ``Runnable`` untouched, so node
    naming/tracing semantics are unchanged) and dispatches ``ainvoke`` on the
    async graph paths — where ``await tool_obj.ainvoke(...)`` runs an async tool
    natively and, for a sync tool, falls back to LangChain's own
    ``run_in_executor`` thread hop exactly as before.

    The sync ``invoke`` path is behaviorally identical to the previous closure
    (same order of operations, same ToolMessage contents, same log lines), so any
    sync caller (``graph.invoke()``) behaves exactly as it did before this change.
    Both paths run the SAME ``enforce_tool_gate`` pre-execution gate, the SAME
    ``dedup_gate_callbacks`` handling so the audit chain still reaches the tool,
    and the SAME ``scan_tool_output`` DLP scan on the result — the only difference
    is sync vs async tool dispatch.
    """

    # Node name for LangGraph/LangSmith; both call sites add this under "tools".
    name = "tools"

    def __init__(self, tools: list, agent_name: str) -> None:
        self._tools_by_name = {t.name: t for t in tools}
        self._agent_name = agent_name

    # -- shared helpers (identical semantics for the sync and async paths) ----

    @staticmethod
    def _tool_calls(state: dict) -> list:
        last = state["messages"][-1]
        return list(getattr(last, "tool_calls", None) or [])

    def _gate(self, name: str, args, config: RunnableConfig | None) -> RunnableConfig | None:
        """Run the pre-execution gate, then return the config to hand the tool.

        Raises PermissionError (fail-closed) if the call violates read-only/DLP —
        the tool is never entered.
        """
        enforce_tool_gate(name, args, self._agent_name)
        # N-1: propagate the graph-invocation config (which carries the
        # ReadOnlyToolValidator -> DLP -> AuditCallbackHandler chain from
        # build_callbacks()) into the tool call. A bare ``.invoke(args)``
        # does NOT forward callbacks, so AuditCallbackHandler never fired
        # and no AgentActionLog row was written for chat-lane tool calls.
        if config is not None and config.get("callbacks"):
            return {**config, "callbacks": dedup_gate_callbacks(config["callbacks"])}
        return config

    def _blocked_content(self, name: str, exc: BaseException) -> str:
        # Read-only / DLP denial (gate or output scan) — distinct path.
        logger.warning(
            "[boundary-gate] blocked tool=%s agent=%s: %s",
            name,
            self._agent_name,
            exc,
        )
        return f"BLOCKED by read-only boundary gate: {exc}"

    def _failed_content(self, name: str, exc: BaseException) -> str:
        # N-4: any other tool-execution failure must NOT crash the whole
        # chat turn (previously it propagated and killed the SSE stream
        # mid-response with a 500). Log it server-side and return a
        # graceful ToolMessage so the model can recover the turn.
        logger.exception(
            "[boundary-gate] tool=%s agent=%s raised: %s",
            name,
            self._agent_name,
            exc,
        )
        return f"tool '{name}' failed: {exc}"

    # -- sync path (unchanged behavior) --------------------------------------

    def invoke(self, state: dict, config: RunnableConfig | None = None, **kwargs) -> dict:
        out: list[ToolMessage] = []
        for call in self._tool_calls(state):
            name = call["name"]
            args = call.get("args") or {}
            tool_obj = self._tools_by_name.get(name)
            if tool_obj is None:
                content = f"unknown tool: {name}"
            else:
                try:
                    gated_config = self._gate(name, args, config)
                    result = tool_obj.invoke(args, config=gated_config)
                    scan_tool_output(name, result, self._agent_name)
                    content = str(result)
                except PermissionError as exc:
                    content = self._blocked_content(name, exc)
                except Exception as exc:  # noqa: BLE001
                    content = self._failed_content(name, exc)
            out.append(ToolMessage(content=content, tool_call_id=call["id"], name=name))
        return {"messages": out}

    # -- async path (new; identical gating, async tool dispatch) -------------

    async def ainvoke(self, state: dict, config: RunnableConfig | None = None, **kwargs) -> dict:
        out: list[ToolMessage] = []
        for call in self._tool_calls(state):
            name = call["name"]
            args = call.get("args") or {}
            tool_obj = self._tools_by_name.get(name)
            if tool_obj is None:
                content = f"unknown tool: {name}"
            else:
                try:
                    gated_config = self._gate(name, args, config)
                    # ``ainvoke`` runs an async-only tool natively and, for a sync
                    # tool, delegates to BaseTool's own run_in_executor fallback —
                    # so sync tool bodies still never block the event loop.
                    result = await tool_obj.ainvoke(args, config=gated_config)
                    scan_tool_output(name, result, self._agent_name)
                    content = str(result)
                except PermissionError as exc:
                    content = self._blocked_content(name, exc)
                except Exception as exc:  # noqa: BLE001
                    content = self._failed_content(name, exc)
            out.append(ToolMessage(content=content, tool_call_id=call["id"], name=name))
        return {"messages": out}


def guarded_tools_node(tools: list, agent_name: str) -> _GuardedToolsNode:
    """Return a LangGraph node that executes tool calls behind the gate.

    See ``_GuardedToolsNode`` for the full contract. The returned object is a
    ``Runnable`` (not a bare closure) so LangGraph dispatches the async path on
    ``ainvoke``/``astream_events`` graphs — required for async-only tools such as
    the headless runner's ``RUNNER_ACTION_TOOLS``.
    """
    return _GuardedToolsNode(tools, agent_name)
