"""Per-session append-only ledger, summary and history projection files."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, message_to_dict, messages_from_dict
from app.agent.messages import is_ephemeral_prompt_frame, is_system_maintenance
from app.storage.ledger_contract import validate_row, validate_scope


_SESSION_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")


class SessionConversationFiles:
    # ChatService and the compiled graph each own an instance for the same
    # workspace, so appends and in-memory indexes must be coordinated across them.
    _process_lock = threading.RLock()

    def __init__(self, agent_home: Path):
        self.root = agent_home / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = self._process_lock
        self._ids: dict[str, set[str]] = {}
        self._seq: dict[str, int] = {}
        self._offsets: dict[str, list[int]] = {}

    def session_dir(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("非法 session id")
        path = self.root / session_id
        if path.resolve().parent != self.root.resolve():
            raise ValueError("非法 session id")
        return path

    def ledger_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "ledger.jsonl"

    def _load_index(self, session_id: str) -> None:
        if session_id in self._ids:
            path = self.ledger_path(session_id)
            indexed_size = self._offsets[session_id][-1]
            actual_size = path.stat().st_size if path.exists() else 0
            if actual_size == indexed_size:
                return
            # Another SessionConversationFiles instance appended since this
            # instance last refreshed its cache.
            self._ids.pop(session_id, None)
            self._seq.pop(session_id, None)
            self._offsets.pop(session_id, None)
        ids: set[str] = set()
        seq = 0
        offsets = [0]
        path = self.ledger_path(session_id)
        if path.exists():
            # A process can die while appending the final JSONL row. Discard only
            # an unterminated final fragment; a newline-terminated bad row remains
            # an integrity error and is never silently rewritten.
            with path.open("r+b") as raw:
                raw.seek(0, os.SEEK_END)
                size = raw.tell()
                if size:
                    raw.seek(-1, os.SEEK_END)
                    if raw.read(1) != b"\n":
                        position = size
                        truncate_at = 0
                        while position > 0:
                            start = max(0, position - 64 * 1024)
                            raw.seek(start)
                            block = raw.read(position - start)
                            index = block.rfind(b"\n")
                            if index >= 0:
                                truncate_at = start + index + 1
                                break
                            position = start
                        raw.truncate(truncate_at)
            with path.open("r", encoding="utf-8") as stream:
                line_no = 0
                while line := stream.readline():
                    line_no += 1
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"JSONL 账本第 {line_no} 行损坏") from exc
                    try:
                        validate_row(row, session_id=session_id)
                    except ValueError as exc:
                        raise ValueError(f"JSONL 账本第 {line_no} 行格式无效: {exc}") from exc
                    message_id = row["message_id"]
                    row_seq = row["seq"]
                    ids.add(message_id)
                    # Preserve malformed sequence history for diagnostics; new
                    # appends continue after the highest durable sequence.
                    seq = max(seq, row_seq)
                    offsets.append(stream.tell())
        self._ids[session_id] = ids
        self._seq[session_id] = seq
        self._offsets[session_id] = offsets

    def append(self, session_id: str, message_id: str, message: dict[str, Any], *, scope: dict[str, str | None], origin: str) -> tuple[int, bool]:
        if not message_id:
            raise ValueError("message_id 不能为空")
        validate_scope(scope, session_id=session_id)
        with self._lock:
            directory = self.session_dir(session_id)
            directory.mkdir(parents=True, exist_ok=True)
            self._load_index(session_id)
            if message_id in self._ids[session_id]:
                return self._seq[session_id], False
            seq = self._seq[session_id] + 1
            row = validate_row({"seq": seq, "message_id": message_id, "scope": scope, "origin": origin, "message": message}, session_id=session_id)
            encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            with self.ledger_path(session_id).open("a", encoding="utf-8", newline="\n") as stream:
                offset = stream.tell()
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self._ids[session_id].add(message_id)
            self._seq[session_id] = seq
            self._offsets[session_id].append(offset + len(encoded.encode("utf-8")))
            return seq, True

    def append_messages(
        self, session_id: str, run_id: str, messages: list[BaseMessage], *, scope: dict[str, str | None]
    ) -> int:
        with self._lock:
            self._load_index(session_id)
            last_seq = self._seq[session_id]
            validate_scope(scope, session_id=session_id)
            if scope["run_id"] != run_id:
                raise ValueError("ledger run_id does not match scope")
            for index, message in enumerate(messages):
                if is_ephemeral_prompt_frame(message):
                    continue
                origin = "system_maintenance" if is_system_maintenance(message) else {
                    "human": "user", "ai": "assistant", "tool": "tool"
                }.get(message.type)
                if origin is None:
                    if (getattr(message, "additional_kwargs", {}) or {}).get("system_maintenance"):
                        origin = "system_maintenance"
                    else:
                        raise ValueError("ledger message has no durable origin")
                vendor_id = str(message.id or "").strip()
                message_id = str((getattr(message, "additional_kwargs", {}) or {}).get("ledger_message_id") or "").strip()
                if not message_id:
                    identity = f"{run_id}:{scope.get('step_id')}:{index}:{message.type}:{getattr(message, 'tool_call_id', '')}:{vendor_id}:{message.content}"
                    message_id = f"msg_{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex}"
                    message.additional_kwargs["ledger_message_id"] = message_id
                serialized = message_to_dict(message)
                last_seq, _added = self.append(session_id, message_id, serialized, scope=scope, origin=origin)
            return last_seq

    def messages(self, session_id: str) -> list[BaseMessage]:
        rows = self.read(session_id)
        return messages_from_dict([row["message"] for row in rows])

    def read(self, session_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        path = self.ledger_path(session_id)
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        with path.open("r", encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"JSONL 账本第 {line_no} 行损坏") from exc
                try:
                    validate_row(row, session_id=session_id)
                except ValueError as exc:
                    raise ValueError(f"JSONL 账本第 {line_no} 行格式无效: {exc}") from exc
                if row["message_id"] in seen_ids:
                    continue
                seen_ids.add(row["message_id"])
                if row["seq"] > after_seq:
                    rows.append(row)
        return rows

    def read_tail(self, session_id: str, *, after_seq: int = 0, offset: int | None = None) -> list[dict[str, Any]]:
        """Read only rows after a checkpointed byte offset (or indexed sequence)."""
        path = self.ledger_path(session_id)
        if not path.exists():
            return []
        if offset is None:
            self._load_index(session_id)
            offsets = self._offsets[session_id]
            start = offsets[min(max(after_seq, 0), len(offsets) - 1)]
        else:
            start = max(0, int(offset))
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as stream:
            stream.seek(start)
            line_no = after_seq
            while line := stream.readline():
                line_no += 1
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"JSONL 账本第 {line_no} 行损坏") from exc
                try:
                    validate_row(row, session_id=session_id)
                except ValueError as exc:
                    raise ValueError(f"JSONL 账本第 {line_no} 行格式无效: {exc}") from exc
                row["_end_offset"] = stream.tell()
                rows.append(row)
        return rows

    def offset_after(self, session_id: str, seq: int) -> int:
        self._load_index(session_id)
        offsets = self._offsets[session_id]
        if int(seq) >= self._seq[session_id]:
            return offsets[-1]
        return offsets[min(max(int(seq), 0), len(offsets) - 1)]

    def latest_seq(self, session_id: str) -> int:
        with self._lock:
            self._load_index(session_id)
            return self._seq.get(session_id, 0)

    def has_sequence_anomalies(self, session_id: str) -> bool:
        previous = 0
        for row in self.read(session_id):
            current = row.get("seq")
            if not isinstance(current, int) or current <= previous:
                return True
            previous = current
        return False

    def summary_path(self, session_id: str, summary_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", summary_id):
            raise ValueError("非法 summary id")
        return self.session_dir(session_id) / "summaries" / f"{summary_id}.json"

    def write_summary(self, session_id: str, summary_id: str, summary: dict[str, Any]) -> str:
        path = self.summary_path(session_id, summary_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(summary, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return str(path.relative_to(self.session_dir(session_id)))

    def read_summary(self, session_id: str, pointer: str) -> dict[str, Any]:
        relative = Path(pointer)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("非法 summary pointer")
        path = self.session_dir(session_id) / relative
        if path.parent != (self.session_dir(session_id) / "summaries"):
            raise ValueError("非法 summary pointer")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"上下文摘要不可用: {pointer}") from exc
        if not isinstance(payload.get("content"), str):
            raise ValueError(f"上下文摘要格式无效: {pointer}")
        return payload

    def delete_session(self, session_id: str) -> None:
        import shutil

        path = self.session_dir(session_id)
        if path.exists():
            shutil.rmtree(path)
        self._ids.pop(session_id, None)
        self._seq.pop(session_id, None)
        self._offsets.pop(session_id, None)
