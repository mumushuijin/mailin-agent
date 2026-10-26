"""Branch-aware timeline reads backed by the immutable session event ledger."""
from __future__ import annotations

import base64
import copy
import json
from typing import Any

from app.storage.conversation_files import SessionConversationFiles
from app.storage.event_projection import project_events


_CURSOR_VERSION = 1
_SUMMARY_LIMIT = 2000


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def _short_summary(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:_SUMMARY_LIMIT]


def normalize_timeline_event(
    event: dict[str, Any],
    conversation_files: SessionConversationFiles | None = None,
) -> dict[str, Any]:
    """Convert ledger-specific row payloads to the compact client timeline envelope."""
    normalized = {
        key: copy.deepcopy(event[key])
        for key in (
            "schema_version", "session_id", "branch_id", "event_seq", "event_id", "turn_id",
            "event_type", "recorded_at", "occurred_at", "activity_id", "parent_activity_id",
            "causation_event_id", "request_id", "batch_id", "call_id", "attempt_id", "interaction_id",
        )
        if key in event
    }
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
    extensions = payload.get("extensions") if isinstance(payload.get("extensions"), dict) else {}
    runtime = extensions.get("runtime") if isinstance(extensions.get("runtime"), dict) else {}
    interaction = payload.get("interaction") if isinstance(payload.get("interaction"), dict) else {}
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    event_type = event.get("event_type")

    if event_type in {"message.accepted", "model.response.completed"}:
        if conversation_files is not None:
            message = conversation_files.resolve_message(event["session_id"], message)
        message_id = _first(payload.get("message_id"), message.get("message_id"), event["event_id"])
        if event_type == "message.accepted":
            client_payload = {
                "message_id": message_id,
                "content": _text_content(message.get("content", payload.get("content"))),
            }
            if payload.get("submission_id") is not None:
                client_payload["submission_id"] = payload["submission_id"]
        else:
            response_id = _first(payload.get("response_id"), message.get("id"), event.get("event_id"))
            request_id = _first(event.get("request_id"), payload.get("request_id"), model.get("request_id"), response_id)
            client_payload = {
                "request_id": request_id,
                "response_id": response_id,
                "message_id": message_id,
                "content": _text_content(message.get("content", payload.get("content"))),
            }
            finish_reason = _first(payload.get("finish_reason"), execution.get("finish_reason"))
            if finish_reason is not None:
                client_payload["finish_reason"] = finish_reason
    elif event_type in {"activity.started", "activity.settled"}:
        client_payload = {}
        kind = _first(payload.get("activity_kind"), payload.get("kind"), runtime.get("activity_kind"), runtime.get("kind"))
        label = _first(payload.get("label"), runtime.get("label"), kind)
        summary = _short_summary(_first(payload.get("summary"), payload.get("safe_summary"), runtime.get("summary")))
        if kind is not None:
            client_payload["activity_kind"] = kind
        if label is not None:
            client_payload["label"] = label
        if summary is not None:
            client_payload["summary"] = summary
        if event_type == "activity.settled":
            client_payload["status"] = _first(payload.get("status"), execution.get("status"), "unknown")
    elif event_type in {"tool.attempt.started", "tool.attempt.settled"}:
        raw_error = execution.get("error") if isinstance(execution.get("error"), dict) else {}
        client_payload = {
            "call_id": _first(event.get("call_id"), payload.get("call_id"), message.get("tool_call_id"), "unknown"),
            "attempt_id": _first(event.get("attempt_id"), payload.get("attempt_id"), runtime.get("attempt_id"), "attempt-unknown"),
        }
        if event_type == "tool.attempt.started":
            client_payload.update({
                "tool_name": _first(payload.get("tool_name"), payload.get("name"), message.get("name"), runtime.get("tool_name"), "unknown"),
            })
            batch_id = _first(event.get("batch_id"), payload.get("batch_id"), runtime.get("batch_id"))
            input_summary = _short_summary(_first(payload.get("input_summary"), payload.get("safe_input_summary"), runtime.get("input_summary")))
            if batch_id is not None:
                client_payload["batch_id"] = batch_id
            if input_summary is not None:
                client_payload["input_summary"] = input_summary
        else:
            client_payload["status"] = _first(payload.get("status"), execution.get("status"), "unknown")
            result_summary = _short_summary(_first(payload.get("result_summary"), payload.get("output_summary"), payload.get("safe_summary")))
            reason_code = _first(payload.get("reason_code"), raw_error.get("code"))
            if result_summary is not None:
                client_payload["result_summary"] = result_summary
            if reason_code is not None:
                client_payload["reason_code"] = reason_code
    elif event_type in {"interaction.opened", "interaction.resolved"}:
        client_payload = {
            "interaction_id": _first(event.get("interaction_id"), payload.get("interaction_id"), interaction.get("id"), "unknown"),
        }
        if event_type == "interaction.opened":
            kind = _first(payload.get("kind"), interaction.get("kind"), "unknown")
            prompt_summary = _short_summary(_first(payload.get("prompt_summary"), interaction.get("prompt_summary")))
            client_payload["kind"] = kind
            if prompt_summary is not None:
                client_payload["prompt_summary"] = prompt_summary
        else:
            client_payload["status"] = _first(payload.get("status"), interaction.get("status"), execution.get("status"), "completed")
            decision_summary = _short_summary(_first(payload.get("decision_summary"), interaction.get("decision_summary")))
            if decision_summary is not None:
                client_payload["decision_summary"] = decision_summary
    elif event_type == "turn.settled":
        client_payload = {"status": _first(payload.get("status"), execution.get("status"), "failed")}
        reason_code = _first(payload.get("reason_code"), runtime.get("reason_code"), runtime.get("reason"), (execution.get("error") or {}).get("code"))
        final_message_id = _first(payload.get("final_message_id"), runtime.get("final_message_id"))
        if reason_code is not None:
            client_payload["reason_code"] = reason_code
        if final_message_id is not None:
            client_payload["final_message_id"] = final_message_id
        for field in ("final_response_event_id", "reason_event_id"):
            value = _first(payload.get(field), runtime.get(field))
            if value is not None:
                client_payload[field] = value
    elif event_type.startswith("model.request."):
        request_id = _first(event.get("request_id"), payload.get("request_id"), model.get("request_id"))
        client_payload = {"request_id": request_id} if request_id is not None else {}
        reason_code = _first(payload.get("reason_code"), (execution.get("error") or {}).get("code"))
        if reason_code is not None:
            client_payload["reason_code"] = reason_code
    elif event_type in {"task.started", "task.settled", "step.started", "step.settled"}:
        names = ("task_id", "step_id", "node", "title", "name", "label", "action", "summary", "status")
        nested_name = "task" if event_type.startswith("task.") else "step"
        nested = payload.get(nested_name) if isinstance(payload.get(nested_name), dict) else {}
        client_payload = {name: _first(payload.get(name), nested.get(name), runtime.get(name)) for name in names
                          if _first(payload.get(name), nested.get(name), runtime.get(name)) is not None}
        if "status" not in client_payload:
            client_payload["status"] = execution.get("status", "running" if event_type.endswith(".started") else "unknown")
    elif event_type == "branch.forked":
        client_payload = {
            "parent_branch_id": payload.get("parent_branch_id"),
            "parent_cursor": copy.deepcopy(payload.get("parent_cursor")),
        }
    elif event_type == "memory.sync.recorded":
        client_payload = {
            name: copy.deepcopy(payload[name])
            for name in ("recorded_count", "last_synced_turn", "source_files")
            if name in payload
        }
    elif event_type == "context.window.materialized":
        client_payload = {
            name: copy.deepcopy(payload[name])
            for name in (
                "window_schema_version", "compression_version", "content_sha256",
                "source_event_cursor", "bootstrap_fingerprint", "reference_fingerprint", "window_ref",
            )
            if name in payload
        }
    else:
        client_payload = {}
    normalized["payload"] = client_payload
    return normalized


