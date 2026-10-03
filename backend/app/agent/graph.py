from functools import lru_cache
import copy
import asyncio
import time
import uuid
from contextlib import contextmanager
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.errors import GraphInterrupt

from app.agent.nodes.agent import call_agent
from app.agent.nodes.router import pending_tool_message, should_continue
from app.agent.nodes.tools import call_tools
from app.agent.hooks import (
    AGENT_NODE_END, AGENT_NODE_START, TOOL_NODE_END, TOOL_NODE_START,
    dispatch_observe_nonblocking,
)
from app.agent.state import AgentState, merge_runtime_updates, to_runtime_view, new_agent_state
from app.storage.conversation_files import SessionConversationFiles
from app.core.settings import get_settings
from app.core.model_request import bind_model_request_scope
from app.storage.checkpoint import get_checkpointer
from app.tools.registry import get_tools


_SENSITIVE_TOOL_KEY = __import__("re").compile(
    r"token|secret|password|authorization|cookie|api[_-]?key|private[_-]?key|env(?:ironment)?",
    __import__("re").IGNORECASE,
)


def _safe_tool_input(value, depth: int = 0):
    if depth > 4:
        return "[截断]"
    if isinstance(value, dict):
        return {str(key): "[已脱敏]" if _SENSITIVE_TOOL_KEY.search(str(key)) else _safe_tool_input(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_tool_input(item, depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value[:300]
    return value


def _safe_tool_summary(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    try:
        import json

        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, (dict, list)):
        text = json.dumps(_safe_tool_input(parsed), ensure_ascii=False, separators=(",", ":"))
    text = __import__("re").sub(
        r"(?i)\b(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b([\s:=]+)([^\s,;]+)",
        r"\1\2[已脱敏]", text,
    )
    text = __import__("re").sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [已脱敏]", text)
    return text[:limit]


def _todo_task_id(todo: dict[str, Any], index: int) -> str:
    item_id = str(todo.get("id") or f"item-{index + 1}")
    return f"task_{item_id}"


def _activate_todo_task(state: dict[str, Any], scope: dict[str, Any]) -> None:
    todos = (state.get("task_details") or {}).get("_runtime", {}).get("todos") or []
    if not isinstance(todos, list) or not todos:
        return
    indexed = [(todo, _todo_task_id(todo, index)) for index, todo in enumerate(todos) if isinstance(todo, dict)]
    if not indexed:
        return
    active = next((row for row in indexed if row[0].get("status") == "in_progress"), None)
    active = active or next((row for row in indexed if row[0].get("status") == "pending"), None)
    if active is None:
        return
    task_id = active[1]
    state["tasks"] = list(dict.fromkeys([*(state.get("tasks") or []), *(row[1] for row in indexed)]))
    state["current_task"] = task_id
    scope["task_id"] = task_id
    details = dict(state.get("task_details") or {})
    for todo, item_task_id in indexed:
        details[item_task_id] = {
            **(details.get(item_task_id) or {}),
            "status": todo.get("status", "pending"),
            "content": str(todo.get("content") or "")[:500],
        }
    state["task_details"] = details


def _resolve_checkpointer():
    try:
        return get_checkpointer()
    except RuntimeError:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()


def _messages_for_ledger_persist(
    updates: dict, *, include_prompt_system_messages: bool = False
) -> list:
    from app.agent.messages import is_ephemeral_prompt_frame
    return [message for message in (updates.get("messages") or []) if not is_ephemeral_prompt_frame(message)]


@contextmanager
def _node_lifecycle(start_event: str, end_event: str, *, scope: dict, node: str, input_summary: str):
    started = time.time()
    dispatch_observe_nonblocking(
        start_event, scope=dict(scope), node=node, status="running",
        started_at=started, input_summary=input_summary[:500],
    )
    error = None
    try:
        yield started
    except BaseException as exc:
        error = exc
        raise
    finally:
        ended = time.time()
        status = (
            "interrupted" if isinstance(error, GraphInterrupt)
            else "cancelled" if isinstance(error, asyncio.CancelledError)
            else "failed" if error else "completed"
        )
        dispatch_observe_nonblocking(
            end_event, scope=dict(scope), node=node, status=status,
            started_at=started, ended_at=ended,
            duration_ms=max(0, int((ended - started) * 1000)),
            output_summary=f"{node} node completed" if error is None else f"{node} node {status}",
        )


def build_graph():
    tools = get_tools()
    builder = StateGraph(AgentState)
    conversation_files = SessionConversationFiles(get_settings().workspace_path)

    def persist_messages(
        state: AgentState, updates: dict, *, include_prompt_system_messages: bool = False
    ) -> int | None:
        messages = _messages_for_ledger_persist(
            updates, include_prompt_system_messages=include_prompt_system_messages
        )
        if not messages:
            return None
        scope = state.get("scope") or {}
        session_id = str(scope.get("session_id") or "")
        run_id = str(scope.get("run_id") or "")
        step_id = str((state.get("current_step") or {}).get("step_id") or "")
        if session_id and run_id:
            return conversation_files.append_messages(session_id, run_id, messages, scope=scope)
        return None

    def persist_summary(state: AgentState, updates: dict) -> str | None:
        count = int(updates.get("compression_count") or state.get("context", {}).get("compression_count", 0))
        previous_count = int(state.get("context", {}).get("compression_count", 0))
        summary = str(updates.get("context_summary") or "")
        if count <= previous_count or not summary:
            return None
        scope = state.get("scope") or {}
        summary_id = f"summary_{uuid.uuid4().hex}"
        return conversation_files.write_summary(
            str(scope.get("session_id") or ""), summary_id,
            {"summary_id": summary_id, "run_id": scope.get("run_id"), "content": summary},
        )

    def store_summary_reference(updates: dict, pointer: str | None) -> dict:
        result = dict(updates)
        if not pointer or not result.get("working_messages"):
            return result
        from langchain_core.messages import HumanMessage
        messages = []
        for message in result["working_messages"]:
            kwargs = getattr(message, "additional_kwargs", {}) or {}
            if kwargs.get("context_summary"):
                message = message.model_copy(update={"content": f"[summary reference: {pointer}]"})
            messages.append(message)
        result["working_messages"] = messages
        result["context_summary"] = ""
        return result

    def agent_node(state: AgentState, config):
        execution_state = copy.deepcopy(state)
        scope = dict(execution_state.get("scope") or {})
        _activate_todo_task(execution_state, scope)
        scope["step_id"] = f"step_{uuid.uuid4().hex}"
        scope["request_id"] = None
        execution_state["scope"] = scope
        execution_state["current_step"] = {
            **(execution_state.get("current_step") or {}),
            "step_id": scope["step_id"], "step_name": "agent", "step_status": "running",
            "started_at": time.time(), "tool_calls": [],
        }
        execution_state["run_status"] = "running"
        input_summary = f"working_messages={len(execution_state.get('context', {}).get('working_message') or [])}"
        runtime_view = to_runtime_view(execution_state)
        session_id = str(scope.get("session_id") or "")
        applied_seq = int((state.get("ledger_pointer") or {}).get("applied_seq", 0))
        tail = conversation_files.read_tail(
            session_id,
            after_seq=applied_seq,
            offset=(state.get("ledger_pointer") or {}).get("applied_offset"),
        )
        runtime_view["messages"] = list(runtime_view.get("working_messages") or [])
        if tail:
            from langchain_core.messages import messages_from_dict
            runtime_view["messages"].extend(messages_from_dict([row["message"] for row in tail]))
        with bind_model_request_scope(scope), _node_lifecycle(AGENT_NODE_START, AGENT_NODE_END, scope=scope, node="agent", input_summary=input_summary):
            updates = call_agent(runtime_view, config)
        scope["request_id"] = updates.get("request_id")
        applied_seq = persist_messages(execution_state, updates)
        summary_pointer = persist_summary(execution_state, updates)
        updates = store_summary_reference(
            updates, summary_pointer or execution_state.get("context", {}).get("summary_pointer")
        )
        result = merge_runtime_updates(execution_state, updates)
        if summary_pointer:
            result["context"]["summary_pointer"] = summary_pointer
        result["scope"] = scope
        memory = dict(result.get("memory") or {})
        recorded_count = int(memory.get("recorded_count") or 0)
        if recorded_count != int((state.get("memory") or {}).get("recorded_count") or 0):
            memory["last_synced_run"] = scope.get("run_id")
        bootstrap_dir = get_settings().workspace_path / "bootstraps"
        memory["source_files"] = [
            str(path.relative_to(get_settings().workspace_path)).replace("\\", "/")
            for path in (bootstrap_dir / "MEMORY.md", bootstrap_dir / "USER.md")
            if path.is_file()
        ]
        result["memory"] = memory
        last_ai = next((message for message in reversed(updates.get("messages") or []) if isinstance(message, AIMessage)), None)
        if last_ai and last_ai.tool_calls:
            existing = {str(call.get("call_id")): call for call in result["current_step"].get("tool_calls", [])}
            for call in last_ai.tool_calls:
                call_id = str(call.get("id") or "")
                if call_id:
                    existing.setdefault(call_id, {
                        "call_id": call_id, "tool_name": call.get("name"),
                        "input_summary": str(_safe_tool_input(call.get("args") or {}))[:500],
                        "attempt": 1, "status": "pending",
                    })
            result["current_step"]["tool_calls"] = list(existing.values())
        task_id = str(scope.get("task_id") or "")
        task_details = dict(result.get("task_details") or {})
        prior_task = dict(task_details.get(task_id) or {})
        task_details[task_id] = {
            **prior_task,
            "status": (
                "failed" if updates.get("terminal_reason") == "step_budget_exhausted" or (last_ai and last_ai.additional_kwargs.get("agent_error"))
                else "running" if last_ai and last_ai.tool_calls
                else "completed"
            ),
            "step_ids": list(dict.fromkeys([*(prior_task.get("step_ids") or []), scope["step_id"]])),
        }
        result["task_details"] = task_details
        if not (last_ai and last_ai.tool_calls):
            if updates.get("terminal_reason") == "handoff":
                result["run_status"] = "handoff"
            elif task_details[task_id]["status"] == "failed":
                result["run_status"] = "failed"
            else:
                result["run_status"] = "completed"
            result["terminal_reason"] = updates.get("terminal_reason") or result["run_status"]
        finished_at = time.time()
        result["current_step"] = {
            **execution_state["current_step"], **result.get("current_step", {}),
            "step_status": "failed" if updates.get("terminal_reason") else "completed",
            "ended_at": finished_at,
            "duration_ms": max(0, int((finished_at - execution_state["current_step"]["started_at"]) * 1000)),
            "token_info": {
                "input_tokens": (updates.get("context_usage") or {}).get("input_tokens"),
                "output_tokens": (updates.get("context_usage") or {}).get("output_tokens"),
                "total_tokens": (updates.get("context_usage") or {}).get("total_tokens"),
            },
        }
        result["state_revision"] = int(state.get("state_revision", 0)) + 1
        result["ledger_pointer"] = dict(execution_state.get("ledger_pointer") or {})
        if applied_seq is not None:
            result["ledger_pointer"]["applied_seq"] = applied_seq
            result["ledger_pointer"]["applied_offset"] = conversation_files.offset_after(session_id, applied_seq)
        result["context"]["_replace_working_message"] = True
        return result

    async def tools_node(state: AgentState, config):
        execution_state = copy.deepcopy(state)
        scope = dict(execution_state.get("scope") or {})
        _activate_todo_task(execution_state, scope)
        scope["step_id"] = f"step_{uuid.uuid4().hex}"
        scope["request_id"] = None
        execution_state["scope"] = scope
        execution_state["current_step"] = {
            **(execution_state.get("current_step") or {}),
            "step_id": scope["step_id"], "step_name": "tools", "step_status": "running",
            "started_at": time.time(), "tool_calls": [],
        }
        runtime_view = to_runtime_view(execution_state)
        session_id = str(scope.get("session_id") or "")
        applied_seq = int((state.get("ledger_pointer") or {}).get("applied_seq", 0))
        runtime_view["messages"] = list(runtime_view.get("working_messages") or [])
        latest_ai = pending_tool_message(state)
        current_message = runtime_view["messages"][-1] if runtime_view["messages"] else None
        if latest_ai is None or not isinstance(current_message, AIMessage) or [
            call.get("id") for call in latest_ai.tool_calls
        ] != [call.get("id") for call in current_message.tool_calls]:
            raise RuntimeError("tool calls do not match checkpoint state")
        execution_state["current_step"]["tool_calls"] = [
            {
                "call_id": str(call.get("id")), "tool_name": call.get("name"),
                "input_summary": str(_safe_tool_input(call.get("args") or {}))[:500], "attempt": 1, "status": "running",
            }
            for call in (latest_ai.tool_calls if latest_ai else []) if call.get("id")
        ]
        input_summary = f"tool_calls={len(execution_state['current_step']['tool_calls'])}"
        with _node_lifecycle(TOOL_NODE_START, TOOL_NODE_END, scope=scope, node="tools", input_summary=input_summary):
            updates = await call_tools(runtime_view, config)
        applied_seq = persist_messages(execution_state, updates)
        summary_pointer = persist_summary(execution_state, updates)
        updates = store_summary_reference(
            updates, summary_pointer or execution_state.get("context", {}).get("summary_pointer")
        )
        result = merge_runtime_updates(execution_state, updates)
        if summary_pointer:
            result["context"]["summary_pointer"] = summary_pointer
        result["scope"] = scope
        if isinstance(updates.get("todos"), list):
            temp = {"task_details": {"_runtime": {"todos": updates["todos"]}}, "tasks": result.get("tasks", [])}
            _activate_todo_task(temp, scope)
            result["tasks"] = temp["tasks"]
            if "current_task" in temp:
                result["current_task"] = temp["current_task"]
            result["task_details"] = {**(result.get("task_details") or {}), **{key: value for key, value in temp["task_details"].items() if key != "_runtime"}, "_runtime": {**((result.get("task_details") or {}).get("_runtime") or {}), "todos": updates["todos"]}}
        task_id = str(scope.get("task_id") or "")
        task_details = dict(result.get("task_details") or {})
        prior_task = dict(task_details.get(task_id) or {})
        task_details[task_id] = {
            **prior_task,
            "status": "running",
            "step_ids": list(dict.fromkeys([*(prior_task.get("step_ids") or []), scope["step_id"]])),
        }
        result["task_details"] = task_details
        calls = list(execution_state["current_step"].get("tool_calls") or [])
        call_positions: dict[str, int] = {
            str(call["call_id"]): index
            for index, call in enumerate(calls)
            if call.get("call_id")
        }
        for message in updates.get("messages") or []:
            if isinstance(message, AIMessage):
                for call in message.tool_calls or []:
                    call_id = str(call.get("id") or "")
                    if not call_id:
                        continue
                    row = {"call_id": call_id, "tool_name": call.get("name"), "input_summary": str(_safe_tool_input(call.get("args") or {}))[:500], "attempt": 1, "status": "running"}
                    if call_id in call_positions:
                        calls[call_positions[call_id]].update(row)
                    else:
                        call_positions[call_id] = len(calls)
                        calls.append(row)
            elif isinstance(message, ToolMessage) and message.tool_call_id:
                call_id = str(message.tool_call_id)
                kwargs = message.additional_kwargs or {}
                row = {
                    "call_id": call_id,
                    "status": (
                        "success" if kwargs.get("tool_status") == "completed"
                        else kwargs.get("tool_status") or ("failed" if kwargs.get("reason_code") else "success")
                    ),
                    "attempt": int(kwargs.get("attempt") or 1),
                    "output_summary": _safe_tool_summary(message.content),
                    "output_pointer": kwargs.get("tool_cache_path"),
                    "is_summary_saved": bool(kwargs.get("tool_cache_path")),
                }
                if call_id in call_positions:
                    calls[call_positions[call_id]].update(row)
                else:
                    call_positions[call_id] = len(calls)
                    calls.append(row)
        finished_at = time.time()
        result["current_step"] = {
            **execution_state["current_step"], **result.get("current_step", {}),
            "tool_calls": calls,
            "step_status": (
                "failed" if any(call.get("status") == "failed" for call in calls)
                else "cancelled" if any(call.get("status") == "cancelled" for call in calls)
                else "completed"
            ),
            "ended_at": finished_at,
            "duration_ms": max(0, int((finished_at - execution_state["current_step"]["started_at"]) * 1000)),
        }
        result["state_revision"] = int(state.get("state_revision", 0)) + 1
        result["ledger_pointer"] = dict(execution_state.get("ledger_pointer") or {})
        if applied_seq is not None:
            result["ledger_pointer"]["applied_seq"] = applied_seq
            result["ledger_pointer"]["applied_offset"] = conversation_files.offset_after(session_id, applied_seq)
        result["context"]["_replace_working_message"] = True
        return result

    builder.add_node("agent", agent_node)
    builder.add_edge(START, "agent")
    if tools:
        builder.add_node("tools", tools_node)

        def after_tools(state: AgentState) -> str:
            terminal_reason = state.get("terminal_reason")
            if terminal_reason == "handoff":
                return END
            return "agent"

        builder.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
        builder.add_conditional_edges("tools", after_tools, {"agent": "agent", END: END})
    else:
        builder.add_edge("agent", END)
    return builder.compile(checkpointer=_resolve_checkpointer())


@lru_cache
def get_graph():
    return build_graph()


def make_thread_config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


def make_initial_state(
    message: str, max_steps: int, *, session_id: str = "", workspace_id: str = "", run_id: str | None = None
) -> dict:
    message_obj = HumanMessage(content=message, additional_kwargs={"timestamp": int(time.time())})
    return new_agent_state(
        workspace_id=workspace_id or "default",
        session_id=session_id or f"session_{uuid.uuid4().hex}",
        message=message_obj,
        max_steps=max_steps,
        run_id=run_id,
    )
