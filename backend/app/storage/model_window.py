"""Bounded, independently persisted model working windows."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from langchain_core.messages import BaseMessage

from app.storage.conversation_files import SessionConversationFiles
from app.storage.message_codec import encode_message, safe_business_value

MODEL_WINDOW_SCHEMA_VERSION = 1
DEFAULT_MAX_WINDOW_BYTES = 1_048_576
DEFAULT_MAX_WINDOW_MESSAGES = 512
_MAX_CONFIGURED_BYTES = 16 * 1_048_576
_MAX_CONFIGURED_MESSAGES = 4096
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_WINDOW_PATH = re.compile(r"^content/windows/([0-9a-f]{16})-([0-9a-f]{64})\.json$")
_REFERENCE_KEYS = {"type", "path", "media_type", "bytes", "sha256"}
_DOCUMENT_KEYS = {
    "schema_version", "window_schema_version", "compression_version", "branch_id",
    "source_event_cursor", "content_sha256", "bootstrap_fingerprint",
    "reference_fingerprint", "messages",
}


class StaleModelWindowError(ValueError):
    """The stored window is valid but does not match the requested provenance."""


@dataclass(frozen=True)
class ModelWindow:
    """Verified encoded window and its content-addressed resource reference."""

    reference: dict[str, Any]
    window_schema_version: str
    compression_version: str
    branch_id: str
    source_event_cursor: dict[str, Any]
    content_sha256: str
    bootstrap_fingerprint: str
    reference_fingerprint: str
    messages: list[dict[str, Any]]


class ModelWindowStore:
    """Persist and validate bounded encoded model context separately from the ledger."""

    def __init__(
        self,
        files: SessionConversationFiles,
        *,
        max_bytes: int = DEFAULT_MAX_WINDOW_BYTES,
        max_messages: int = DEFAULT_MAX_WINDOW_MESSAGES,
    ) -> None:
        if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_CONFIGURED_BYTES:
            raise ValueError(f"max_bytes must be between 1 and {_MAX_CONFIGURED_BYTES}")
        if type(max_messages) is not int or not 1 <= max_messages <= _MAX_CONFIGURED_MESSAGES:
            raise ValueError(f"max_messages must be between 1 and {_MAX_CONFIGURED_MESSAGES}")
        self.files = files
        self.max_bytes = max_bytes
        self.max_messages = max_messages

    @staticmethod
    def _cursor(value: Any) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or set(value) != {"branch_id", "event_seq", "event_id"}
            or not isinstance(value.get("branch_id"), str)
            or not value["branch_id"]
            or len(value["branch_id"]) > 256
            or type(value.get("event_seq")) is not int
            or value["event_seq"] < 0
            or not isinstance(value.get("event_id"), str)
            or not value["event_id"]
            or len(value["event_id"]) > 256
            or (value["event_seq"] == 0) != (value["event_id"] == "origin")
        ):
            raise ValueError("source_event_cursor must identify a branch event or origin")
        return dict(value)

    @staticmethod
    def _fingerprint(value: Any, field: str) -> str:
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ValueError(f"{field} must be a lowercase SHA-256 digest")
        return value

    @staticmethod
    def _version(value: Any, field: str) -> str:
        if not isinstance(value, str) or not _VERSION.fullmatch(value):
            raise ValueError(f"{field} must be a version identifier")
        return value

    def _messages(self, messages: Iterable[BaseMessage | dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in messages:
            if isinstance(item, BaseMessage):
                encoded = encode_message(item)
            elif isinstance(item, dict):
                encoded = item
            else:
                raise ValueError("window messages must be encoded messages or BaseMessage objects")
            safe = safe_business_value(encoded)
            if not isinstance(safe, dict) or not isinstance(safe.get("message"), dict):
                raise ValueError("window message has an invalid encoded shape")
            role = safe["message"].get("role")
            if role not in {"user", "assistant", "tool", "system"}:
                raise ValueError("window message role must be user/assistant/tool/system")
            result.append(safe)
            if len(result) > self.max_messages:
                raise ValueError("model window exceeds configured message limit")
        return result

    def _validate_references(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        total = 0
        for encoded in messages:
            content = (encoded.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "reference":
                    continue
                if (
                    set(block) != _REFERENCE_KEYS
                    or not isinstance(block.get("path"), str)
                    or not isinstance(block.get("media_type"), str)
                    or not isinstance(block.get("sha256"), str)
                    or not _SHA256.fullmatch(block["sha256"])
                ):
                    raise ValueError("invalid model window content reference")
                size = block.get("bytes")
                if type(size) is not int or size < 0 or size > self.max_bytes:
                    raise ValueError("model window reference exceeds configured byte limit")
                total += size
                if total > self.max_bytes:
                    raise ValueError("model window references exceed configured byte limit")
                self.files.read_reference(session_id, block)

    def save(
        self,
        session_id: str,
        messages: Iterable[BaseMessage | dict[str, Any]],
        *,
        source_event_cursor: dict[str, Any],
        window_schema_version: str,
        compression_version: str,
        bootstrap_fingerprint: str,
        reference_fingerprint: str,
    ) -> ModelWindow:
        cursor = self._cursor(source_event_cursor)
        messages_value = self._messages(messages)
        self._validate_references(session_id, messages_value)
        message_bytes = self._canonical(messages_value)
        content_sha256 = hashlib.sha256(message_bytes).hexdigest()
        document = {
            "schema_version": MODEL_WINDOW_SCHEMA_VERSION,
            "window_schema_version": self._version(window_schema_version, "window_schema_version"),
            "compression_version": self._version(compression_version, "compression_version"),
            "branch_id": cursor["branch_id"],
            "source_event_cursor": cursor,
            "content_sha256": content_sha256,
            "bootstrap_fingerprint": self._fingerprint(bootstrap_fingerprint, "bootstrap_fingerprint"),
            "reference_fingerprint": self._fingerprint(reference_fingerprint, "reference_fingerprint"),
            "messages": messages_value,
        }
        data = self._canonical(document)
        self._check_size(data)
        identity = hashlib.sha256(data).hexdigest()
        branch_identity = hashlib.sha256(cursor["branch_id"].encode("utf-8")).hexdigest()[:16]
        pointer = f"content/windows/{branch_identity}-{identity}.json"
        reference = self.files.write_content(session_id, pointer, data, media_type="application/json")
        return ModelWindow(
            reference=reference,
            window_schema_version=document["window_schema_version"],
            compression_version=document["compression_version"],
            branch_id=document["branch_id"],
            source_event_cursor=cursor,
            content_sha256=content_sha256,
            bootstrap_fingerprint=document["bootstrap_fingerprint"],
            reference_fingerprint=document["reference_fingerprint"],
            messages=messages_value,
        )

    def load(
        self,
        session_id: str,
        reference: dict[str, Any],
        *,
        expected_source_event_cursor: dict[str, Any] | None = None,
        expected_bootstrap_fingerprint: str | None = None,
        expected_reference_fingerprint: str | None = None,
        expected_window_schema_version: str | None = None,
        expected_compression_version: str | None = None,
    ) -> ModelWindow:
        if not isinstance(reference, dict) or set(reference) != _REFERENCE_KEYS:
            raise ValueError("invalid model window resource reference")
        pointer = reference.get("path")
        path_match = _WINDOW_PATH.fullmatch(pointer) if isinstance(pointer, str) else None
        if reference.get("type") != "reference" or path_match is None:
            raise ValueError("model window reference must point into content/windows")
        if reference.get("sha256") != path_match.group(2):
            raise ValueError("model window reference path digest does not match its content hash")
        size = reference.get("bytes") if isinstance(reference, dict) else None
        if type(size) is not int or size < 0 or size > self.max_bytes:
            raise ValueError("model window exceeds configured byte limit")
        if reference.get("media_type") != "application/json":
            raise ValueError("model window reference must contain application/json")
        data = self.files.read_reference(session_id, reference)
        self._check_size(data)
        if hashlib.sha256(data).hexdigest() != path_match.group(2):
            raise ValueError("model window resource path digest integrity error")
        try:
            document = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("model window resource is not valid JSON") from exc
        if not isinstance(document, dict) or set(document) != _DOCUMENT_KEYS:
            raise ValueError("unsupported model window document format")
        if type(document.get("schema_version")) is not int or document["schema_version"] != MODEL_WINDOW_SCHEMA_VERSION:
            raise ValueError("unsupported model window schema version")
        if not isinstance(document["messages"], list):
            raise ValueError("model window messages must be a list")
        messages_value = self._messages(document["messages"])
        if messages_value != document["messages"]:
            raise ValueError("model window contains non-canonical or unsafe messages")
        message_bytes = self._canonical(messages_value)
        digest = hashlib.sha256(message_bytes).hexdigest()
        if document.get("content_sha256") != digest:
            raise ValueError("model window content hash integrity error")
        cursor = self._cursor(document.get("source_event_cursor"))
        if document.get("branch_id") != cursor["branch_id"]:
            raise ValueError("model window branch does not match source cursor")
        branch_identity = hashlib.sha256(cursor["branch_id"].encode("utf-8")).hexdigest()[:16]
        if branch_identity != path_match.group(1):
            raise ValueError("model window resource path branch does not match its source cursor")
        window_schema_version = self._version(document.get("window_schema_version"), "window_schema_version")
        compression_version = self._version(document.get("compression_version"), "compression_version")
        bootstrap_fingerprint = self._fingerprint(document.get("bootstrap_fingerprint"), "bootstrap_fingerprint")
        reference_fingerprint = self._fingerprint(document.get("reference_fingerprint"), "reference_fingerprint")
        self._validate_references(session_id, messages_value)

        expected: list[tuple[bool, str]] = [
            (expected_source_event_cursor is not None and cursor != self._cursor(expected_source_event_cursor), "source event cursor"),
            (expected_bootstrap_fingerprint is not None and bootstrap_fingerprint != self._fingerprint(expected_bootstrap_fingerprint, "expected_bootstrap_fingerprint"), "bootstrap fingerprint"),
            (expected_reference_fingerprint is not None and reference_fingerprint != self._fingerprint(expected_reference_fingerprint, "expected_reference_fingerprint"), "reference fingerprint"),
            (expected_window_schema_version is not None and window_schema_version != self._version(expected_window_schema_version, "expected_window_schema_version"), "window schema version"),
            (expected_compression_version is not None and compression_version != self._version(expected_compression_version, "expected_compression_version"), "compression version"),
        ]
        stale_field = next((field for stale, field in expected if stale), None)
        if stale_field:
            raise StaleModelWindowError(f"model window is stale: {stale_field} does not match current context")
        return ModelWindow(
            reference=dict(reference),
            window_schema_version=window_schema_version,
            compression_version=compression_version,
            branch_id=cursor["branch_id"],
            source_event_cursor=cursor,
            content_sha256=digest,
            bootstrap_fingerprint=bootstrap_fingerprint,
            reference_fingerprint=reference_fingerprint,
            messages=messages_value,
        )

    def load_if_current(self, session_id: str, reference: dict[str, Any], **expected: Any) -> ModelWindow | None:
        """Return ``None`` for valid-but-stale data; corruption remains an error."""
        try:
            return self.load(session_id, reference, **expected)
        except StaleModelWindowError:
            return None

    def _canonical(self, value: Any) -> bytes:
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("model window must contain JSON-compatible values") from exc

    def _check_size(self, data: bytes) -> None:
        if len(data) > self.max_bytes:
            raise ValueError("model window exceeds configured byte limit")
