"""Canonical, checkpointable agent state and field-aware reducer."""
from __future__ import annotations

import copy
import hashlib
import re
import uuid
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage


CHECKPOINT_SCHEMA_VERSION = 2
TERMINAL_TURN_STATUSES = {"completed", "failed", "cancelled", "handoff"}
REQUIRED_TOP_LEVEL = {
    "schema_version", "checkpoint_id", "scope", "context", "max_step_every_turn", "tasks",
    "current_task", "current_step", "memory", "turn_status", "ledger_pointer",
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
            from app.agent.recovery import append_window_messages
            result["working_message"] = append_window_messages(
                (current or {}).get("working_message", []), patch["working_message"] or []
            )
        result.pop("_replace_working_message", None)
    return result


def _merge_tasks(current: list[str] | None, patch: list[str] | None) -> list[str]:
    return list(dict.fromkeys([*(current or []), *(patch or [])]))


def _merge_step(current: dict[str, Any] | None, patch: dict[str, Any] | None) -> dict[str, Any]:
    if patch is None:
        return {}
    if current and "step_id" in patch and patch.get("step_id") != current.get("step_id"):
        return copy.deepcopy(patch)
    result = _merge_mapping(current or {}, patch or {})
    if patch and "tool_calls" in patch:
        result["tool_calls"] = _merge_by_id(
            (current or {}).get("tool_calls", []), patch["tool_calls"] or [], "call_id"
        )
    return result


def _merge_task_details(current: dict[str, Any] | None, patch: dict[str, Any] | None) -> dict[str, Any]:
    patch = patch or {}
    if not patch or set(patch) != {"_runtime"}:
        return copy.deepcopy(patch)
    return _merge_mapping(current or {}, patch)


def _merge_terminal(current: str | None, patch: str | None) -> str | None:
    if current in TERMINAL_TURN_STATUSES and patch not in {current, "created"}:
        return current
    return patch or current


class AgentState(TypedDict, total=False):
    # LangGraph reserves checkpoint_id as its own runtime metadata channel.
    checkpoint_uid: str
    schema_version: int
    state_revision: int
    scope: Annotated[dict[str, str | None], _merge_scope]
    context: Annotated[dict[str, Any], _merge_context]
    max_step_every_turn: int
    tasks: list[str]
    current_task: str | None
    current_step: Annotated[dict[str, Any], _merge_step]
    memory: Annotated[dict[str, Any], _merge_branch]
    turn_status: Annotated[str, _merge_terminal]
    terminal_reason: str | None
    task_details: Annotated[dict[str, Any], _merge_task_details]
    ledger_pointer: Annotated[dict[str, Any], _merge_branch]
    pending_interaction: dict[str, Any] | None


def utc_now() -> int:
    import time
    return time.time_ns() // 1_000_000


def _checkpoint_id(scope: dict[str, str | None] | None = None) -> str:
    return f"cp_{uuid.uuid4().hex}"


def make_scope(
    *, workspace_id: str, session_id: str, turn_id: str | None = None,
) -> dict[str, str | None]:
    stable_workspace_id = "ws_" + hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:20]
    return {
        "workspace_id": stable_workspace_id,
        "session_id": session_id,
        "turn_id": turn_id or f"turn_{uuid.uuid4().hex}",
    }


