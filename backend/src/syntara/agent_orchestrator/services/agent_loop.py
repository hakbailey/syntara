"""Dependency-light LangGraph reasoning loop shared by sandbox harnesses.

The loop owns only model/tool orchestration. Persistence, Redis publication,
Temporal callbacks, credential resolution, and context retrieval remain above
the :class:`~syntara.agent_orchestrator.runtime.protocol.AgentRuntime` boundary.
"""

import json
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from langchain.messages import AnyMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from syntara.agent_orchestrator.models.agent_state import AgentState
from syntara.agent_orchestrator.utils.response_metadata import normalize_response_metadata
from syntara.agent_orchestrator.utils.token_usage import aggregate_token_usage

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

LoopEventHandler = Callable[[dict[str, Any]], Awaitable[None]]
AgentNode = Callable[[AgentState], Coroutine[Any, Any, AgentState | dict[str, Any]]]

_MAX_TOOL_OUTPUT_LENGTH = 10_000
_MAX_TOOL_CONTENT_LENGTH = 200


class TraceAccumulator:
    """Build the persisted trace from LangGraph's model/tool events."""

    def __init__(self) -> None:
        """Initialize an empty trace."""
        self._steps: list[dict[str, Any]] = []
        self._reasoning_buffer: list[str] = []
        self._reasoning_start_ns: int | None = None
        self._tool_start_ns: dict[int, int] = {}
        self._tool_run_to_call_index: dict[str, int] = {}
        self._tool_name_to_call_indices: dict[str, deque[int]] = defaultdict(deque)
        self._tool_call_counter = 0

    def accumulate(self, event: dict[str, Any]) -> None:
        """Accumulate one raw LangGraph event."""
        event_type = event.get("event")
        if event_type == "on_chat_model_stream":
            data = event.get("data")
            chunk = data.get("chunk") if isinstance(data, dict) else None
            content = getattr(chunk, "content", None)
            if content:
                if self._reasoning_start_ns is None:
                    self._reasoning_start_ns = time.monotonic_ns()
                self._reasoning_buffer.append(str(content))
        elif event_type == "on_tool_start":
            self._on_tool_start(event)
        elif event_type == "on_tool_end":
            self._on_tool_end(event)

    def _flush_reasoning(self) -> None:
        if not self._reasoning_buffer:
            return
        duration_ms = 0
        if self._reasoning_start_ns is not None:
            duration_ms = int((time.monotonic_ns() - self._reasoning_start_ns) / 1_000_000)
        self._steps.append(
            {
                "type": "reasoning",
                "timestamp": datetime.now(UTC).isoformat(),
                "content": "".join(self._reasoning_buffer),
                "tokens": len(self._reasoning_buffer),
                "duration_ms": duration_ms,
            }
        )
        self._reasoning_buffer.clear()
        self._reasoning_start_ns = None

    def _on_tool_start(self, event: dict[str, Any]) -> None:
        self._flush_reasoning()
        tool_name = str(event.get("name", "unknown"))
        data = event.get("data")
        raw_input = data.get("input", {}) if isinstance(data, dict) else {}
        tool_input = raw_input if isinstance(raw_input, dict) else {}
        call_index = self._tool_call_counter
        self._tool_call_counter += 1
        self._tool_start_ns[call_index] = time.monotonic_ns()
        self._tool_name_to_call_indices[tool_name].append(call_index)
        run_id = event.get("run_id")
        if isinstance(run_id, str) and run_id:
            self._tool_run_to_call_index[run_id] = call_index
        self._steps.append(
            {
                "type": "tool_call",
                "timestamp": datetime.now(UTC).isoformat(),
                "content": f"Calling {tool_name}",
                "tool_name": tool_name,
                "tool_input": _json_safe_dict(tool_input),
                "call_id": f"call-{call_index}",
            }
        )

    def _on_tool_end(self, event: dict[str, Any]) -> None:
        tool_name = str(event.get("name", "unknown"))
        data = event.get("data")
        raw_output = data.get("output", "") if isinstance(data, dict) else ""
        output = str(getattr(raw_output, "content", raw_output))
        if len(output) > _MAX_TOOL_OUTPUT_LENGTH:
            output = f"{output[:_MAX_TOOL_OUTPUT_LENGTH]}... [truncated]"
        run_id = event.get("run_id")
        call_index = self._tool_run_to_call_index.pop(run_id, None) if isinstance(run_id, str) else None
        if call_index is None and self._tool_name_to_call_indices[tool_name]:
            call_index = self._tool_name_to_call_indices[tool_name].popleft()
        duration_ms = None
        if call_index is not None and (started := self._tool_start_ns.pop(call_index, None)) is not None:
            duration_ms = int((time.monotonic_ns() - started) / 1_000_000)
        step: dict[str, Any] = {
            "type": "tool_result",
            "timestamp": datetime.now(UTC).isoformat(),
            "content": output[:_MAX_TOOL_CONTENT_LENGTH],
            "tool_name": tool_name,
            "tool_output": output,
            "status": "success",
        }
        if call_index is not None:
            step["call_id"] = f"call-{call_index}"
        if duration_ms is not None:
            step["duration_ms"] = duration_ms
        self._steps.append(step)

    def finalize(self, model_name: str, final_answer: object) -> dict[str, Any]:
        """Return the stable persisted trace shape."""
        self._flush_reasoning()
        answer = final_answer if isinstance(final_answer, str) else json.dumps(final_answer)
        if answer:
            self._steps.append(
                {
                    "type": "final_answer",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "content": answer,
                }
            )
        return {
            "model": model_name,
            "total_tokens": sum(int(step.get("tokens", 0)) for step in self._steps),
            "total_duration_ms": sum(int(step.get("duration_ms", 0)) for step in self._steps),
            "steps": self._steps,
        }


