"""Durable event ledger and verifiable session resources."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from langchain_core.messages import BaseMessage
from app.agent.messages import is_ephemeral_prompt_frame
from app.storage.ledger_contract import (
    LEDGER_SCHEMA_VERSION,
    is_audit_row,
    lifecycle_subject,
    validate_event,
    validate_metadata,
    validate_row,
    validate_scope,
)
from app.storage.message_codec import decode_message, encode_message, safe_business_value

_SESSION_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
INLINE_CONTENT_BYTES = 64 * 1024

def utc_milliseconds() -> int:
    return time.time_ns() // 1_000_000

def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid.uuid4().hex[:16]}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

class SessionConversationFiles:
    _process_lock = threading.RLock()

    def __init__(self, agent_home: Path, workspace_id: str | None = None):
        self.root = agent_home / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace_id = workspace_id if workspace_id is not None else "ws_" + hashlib.sha256(str(agent_home).encode("utf-8")).hexdigest()[:20]
        self._lock = self._process_lock
        self._records: dict[str, dict[str, dict[str, Any]]] = {}
        self._offsets: dict[str, list[int]] = {}
        self._signatures: dict[str, tuple[int, int]] = {}

    def session_dir(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("非法 session id")
        path = self.root / session_id
        if path.resolve().parent != self.root.resolve():
            raise ValueError("非法 session id")
        return path

    def ledger_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "ledger.jsonl"

    def metadata_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "ledger_meta.json"

    def read_metadata(self, session_id: str) -> dict[str, Any]:
        try:
            data = json.loads(self.metadata_path(session_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("不支持的账本格式: ledger metadata missing or corrupt; existing data is preserved") from exc
        return validate_metadata(data, workspace_id=self.workspace_id, session_id=session_id)

    def ensure_metadata(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            path = self.metadata_path(session_id)
            if path.exists():
                return self.read_metadata(session_id)
            directory = self.session_dir(session_id)
            if directory.exists() and any(directory.iterdir()):
                raise ValueError("不支持的账本格式: metadata missing; existing data is preserved")
            value = {"schema_version": LEDGER_SCHEMA_VERSION, "workspace_id": self.workspace_id,
                     "session_id": session_id, "resource_layout": "session-v1"}
            _atomic_write(path, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            return value

    def _signature(self, session_id: str) -> tuple[int, int]:
        path = self.ledger_path(session_id)
        if not path.exists():
            return (0, 0)
        stat = path.stat()
        return (stat.st_size, stat.st_mtime_ns)

    def _load_index(self, session_id: str) -> None:
        path = self.ledger_path(session_id)
        self.read_metadata(session_id)
        if session_id in self._records and self._signatures.get(session_id) == self._signature(session_id):
            return
        prior_signature = self._signatures.get(session_id)
        signature = self._signature(session_id)
        incremental = session_id in self._records and prior_signature is not None and signature[0] > prior_signature[0]
        records = dict(self._records[session_id]) if incremental else {}
        offsets = list(self._offsets[session_id]) if incremental else [0]
        if path.exists():
            with path.open("r+b") as raw:
                raw.seek(0, os.SEEK_END)
                size = raw.tell()
                if size:
                    raw.seek(-1, os.SEEK_END)
                    if raw.read(1) != b"\n":
                        position, truncate_at = size, 0
                        while position > 0:
                            start = max(0, position - 65536)
                            raw.seek(start)
                            index = raw.read(position - start).rfind(b"\n")
                            if index >= 0:
                                truncate_at = start + index + 1
                                break
                            position = start
                        raw.truncate(truncate_at)
                        raw.flush()
                        os.fsync(raw.fileno())
            with path.open("rb") as stream:
                stream.seek(offsets[-1])
                for line_no, line in enumerate(stream, len(records) + 1):
                    try:
                        row = validate_event(json.loads(line), session_id=session_id, expected_seq=line_no)
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise ValueError(f"JSONL 账本第 {line_no} 行格式无效: {exc}") from exc
                    if row["event_id"] in records:
                        raise ValueError(f"ledger duplicate event identity at line {line_no}")
                    records[row["event_id"]] = row
                    offsets.append(stream.tell())
        self._records[session_id], self._offsets[session_id] = records, offsets
        self._signatures[session_id] = self._signature(session_id)

    def append_event(self, session_id: str, event_type: str, payload: dict[str, Any], *,
                     event_id: str | None = None, turn_id: str | None = None, branch_id: str = "main",
                     occurred_at: str | None = None, recorded_at: str | None = None,
                     activity_id: str | None = None, parent_activity_id: str | None = None,
                     causation_event_id: str | None = None, request_id: str | None = None,
                     batch_id: str | None = None, call_id: str | None = None, attempt_id: str | None = None,
                     interaction_id: str | None = None) -> tuple[int, bool]:
        """Append one immutable event, returning its physical session sequence and whether it was new."""
        with self._lock:
            self.ensure_metadata(session_id)
            self._load_index(session_id)
            stable_id = event_id or f"evt_{uuid.uuid4().hex}"
            existing = self._records[session_id].get(stable_id)
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            payload_value = safe_business_value(payload)
            envelope = {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "session_id": session_id,
                "event_seq": len(self._records[session_id]) + 1,
                "event_id": stable_id,
                "recorded_at": recorded_at or (existing or {}).get("recorded_at") or now,
                "occurred_at": occurred_at or (existing or {}).get("occurred_at") or now,
                "branch_id": branch_id,
                "event_type": event_type,
                "payload": payload_value,
            }
            for key, value in (("turn_id", turn_id), ("activity_id", activity_id),
                               ("parent_activity_id", parent_activity_id), ("causation_event_id", causation_event_id),
                               ("request_id", request_id), ("batch_id", batch_id), ("call_id", call_id),
                               ("attempt_id", attempt_id), ("interaction_id", interaction_id)):
                if value is not None:
                    envelope[key] = value
            if existing:
                candidate = {**envelope, "event_seq": existing["event_seq"], "recorded_at": existing["recorded_at"]}
                if candidate != existing:
                    raise ValueError(f"ledger event_id integrity conflict: {stable_id}")
                self._verify_event_references(session_id, existing)
                return existing["event_seq"], False
            envelope = validate_event(envelope, session_id=session_id, expected_seq=envelope["event_seq"])
            projection = self._event_to_projection(envelope)
            if projection is not None:
                validate_row({key: value for key, value in projection.items()
                              if key in {"seq", "message_id", "timestamp", "message", "turn_id", "model", "execution", "interaction", "extensions"}})
            self._verify_event_references(session_id, envelope)
            data = (json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            with self.ledger_path(session_id).open("ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                end_offset = stream.tell()
            self._records[session_id][stable_id] = envelope
            self._offsets[session_id].append(end_offset)
            self._signatures[session_id] = self._signature(session_id)
            return envelope["event_seq"], True

    def fork_branch(self, session_id: str, branch_id: str, *,
                    parent_cursor: dict[str, Any], event_id: str | None = None) -> tuple[int, bool]:
        """Create an immutable branch fork from a cursor owned by its parent branch.

        The cursor's ``branch_id`` selects the parent, and a non-origin cursor must
        identify an event physically written on that branch. To fork at an inherited
        event, callers must select the branch that owns that event as the parent.
        """
        if not isinstance(branch_id, str) or not branch_id or len(branch_id) > 256:
            raise ValueError("branch_id must be a non-empty string of at most 256 characters")
        if branch_id == "main":
            raise ValueError("main is the root branch and cannot be forked into")
        if (
            not isinstance(parent_cursor, dict)
            or set(parent_cursor) != {"branch_id", "event_seq", "event_id"}
            or not isinstance(parent_cursor.get("branch_id"), str)
            or not parent_cursor["branch_id"]
            or type(parent_cursor.get("event_seq")) is not int
            or parent_cursor["event_seq"] < 0
            or not isinstance(parent_cursor.get("event_id"), str)
            or not parent_cursor["event_id"]
        ):
            raise ValueError("parent_cursor must identify a parent branch event")
        parent_branch_id = parent_cursor["branch_id"]
        if parent_branch_id == branch_id:
            raise ValueError("a branch cannot fork from itself")
        if parent_cursor["event_seq"] == 0 and parent_cursor["event_id"] != "origin":
            raise ValueError("parent_cursor origin identity is invalid")
        if parent_cursor["event_seq"] > 0 and parent_cursor["event_id"] == "origin":
            raise ValueError("parent_cursor origin identity is invalid")

        with self._lock:
            self.ensure_metadata(session_id)
            self._load_index(session_id)
            events = list(self._records[session_id].values())
            from app.storage.event_projection import project_events

            fork_events = [
                event for event in events
                if event["event_type"] == "branch.forked" and event["branch_id"] == branch_id
            ]
            existing_branch_events = [event for event in events if event["branch_id"] == branch_id]
            if existing_branch_events and not fork_events:
                raise ValueError(f"branch {branch_id} already has events without a fork event")
            if fork_events:
                if len(fork_events) != 1:
                    raise ValueError(f"branch {branch_id} has multiple fork events")
                existing = fork_events[0]
                if (
                    existing_branch_events
                    and min(existing_branch_events, key=lambda event: event["event_seq"])["event_id"] != existing["event_id"]
                ):
                    raise ValueError(f"branch {branch_id} has events before its fork event")
                expected = {
                    "parent_branch_id": parent_branch_id,
                    "parent_cursor": parent_cursor,
                }
                if existing["payload"] != expected:
                    raise ValueError(f"branch {branch_id} already exists with a different parent cursor")
                if event_id is not None and existing["event_id"] != event_id:
                    raise ValueError(f"branch {branch_id} already exists with a different fork event id")
                project_events(events, branch_id=branch_id, session_id=session_id)
                return existing["event_seq"], False

            known_branches = {"main"} | {
                event["branch_id"] for event in events if event["event_type"] == "branch.forked"
            }
            if parent_branch_id not in known_branches:
                raise ValueError(f"unknown parent branch: {parent_branch_id}")
            if parent_cursor["event_seq"] > 0:
                cursor_event = next(
                    (event for event in events if event["event_seq"] == parent_cursor["event_seq"]),
                    None,
                )
                if (
                    cursor_event is None
                    or cursor_event["event_id"] != parent_cursor["event_id"]
                    or cursor_event["branch_id"] != parent_branch_id
                ):
                    raise ValueError("parent_cursor must identify an event owned by the selected parent branch")
                parent_projection = project_events(events, branch_id=parent_branch_id, session_id=session_id)
                if parent_projection["applied_events"].get(cursor_event["event_id"]) != {
                    "branch_id": parent_branch_id,
                    "event_seq": parent_cursor["event_seq"],
                }:
                    raise ValueError("parent_cursor event is not visible on the selected parent branch")

            stable_id = event_id or "evt_" + hashlib.sha256(f"branch-fork:{branch_id}".encode("utf-8")).hexdigest()[:32]
            return self.append_event(
                session_id,
                "branch.forked",
                {"parent_branch_id": parent_branch_id, "parent_cursor": copy.deepcopy(parent_cursor)},
                event_id=stable_id,
                branch_id=branch_id,
            )

    def find_submission(self, session_id: str, submission_id: str) -> dict[str, Any] | None:
        if not isinstance(submission_id, str) or not submission_id:
            raise ValueError("submission_id must be a non-empty string")
        with self._lock:
            self._load_index(session_id)
            for event in self._records[session_id].values():
                if event["event_type"] == "message.accepted" and (event.get("payload") or {}).get("submission_id") == submission_id:
                    return copy.deepcopy(event)
            return None

    @staticmethod
    def _event_to_projection(event: dict[str, Any]) -> dict[str, Any] | None:
        payload = event.get("payload") or {}
        if not {"message_id", "timestamp", "message"}.issubset(payload):
            return None
        row = {key: copy.deepcopy(payload[key]) for key in
               ("message_id", "timestamp", "message", "turn_id", "model", "execution", "interaction", "extensions")
               if key in payload}
        row["seq"] = event["event_seq"]
        row["event_id"] = event["event_id"]
        row["event_type"] = event["event_type"]
        row["branch_id"] = event["branch_id"]
        return row

    @staticmethod
    def _event_type_for_row(row: dict[str, Any]) -> str:
        role = (row.get("message") or {}).get("role")
        subject = lifecycle_subject(row)
        execution = row.get("execution") or {}
        status = execution.get("status")
        if role == "user" and row.get("interaction"):
            return "interaction.resolved"
        if role == "user":
            return "message.accepted"
        if role == "assistant":
            return "model.response.completed"
        if role == "tool":
            return "tool.attempt.settled"
        if subject == "model_request":
            if status == "running":
                return "model.request.started"
            if status == "unknown":
                return "model.request.interrupted"
            if status in {"failed", "cancelled"}:
                return "model.request.failed"
            return "model.response.completed"
        if subject == "tool":
            return "tool.attempt.started" if status == "running" else "tool.attempt.settled"
        if subject == "interaction":
            return "interaction.opened" if status == "waiting" else "interaction.resolved"
        if subject in {"task", "step"}:
            return f"{subject}.started" if status in {"pending", "running"} else f"{subject}.settled"
        if subject == "activity":
            return "activity.started" if status in {"pending", "running"} else "activity.settled"
        if subject == "turn":
            return "turn.settled"
        if subject == "context":
            return "context.window.materialized"
        raise ValueError(f"cannot map message projection to a durable event type: {subject or role}")

    def append(self, session_id: str, message_id: str, message: dict[str, Any], *, turn_id: str | None = None,
               timestamp: int | None = None, model: dict | None = None, execution: dict | None = None,
               interaction: dict | None = None, extensions: dict | None = None,
               submission_id: str | None = None, branch_id: str = "main", event_id: str | None = None) -> tuple[int, bool]:
        with self._lock:
            self.ensure_metadata(session_id)
            self._load_index(session_id)
            prior = next((self._event_to_projection(event) for event in self._records[session_id].values()
                          if (event.get("payload") or {}).get("message_id") == message_id), None)
            row = {"seq": len(self._records[session_id]) + 1, "message_id": message_id,
                   "message": safe_business_value(message),
                   "timestamp": timestamp if timestamp is not None else prior["timestamp"] if prior else utc_milliseconds()}
            for key, value in (("turn_id", turn_id), ("model", model), ("execution", execution),
                               ("interaction", interaction), ("extensions", extensions)):
                if value is not None:
                    row[key] = copy.deepcopy(value) if key == "model" else safe_business_value(value)
            validate_row(row)
            offloaded = {**row, "message": self._offload_content(session_id, message_id, row["message"])}
            event_type = self._event_type_for_row(offloaded)
            payload = {key: value for key, value in offloaded.items() if key != "seq"}
            if event_type == "message.accepted":
                payload["submission_id"] = submission_id or message_id
                existing_submission = self.find_submission(session_id, payload["submission_id"])
                if existing_submission:
                    prior_payload = existing_submission["payload"]
                    prior_message = self.resolve_message(session_id, prior_payload.get("message") or {})
                    new_message = self.resolve_message(session_id, payload.get("message") or {})
                    if (
                        prior_message.get("content") != new_message.get("content")
                        or prior_payload.get("message_id") != message_id
                        or existing_submission.get("turn_id") != turn_id
                    ):
                        raise ValueError(f"submission_id integrity conflict: {payload['submission_id']}")
                    return existing_submission["event_seq"], False
            if event_type == "context.window.materialized":
                context = (offloaded.get("extensions") or {}).get("context") or {}
                for field in ("window_schema_version", "compression_version", "content_sha256",
                              "source_event_cursor", "bootstrap_fingerprint", "reference_fingerprint", "window_ref"):
                    if field in context:
                        payload[field] = copy.deepcopy(context[field])
                self._verify_references(session_id, offloaded)
            ext_runtime = (offloaded.get("extensions") or {}).get("runtime") or {}
            if event_type == "turn.settled":
                for field in ("final_response_event_id", "final_message_id", "reason_event_id"):
                    if ext_runtime.get(field):
                        payload[field] = ext_runtime[field]
            model_value = offloaded.get("model") or {}
            execution_value = offloaded.get("execution") or {}
            interaction_value = offloaded.get("interaction") or {}
            stable_event_id = event_id or "evt_" + hashlib.sha256(f"message:{message_id}".encode("utf-8")).hexdigest()[:32]
            event_seq, created = self.append_event(
                session_id, event_type, payload, event_id=stable_event_id, turn_id=turn_id, branch_id=branch_id,
                occurred_at=datetime.fromtimestamp(offloaded["timestamp"] / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                activity_id=ext_runtime.get("activity_id"), parent_activity_id=ext_runtime.get("parent_activity_id"),
                causation_event_id=ext_runtime.get("causation_event_id"), request_id=model_value.get("request_id"),
                batch_id=ext_runtime.get("batch_id"),
                call_id=(offloaded.get("message") or {}).get("tool_call_id") or ext_runtime.get("call_id"),
                attempt_id=ext_runtime.get("attempt_id") or (f"attempt-{execution_value['attempt']}" if execution_value.get("attempt") else None),
                interaction_id=interaction_value.get("id"),
            )
            return event_seq, created

    def append_messages(self, session_id: str, turn_id: str | None, messages: list[BaseMessage], *, scope: dict | None = None,
                        submission_id: str | None = None, branch_id: str = "main",
                        model: dict | None = None, execution: dict | None = None, interaction: dict | None = None, extensions: dict | None = None) -> int:
        if scope is not None:
            validate_scope(scope, session_id=session_id)
            if scope["turn_id"] != turn_id or scope["workspace_id"] != self.workspace_id:
                raise ValueError("ledger scope does not match container/turn")
        with self._lock:
            self.ensure_metadata(session_id)
            self._load_index(session_id)
            for message in messages:
                if is_ephemeral_prompt_frame(message):
                    continue
                kwargs = message.additional_kwargs
                message_id = kwargs.setdefault("ledger_message_id", f"msg_{uuid.uuid4().hex}")
                timestamp = kwargs.setdefault("ledger_timestamp", kwargs.get("timestamp", utc_milliseconds()))
                values = encode_message(message)
                for name, value in (("model", model), ("execution", execution), ("interaction", interaction), ("extensions", extensions)):
                    if value is not None and not (name == "model" and values["message"]["role"] not in {"assistant", "system"}):
                        values[name] = {**values.get(name, {}), **copy.deepcopy(value)}
                        kwargs[f"ledger_{name}"] = copy.deepcopy(values[name])
                values["message"] = self._offload_content(session_id, message_id, values["message"])
                current_submission = submission_id if values.get("message", {}).get("role") == "user" else None
                self.append(session_id, message_id, turn_id=turn_id, timestamp=timestamp,
                            submission_id=current_submission, branch_id=branch_id, **values)
            return len(self._records[session_id])

    def visible_events(self, session_id: str, *, branch_id: str = "main", after_seq: int = 0) -> list[dict[str, Any]]:
        """Read only the selected branch's immutable lineage, retaining physical order."""
        from app.storage.event_projection import _branch_visibility
        events = self.read_events(session_id)
        limits = dict(_branch_visibility(events, branch_id))
        return [event for event in events if event["branch_id"] in limits
                and (limits[event["branch_id"]] is None or event["event_seq"] <= limits[event["branch_id"]])
                and event["event_seq"] > after_seq]

    def read_events(self, session_id: str, *, after_seq: int = 0, branch_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._load_index(session_id)
            events = [copy.deepcopy(event) for event in self._records[session_id].values()
                      if event["event_seq"] > after_seq and (branch_id is None or event["branch_id"] == branch_id)]
            for event in events:
                self._verify_event_references(session_id, event)
            return events

    def read(self, session_id: str, *, after_seq: int = 0, branch_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._load_index(session_id)
            rows = []
            visible = {event["event_id"] for event in self.visible_events(session_id, branch_id=branch_id)} if branch_id is not None else None
            for event in self._records[session_id].values():
                if event["event_seq"] <= after_seq:
                    continue
                if visible is not None and event["event_id"] not in visible:
                    continue
                self._verify_event_references(session_id, event)
                row = self._event_to_projection(event)
                if row is not None:
                    rows.append(row)
            return rows

    def row_at(self, session_id: str, seq: int) -> dict[str, Any] | None:
        with self._lock:
            self._load_index(session_id)
            offsets = self._offsets[session_id]
            if type(seq) is not int or seq <= 0 or seq >= len(offsets):
                return None
            with self.ledger_path(session_id).open("rb") as stream:
                stream.seek(offsets[seq - 1])
                event = validate_event(json.loads(stream.readline()), session_id=session_id, expected_seq=seq)
            self._verify_event_references(session_id, event)
            return self._event_to_projection(event)

    def read_tail(self, session_id: str, *, after_seq: int = 0, offset: int | None = None,
                  branch_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._load_index(session_id)
            offsets = self._offsets[session_id]
            if type(after_seq) is not int or after_seq < 0 or after_seq >= len(offsets):
                raise ValueError("ledger cursor is beyond the durable ledger")
            # Ignore incorrect offset caches; seq always determines the boundary.
            start = offsets[after_seq]
            path = self.ledger_path(session_id)
            if not path.exists():
                return []
            rows = []
            visible = {event["event_id"] for event in self.visible_events(session_id, branch_id=branch_id)} if branch_id is not None else None
            with path.open("rb") as stream:
                stream.seek(start)
                for line in stream:
                    event = validate_event(json.loads(line), session_id=session_id)
                    if visible is not None and event["event_id"] not in visible:
                        continue
                    self._verify_event_references(session_id, event)
                    row = self._event_to_projection(event)
                    if row is not None:
                        row["_end_offset"] = stream.tell()
                        rows.append(row)
            return rows

    def offset_after(self, session_id: str, seq: int) -> int:
        with self._lock:
            self._load_index(session_id)
            if type(seq) is not int or seq < 0 or seq >= len(self._offsets[session_id]):
                raise ValueError("ledger cursor is beyond the durable ledger")
            return self._offsets[session_id][seq]

    def latest_seq(self, session_id: str) -> int:
        with self._lock:
            self._load_index(session_id)
            return len(self._records[session_id])

    def ledger_pointer(self, session_id: str, seq: int | None = None, *, branch_id: str = "main") -> dict[str, Any]:
        """Build a checkpoint cursor that binds the physical sequence to its event identity."""
        with self._lock:
            self._load_index(session_id)
            visible = self.visible_events(session_id, branch_id=branch_id)
            current = (visible[-1]["event_seq"] if visible else 0) if seq is None else seq
            if type(current) is not int or current < 0 or current > len(self._records[session_id]):
                raise ValueError("ledger cursor is beyond the durable ledger")
            if current == 0:
                cursor = {"branch_id": branch_id, "event_seq": 0, "event_id": "origin"}
            else:
                event = next((item for item in visible if item["event_seq"] == current), None)
                if event is None:
                    raise ValueError("ledger cursor is not visible on the selected branch")
                cursor = {"branch_id": branch_id, "event_seq": current, "event_id": event["event_id"]}
            return {"event_cursor": cursor, "applied_seq": current,
                    "applied_offset": self._offsets[session_id][current]}

    def message_by_id(self, session_id: str, message_id: str) -> dict | None:
        with self._lock:
            self._load_index(session_id)
            event = next((item for item in self._records[session_id].values()
                          if (item.get("payload") or {}).get("message_id") == message_id), None)
            return self._event_to_projection(event) if event else None

    def has_sequence_anomalies(self, session_id: str) -> bool:
        self._load_index(session_id)
        return False

    def decode_row(self, row: dict, *, session_id: str | None = None, resolve_references: bool = False) -> BaseMessage:
        value = copy.deepcopy(row)
        value.pop("_end_offset", None)
        if resolve_references:
            if not session_id:
                raise ValueError("session_id required to resolve content references")
            value["message"] = self.resolve_message(session_id, value["message"])
        return decode_message(value)

    def messages(self, session_id: str, *, include_audit: bool = False) -> list[BaseMessage]:
        return [self.decode_row(row, session_id=session_id, resolve_references=True) for row in self.read(session_id)
                if include_audit or not is_audit_row(row)]

    def _resource_path(self, session_id: str, pointer: str) -> Path:
        if not isinstance(pointer, str) or "\\" in pointer or ":" in pointer:
            raise ValueError("invalid content reference path")
        relative = PurePosixPath(pointer)
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2 or relative.parts[0] not in {"tool-results", "content"}:
            raise ValueError("content reference outside controlled session resources")
        root = self.session_dir(session_id).resolve()
        path = root.joinpath(*relative.parts)
        if not path.resolve().is_relative_to(root):
            raise ValueError("content reference escapes session resources")
        if os.name == "nt" and len(str(path)) >= 240 and not str(path).startswith("\\\\?\\"):
            # Content-addressed resources can exceed Win32's legacy path limit
            # under a long runtime root. Validate first, then use extended paths.
            path = Path("\\\\?\\" + str(path))
        return path

    def write_content(self, session_id: str, pointer: str, content: str | bytes | list, *, media_type: str | None = None) -> dict:
        path = self._resource_path(session_id, pointer)
        if isinstance(content, list):
            data = json.dumps(safe_business_value(content), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            media_type = media_type or "application/json"
        elif isinstance(content, str):
            data = safe_business_value(content).encode("utf-8")
            media_type = media_type or "text/plain"
        else:
            data = content
            media_type = media_type or "application/octet-stream"
        with self._lock:
            self.ensure_metadata(session_id)
            if path.exists():
                if path.read_bytes() != data:
                    raise ValueError("immutable content reference conflict")
            else:
                _atomic_write(path, data)
        return {"type": "reference", "path": pointer, "media_type": media_type, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def read_reference(self, session_id: str, reference: dict) -> bytes:
        try:
            data = self._resource_path(session_id, reference["path"]).read_bytes()
        except OSError as exc:
            raise ValueError(f"content reference unavailable: {reference['path']}") from exc
        if len(data) != reference["bytes"] or hashlib.sha256(data).hexdigest() != reference["sha256"]:
            raise ValueError(f"content reference integrity error: {reference['path']}")
        return data

    def _load_context_window(self, session_id: str, context: dict):
        from app.storage.model_window import ModelWindowStore
        window = ModelWindowStore(self).load(
            session_id, context["window_ref"],
            expected_source_event_cursor=context.get("source_event_cursor"),
            expected_bootstrap_fingerprint=context.get("bootstrap_fingerprint"),
            expected_reference_fingerprint=context.get("reference_fingerprint"),
            expected_window_schema_version=context.get("window_schema_version"),
            expected_compression_version=context.get("compression_version"),
        )
        if window.content_sha256 != context.get("content_sha256"):
            raise ValueError("context window content hash does not match its ledger event")
        return window

    def _verify_event_references(self, session_id: str, event: dict) -> None:
        projection = self._event_to_projection(event)
        if projection is not None:
            self._verify_references(session_id, projection)
        if event["event_type"] == "context.window.materialized":
            self._load_context_window(session_id, event["payload"])

    def _verify_references(self, session_id: str, row: dict) -> None:
        content = row["message"].get("content")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "reference":
                    self.read_reference(session_id, block)
        pointer = ((row.get("extensions") or {}).get("context") or {}).get("summary_ref")
        if pointer:
            self.read_summary(session_id, pointer)
        context = ((row.get("extensions") or {}).get("context") or {})
        if context.get("window_ref"):
            self._load_context_window(session_id, context)

    def _offload_content(self, session_id: str, message_id: str, message: dict) -> dict:
        content = message.get("content")
        if content is None or (isinstance(content, list) and any(block.get("type") == "reference" for block in content)):
            return message
        encoded = content.encode("utf-8") if isinstance(content, str) else json.dumps(content, ensure_ascii=False).encode("utf-8")
        if len(encoded) <= INLINE_CONTENT_BYTES:
            return message
        digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
        pointer = f"tool-results/{digest}.txt" if isinstance(content, str) else f"content/{digest}.json"
        return {**message, "content": [self.write_content(session_id, pointer, content)]}

    def resolve_message(self, session_id: str, message: dict) -> dict:
        content = message.get("content")
        if not isinstance(content, list) or not content or content[0].get("type") != "reference":
            return copy.deepcopy(message)
        reference = content[0]
        data = self.read_reference(session_id, reference)
        try:
            value = json.loads(data) if reference["media_type"] == "application/json" else data.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("referenced message content cannot be decoded") from exc
        if not isinstance(value, (str, list)):
            raise ValueError("referenced message must contain text or content blocks")
        return {**copy.deepcopy(message), "content": value}

    def summary_path(self, session_id: str, summary_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", summary_id):
            raise ValueError("非法 summary id")
        return self.session_dir(session_id) / "summaries" / f"{summary_id}.json"

    def write_summary(self, session_id: str, summary_id: str, summary: dict) -> str:
        with self._lock:
            self.ensure_metadata(session_id)
            path = self.summary_path(session_id, summary_id)
            _atomic_write(path, json.dumps(safe_business_value(summary), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            return path.relative_to(self.session_dir(session_id)).as_posix()

    def read_summary(self, session_id: str, pointer: str) -> dict:
        relative = Path(pointer)
        if relative.is_absolute() or ".." in relative.parts or relative.parent != Path("summaries"):
            raise ValueError("非法 summary pointer")
        path = self.session_dir(session_id) / relative
        if not path.resolve().is_relative_to(self.session_dir(session_id).resolve()):
            raise ValueError("非法 summary pointer")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"上下文摘要不可用: {pointer}") from exc
        if not isinstance(value.get("content"), str):
            raise ValueError(f"上下文摘要格式无效: {pointer}")
        return value

    def export_files(self, session_id: str) -> dict[str, bytes]:
        with self._lock:
            self.read_metadata(session_id)
            events = self.read_events(session_id)
            rows = [row for event in events if (row := self._event_to_projection(event)) is not None]
            paths = {"ledger_meta.json": self.metadata_path(session_id)}
            if self.ledger_path(session_id).exists():
                paths["ledger.jsonl"] = self.ledger_path(session_id)
            for row in rows:
                content = row["message"].get("content")
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "reference":
                            paths[block["path"]] = self._resource_path(session_id, block["path"])
                pointer = ((row.get("extensions") or {}).get("context") or {}).get("summary_ref")
                if pointer:
                    self.read_summary(session_id, pointer)
                    paths[pointer] = self.session_dir(session_id) / pointer
            window_contexts = [event["payload"] for event in events if event["event_type"] == "context.window.materialized"]
            window_contexts.extend(context for row in rows
                                   if (context := (row.get("extensions") or {}).get("context") or {}).get("window_ref"))
            for context in window_contexts:
                window = self._load_context_window(session_id, context)
                paths[window.reference["path"]] = self._resource_path(session_id, window.reference["path"])
                for encoded in window.messages:
                    content = (encoded.get("message") or {}).get("content")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "reference":
                                self.read_reference(session_id, block)
                                paths[block["path"]] = self._resource_path(session_id, block["path"])
                if context.get("summary_ref"):
                    self.read_summary(session_id, context["summary_ref"])
                    paths[context["summary_ref"]] = self.session_dir(session_id) / context["summary_ref"]
            for path in (self.session_dir(session_id) / "summaries").glob("*.json"):
                if not path.resolve().is_relative_to(self.session_dir(session_id).resolve()):
                    raise ValueError("summary resource escapes session directory")
                paths[f"summaries/{path.name}"] = path
            return {name: path.read_bytes() for name, path in paths.items()}

    def delete_session(self, session_id: str) -> None:
        import shutil
        with self._lock:
            path = self.session_dir(session_id)
            if path.exists():
                shutil.rmtree(path)
            self._records.pop(session_id, None)
            self._offsets.pop(session_id, None)
            self._signatures.pop(session_id, None)