def _encode_cursor(session_id: str, branch_id: str, event: dict[str, Any] | None) -> str:
    value = {
        "v": _CURSOR_VERSION,
        "session_id": session_id,
        "branch_id": branch_id,
        "event_seq": event["event_seq"] if event else 0,
        "event_id": event["event_id"] if event else "origin",
    }
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token or len(token) > 2048:
        raise ValueError("invalid timeline cursor")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid timeline cursor") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"v", "session_id", "branch_id", "event_seq", "event_id"}
        or type(value.get("v")) is not int
        or value["v"] != _CURSOR_VERSION
        or not isinstance(value.get("session_id"), str)
        or not value["session_id"]
        or not isinstance(value.get("branch_id"), str)
        or not value["branch_id"]
        or type(value.get("event_seq")) is not int
        or value["event_seq"] < 0
        or not isinstance(value.get("event_id"), str)
        or not value["event_id"]
    ):
        raise ValueError("invalid timeline cursor")
    return value


class TimelineService:
    def __init__(self, conversation_files: SessionConversationFiles):
        self.conversation_files = conversation_files

    def read_page(
        self,
        session_id: str,
        *,
        branch_id: str = "main",
        limit: int = 100,
        after_cursor: str | None = None,
        include_projection: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(branch_id, str) or not branch_id or len(branch_id) > 256:
            raise ValueError("branch_id must be a non-empty string of at most 256 characters")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")

        events = self.conversation_files.read_events(session_id)
        known_branches = {
            event["branch_id"]
            for event in events
            if event["event_type"] == "branch.forked"
        }
        if branch_id != "main" and branch_id not in known_branches:
            raise ValueError("unknown timeline branch")

        projection_events = []
        for event in events:
            replay_event = copy.deepcopy(event)
            payload = replay_event.get("payload") or {}
            message = payload.get("message")
            if (
                event["event_type"] in {"message.accepted", "model.response.completed"}
                and isinstance(message, dict)
            ):
                payload["message"] = self.conversation_files.resolve_message(session_id, message)
            projection_events.append(replay_event)
        projection = project_events(projection_events, branch_id=branch_id, session_id=session_id)
        visible = [
            event
            for event in events
            if projection["applied_events"].get(event["event_id"])
            == {"branch_id": event["branch_id"], "event_seq": event["event_seq"]}
        ]
        visible.sort(key=lambda event: event["event_seq"])

        start_seq = 0
        if after_cursor is not None:
            cursor = _decode_cursor(after_cursor)
            if cursor["session_id"] != session_id:
                raise ValueError("timeline cursor belongs to another session")
            if cursor["branch_id"] != branch_id:
                raise ValueError("timeline cursor belongs to another branch")
            start_seq = cursor["event_seq"]
            if start_seq == 0:
                if cursor["event_id"] != "origin":
                    raise ValueError("timeline cursor event identity is invalid")
            else:
                applied = projection["applied_events"].get(cursor["event_id"])
                if applied is None or applied["event_seq"] != start_seq:
                    raise ValueError("timeline cursor is not visible on this branch")

        remaining = [event for event in visible if event["event_seq"] > start_seq]
        page = remaining[:limit]
        snapshot_event = visible[-1] if visible else None
        page_event = page[-1] if page else None
        next_cursor = (
            _encode_cursor(session_id, branch_id, page_event)
            if page_event is not None
            else after_cursor or _encode_cursor(session_id, branch_id, None)
        )
        result = {
            "session_id": session_id,
            "branch_id": branch_id,
            "as_of_event_seq": snapshot_event["event_seq"] if snapshot_event else 0,
            "events": [
                normalize_timeline_event(event, self.conversation_files)
                for event in page
            ],
            "snapshot_cursor": _encode_cursor(session_id, branch_id, snapshot_event),
            "next_cursor": next_cursor,
            "has_more": len(remaining) > len(page),
        }
        if include_projection:
            result["projection"] = projection
        return result
