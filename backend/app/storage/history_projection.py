from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.schemas.session import ChatMessage
from app.storage.ledger_contract import is_audit_row, lifecycle_subject
from app.storage.message_codec import safe_business_value
from app.storage.conversation_files import _atomic_write, utc_milliseconds

PROJECTION_VERSION = 4


def _safe_session_id(session_id: str) -> str:
    return re.sub(r"[^\w\-]", "_", session_id)


def project_ledger(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild display messages and lifecycle tracks from immutable facts."""
    messages: list[dict[str, Any]] = []
    turns: dict[str, dict[str, Any]] = {}
    calls: dict[tuple[str, str], dict[str, Any]] = {}
    steps: dict[tuple[str, str], dict[str, Any]] = {}
    tasks: dict[tuple[str, str], dict[str, Any]] = {}
    interactions: dict[tuple[str, str], dict[str, Any]] = {}
    seen: set[str] = set()
    usage_requests: set[str] = set()
    totals: dict[str, int] = {}
    from app.tools.tool_search import resolve_tool_display, should_show_tool_in_ui
    hidden_calls: set[tuple[str, str]] = set()
    current_steps: dict[str, str] = {}
    for original in rows:
        if original["message_id"] in seen:
            continue
        seen.add(original["message_id"])
        row = safe_business_value(original)
        payload = row["message"]
        turn_id = row.get("turn_id")
        track = None
        if turn_id:
            track = turns.setdefault(turn_id, {"turn_id": turn_id, "steps": [], "tasks": [], "tools": [], "interactions": []})
        execution = row.get("execution") or {}
        runtime = (row.get("extensions") or {}).get("runtime") or {}
        subject = lifecycle_subject(row)
        call_id = payload.get("tool_call_id")
        for definition in payload.get("tool_calls", []):
            if not track:
                continue
            display_name, _ = resolve_tool_display(definition["name"], definition["arguments"])
            if not should_show_tool_in_ui(display_name):
                hidden_calls.add((turn_id, definition["id"]))
                continue
            call = {"call_id": definition["id"], "definition_seq": row["seq"], "attempts": []}
            calls[(turn_id, definition["id"])] = call
            track["tools"].append(call)
        if track and call_id and (subject == "tool" or payload["role"] == "tool"):
            if (turn_id, call_id) in hidden_calls:
                continue
            key = (turn_id, call_id)
            call = calls.get(key)
            if call is None:
                call = {"call_id": call_id, "attempts": []}
                calls[key] = call
                track["tools"].append(call)
            attempt = execution.get("attempt")
            if attempt is not None:
                record = next((item for item in call["attempts"] if item["attempt"] == attempt), None)
                if record is None:
                    record = {"attempt": attempt}
                    call["attempts"].append(record)
                if runtime.get("late_result") is not None:
                    record.setdefault("late_evidence", []).append({"seq": row["seq"], **execution})
                else:
                    record.update(execution)
            if not runtime.get("late_result"):
                for name in ("status",):
                    if execution.get(name):
                        call[name] = execution[name]
                if execution.get("error", {}).get("code"):
                    call["reason_code"] = execution["error"]["code"]
                elif execution.get("status") in {"running", "succeeded"}:
                    call.pop("reason_code", None)
            if turn_id in current_steps:
                call["step_id"] = current_steps[turn_id]
            if payload["role"] == "tool":
                call["result_seq"] = row["seq"]
        if track and subject == "step" and runtime.get("step_id"):
            current_steps[turn_id] = runtime["step_id"]
            key = (turn_id, runtime["step_id"])
            step = steps.get(key)
            if step is None:
                step = {"step_id": runtime["step_id"]}
                steps[key] = step
                track["steps"].append(step)
            step.update(execution)
            if runtime.get("node"):
                step["step_name"] = runtime["node"]
            if runtime.get("task_id"):
                step["task_id"] = runtime["task_id"]
        if track and subject == "task" and runtime.get("task_id"):
            key = (turn_id, runtime["task_id"])
            task = tasks.get(key)
            if task is None:
                task = {"task_id": runtime["task_id"]}
                tasks[key] = task
                track["tasks"].append(task)
            task.update(execution)
            for name in ("title", "action"):
                if runtime.get(name):
                    task[name] = runtime[name]
        interaction = row.get("interaction")
        if track and interaction:
            key = (turn_id, interaction["id"])
            item = interactions.get(key)
            if item is None:
                item = {"id": interaction["id"], "definition_seq": row["seq"]}
                interactions[key] = item
                track["interactions"].append(item)
            item.update(interaction)
            if call_id:
                item["call_id"] = call_id
            if execution.get("status"):
                item["status"] = execution["status"]
            if interaction.get("decision") is not None:
                item["decision_seq"] = row["seq"]
                decision = interaction["decision"]
                source = decision.get("source") if isinstance(decision, dict) else None
                answer = decision.get("decision") if isinstance(decision, dict) else decision
                item["status"] = "expired" if source == "timeout" else "cancelled" if source == "cancelled" else "denied" if answer == "deny" else "completed"
        if track and subject == "turn":
            track["status"] = execution["status"]
            if execution.get("finished_at") is not None:
                track["finished_at"] = execution["finished_at"]
            reason = runtime.get("reason") or execution.get("error", {}).get("code")
            if reason:
                track["reason_code"] = reason
        if track and payload["role"] == "user":
            track.setdefault("started_at", row["timestamp"])
        model = row.get("model") or {}
        request_id = model.get("request_id")
        if request_id and request_id not in usage_requests and model.get("usage"):
            usage_requests.add(request_id)
            for name, count in model["usage"].items():
                if type(count) is int:
                    totals[name] = totals.get(name, 0) + count
        if is_audit_row(row):
            continue
        displayed = {key: row[key] for key in ("message_id", "turn_id", "seq", "timestamp") if key in row}
        displayed["role"] = payload["role"]
        if interaction:
            displayed["interaction"] = interaction
        if payload["role"] == "tool":
            displayed["tool_call_id"] = call_id
            if execution.get("status"):
                displayed["tool_status"] = execution["status"]
            if execution.get("error", {}).get("code"):
                displayed["tool_reason_code"] = execution["error"]["code"]
            content = payload.get("content")
            if isinstance(content, list) and content and content[0].get("type") == "reference":
                displayed["tool_result_reference"] = content[0]
        else:
            if "content" in payload and payload["content"]:
                displayed["content"] = payload["content"]
            if payload.get("tool_calls"):
                displayed_calls = []
                for definition in payload["tool_calls"]:
                    name, arguments = resolve_tool_display(definition["name"], definition["arguments"])
                    if should_show_tool_in_ui(name):
                        displayed_calls.append({"id": definition["id"], "name": name, "arguments": safe_business_value(arguments)})
                if displayed_calls:
                    displayed["tool_calls"] = displayed_calls
            if not displayed.get("content") and not displayed.get("tool_calls"):
                continue
        messages.append(displayed)
    result: dict[str, Any] = {"messages": messages, "timelines": list(turns.values())}
    if totals:
        if "input_tokens" in totals and "output_tokens" in totals:
            totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
        result["api_usage"] = totals
    return result


class HistoryProjectionStore:
    """Display-only chat history projection used for fast paged reads."""

    def __init__(self, workspace: Path):
        self.root = workspace / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, session_id: str) -> Path:
        safe_id = _safe_session_id(session_id)
        if safe_id in {"", ".", ".."}:
            raise ValueError("非法 session id")
        return self.root / safe_id / "history_projection.json"

    def read(self, session_id: str) -> dict[str, Any] | None:
        path = self.path_for(session_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("version") != PROJECTION_VERSION:
            raise ValueError(f"不支持的历史投影格式: expected projection version {PROJECTION_VERSION}; existing data is preserved")
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None
        return payload

    def write(
        self,
        session_id: str,
        messages: list[ChatMessage],
        *,
        context_usage: dict | None = None,
        api_usage: dict | None = None,
        session_token_stats: dict | None = None,
        todos: list[dict] | None = None,
        ledger_seq: int | None = None,
        timelines: list[dict] | None = None,
    ) -> None:
        payload = {
            "version": PROJECTION_VERSION,
            "session_id": session_id,
            "branch_id": "main",
            "updated_at": utc_milliseconds(),
            "messages": [safe_business_value(m.model_dump(mode="json", exclude_none=True, exclude_defaults=True)) for m in messages],
        }
        for name, value in (("context_usage", context_usage), ("api_usage", api_usage), ("session_token_stats", session_token_stats),
                            ("todos", todos), ("ledger_seq", ledger_seq), ("timelines", timelines)):
            if value is not None:
                payload[name] = safe_business_value(value)
        _atomic_write(self.path_for(session_id), json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def page(
        self,
        payload: dict[str, Any],
        *,
        limit: int,
        before: str | None = None,
    ) -> tuple[list[ChatMessage], bool, str | None]:
        raw_messages = payload.get("messages") or []
        total = len(raw_messages)
        end = total
        if before:
            try:
                end = max(0, min(total, int(before)))
            except ValueError:
                end = total
        start = max(0, end - limit)
        page_messages = [
            ChatMessage.model_validate(item)
            for item in raw_messages[start:end]
            if isinstance(item, dict)
        ]
        return page_messages, start > 0, str(start) if start > 0 else None

    def delete(self, session_id: str) -> None:
        try:
            self.path_for(session_id).unlink()
        except FileNotFoundError:
            pass