def _json_safe_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Drop values that cannot cross the HTTP/Redis boundary."""
    serializable = (str, int, float, bool, list, dict, type(None))
    return {key: item for key, item in value.items() if isinstance(item, serializable)}


def graph_event_to_wire(event: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a raw LangGraph event into the runtime wire protocol."""
    event_type = event.get("event")
    data = event.get("data")
    if event_type == "on_chat_model_stream" and isinstance(data, dict):
        content = getattr(data.get("chunk"), "content", None)
        if content:
            return {"type": "delta", "data": {"delta": str(content)}}
    if event_type == "on_tool_start":
        raw_input = data.get("input", {}) if isinstance(data, dict) else {}
        return {
            "type": "tool_call",
            "data": {
                "tool_name": str(event.get("name", "unknown")),
                "tool_input": _json_safe_dict(raw_input if isinstance(raw_input, dict) else {}),
            },
        }
    if event_type == "on_tool_end":
        raw_output = data.get("output", "") if isinstance(data, dict) else ""
        return {
            "type": "tool_result",
            "data": {
                "tool_name": str(event.get("name", "unknown")),
                "tool_output": str(getattr(raw_output, "content", raw_output)),
            },
        }
    return None


def compile_agent_graph(
    *,
    agent_node: AgentNode,
    tool_node: ToolNode,
    next_after_agent: Callable[[AgentState], str],
    orchestrator_node: AgentNode | None = None,
    route_after_orchestrator: Callable[[AgentState], str] | None = None,
) -> CompiledStateGraph[AgentState, None, Any, Any]:
    """Compile the graph topology used by both in-process and sandboxed loops."""
    workflow = StateGraph(AgentState)
    workflow.add_node("generic_agent", cast("Any", agent_node))
    workflow.add_node("tools", tool_node)
    if orchestrator_node is not None and route_after_orchestrator is not None:
        workflow.add_node("orchestrator", cast("Any", orchestrator_node))
        workflow.set_entry_point("orchestrator")
        workflow.add_conditional_edges("orchestrator", route_after_orchestrator, {"generic_agent": "generic_agent"})
    else:
        workflow.set_entry_point("generic_agent")
    workflow.add_conditional_edges("generic_agent", next_after_agent, ["tools", END])
    workflow.add_edge("tools", "generic_agent")
    return workflow.compile(checkpointer=MemorySaver())


async def stream_agent_graph(
    graph: CompiledStateGraph[AgentState, None, Any, Any],
    initial_state: AgentState,
    config: "RunnableConfig",
    on_event: LoopEventHandler,
) -> AgentState | None:
    """Drive a compiled graph and return its final state after observing events."""
    final_state: AgentState | None = None
    async for raw_event in graph.astream_events(initial_state, config, version="v2"):
        event = cast("dict[str, Any]", raw_event)
        await on_event(event)
        if event.get("event") == "on_chain_end" and event.get("name") == "LangGraph":
            data = event.get("data")
            if isinstance(data, dict):
                final_state = cast("AgentState | None", data.get("output"))
    return final_state


