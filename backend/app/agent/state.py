"""Canonical, checkpointable agent state and field-aware reducer."""
from __future__ import annotations

import copy
import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "handoff"}
REQUIRED_TOP_LEVEL = {
    "checkpoint_id", "scope", "context", "max_step_every_run", "tasks",
    "current_task", "current_step", "memory",
}
REQUIRED_TOP_LEVEL.add("state_revision")


def _merge_branch(current: dict[str, Any] | None, patch: dict[str, Any] | None) -> dict[str, Any]:
    return _merge_mapping(current or {}, patch or {})


def _merge_scope(current: dict[str, str | None] | None, patch: dict[str, str | None] | None) -> dict[str, str | None]:
    """A null identity clears the prior request/step instead of retaining it."""
    return {**(current or {}), **(patch or {})}


def _merge_context(current: dict[str, Any] | None, patch: dict[str, Any] | None) -> dict[str, Any]:
    result = _merge_mapping(current or {}, patch or {})
    if patch and "working_message" in patch:
        if patch.get("_replace_working_message"):
            result["working_message"] = copy.deepcopy(patch["working_message"] or [])
        else:
            result["working_message"] = add_messages(
                (current or {}).get("working_message", []), patch["working_message"] or []
            )
        result.pop("_replace_working_message", None)
    return result


def _merge_tasks(current: list[str] | None, patch: list[str] | None) -> list[str]:
    return list(dict.fromkeys([*(current or []), *(patch or [])]))


def _merge_step(current: dict[str, Any] | None, patch: dict[str, Any] | None) -> dict[str, Any]:
    if current and patch and current.get("step_id") and patch.get("step_id") != current.get("step_id"):
        return copy.deepcopy(patch)
    result = _merge_mapping(current or {}, patch or {})
    if patch and "tool_calls" in patch:
        result["tool_calls"] = _merge_by_id(
            (current or {}).get("tool_calls", []), patch["tool_calls"] or [], "call_id"
        )
    return result


def _merge_task_details(current: dict[str, Any] | None, patch: dict[str, Any] | None) -> dict[str, Any]:
    patch = patch or {}
    if patch and set(patch) != {"_runtime"}:
        return copy.deepcopy(patch)
    return _merge_mapping(current or {}, patch)


def _merge_terminal(current: str | None, patch: str | None) -> str | None:
    if current in TERMINAL_RUN_STATUSES and patch not in {current, "created"}:
        return current
    return patch or current


class AgentState(TypedDict, total=False):
    # LangGraph reserves checkpoint_id as its own runtime metadata channel.
    checkpoint_uid: str
    state_revision: int
    scope: Annotated[dict[str, str | None], _merge_scope]
    context: Annotated[dict[str, Any], _merge_context]
    max_step_every_run: int
    tasks: Annotated[list[str], _merge_tasks]
    current_task: str | None
    current_step: Annotated[dict[str, Any], _merge_step]
    memory: Annotated[dict[str, Any], _merge_branch]
    run_status: Annotated[str, _merge_terminal]
    terminal_reason: str | None
    task_details: Annotated[dict[str, Any], _merge_task_details]
    ledger_pointer: Annotated[dict[str, Any], _merge_branch]
    pending_interaction: dict[str, Any] | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _checkpoint_id(scope: dict[str, str | None]) -> str:
    identity = "_".join(str(scope.get(key) or "na") for key in (
        "workspace_id", "session_id", "run_id", "task_id", "request_id", "step_id"
    ))
    return f"cp_{identity}_{uuid.uuid4().hex}"


def make_scope(
    *, workspace_id: str, session_id: str, run_id: str | None = None,
    task_id: str | None = None, request_id: str | None = None,
    step_id: str | None = None,
) -> dict[str, str | None]:
    stable_workspace_id = "ws_" + hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:20]
    return {
        "workspace_id": stable_workspace_id,
        "session_id": session_id,
        "run_id": run_id or f"run_{uuid.uuid4().hex}",
        "task_id": task_id,
        "request_id": request_id,
        "step_id": step_id,
    }


