from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from app.schemas.session import ChatMessage

PROJECTION_VERSION = 1


def _safe_session_id(session_id: str) -> str:
    return re.sub(r"[^\w\-]", "_", session_id)


class HistoryProjectionStore:
    """Display-only chat history projection used for fast paged reads."""

    def __init__(self, workspace: Path):
        self.root = workspace / "sessions" / "history_projection"
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, session_id: str) -> Path:
        return self.root / f"{_safe_session_id(session_id)}.json"

    def read(self, session_id: str) -> dict[str, Any] | None:
        path = self.path_for(session_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("version") != PROJECTION_VERSION:
            return None
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
    ) -> None:
        payload = {
            "version": PROJECTION_VERSION,
            "session_id": session_id,
            "updated_at": int(time.time()),
            "messages": [m.model_dump(mode="json") for m in messages],
            "context_usage": context_usage,
            "api_usage": api_usage,
            "session_token_stats": session_token_stats,
            "todos": todos,
        }
        self.path_for(session_id).write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

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
