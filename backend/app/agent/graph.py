from functools import lru_cache
import asyncio
import copy
import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.errors import GraphInterrupt

from app.agent.nodes.agent import call_agent
from app.agent.nodes.router import pending_tool_message, should_continue
from app.agent.nodes.tools import call_tools
from app.agent.hooks import AGENT_NODE_END, AGENT_NODE_START, TOOL_NODE_END, TOOL_NODE_START, dispatch_observe_nonblocking
from app.agent.state import AgentState, merge_runtime_updates, to_runtime_view, new_agent_state, utc_now, TERMINAL_TURN_STATUSES
from app.agent.audit import RuntimeLedgerAudit, bind_runtime_audit
from app.storage.conversation_files import SessionConversationFiles
from app.storage.model_window import ModelWindowStore
from app.core.settings import get_settings
from app.core.model_request import bind_model_request_scope
from app.storage.checkpoint import get_checkpointer
from app.tools.registry import get_tools

_SENSITIVE_TOOL_KEY = re.compile(r"token|secret|password|authorization|cookie|api[_-]?key|private[_-]?key|env(?:ironment)?", re.I)


def _safe_tool_input(value, depth: int = 0):
    if depth > 4:
        return "[截断]"
    if isinstance(value, dict):
        return {str(key): "[已脱敏]" if _SENSITIVE_TOOL_KEY.search(str(key)) else _safe_tool_input(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_tool_input(item, depth + 1) for item in value[:20]]
    return value[:300] if isinstance(value, str) else value


def _safe_tool_summary(value: Any, limit: int = 500) -> str:
    import json
    text = str(value or "")
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, (dict, list)):
        text = json.dumps(_safe_tool_input(parsed), ensure_ascii=False, separators=(",", ":"))
    text = re.sub(r"(?i)\b(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b([\s:=]+)([^\s,;]+)", r"\1\2[已脱敏]", text)
    return re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [已脱敏]", text)[:limit]


def _todo_task_id(todo: dict[str, Any], index: int) -> str:
    return f"task_{todo.get('id') or f'item-{index + 1}'}"


def _activate_todo_task(state: dict[str, Any], scope: dict[str, Any] | None = None) -> None:
    todos = (state.get("task_details") or {}).get("_runtime", {}).get("todos") or []
    if not isinstance(todos, list) or not todos:
        return
    indexed = [(todo, _todo_task_id(todo, index)) for index, todo in enumerate(todos) if isinstance(todo, dict)]
    state["tasks"] = list(dict.fromkeys([*(state.get("tasks") or []), *(row[1] for row in indexed)]))
    active = next((row for row in indexed if row[0].get("status") == "in_progress"), None)
    active = active or next((row for row in indexed if row[0].get("status") == "pending"), None)
    state["current_task"] = active[1] if active else None
    details = dict(state.get("task_details") or {})
    for todo, task_id in indexed:
        details[task_id] = {**(details.get(task_id) or {}), "status": todo.get("status", "pending"), "content": str(todo.get("content") or "")[:500]}
    state["task_details"] = details


def _audit_task_changes(audit, previous, result):
    for task_id in result.get("tasks") or []:
        before = (previous.get("task_details") or {}).get(task_id)
        after = (result.get("task_details") or {}).get(task_id) or {}
        if before != after:
            status = "running" if after.get("status") == "in_progress" else after.get("status", "pending")
            runtime = {"task_id": task_id}
            if status == "handoff":
                status = "completed"
                runtime["reason"] = "handoff"
            audit.lifecycle("task", status, extensions={"runtime": runtime}, detail="created" if before is None else "updated")
    if previous.get("current_task") != result.get("current_task"):
        audit.lifecycle("task", "running" if result.get("current_task") else "completed", extensions={"runtime": {"task_id": result.get("current_task")}}, detail="selected")


def _resolve_checkpointer():
    try:
        return get_checkpointer()
    except RuntimeError:
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()


def _messages_for_ledger_persist(updates: dict, *, include_prompt_system_messages: bool = False) -> list:
    from app.agent.messages import is_ephemeral_prompt_frame
    return [message for message in updates.get("messages") or [] if not is_ephemeral_prompt_frame(message)]


@contextmanager
def _node_lifecycle(start_event, end_event, *, scope, node, input_summary):
    started = utc_now()
    dispatch_observe_nonblocking(start_event, scope=dict(scope), node=node, status="running", started_at=started, input_summary=input_summary[:500])
    error = None
    try:
        yield started
    except BaseException as exc:
        error = exc
        raise
    finally:
        ended = utc_now()
        status = "interrupted" if isinstance(error, GraphInterrupt) else "cancelled" if isinstance(error, asyncio.CancelledError) else "failed" if error else "completed"
        dispatch_observe_nonblocking(end_event, scope=dict(scope), node=node, status=status, started_at=started, ended_at=ended, duration_ms=max(0, ended - started), output_summary=f"{node} node {status}")


