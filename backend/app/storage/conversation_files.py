"""Durable single-message ledger and verifiable session resources."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from langchain_core.messages import BaseMessage
from app.agent.messages import is_ephemeral_prompt_frame
from app.storage.ledger_contract import LEDGER_SCHEMA_VERSION, is_audit_row, validate_metadata, validate_row, validate_scope
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
            value = {"schema_version": LEDGER_SCHEMA_VERSION, "workspace_id": self.workspace_id, "session_id": session_id}
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
                        row = validate_row(json.loads(line))
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise ValueError(f"JSONL 账本第 {line_no} 行格式无效: {exc}") from exc
                    if row["seq"] != line_no:
                        raise ValueError(f"ledger sequence integrity error at line {line_no}")
                    if row["message_id"] in records:
                        raise ValueError(f"ledger duplicate message identity at line {line_no}")
                    records[row["message_id"]] = row
                    offsets.append(stream.tell())
        self._records[session_id], self._offsets[session_id] = records, offsets
        self._signatures[session_id] = self._signature(session_id)

    def append(self, session_id: str, message_id: str, message: dict[str, Any], *, turn_id: str | None = None,
               timestamp: int | None = None, model: dict | None = None, execution: dict | None = None,
               interaction: dict | None = None, extensions: dict | None = None) -> tuple[int, bool]:
        with self._lock:
            self.ensure_metadata(session_id)
            self._load_index(session_id)
            existing = self._records[session_id].get(message_id)
            row = {"seq": len(self._records[session_id]) + 1, "message_id": message_id, "message": safe_business_value(message),
                   "timestamp": timestamp if timestamp is not None else existing["timestamp"] if existing else utc_milliseconds()}
            for key, value in (("turn_id", turn_id), ("model", model), ("execution", execution), ("interaction", interaction), ("extensions", extensions)):
                if value is not None:
                    row[key] = copy.deepcopy(value) if key == "model" else safe_business_value(value)
            validate_row(row)
            if existing:
                if {**row, "seq": existing["seq"]} != existing:
                    raise ValueError(f"ledger message_id integrity conflict: {message_id}")
                self._verify_references(session_id, existing)
                return existing["seq"], False
            self._verify_references(session_id, row)
            data = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            with self.ledger_path(session_id).open("ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                end_offset = stream.tell()
            self._records[session_id][message_id] = row
            self._offsets[session_id].append(end_offset)
            self._signatures[session_id] = self._signature(session_id)
            return row["seq"], True

    def append_messages(self, session_id: str, turn_id: str | None, messages: list[BaseMessage], *, scope: dict | None = None,
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
                self.append(session_id, message_id, turn_id=turn_id, timestamp=timestamp, **values)
            return len(self._records[session_id])

    def read(self, session_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            self._load_index(session_id)
            rows = [copy.deepcopy(row) for row in self._records[session_id].values() if row["seq"] > after_seq]
            for row in rows:
                self._verify_references(session_id, row)
            return rows

    def row_at(self, session_id: str, seq: int) -> dict[str, Any] | None:
        with self._lock:
            self._load_index(session_id)
            offsets = self._offsets[session_id]
            if type(seq) is not int or seq <= 0 or seq >= len(offsets):
                return None
            with self.ledger_path(session_id).open("rb") as stream:
                stream.seek(offsets[seq - 1])
                row = validate_row(json.loads(stream.readline()))
            self._verify_references(session_id, row)
            return row

    def read_tail(self, session_id: str, *, after_seq: int = 0, offset: int | None = None) -> list[dict[str, Any]]:
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
            with path.open("rb") as stream:
                stream.seek(start)
                for line in stream:
                    row = validate_row(json.loads(line))
                    self._verify_references(session_id, row)
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

    def message_by_id(self, session_id: str, message_id: str) -> dict | None:
        with self._lock:
            self._load_index(session_id)
            row = self._records[session_id].get(message_id)
            return copy.deepcopy(row) if row else None

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

    def _verify_references(self, session_id: str, row: dict) -> None:
        content = row["message"].get("content")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "reference":
                    self.read_reference(session_id, block)
        pointer = ((row.get("extensions") or {}).get("context") or {}).get("summary_ref")
        if pointer:
            self.read_summary(session_id, pointer)

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
            rows = self.read(session_id)
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