class AgentLoop:
    """Run the generic-agent/tool loop with injected model and tools."""

    def __init__(self, llm: BaseChatModel, tools: list[BaseTool], system_prompt: str) -> None:
        """Store injected below-boundary dependencies."""
        self._llm = llm
        self._tools = tools
        self._system_prompt = system_prompt

    async def execute(self, initial_state: AgentState, emit: LoopEventHandler) -> dict[str, Any]:
        """Execute a prepared state and emit serializable progress events."""
        graph = self._build_graph()
        config = cast("RunnableConfig", {"configurable": {"thread_id": initial_state["session_id"]}})
        trace = TraceAccumulator()

        async def on_event(event: dict[str, Any]) -> None:
            trace.accumulate(event)
            wire_event = graph_event_to_wire(event)
            if wire_event is not None:
                await emit(wire_event)

        final_state = await stream_agent_graph(graph, initial_state, config, on_event)

        result = self._build_result(final_state)
        result["agent_trace"] = trace.finalize(self.model_name, result.get("content", ""))
        usage_log = result.get("llm_token_usage_log") or []
        if usage_log:
            _prompt, _completion, total_tokens, _details = aggregate_token_usage(usage_log)
            result["agent_trace"]["total_tokens"] = total_tokens
        result["tokens_used"] = result["agent_trace"]["total_tokens"]
        steps = result["agent_trace"]["steps"]
        result["tools_used"] = [step["tool_name"] for step in steps if step.get("type") == "tool_call"]
        result["tool_calls"] = [
            {
                "tool_name": step["tool_name"],
                "duration_ms": step.get("duration_ms"),
                "status": step.get("status", "success"),
            }
            for step in steps
            if step.get("type") == "tool_result"
        ]
        return result

    @property
    def model_name(self) -> str:
        """Return a stable model label for result metadata."""
        return str(getattr(self._llm, "model_name", getattr(self._llm, "model", "unknown")))

    def _build_graph(self) -> CompiledStateGraph[AgentState, None, Any, Any]:
        return compile_agent_graph(
            agent_node=self._call_model,
            tool_node=ToolNode(self._tools),
            next_after_agent=self._next_node,
        )

    async def _call_model(self, state: AgentState) -> dict[str, Any]:
        messages: list[AnyMessage] = [
            SystemMessage(content=self._system_prompt),
            HumanMessage(content=state["prompt"]),
        ]
        messages.extend(message for message in state["messages"] if not isinstance(message, HumanMessage))

        response_schema = state.get("response_schema")
        if response_schema and not self._tools:
            return await self._call_structured(messages, response_schema)

        runnable = self._llm.bind_tools(self._tools)
        response = await runnable.ainvoke(messages)
        content: object = str(response.text)
        token_log = _token_usage_entry(response)
        if response_schema and not response.tool_calls and content:
            content, extraction_message = await self._extract_structured(content, response_schema)
            extraction_usage = _token_usage_entry(extraction_message) if extraction_message is not None else None
            if extraction_usage is not None:
                token_log.extend(extraction_usage)
        return {
            "messages": [response],
            "result": {
                "content": content,
                "response_metadata": normalize_response_metadata(
                    response.response_metadata,
                    usage_metadata=response.usage_metadata,
                ),
            },
            "llm_token_usage_log": token_log,
        }

    async def _call_structured(self, messages: list[AnyMessage], schema: dict[str, Any]) -> dict[str, Any]:
        runnable = self._llm.with_structured_output(schema, method="json_mode", include_raw=True)
        response = await runnable.ainvoke(messages)
        parsed = response.get("parsed") if isinstance(response, dict) else response
        raw = response.get("raw") if isinstance(response, dict) else None
        raw_message = raw if isinstance(raw, AIMessage) else None
        return {
            "messages": [raw_message] if raw_message is not None else [],
            "result": {"content": parsed, "response_metadata": {}},
            "llm_token_usage_log": _token_usage_entry(raw_message) if raw_message is not None else [],
        }

    async def _extract_structured(self, content: object, schema: dict[str, Any]) -> tuple[object, AIMessage | None]:
        runnable = self._llm.with_structured_output(schema, method="json_mode", include_raw=True)
        response = await runnable.ainvoke(
            [
                SystemMessage(content=f"Return only JSON matching this schema: {json.dumps(schema)}"),
                HumanMessage(content=f"Extract from this text:\n\n{content}"),
            ]
        )
        if isinstance(response, dict):
            raw = response.get("raw")
            return response.get("parsed", content), raw if isinstance(raw, AIMessage) else None
        return response, None

    @staticmethod
    def _next_node(state: AgentState) -> str:
        messages = state.get("messages") or []
        last_message = messages[-1] if messages else None
        return "tools" if isinstance(last_message, AIMessage) and last_message.tool_calls else END

    def _build_result(self, final_state: AgentState | None) -> dict[str, Any]:
        result_value = final_state.get("result") if final_state else None
        result: dict[str, Any]
        if final_state and isinstance(result_value, dict):
            result = dict(result_value)
            usage = final_state.get("llm_token_usage_log") or []
            if usage:
                result["llm_token_usage_log"] = usage
        else:
            result = {"content": "Unable to generate response", "response_metadata": {}}
        metadata = result.get("response_metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            result["response_metadata"] = metadata
        metadata.update({"source": "streaming", "orchestration": "langgraph", "model": self.model_name})
        context_package = final_state.get("context_package") if final_state else None
        if isinstance(context_package, dict):
            result["grounding_score"] = context_package.get("grounding_score")
            result["context_enhancement"] = {
                "turn_id": context_package.get("package_id"),
                "citations": context_package.get("citations"),
                "context_applied": context_package.get("context_applied", True),
            }
        return result


def _token_usage_entry(message: AIMessage) -> list[dict[str, Any]]:
    usage = message.usage_metadata
    if isinstance(usage, dict) and usage.get("input_tokens") is not None:
        return [
            {
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage.get("output_tokens", 0),
                "usage_details": dict(usage),
            }
        ]
    token_usage = message.response_metadata.get("token_usage") if message.response_metadata else None
    if isinstance(token_usage, dict) and token_usage.get("prompt_tokens") is not None:
        return [
            {
                "input_tokens": token_usage["prompt_tokens"],
                "output_tokens": token_usage.get("completion_tokens", 0),
                "usage_details": dict(token_usage),
            }
        ]
    return []