def build_graph():
    tools = get_tools()
    builder = StateGraph(AgentState)
    files = SessionConversationFiles(get_settings().workspace_path)

    def start_node(state, name):
        value = copy.deepcopy(state)
        _activate_todo_task(value)
        resuming = name == "tools" and value.get("current_step", {}).get("step_status") == "waiting_user"
        step_id = value["current_step"].get("step_id") if resuming else f"step_{uuid.uuid4().hex}"
        started = value["current_step"].get("started_at", utc_now()) if resuming else utc_now()
        value["current_step"] = {"step_id": step_id, "step_name": name, "step_status": "running", "started_at": started, "tool_calls": []}
        value["turn_status"] = "running"
        value["state_revision"] = int(state.get("state_revision", 0)) + 1
        value["checkpoint_uid"] = f"cp_{uuid.uuid4().hex}"
        value.pop("checkpoint_id", None)
        branch_id = value.get("ledger_pointer", {}).get("event_cursor", {}).get("branch_id", "main")
        audit = RuntimeLedgerAudit(files, value["scope"], step_id=step_id, task_id=value.get("current_task"), branch_id=branch_id)
        if not resuming:
            audit.node("running", name=name)
        return value, audit

    def finish_node(previous, value, updates, audit, name):
        session_id = value["scope"]["session_id"]
        branch_id = value.get("ledger_pointer", {}).get("event_cursor", {}).get("branch_id", "main")
        updates = dict(updates)
        updates["messages"] = [audit.settle_tool(message) if isinstance(message, ToolMessage) else message for message in updates.get("messages") or []]
        messages = _messages_for_ledger_persist(updates)
        if messages:
            # Tools already have their one immutable settlement row.
            files.append_messages(session_id, value["scope"]["turn_id"], [message for message in messages if not isinstance(message, ToolMessage)], scope=value["scope"], branch_id=branch_id)
        result = merge_runtime_updates(value, updates)
        count = int(updates.get("compression_count") or 0)
        summary = str(updates.get("context_summary") or "")
        if count > int(previous.get("context", {}).get("compression_count", 0)) and summary:
            summary_id = f"summary_{uuid.uuid4().hex}"
            pointer = files.write_summary(session_id, summary_id, {"summary_id": summary_id, "turn_id": value["scope"]["turn_id"], "content": summary})
            result["context"]["summary_pointer"] = pointer
        pointer = result["context"].get("summary_pointer")
        if pointer:
            for index, message in enumerate(result["context"].get("working_message") or []):
                if (message.additional_kwargs or {}).get("context_summary"):
                    result["context"]["working_message"][index] = message.model_copy(update={"content": f"[summary reference: {pointer}]"})
        rows = files.read_tail(session_id, after_seq=int(previous.get("ledger_pointer", {}).get("applied_seq", 0)), branch_id=branch_id)
        from app.agent.recovery import bounded_model_message
        by_identity = {row["message_id"]: row for row in rows}
        for index, message in enumerate(result["context"]["working_message"]):
            row = by_identity.get((message.additional_kwargs or {}).get("ledger_message_id"))
            if row:
                result["context"]["working_message"][index] = bounded_model_message(message, row)
        if messages or updates.get("working_messages") is not None or summary:
            from app.storage.message_codec import encode_message
            window_messages = result["context"].get("working_message") or []
            encoded = [encode_message(message) for message in window_messages]
            references = [block for message in encoded for block in (message.get("message", {}).get("content") or [])
                          if isinstance(block, dict) and block.get("type") == "reference"]
            reference_bytes = json.dumps(references, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            source_events = files.visible_events(session_id, branch_id=branch_id)
            source_event = next((event for event in reversed(source_events) if event["event_type"] in {
                "message.accepted", "model.response.completed", "tool.attempt.settled",
            }), None)
            source_cursor = {"branch_id": branch_id, "event_seq": source_event["event_seq"] if source_event else 0,
                             "event_id": source_event["event_id"] if source_event else "origin"}
            bootstrap_fingerprint = result["context"].get("bootstrap_fingerprint") or hashlib.sha256(b"").hexdigest()
            reference_fingerprint = hashlib.sha256(reference_bytes).hexdigest()
            window = ModelWindowStore(files).save(
                session_id, window_messages, source_event_cursor=source_cursor,
                window_schema_version="working-window-v1", compression_version="four-layer-v1",
                bootstrap_fingerprint=bootstrap_fingerprint, reference_fingerprint=reference_fingerprint,
            )
            result["context"]["window_pointer"] = window.reference
            audit.lifecycle("context", "completed", extensions={"context": {
                "summary_ref": pointer, "covered_seq": [1, source_cursor["event_seq"]],
                "compression_count": result["context"].get("compression_count", 0),
                "window_schema_version": "working-window-v1", "compression_version": "four-layer-v1",
                "window_ref": window.reference, "content_sha256": window.content_sha256, "source_event_cursor": source_cursor,
                "bootstrap_fingerprint": bootstrap_fingerprint, "reference_fingerprint": reference_fingerprint,
            }})
        definitions = {}
        outcomes = {}
        attempts = {}
        for row in rows:
            for call in row["message"].get("tool_calls") or []:
                definitions[call["id"]] = row["seq"]
            call_id = row["message"].get("tool_call_id")
            if call_id and (row.get("execution") or {}).get("attempt"):
                attempts[call_id] = row["execution"]["attempt"]
            if row["message"]["role"] == "tool":
                outcomes[call_id] = row
        last_ai = next((m for m in reversed(updates.get("messages") or []) if isinstance(m, AIMessage)), None)
        pending = list(last_ai.tool_calls) if last_ai and last_ai.tool_calls else []
        calls = pending if name == "agent" else (pending_tool_message(previous).tool_calls if pending_tool_message(previous) else [])
        prior_calls = {call["call_id"]: call for call in previous.get("current_step", {}).get("tool_calls") or []}
        current_calls = []
        for call in calls:
            call_id = str(call["id"])
            definition_seq = definitions.get(call_id) or prior_calls.get(call_id, {}).get("definition_seq")
            if not definition_seq:
                raise RuntimeError(f"tool definition unavailable: {call_id}")
            current = {"call_id": call_id, "definition_seq": definition_seq, "status": "pending"}
            if call_id in attempts:
                current["attempt"] = attempts[call_id]
            if call_id in outcomes:
                outcome = outcomes[call_id]
                current.update({"result_seq": outcome["seq"], "status": outcome["execution"]["status"], "output_summary": _safe_tool_summary(outcome["message"]["content"])})
            current_calls.append(current)
        ended = utc_now()
        step_status = "failed" if updates.get("terminal_reason") == "step_budget_exhausted" or (last_ai and last_ai.additional_kwargs.get("agent_error")) else "completed"
        if any(call["status"] in {"failed", "unknown"} for call in current_calls):
            step_status = "failed"
        result["current_step"] = {**value["current_step"], "tool_calls": current_calls, "step_status": step_status, "ended_at": ended, "duration_ms": max(0, ended - value["current_step"]["started_at"])}
        if name == "agent" and (updates.get("request_id") or audit.last_request_id):
            result["current_step"]["model"] = {"request_id": updates.get("request_id") or audit.last_request_id}
            if last_ai and last_ai.usage_metadata:
                result["current_step"]["model"]["usage"] = {key: item for key, item in last_ai.usage_metadata.items() if key != "total_tokens"}
        if updates.get("todos") is not None:
            result.setdefault("task_details", {}).setdefault("_runtime", {})["todos"] = updates["todos"]
            _activate_todo_task(result)
        task_id = result.get("current_task")
        if task_id:
            result["task_details"][task_id]["status"] = "running" if pending or name == "tools" else step_status
        terminal = "handoff" if updates.get("terminal_reason") == "handoff" else "failed" if any(c["status"] == "unknown" for c in current_calls) else "failed" if name == "agent" and step_status == "failed" else "completed" if name == "agent" and not pending else None
        if terminal:
            result["turn_status"] = terminal
            result["terminal_reason"] = "execution_outcome_unknown" if any(c["status"] == "unknown" for c in current_calls) else updates.get("terminal_reason") or terminal
            for explicit_task in result.get("tasks") or []:
                detail = result["task_details"].get(explicit_task) or {}
                if detail.get("status") not in TERMINAL_TURN_STATUSES:
                    detail["status"] = terminal
                    result["task_details"][explicit_task] = detail
        else:
            result["turn_status"] = "running"
        _audit_task_changes(audit, previous, result)
        audit.node(step_status, name=name)
        if terminal:
            audit.turn_terminal(terminal, result["terminal_reason"])
        if int(result.get("memory", {}).get("recorded_count", 0)) != int(previous.get("memory", {}).get("recorded_count", 0)):
            result["memory"]["last_synced_turn"] = value["scope"]["turn_id"]
        bootstraps = get_settings().workspace_path / "bootstraps"
        result["memory"]["source_files"] = [str(path.relative_to(get_settings().workspace_path)).replace("\\", "/") for path in (bootstraps / "MEMORY.md", bootstraps / "USER.md") if path.is_file()]
        if result["memory"].get("last_synced_turn") and result["memory"] != previous.get("memory"):
            files.append_event(session_id, "memory.sync.recorded", result["memory"],
                               turn_id=value["scope"]["turn_id"], branch_id=branch_id)
        result["ledger_pointer"] = files.ledger_pointer(session_id, branch_id=branch_id)
        result["state_revision"] = int(previous.get("state_revision", 0)) + 2
        result["checkpoint_uid"] = f"cp_{uuid.uuid4().hex}"
        result.pop("checkpoint_id", None)
        result["context"]["_replace_working_message"] = True
        result["pending_interaction"] = None
        return result

    def agent_node(state: AgentState, config):
        if state.get("turn_status") in TERMINAL_TURN_STATUSES:
            return state
        value, audit = start_node(state, "agent")
        from langchain_core.callbacks.manager import dispatch_custom_event
        dispatch_custom_event("runtime_node_start", {"node": "agent", "state": value}, config=config)
        runtime = to_runtime_view(value)
        with bind_model_request_scope(value["scope"]), bind_runtime_audit(audit), _node_lifecycle(AGENT_NODE_START, AGENT_NODE_END, scope=value["scope"], node="agent", input_summary=f"working_messages={len(runtime['messages'])}"):
            try:
                updates = call_agent(runtime, config)
            except BaseException:
                audit.node("failed", name="agent")
                raise
        return finish_node(state, value, updates, audit, "agent")

    async def tools_node(state: AgentState, config):
        value, audit = start_node(state, "tools")
        from langchain_core.callbacks.manager import adispatch_custom_event
        await adispatch_custom_event("runtime_node_start", {"node": "tools", "state": value}, config=config)
        runtime = to_runtime_view(value)
        latest_ai = pending_tool_message(state)
        if latest_ai is None:
            raise RuntimeError("tool calls do not match checkpoint state")
        # A persisted interaction reply follows its assistant call definition.
        # The tool adapter receives the unresolved definition as its last frame.
        last_identity = latest_ai.additional_kwargs.get("ledger_message_id")
        runtime["messages"] = [message for message in runtime["messages"] if not (last_identity and message.additional_kwargs.get("ledger_message_id") == last_identity)] + [latest_ai]
        with bind_runtime_audit(audit), _node_lifecycle(TOOL_NODE_START, TOOL_NODE_END, scope=value["scope"], node="tools", input_summary=f"tool_calls={len(latest_ai.tool_calls)}"):
            try:
                updates = await call_tools(runtime, config)
            except BaseException as exc:
                audit.node("waiting_user" if isinstance(exc, GraphInterrupt) else "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", name="tools")
                raise
        return finish_node(state, value, updates, audit, "tools")

    builder.add_node("agent", agent_node)
    builder.add_edge(START, "agent")
    if tools:
        builder.add_node("tools", tools_node)
        builder.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
        builder.add_conditional_edges("tools", lambda state: END if state.get("turn_status") in TERMINAL_TURN_STATUSES else "agent", {"agent": "agent", END: END})
    else:
        builder.add_edge("agent", END)
    from app.agent.checkpoint import RuntimeCheckpointSaver
    return builder.compile(checkpointer=RuntimeCheckpointSaver(_resolve_checkpointer(), files))


@lru_cache
def get_graph():
    return build_graph()


def make_thread_config(session_id: str, branch_id: str = "main") -> dict:
    if not isinstance(branch_id, str) or not branch_id:
        raise ValueError("branch_id must be a non-empty string")
    thread_id = session_id if branch_id == "main" else (
        session_id + ":branch:" + hashlib.sha256(branch_id.encode("utf-8")).hexdigest()
    )
    return {"configurable": {"thread_id": thread_id}}


def make_initial_state(message: str, max_steps: int, *, session_id: str = "", workspace_id: str = "", turn_id: str | None = None) -> dict:
    return new_agent_state(workspace_id=workspace_id or str(get_settings().workspace_path), session_id=session_id or f"session_{uuid.uuid4().hex}", message=HumanMessage(content=message, additional_kwargs={"timestamp": utc_now()}), max_steps=max_steps, turn_id=turn_id)