def new_agent_state(
    *, workspace_id: str, session_id: str, message: BaseMessage,
    max_steps: int = 48, run_id: str | None = None,
) -> AgentState:
    task_id = f"task_{uuid.uuid4().hex}"
    scope = make_scope(workspace_id=workspace_id, session_id=session_id, run_id=run_id, task_id=task_id)
    return {
        "checkpoint_id": _checkpoint_id(scope),
        "state_revision": 0,
        "scope": scope,
        "context": {
            "working_message": [message], "window_size_tokens": 256_000,
            "token_budget": 20_000, "used_tokens": 0, "usage_ratio": 0.0,
            "summary_pointer": None, "compression_count": 0, "last_built_at": utc_now(),
            "bootstrap_fingerprint": "",
        },
        "max_step_every_run": int(max_steps),
        "tasks": [task_id],
        "current_task": task_id,
        "current_step": {
            "step_id": None, "step_name": "agent", "step_status": "pending",
            "started_at": None, "ended_at": None, "duration_ms": None,
            "tool_calls": [], "token_info": {},
        },
        "memory": {"recorded_count": 0, "last_synced_run": None, "source_files": []},
        "run_status": "created", "terminal_reason": None,
        "task_details": {task_id: {"status": "pending", "step_ids": []}},
        "ledger_pointer": {"file": "ledger.jsonl", "applied_seq": 0},
        "pending_interaction": None,
    }


def to_graph_state(state: dict[str, Any]) -> dict[str, Any]:
    """Map the public checkpoint_id around LangGraph's reserved channel name."""
    result = copy.deepcopy(state)
    if "checkpoint_id" in result:
        result["checkpoint_uid"] = result.pop("checkpoint_id")
    result.setdefault("context", {})["_replace_working_message"] = True
    return result


def validate_agent_state(state: dict[str, Any]) -> None:
    validation_keys = set(state)
    if "checkpoint_uid" in state and "checkpoint_id" not in validation_keys:
        validation_keys.add("checkpoint_id")
    missing = REQUIRED_TOP_LEVEL - validation_keys
    if missing:
        raise ValueError(f"canonical agent state missing keys: {', '.join(sorted(missing))}")
    checkpoint_value = state.get("checkpoint_id", state.get("checkpoint_uid"))
    if not isinstance(checkpoint_value, str) or not checkpoint_value:
        raise ValueError("checkpoint_id must be a non-empty string")
    from app.storage.ledger_contract import validate_scope
    try:
        validate_scope(state["scope"])
    except ValueError as exc:
        raise ValueError(f"invalid canonical scope: {exc}") from exc
    if not isinstance(state["context"], dict) or not isinstance(state["context"].get("working_message"), list):
        raise ValueError("context.working_message must be an array")
    if not isinstance(state["tasks"], list) or not all(isinstance(v, str) for v in state["tasks"]):
        raise ValueError("tasks must be an array of task ids")
    step = state["current_step"]
    if not isinstance(step, dict) or not isinstance(step.get("tool_calls"), list):
        raise ValueError("current_step must include tool_calls")
    # Explicitly fail closed for the removed checkpoint shape.
    if {"meta", "session", "data", "task"}.issubset(state) and not REQUIRED_TOP_LEVEL.issubset(state):
        raise ValueError("unsupported legacy checkpoint state")


def public_state_projection(state: dict[str, Any]) -> AgentState:
    """Bound sensitive state before exposing a resumable snapshot to a client."""
    result = copy.deepcopy(state)
    if "checkpoint_id" not in result:
        result["checkpoint_id"] = result.pop("checkpoint_uid", _checkpoint_id(result.get("scope") or {}))
    else:
        result.pop("checkpoint_uid", None)
    context = result.get("context") or {}
    context.pop("_replace_working_message", None)
    messages = context.get("working_message") or []
    context["working_message"] = [
        {"type": getattr(message, "type", "message"), "id": getattr(message, "id", None)}
        for message in messages
    ]
    result["context"] = context
    step = result.get("current_step") or {}
    safe_calls = []
    for call in step.get("tool_calls", []):
        safe = {key: value for key, value in call.items() if key not in {"input", "args", "output"}}
        if "input_summary" in call:
            safe["input_summary"] = str(call["input_summary"])[:500]
        if "output_summary" in call:
            summary = str(call["output_summary"])[:500]
            summary = re.sub(
                r"(?i)\b(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b([\s:=]+)([^\s,;]+)",
                r"\1\2[已脱敏]", summary,
            )
            summary = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [已脱敏]", summary)
            safe["output_summary"] = summary
        safe_calls.append(safe)
    step["tool_calls"] = safe_calls
    result["current_step"] = step
    return result  # type: ignore[return-value]