def new_agent_state(
    *, workspace_id: str, session_id: str, message: BaseMessage,
    max_steps: int = 48, turn_id: str | None = None,
) -> AgentState:
    scope = make_scope(workspace_id=workspace_id, session_id=session_id, turn_id=turn_id)
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": _checkpoint_id(scope),
        "state_revision": 0,
        "scope": scope,
        "context": {
            "working_message": [message], "window_size_tokens": 256_000,
            "token_budget": 20_000, "used_tokens": 0, "usage_ratio": 0.0,
            "summary_pointer": None, "compression_count": 0, "last_built_at": utc_now(),
            "bootstrap_fingerprint": "",
        },
        "max_step_every_turn": int(max_steps),
        "tasks": [],
        "current_task": None,
        "current_step": {
            "step_id": None, "step_name": "agent", "step_status": "pending",
            "tool_calls": [],
        },
        "memory": {"recorded_count": 0, "last_synced_turn": None, "source_files": []},
        "turn_status": "created", "terminal_reason": None,
        "task_details": {},
        "ledger_pointer": {"applied_seq": 0},
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
    if type(state.get("schema_version")) is not int or state.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("unsupported checkpoint schema version")
    if {"run_id", "run_status", "max_step_every_run", "meta", "session", "data", "task"} & set(state):
        raise ValueError("unsupported legacy checkpoint fields")
    checkpoint_value = state.get("checkpoint_id", state.get("checkpoint_uid"))
    if not isinstance(checkpoint_value, str) or not re.fullmatch(r"cp_[0-9a-f]{32}", checkpoint_value):
        raise ValueError("checkpoint_id must be an independent cp_<uuid> snapshot identity")
    from app.storage.ledger_contract import validate_scope
    try:
        validate_scope(state["scope"])
    except ValueError as exc:
        raise ValueError(f"invalid canonical scope: {exc}") from exc
    if not isinstance(state["context"], dict) or not isinstance(state["context"].get("working_message"), list):
        raise ValueError("context.working_message must be an array")
    if not isinstance(state["tasks"], list) or not all(isinstance(v, str) for v in state["tasks"]):
        raise ValueError("tasks must be an array of task ids")
    if state.get("current_task") is not None and state["current_task"] not in state["tasks"]:
        raise ValueError("current_task must reference an explicit task")
    if type(state["state_revision"]) is not int or state["state_revision"] < 0:
        raise ValueError("state_revision must be a non-negative integer")
    if type(state["max_step_every_turn"]) is not int or state["max_step_every_turn"] <= 0:
        raise ValueError("max_step_every_turn must be a positive integer")
    if state.get("turn_status") not in {"created", "running", "waiting_user", *TERMINAL_TURN_STATUSES}:
        raise ValueError("invalid turn status")
    step = state["current_step"]
    if not isinstance(step, dict) or not isinstance(step.get("tool_calls"), list):
        raise ValueError("current_step must include tool_calls")
    if step.get("step_status") not in {"pending", "running", "waiting_user", "completed", "failed", "cancelled", "skipped"}:
        raise ValueError("invalid step status")
    if "token_info" in step:
        raise ValueError("current_step.token_info is unsupported; use model.usage")
    if step.get("model") is not None:
        if not isinstance(step["model"], dict) or not step["model"].get("request_id"):
            raise ValueError("current_step.model requires request_id")
        if "total_tokens" in (step["model"].get("usage") or {}):
            raise ValueError("current_step.model.usage must omit derived total_tokens")
    for key in ("started_at", "ended_at"):
        if key in step and (type(step[key]) is not int or step[key] < 0):
            raise ValueError(f"current_step.{key} must be UTC milliseconds")
    for call in step["tool_calls"]:
        if not isinstance(call, dict) or not call.get("call_id") or {"args", "input", "output"} & set(call):
            raise ValueError("tool checkpoint requires call references, not full payloads")
        if call.get("status") not in {"pending", "running", "waiting_user", "succeeded", "failed", "cancelled", "denied", "skipped", "unknown"}:
            raise ValueError("invalid tool execution status")
        for key in ("definition_seq", "result_seq", "attempt"):
            if key in call and (type(call[key]) is not int or call[key] <= 0):
                raise ValueError(f"tool {key} must be a positive integer")
    memory = state.get("memory") or {}
    if "last_synced_run" in memory or not {"recorded_count", "last_synced_turn", "source_files"}.issubset(memory):
        raise ValueError("invalid memory checkpoint metadata")
    if "file" in (state.get("ledger_pointer") or {}):
        raise ValueError("ledger_pointer.file is unsupported")
    cursor = (state.get("ledger_pointer") or {}).get("applied_seq")
    if type(cursor) is not int or cursor < 0:
        raise ValueError("ledger_pointer.applied_seq must be a non-negative integer")
    pending = state.get("pending_interaction")
    if pending and ({"scope", "run_id", "turn_id", "step_id", "session_id", "workspace_id"} & set(pending)):
        raise ValueError("pending interaction must not duplicate checkpoint identities")
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
    if any(patch_scope.get(key, current_scope.get(key)) != current_scope.get(key) for key in ("workspace_id", "session_id", "turn_id")):
        return copy.deepcopy(current)  # type: ignore[return-value]
    if int(patch.get("state_revision", current.get("state_revision", 0))) <= int(current.get("state_revision", 0)):
        return copy.deepcopy(current)  # type: ignore[return-value]
    result = copy.deepcopy(current)
    for key in ("checkpoint_uid", "checkpoint_id", "schema_version", "state_revision", "scope", "max_step_every_turn", "current_task", "turn_status", "terminal_reason", "pending_interaction", "ledger_pointer"):
        if key in patch:
            result[key] = copy.deepcopy(patch[key])
    if patch_scope:
        result["scope"] = _merge_scope(current_scope, patch_scope)
    if "pending_interaction" in patch and patch["pending_interaction"] is None:
        result["pending_interaction"] = None
    if current.get("turn_status") in TERMINAL_TURN_STATUSES:
        result["turn_status"] = current["turn_status"]
        result["terminal_reason"] = current.get("terminal_reason")
    if "context" in patch:
        result["context"] = _merge_mapping(result.get("context", {}), patch["context"] or {})
        if "working_message" in (patch["context"] or {}):
            if patch["context"].get("_replace_working_message"):
                result["context"]["working_message"] = copy.deepcopy(patch["context"]["working_message"])
            else:
                from app.agent.recovery import append_window_messages
                result["context"]["working_message"] = append_window_messages(
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
        result["current_step"] = _merge_step(result.get("current_step"), patch["current_step"])
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
        "max_steps": int(state.get("max_step_every_turn", 48)),
        "context_summary": summary, "compression_count": int(context.get("compression_count", 0)),
        "memory_turn_counter": int(memory.get("recorded_count", 0)),
        "memory_nudge_pending": bool(memory.get("nudge_pending")),
        "context_usage": copy.deepcopy(context.get("usage") or {}),
        "api_usage": copy.deepcopy(memory.get("api_usage") or {}),
        "session_token_stats": copy.deepcopy(memory.get("token_stats") or {}),
        "last_invoke_ledger_len": int((state.get("ledger_pointer") or {}).get("applied_seq", 0)),
        "todos": copy.deepcopy(state.get("task_details", {}).get("_runtime", {}).get("todos") or []),
        "scope": copy.deepcopy(state.get("scope") or {}),
        "current_step": copy.deepcopy(state.get("current_step") or {}),
        "terminal_reason": state.get("terminal_reason"),
    }


def merge_runtime_updates(state: dict[str, Any], updates: dict[str, Any]) -> AgentState:
    patch: dict[str, Any] = {"state_revision": int(state.get("state_revision", 0)) + 1}
    if "working_messages" in updates:
        window = list(updates["working_messages"] or [])
        if updates.get("messages"):
            from app.agent.recovery import append_window_messages
            window = append_window_messages(window, updates["messages"])
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
                patch["turn_status"] = (
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
    if status not in TERMINAL_TURN_STATUSES:
        raise ValueError(f"invalid terminal turn status: {status}")
    details = copy.deepcopy(state.get("task_details") or {})
    for task_id in state.get("tasks") or []:
        task = details.get(task_id)
        if not isinstance(task, dict) or task.get("status") in {"completed", "failed", "cancelled", "handoff"}:
            continue
        task["status"] = status
    return {
        "state_revision": int(state.get("state_revision", 0)) + 1,
        "checkpoint_uid": f"cp_{uuid.uuid4().hex}",
        "turn_status": status,
        "terminal_reason": reason,
        "task_details": details,
        "pending_interaction": None,
    }
