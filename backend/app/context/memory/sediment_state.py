"""每日记忆沉淀状态追踪（独立模块，避免与 consolidator 循环依赖）。"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from app.storage.workspace import MemoryStore


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


class SedimentState:
    """memory/.sediment_state.json — 追踪每日记忆的沉淀状态。"""

    FILENAME = ".sediment_state.json"

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.path = workspace / "memory" / self.FILENAME

    def load(self) -> dict:
        if not self.path.exists():
            return {
                "files": {},
                "last_longterm_consolidate_at": None,
                "consolidate_count": 0,
            }
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"files": {}, "last_longterm_consolidate_at": None, "consolidate_count": 0}
        data.setdefault("files", {})
        data.setdefault("last_longterm_consolidate_at", None)
        data.setdefault("consolidate_count", 0)
        return data

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def file_hash(self, filename: str, content: str) -> str:
        data = self.load()
        entry = data["files"].setdefault(filename, {})
        h = content_hash(content)
        if entry.get("content_hash") != h:
            entry["content_hash"] = h
            self.save(data)
        return h

    def is_pending(self, filename: str, content: str) -> bool:
        h = content_hash(content)
        entry = self.load()["files"].get(filename, {})
        return entry.get("last_consolidated_hash") != h

    def mark_consolidated(self, filename: str, content: str) -> None:
        data = self.load()
        h = content_hash(content)
        entry = data["files"].setdefault(filename, {})
        entry["content_hash"] = h
        entry["last_consolidated_hash"] = h
        entry["consolidated_at"] = datetime.now().isoformat(timespec="seconds")
        self.save(data)

    def mark_longterm_consolidated(self) -> int:
        data = self.load()
        data["last_longterm_consolidate_at"] = datetime.now().isoformat(timespec="seconds")
        data["consolidate_count"] = int(data.get("consolidate_count") or 0) + 1
        self.save(data)
        return data["consolidate_count"]

    def last_consolidate_at(self) -> datetime | None:
        raw = self.load().get("last_longterm_consolidate_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def pending_entries(self, store: MemoryStore, days: int = 7) -> list[dict]:
        cutoff = date.today() - timedelta(days=days)
        pending: list[dict] = []
        for entry in store.list_entries():
            if entry["filename"].startswith("."):
                continue
            try:
                entry_date = date.fromisoformat(entry["date"])
            except ValueError:
                continue
            if entry_date < cutoff:
                continue
            content = entry["content"].strip()
            if not content:
                continue
            if self.is_pending(entry["filename"], content):
                pending.append(entry)
        return pending