def _merge_mapping(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(current)
    for key, value in patch.items():
        if value is None:
            result[key] = None
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_mapping(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _merge_by_id(current: list[dict[str, Any]], patch: list[dict[str, Any]], id_key: str) -> list[dict[str, Any]]:
    result = copy.deepcopy(current)
    positions = {str(row.get(id_key)): i for i, row in enumerate(result) if row.get(id_key) is not None}
    for row in patch:
        row_id = row.get(id_key)
        if row_id is not None and str(row_id) in positions:
            index = positions[str(row_id)]
            result[index] = _merge_mapping(result[index], row)
        else:
            result.append(copy.deepcopy(row))
            if row_id is not None:
                positions[str(row_id)] = len(result) - 1
    return result


def merge_agent_state(current: dict[str, Any], patch: dict[str, Any]) -> AgentState:
    """Apply a same-scope semantic patch; stale revisions and terminal regressions are ignored."""
    current_scope = current.get("scope") or {}
    patch_scope = patch.get("scope") or {}
    if any(patch_scope.get(key, current_scope.get(key)) != current_scope.get(key) for key in ("workspace_id", "session_id", "run_id")):
        return copy.deepcopy(current)  # type: ignore[return-value]
    if int(patch.get("state_revision", current.get("state_revision", 0))) <= int(current.get("state_revision", 0)):
        return copy.deepcopy(current)  # type: ignore[return-value]
    result = copy.deepcopy(current)
    for key in ("checkpoint_uid", "checkpoint_id", "state_revision", "scope", "max_step_every_run", "current_task", "run_status", "terminal_reason", "pending_interaction", "ledger_pointer"):
        if key in patch and patch[key] is not None:
            result[key] = copy.deepcopy(patch[key])
    if patch_scope:
        result["scope"] = _merge_scope(current_scope, patch_scope)
    if "pending_interaction" in patch and patch["pending_interaction"] is None:
        result["pending_interaction"] = None
    if current.get("run_status") in TERMINAL_RUN_STATUSES:
        result["run_status"] = current["run_status"]
        result["terminal_reason"] = current.get("terminal_reason")
    if "context" in patch:
        result["context"] = _merge_mapping(result.get("context", {}), patch["context"] or {})
        if "working_message" in (patch["context"] or {}):
            if patch["context"].get("_replace_working_message"):
                result["context"]["working_message"] = copy.deepcopy(patch["context"]["working_message"])
            else:
                result["context"]["working_message"] = add_messages(
                    current.get("context", {}).get("working_message", []), patch["context"]["working_message"]
                )
            result["context"].pop("_replace_working_message", None)
    if "memory" in patch:
        result["memory"] = _merge_mapping(result.get("memory", {}), patch["memory"] or {})
        if "source_files" in (patch["memory"] or {}):
            result["memory"]["source_files"] = list(dict.fromkeys(result["memory"].get("source_files", [])))
    if "tasks" in patch:
        result["tasks"] = list(dict.fromkeys([*result.get("tasks", []), *patch["tasks"]]))
    if "task_details" in patch:
        result["task_details"] = _merge_mapping(result.get("task_details", {}), patch["task_details"] or {})
    if "current_step" in patch:
        step = _merge_mapping(result.get("current_step", {}), patch["current_step"] or {})
        if "tool_calls" in (patch["current_step"] or {}):
            step["tool_calls"] = _merge_by_id(
                result.get("current_step", {}).get("tool_calls", []),
                patch["current_step"]["tool_calls"], "call_id",
            )
        result["current_step"] = step
    result["checkpoint_uid"] = patch.get("checkpoint_uid", patch.get("checkpoint_id")) or _checkpoint_id(result.get("scope") or {})
    result.pop("checkpoint_id", None)
    return result  # type: ignore[return-value]


def to_runtime_view(state: dict[str, Any]) -> dict[str, Any]:
    """Short-lived adapter for existing context and tool code; never checkpointed."""
    context = state.get("context", {})
    summary = ""
    pointer = context.get("summary_pointer")
    if pointer:
        from app.core.settings import get_settings
        from app.storage.conversation_files import SessionConversationFiles

        summary = SessionConversationFiles(get_settings().workspace_path).read_summary(
            str((state.get("scope") or {}).get("session_id") or ""), str(pointer)
        )["content"]
    memory = state.get("memory", {})
    return {
        "messages": copy.deepcopy(state.get("context", {}).get("working_message", [])),
        "working_messages": copy.deepcopy(state.get("context", {}).get("working_message", [])),
        "step": int(state.get("task_details", {}).get("_runtime", {}).get("step", 0)),
        "max_steps": int(state.get("max_step_every_run", 48)),
        "context_summary": summary, "compression_count": int(context.get("compression_count", 0)),
        "memory_turn_counter": int(memory.get("recorded_count", 0)),
        "memory_nudge_pending": bool(memory.get("nudge_pending")),
        "context_usage": copy.deepcopy(context.get("usage") or {}),
        "api_usage": copy.deepcopy(memory.get("api_usage") or {}),
        "session_token_stats": copy.deepcopy(memory.get("token_stats") or {}),
        "last_invoke_ledger_len": int((state.get("ledger_pointer") or {}).get("applied_seq", 0)),
        "todos": [], "terminal_reason": state.get("terminal_reason"),
    }


def merge_runtime_updates(state: dict[str, Any], updates: dict[str, Any]) -> AgentState:
    patch: dict[str, Any] = {"state_revision": int(state.get("state_revision", 0)) + 1}
    if "working_messages" in updates:
        window = list(updates["working_messages"] or [])
        if updates.get("messages"):
            window = add_messages(window, updates["messages"])
        patch["context"] = {"working_message": window, "_replace_working_message": True}
    elif "messages" in updates:
        patch["context"] = {"working_message": updates["messages"]}
    for runtime_key, (branch, key) in {
        "compression_count": ("context", "compression_count"),
        "context_usage": ("context", "usage"),
        "memory_turn_counter": ("memory", "recorded_count"),
        "api_usage": ("memory", "api_usage"),
        "session_token_stats": ("memory", "token_stats"),
        "todos": ("task_details", "todos"),
        "step": ("current_step", "index"),
        "terminal_reason": ("terminal_reason", None),
    }.items():
        if runtime_key not in updates:
            continue
        value = updates[runtime_key]
        if branch == "terminal_reason":
            patch[branch] = value
            if value:
                patch["run_status"] = (
                    "handoff" if value == "handoff"
                    else "failed" if value == "step_budget_exhausted"
                    else "completed"
                )
        elif branch == "task_details":
            patch[branch] = {"_runtime": {key: value}}
        elif branch == "current_step" and key == "index":
            patch["task_details"] = {"_runtime": {"step": value}}
        else:
            patch.setdefault(branch, {})[key] = value
    usage = updates.get("context_usage")
    if isinstance(usage, dict):
        used = int(usage.get("total_tokens") or usage.get("used_tokens") or 0)
        budget = int(usage.get("max_tokens") or state.get("context", {}).get("token_budget") or 1)
        patch.setdefault("context", {}).update({
            "usage": usage,
            "used_tokens": used,
            "token_budget": budget,
            "window_size_tokens": int(usage.get("max_tokens") or state.get("context", {}).get("window_size_tokens") or 256_000),
            "usage_ratio": min(1.0, used / budget) if budget > 0 else 0.0,
            "last_built_at": utc_now(),
            "compression_count": int(updates.get("compression_count") or state.get("context", {}).get("compression_count", 0)),
        })
    return merge_agent_state(state, patch)


def terminal_patch(state: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    """One terminal transition for sync, stream, cancellation and resume paths."""
    if status not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"invalid terminal run status: {status}")
    details = copy.deepcopy(state.get("task_details") or {})
    for task_id in state.get("tasks") or []:
        task = details.get(task_id)
        if not isinstance(task, dict) or task.get("status") in {"completed", "failed", "cancelled", "handoff"}:
            continue
        task["status"] = status
    return {
        "state_revision": int(state.get("state_revision", 0)) + 1,
        "checkpoint_uid": f"cp_{uuid.uuid4().hex}",
        "run_status": status,
        "terminal_reason": reason,
        "task_details": details,
        "pending_interaction": None,
    }
