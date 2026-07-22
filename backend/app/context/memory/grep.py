"""温层每日笔记的关键词搜索（无向量、无 embedding）。"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from app.storage.workspace import MemoryStore


def grep_daily_memories(
    keyword: str,
    workspace: Path,
    *,
    days: int = 30,
    limit: int = 8,
    context_chars: int = 200,
) -> str:
    keyword = (keyword or "").strip()
    if not keyword:
        return "请提供搜索关键词。"

    store = MemoryStore(workspace)
    cutoff = date.today() - timedelta(days=max(1, days))
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    hits: list[str] = []

    for entry in store.list_entries():
        if entry["filename"].startswith("."):
            continue
        try:
            entry_date = date.fromisoformat(entry["date"])
        except ValueError:
            continue
        if entry_date < cutoff:
            continue
        content = entry.get("content") or ""
        for match in pattern.finditer(content):
            start = max(0, match.start() - context_chars // 2)
            end = min(len(content), match.end() + context_chars // 2)
            snippet = content[start:end].replace("\n", " ").strip()
            hits.append(f"- **{entry['date']}** …{snippet}…")
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break

    if not hits:
        return f"未在近 {days} 日每日记忆中找到「{keyword}」。长期记忆请查看 Bootstrap 中的 MEMORY.md。"
    return "每日记忆搜索结果：\n" + "\n".join(hits)
