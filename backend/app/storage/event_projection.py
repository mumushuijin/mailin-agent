"""Deterministic projections of versioned session events."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from app.storage.ledger_contract import validate_event

_TURN_OUTCOMES = {"completed", "failed", "cancelled", "handoff"}
_TOOL_OUTCOMES = {"running", "succeeded", "failed", "cancelled", "denied", "skipped", "unknown"}
_INTERACTION_OUTCOMES = {"waiting", "completed", "failed", "cancelled", "denied", "expired"}
_TERMINAL_TASK_STEP = {"completed", "failed", "cancelled", "skipped"}


def _key(*parts: Any) -> str:
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _duration_ms(started_at: str | None, settled_at: str | None) -> int | None:
    if started_at is None or settled_at is None:
        return None
    start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    settled = datetime.fromisoformat(settled_at.replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if settled.tzinfo is None:
        settled = settled.replace(tzinfo=timezone.utc)
    return max(0, int((settled - start).total_seconds() * 1000))


def _settle_timing(item: dict[str, Any], event: dict[str, Any], field: str = "settled_at") -> None:
    item[field] = event["occurred_at"]
    duration = _duration_ms(item.get("started_at"), item[field])
    if duration is not None:
        item["duration_ms"] = duration


def _message_payload(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message")
    if isinstance(message, dict):
        return message
    return {}


def _execution(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("execution")
    return value if isinstance(value, dict) else {}


def _runtime(payload: dict[str, Any]) -> dict[str, Any]:
    extensions = payload.get("extensions")
    runtime = extensions.get("runtime") if isinstance(extensions, dict) else None
    return runtime if isinstance(runtime, dict) else {}


def _event_identity(
    events: Iterable[dict[str, Any]],
    expected_session_id: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    by_id: dict[str, dict[str, Any]] = {}
    by_seq: dict[int, dict[str, Any]] = {}
    session_id: str | None = None
    for original in events:
        event = validate_event(original, session_id=session_id)
        if session_id is None:
            session_id = event["session_id"]
        event_id, event_seq = event["event_id"], event["event_seq"]
        prior_id = by_id.get(event_id)
        if prior_id is not None:
            if prior_id != event:
                raise ValueError(f"event_id integrity conflict: {event_id}")
            continue
        prior_seq = by_seq.get(event_seq)
        if prior_seq is not None:
            if prior_seq != event:
                raise ValueError(f"event sequence integrity conflict: {event_seq}")
            continue
        by_id[event_id] = event
        by_seq[event_seq] = event
    if session_id is None:
        session_id = expected_session_id
    if session_id is None:
        raise ValueError("session_id is required when projecting an empty event stream")
    return sorted(by_seq.values(), key=lambda item: item["event_seq"]), session_id


def _branch_visibility(
    events: list[dict[str, Any]],
    branch_id: str,
) -> list[tuple[str, int | None]]:
    by_seq = {event["event_seq"]: event for event in events}
    forks: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["event_type"] != "branch.forked":
            continue
        branch = event["branch_id"]
        prior = forks.get(branch)
        if prior is not None and prior != event:
            raise ValueError(f"branch has multiple fork events: {branch}")
        forks[branch] = event

    visiting: set[str] = set()
    memo: dict[str, list[tuple[str, int | None]]] = {}

    def lineage(current: str) -> list[tuple[str, int | None]]:
        if current in memo:
            return memo[current]
        if current in visiting:
            raise ValueError("branch lineage contains a cycle")
        visiting.add(current)
        fork = forks.get(current)
        if fork is None:
            result = [(current, None)]
        else:
            parent = fork["payload"]["parent_branch_id"]
            cursor = fork["payload"]["parent_cursor"]
            cursor_seq = cursor["event_seq"]
            if parent == current or cursor["branch_id"] != parent:
                raise ValueError("branch.forked parent cursor does not identify its parent branch")
            parent_lineage = lineage(parent)
            if cursor_seq:
                cursor_event = by_seq.get(cursor_seq)
                if (
                    cursor_event is None
                    or cursor_event["event_id"] != cursor["event_id"]
                    or cursor_event["branch_id"] != parent
                    or not any(
                        visible_branch == parent
                        and (limit is None or cursor_seq <= limit)
                        for visible_branch, limit in parent_lineage
                    )
                ):
                    raise ValueError("branch.forked parent cursor does not identify a visible event")
            capped: list[tuple[str, int | None]] = []
            for visible_branch, limit in parent_lineage:
                if visible_branch == parent:
                    capped.append((visible_branch, cursor_seq if limit is None else min(limit, cursor_seq)))
                else:
                    capped.append((visible_branch, limit))
            result = capped + [(current, None)]
        visiting.remove(current)
        memo[current] = result
        return result

    return lineage(branch_id)


def _append_item(items: list[dict[str, Any]], kind: str, entity_key: str, event_seq: int) -> None:
    if not any(item["kind"] == kind and item["key"] == entity_key for item in items):
        items.append({"kind": kind, "key": entity_key, "event_seq": event_seq})


def _projection_state(session_id: str, branch_id: str, lineage: list[tuple[str, int | None]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "session_id": session_id,
        "branch_view": {
            "branch_id": branch_id,
            "visible_branches": [
                {"branch_id": visible, "through_event_seq": limit}
                for visible, limit in lineage
            ],
        },
        "cursor": None,
        "applied_events": {},
        "turns": {},
        "turn_order": [],
        "messages": {},
        "message_order": [],
        "activities": {},
        "activity_order": [],
        "tools": {},
        "tool_order": [],
        "interactions": {},
        "interaction_order": [],
        "tasks": {},
        "task_order": [],
        "steps": {},
        "step_order": [],
        "model_requests": {},
        "model_request_order": [],
        "items": [],
    }


def _ensure_turn(state: dict[str, Any], turn_id: str | None, event: dict[str, Any]) -> dict[str, Any] | None:
    if not turn_id:
        return None
    turns = state["turns"]
    turn = turns.get(turn_id)
    if turn is None:
        turn = {
            "turn_id": turn_id,
            "status": "running",
            "started_seq": event["event_seq"],
            "started_at": event["occurred_at"],
        }
        turns[turn_id] = turn
        state["turn_order"].append(turn_id)
    return turn


def _message_event(state: dict[str, Any], event: dict[str, Any], turn_id: str) -> None:
    payload = event["payload"]
    raw_message = _message_payload(payload)
    role = raw_message.get("role") or ("user" if event["event_type"] == "message.accepted" else "assistant")
    message_id = _first(payload.get("message_id"), raw_message.get("message_id"), event["event_id"])
    key = _key(turn_id, message_id)
    content = _first(raw_message.get("content"), payload.get("content"), "")
    projected: dict[str, Any] = {
        "key": key,
        "message_id": message_id,
        "turn_id": turn_id,
        "role": role,
        "content": copy.deepcopy(content),
        "event_seq": event["event_seq"],
        "event_id": event["event_id"],
        "occurred_at": event["occurred_at"],
        "recorded_at": event["recorded_at"],
    }
    if raw_message.get("id") is not None:
        projected["raw_message_id"] = raw_message["id"]
    if raw_message.get("tool_calls") is not None:
        projected["tool_calls"] = copy.deepcopy(raw_message["tool_calls"])
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    request_id = _first(event.get("request_id"), payload.get("request_id"), model.get("request_id"))
    if request_id is not None:
        projected["request_id"] = request_id
    for name in ("response_id", "finish_reason"):
        if payload.get(name) is not None:
            projected[name] = payload[name]
    existing = state["messages"].get(key)
    if existing is not None:
        comparable = ("role", "content", "raw_message_id", "tool_calls", "request_id", "response_id", "finish_reason")
        if any(existing.get(name) != projected.get(name) for name in comparable):
            raise ValueError(f"message identity conflict: {message_id}")
        return
    state["messages"][key] = projected
    state["message_order"].append(key)
    _append_item(state["items"], "message", key, event["event_seq"])


def _activity_event(state: dict[str, Any], event: dict[str, Any], turn_id: str) -> None:
    payload = event["payload"]
    activity_id = _first(event.get("activity_id"), payload.get("activity_id"), _runtime(payload).get("activity_id"))
    if not activity_id:
        raise ValueError(f"{event['event_type']} requires activity_id")
    key = _key(turn_id, activity_id)
    existing = state["activities"].get(key)
    started = event["event_type"] == "activity.started"
    status = "running" if started else _first(payload.get("status"), _execution(payload).get("status"))
    if status is None:
        raise ValueError("activity.settled requires a status")
    values = {
        "activity_id": activity_id,
        "turn_id": turn_id,
        "parent_activity_id": _first(event.get("parent_activity_id"), payload.get("parent_activity_id")),
        "kind": _first(payload.get("activity_kind"), payload.get("kind"), payload.get("type"), "unknown"),
        "label": _first(payload.get("label"), "Activity"),
        "summary": _first(payload.get("summary"), payload.get("safe_summary")),
    }
    if existing is None:
        item = {
            "key": key,
            **values,
            "status": status,
            "started_seq": event["event_seq"],
            "started_at": event["occurred_at"],
        }
        if not started:
            item["settled_seq"] = event["event_seq"]
            _settle_timing(item, event)
        state["activities"][key] = item
        state["activity_order"].append(key)
        _append_item(state["items"], "activity", key, event["event_seq"])
    elif existing["status"] == "running" and not started:
        existing.update({name: value for name, value in values.items() if value is not None})
        existing["status"] = status
        existing["settled_seq"] = event["event_seq"]
        _settle_timing(existing, event)
    elif started:
        return


def _tool_event(state: dict[str, Any], event: dict[str, Any], turn_id: str) -> None:
    payload = event["payload"]
    raw_message = _message_payload(payload)
    runtime = _runtime(payload)
    execution = _execution(payload)
    call_id = _first(event.get("call_id"), payload.get("call_id"), raw_message.get("tool_call_id"))
    if call_id is None:
        raise ValueError(f"{event['event_type']} requires call_id")
    attempt_number = execution.get("attempt")
    attempt_id = _first(event.get("attempt_id"), payload.get("attempt_id"), runtime.get("attempt_id"))
    if attempt_id is None and attempt_number is not None:
        attempt_id = f"attempt-{attempt_number}"
    if not attempt_id:
        raise ValueError(f"{event['event_type']} requires attempt_id")
    key = _key(turn_id, call_id)
    tool = state["tools"].get(key)
    if tool is None:
        tool = {
            "key": key,
            "call_id": call_id,
            "turn_id": turn_id,
            "activity_id": _first(event.get("activity_id"), payload.get("activity_id")),
            "parent_activity_id": _first(event.get("parent_activity_id"), payload.get("parent_activity_id")),
            "batch_id": _first(event.get("batch_id"), payload.get("batch_id")),
            "tool_name": _first(payload.get("tool_name"), payload.get("name"), runtime.get("tool_name"), "unknown"),
            "input_summary": _first(payload.get("input_summary"), payload.get("safe_input_summary")),
            "status": "unknown",
            "attempts": [],
            "started_seq": event["event_seq"],
            "started_at": event["occurred_at"],
        }
        state["tools"][key] = tool
        state["tool_order"].append(key)
        _append_item(state["items"], "tool", key, event["event_seq"])
    else:
        tool["activity_id"] = _first(event.get("activity_id"), payload.get("activity_id"), tool.get("activity_id"))
        tool["parent_activity_id"] = _first(event.get("parent_activity_id"), payload.get("parent_activity_id"), tool.get("parent_activity_id"))
        tool["batch_id"] = _first(event.get("batch_id"), payload.get("batch_id"), tool.get("batch_id"))
        tool["tool_name"] = _first(payload.get("tool_name"), payload.get("name"), runtime.get("tool_name"), tool.get("tool_name"))
        tool["input_summary"] = _first(payload.get("input_summary"), payload.get("safe_input_summary"), tool.get("input_summary"))

    attempts = tool["attempts"]
    index = next((i for i, item in enumerate(attempts) if item["attempt_id"] == attempt_id), None)
    started = event["event_type"] == "tool.attempt.started"
    if started:
        if index is not None:
            return
        if tool["status"] not in {"unknown", "failed"} and attempts:
            return
        attempt = {
            "attempt_id": attempt_id,
            "status": "running",
            "started_seq": event["event_seq"],
            "started_at": event["occurred_at"],
        }
        attempts.append(attempt)
        tool["status"] = "running"
        tool.pop("settled_seq", None)
        tool.pop("result_summary", None)
        tool.pop("reason_code", None)
        return

    status = _first(payload.get("status"), execution.get("status"))
    if status not in _TOOL_OUTCOMES - {"running"}:
        raise ValueError("tool.attempt.settled requires a terminal tool outcome")
    prior = attempts[index] if index is not None else None
    if prior is not None and prior["status"] != "running":
        return
    attempt = dict(prior or {
        "attempt_id": attempt_id,
        "started_seq": event["event_seq"],
        "started_at": event["occurred_at"],
    })
    attempt.update({"status": status, "settled_seq": event["event_seq"]})
    _settle_timing(attempt, event)
    result_summary = _first(payload.get("result_summary"), payload.get("output_summary"), payload.get("safe_summary"))
    error = execution.get("error")
    reason_code = _first(payload.get("reason_code"), error.get("code") if isinstance(error, dict) else None)
    if result_summary is not None:
        attempt["result_summary"] = copy.deepcopy(result_summary)
    if reason_code is not None:
        attempt["reason_code"] = reason_code
    if index is None:
        attempts.append(attempt)
    else:
        attempts[index] = attempt
    tool["status"] = status
    tool["settled_seq"] = event["event_seq"]
    _settle_timing(tool, event)
    if result_summary is None:
        tool.pop("result_summary", None)
    else:
        tool["result_summary"] = copy.deepcopy(result_summary)
    if reason_code is None:
        tool.pop("reason_code", None)
    else:
        tool["reason_code"] = reason_code


def _interaction_event(state: dict[str, Any], event: dict[str, Any], turn_id: str) -> None:
    payload = event["payload"]
    interaction = payload.get("interaction") if isinstance(payload.get("interaction"), dict) else {}
    interaction_id = _first(event.get("interaction_id"), payload.get("interaction_id"), interaction.get("id"))
    if not interaction_id:
        raise ValueError(f"{event['event_type']} requires interaction_id")
    key = _key(turn_id, interaction_id)
    existing = state["interactions"].get(key)
    opened = event["event_type"] == "interaction.opened"
    if opened:
        if existing is not None:
            return
        item = {
            "key": key,
            "interaction_id": interaction_id,
            "turn_id": turn_id,
            "activity_id": _first(event.get("activity_id"), payload.get("activity_id")),
            "kind": _first(payload.get("kind"), interaction.get("kind"), "unknown"),
            "prompt_summary": _first(payload.get("prompt_summary"), interaction.get("prompt_summary")),
            "status": "waiting",
            "opened_seq": event["event_seq"],
            "opened_at": event["occurred_at"],
        }
        state["interactions"][key] = item
        state["interaction_order"].append(key)
        _append_item(state["items"], "interaction", key, event["event_seq"])
        return

    status = _first(payload.get("status"), interaction.get("status"), _execution(payload).get("status"), "completed")
    if status not in _INTERACTION_OUTCOMES - {"waiting"}:
        raise ValueError("interaction.resolved requires a resolved interaction status")
    if existing is not None and existing["status"] != "waiting":
        return
    item = existing or {
        "key": key,
        "interaction_id": interaction_id,
        "turn_id": turn_id,
        "activity_id": _first(event.get("activity_id"), payload.get("activity_id")),
        "kind": _first(payload.get("kind"), interaction.get("kind"), "unknown"),
        "opened_seq": event["event_seq"],
        "opened_at": event["occurred_at"],
    }
    item.update({
        "status": status,
        "resolved_seq": event["event_seq"],
        "resolved_at": event["occurred_at"],
    })
    duration = _duration_ms(item.get("opened_at"), item["resolved_at"])
    if duration is not None:
        item["duration_ms"] = duration
    decision_summary = _first(payload.get("decision_summary"), interaction.get("decision_summary"))
    if decision_summary is not None:
        item["decision_summary"] = copy.deepcopy(decision_summary)
    if existing is None:
        state["interactions"][key] = item
        state["interaction_order"].append(key)
        _append_item(state["items"], "interaction", key, event["event_seq"])


def _lifecycle_entity(state: dict[str, Any], event: dict[str, Any], turn_id: str, subject: str) -> None:
    payload = event["payload"]
    nested = payload.get(subject) if isinstance(payload.get(subject), dict) else {}
    runtime = _runtime(payload)
    identity_name = f"{subject}_id"
    entity_id = _first(payload.get(identity_name), nested.get(identity_name), nested.get("id"), runtime.get(identity_name))
    if not entity_id:
        raise ValueError(f"{event['event_type']} requires {identity_name}")
    state_name = "tasks" if subject == "task" else "steps"
    order_name = "task_order" if subject == "task" else "step_order"
    key = _key(turn_id, entity_id)
    existing = state[state_name].get(key)
    started = event["event_type"].endswith(".started")
    execution = _execution(payload)
    status = "running" if started else _first(payload.get("status"), nested.get("status"), execution.get("status"))
    if status is None:
        raise ValueError(f"{event['event_type']} requires a status")
    if existing is not None and existing["status"] in _TERMINAL_TASK_STEP:
        return
    values = {name: copy.deepcopy(payload[name]) for name in (
        "title", "action", "name", "label", "summary", "task_id", "step_id", "node", "step_name"
    ) if payload.get(name) is not None}
    values.update({name: copy.deepcopy(nested[name]) for name in (
        "title", "action", "name", "label", "summary", "task_id", "step_id", "node", "step_name"
    ) if nested.get(name) is not None})
    for name in ("title", "action", "name", "label", "summary", "task_id", "step_id", "node", "step_name"):
        if runtime.get(name) is not None:
            values[name] = copy.deepcopy(runtime[name])
    if subject == "step" and payload.get("task_id") is not None:
        values["task_id"] = payload["task_id"]
    if existing is None:
        item = {
            "key": key,
            f"{subject}_id": entity_id,
            "turn_id": turn_id,
            "status": status,
            "started_seq": event["event_seq"],
            "started_at": event["occurred_at"],
        }
        item.update(values)
        if not started:
            item["settled_seq"] = event["event_seq"]
            _settle_timing(item, event)
        state[state_name][key] = item
        state[order_name].append(key)
        _append_item(state["items"], subject, key, event["event_seq"])
    else:
        existing.update({name: value for name, value in values.items() if value is not None})
        existing["status"] = status
        if not started:
            existing["settled_seq"] = event["event_seq"]
            _settle_timing(existing, event)


def _model_request_event(state: dict[str, Any], event: dict[str, Any], turn_id: str | None) -> None:
    payload = event["payload"]
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    request_id = _first(event.get("request_id"), payload.get("request_id"), model.get("request_id"))
    if not request_id:
        raise ValueError(f"{event['event_type']} requires request_id")
    key = _key(turn_id or "", request_id)
    current = state["model_requests"].get(key)
    if current is None:
        current = {
            "key": key,
            "request_id": request_id,
            "turn_id": turn_id,
            "status": "running",
            "started_seq": event["event_seq"],
            "started_at": event["occurred_at"],
        }
        state["model_requests"][key] = current
        state["model_request_order"].append(key)
    event_type = event["event_type"]
    if event_type == "model.response.completed":
        if current["status"] == "running":
            current["status"] = "succeeded"
            current["settled_seq"] = event["event_seq"]
            _settle_timing(current, event)
    elif event_type == "model.request.failed" and current["status"] == "running":
        current["status"] = "failed"
        current["settled_seq"] = event["event_seq"]
        _settle_timing(current, event)
        error = _execution(payload).get("error")
        reason = _first(payload.get("reason_code"), error.get("code") if isinstance(error, dict) else None)
        if reason is not None:
            current["reason_code"] = reason
    elif event_type == "model.request.interrupted" and current["status"] == "running":
        current["status"] = "unknown"
        current["settled_seq"] = event["event_seq"]
        _settle_timing(current, event)


def _apply_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    turn_id = event.get("turn_id")
    turn = _ensure_turn(state, turn_id, event)
    event_type = event["event_type"]
    turn_scoped = {
        "message.accepted", "model.response.completed", "activity.started", "activity.settled",
        "tool.attempt.started", "tool.attempt.settled", "interaction.opened", "interaction.resolved",
        "task.started", "task.settled", "step.started", "step.settled", "turn.settled",
    }
    if event_type in turn_scoped and not turn_id:
        raise ValueError(f"{event_type} requires turn_id")
    if event_type in {"message.accepted", "model.response.completed"}:
        _message_event(state, event, turn_id)
    if event_type in {"model.request.started", "model.request.failed", "model.request.interrupted", "model.response.completed"}:
        _model_request_event(state, event, turn_id)
    if event_type in {"activity.started", "activity.settled"}:
        _activity_event(state, event, turn_id)
    elif event_type in {"tool.attempt.started", "tool.attempt.settled"}:
        _tool_event(state, event, turn_id)
    elif event_type in {"interaction.opened", "interaction.resolved"}:
        _interaction_event(state, event, turn_id)
    elif event_type in {"task.started", "task.settled"}:
        _lifecycle_entity(state, event, turn_id, "task")
    elif event_type in {"step.started", "step.settled"}:
        _lifecycle_entity(state, event, turn_id, "step")
    elif event_type == "turn.settled" and turn is not None:
        payload = event["payload"]
        status = payload.get("status") or _execution(payload).get("status")
        if status not in _TURN_OUTCOMES:
            raise ValueError("turn.settled requires a terminal turn outcome")
        if turn["status"] == "running":
            turn["status"] = status
            turn["settled_seq"] = event["event_seq"]
            _settle_timing(turn, event)
            runtime = _runtime(payload)
            error = _execution(payload).get("error")
            reason = _first(payload.get("reason_code"), payload.get("reason"), runtime.get("reason"), error.get("code") if isinstance(error, dict) else None)
            final_message_id = _first(payload.get("final_message_id"), payload.get("message_id"))
            if reason is not None:
                turn["reason_code"] = reason
            if final_message_id is not None:
                turn["final_message_id"] = final_message_id


def project_events(
    events: Iterable[dict[str, Any]],
    *,
    branch_id: str,
    session_id: str | None = None,
    as_of_event_seq: int | None = None,
) -> dict[str, Any]:
    """Replay a complete event history into the selected branch's persistent projection.

    `event_seq` remains the physical session sequence. The branch filter follows each
    `branch.forked` parent cursor, so visible sequences may have gaps.
    """
    if not isinstance(branch_id, str) or not branch_id:
        raise ValueError("branch_id must be a non-empty string")
    if as_of_event_seq is not None and (type(as_of_event_seq) is not int or as_of_event_seq < 0):
        raise ValueError("as_of_event_seq must be a non-negative integer")
    normalized, inferred_session_id = _event_identity(events, session_id)
    if session_id is not None and session_id != inferred_session_id:
        raise ValueError("event session_id does not match projection session")
    lineage = _branch_visibility(normalized, branch_id)
    state = _projection_state(inferred_session_id, branch_id, lineage)
    limits = {
        visible: min(limit, as_of_event_seq) if limit is not None and as_of_event_seq is not None
        else as_of_event_seq if as_of_event_seq is not None
        else limit
        for visible, limit in lineage
    }
    visible_events = [
        event for event in normalized
        if event["branch_id"] in limits
        and (limits[event["branch_id"]] is None or event["event_seq"] <= limits[event["branch_id"]])
    ]
    for event in visible_events:
        _apply_event(state, event)
        state["applied_events"][event["event_id"]] = {
            "branch_id": event["branch_id"],
            "event_seq": event["event_seq"],
        }
        state["cursor"] = {
            "branch_id": branch_id,
            "event_seq": event["event_seq"],
            "event_id": event["event_id"],
        }
    return state
