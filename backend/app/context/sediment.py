from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from langchain_core.messages import HumanMessage

from app.agent.messages import is_real_user_message
from app.context.api_usage import extract_api_usage
from app.context.budget import load_context_config
from app.context.memory.consolidator import user_requested_remember
from app.context.memory.sediment_state import SedimentState
from app.core.llm import get_chat_model
from app.core.settings import get_settings
from app.storage.workspace import MemoryStore

RESCUE_PROMPT = """从以下对话历史中提取**跨会话可能仍有价值**的要点（用户偏好、长期项目、重要决策、约定、需持续参照的约束）。
不要复述对话流水账，不要包含一次性临时信息。若无值得保留的内容，仅输出「无」。

300 字以内，简洁 Markdown 条目：

{history}"""


def daily_memory_path(workspace: Path | None = None, day: date | None = None) -> Path:
    workspace = workspace or get_settings().workspace_path
    day = day or date.today()
    store = MemoryStore(workspace)
    return store.memory_dir / f"{day.isoformat()}.md"


def _memory_config(workspace: Path | None = None) -> dict:
    return load_context_config(workspace).get("memory", {})


def append_daily_memory(content: str, workspace: Path | None = None, source: str = "context") -> Path:
    """温层：追加到当日 memory/YYYY-MM-DD.md。"""
    path = daily_memory_path(workspace)
    header = f"# {date.today().isoformat()}\n\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else header
    stamp = datetime.now().strftime("%H:%M")
    block = f"\n\n## [{stamp}] {source}\n\n{content.strip()}\n"
    path.write_text(existing.rstrip() + block, encoding="utf-8")
    return path


def append_rescue_summary(summary: str, workspace: Path | None = None) -> None:
    """压缩抢救：将摘要写入温层每日记忆。"""
    text = summary.strip()
    if not text or text == "无":
        return
    for prefix in ("[会话摘要]\n", "## 会话摘要\n\n"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    if not text or len(text) < 10:
        return
    append_daily_memory(text, workspace, source="压缩抢救")


def rescue_before_compression(
    messages_text: str,
    workspace: Path | None = None,
    *,
    summary: str | None = None,
) -> list[dict]:
    """压缩前抢救：写入 LLM 摘要到温层。"""
    workspace = workspace or get_settings().workspace_path
    api_usages: list[dict] = []

    if summary is not None:
        append_rescue_summary(summary, workspace)
        return api_usages

    if not messages_text.strip():
        return api_usages

    try:
        model = get_chat_model()
        prompt = RESCUE_PROMPT.format(history=messages_text[:12000])
        resp = model.invoke([HumanMessage(content=prompt)])
        usage = extract_api_usage(resp, source="compression_rescue")
        if usage:
            api_usages.append(usage)
        content = str(resp.content or "").strip()
        if content and content != "无":
            append_daily_memory(content, workspace, source="压缩抢救")
    except Exception:
        pass
    return api_usages


def _remember_hint_from_state(state: dict) -> str | None:
    ledger = state.get("ledger") or state.get("messages") or []
    for msg in reversed(ledger):
        if not is_real_user_message(msg):
            continue
        content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
        if user_requested_remember(content):
            return (
                "【记忆提醒】用户明确要求记住，请调用 memory_consolidate，"
                "fact 填写要去沉淀的事实正文（去掉「请记住」等触发语），"
                "直接写入热层 MEMORY.md / USER.md；不要使用 memory_add。"
            )
        break
    return None


def build_memory_hints(
    state: dict,
    workspace: Path | None = None,
    *,
    increment_turn: bool = True,
) -> tuple[str, int, bool]:
    """构建记忆引导文案。返回 (hints, counter, nudge_pending)。"""
    workspace = workspace or get_settings().workspace_path
    cfg = _memory_config(workspace)
    hints: list[str] = []
    nudge_pending = False

    remember_hint = _remember_hint_from_state(state)
    if remember_hint:
        hints.append(remember_hint)

    counter = int(state.get("memory_turn_counter") or 0)
    warm_cfg = cfg.get("warm", {})
    daily_every = int(warm_cfg.get("nudge_every_user_turns", cfg.get("daily_sediment_every_turns", 5)))

    if increment_turn and daily_every > 0:
        counter += 1
        if counter >= daily_every:
            nudge_pending = True
            counter = 0

    interval_h = int(cfg.get("consolidate_interval_hours", cfg.get("longterm_consolidate_interval_hours", 24)))
    if interval_h > 0:
        sediment_state = SedimentState(workspace)
        days = int(cfg.get("longterm_consolidate_days", 7))
        pending = sediment_state.pending_entries(MemoryStore(workspace), days=days)
        last = sediment_state.last_consolidate_at()
        hours_ok = last is None or (datetime.now() - last).total_seconds() >= interval_h * 3600
        if pending and hours_ok:
            hints.append(
                f"【记忆提醒】有 {len(pending)} 个未沉淀的每日记忆，可在会话开始时调用 memory_consolidate。"
            )

    if not hints:
        return "", counter, nudge_pending
    return "\n".join(hints), counter, nudge_pending


def should_increment_memory_turn(ledger: list) -> bool:
    if not ledger:
        return False
    return is_real_user_message(ledger[-1])


__all__ = [
    "SedimentState",
    "append_daily_memory",
    "append_rescue_summary",
    "rescue_before_compression",
    "build_memory_hints",
    "should_increment_memory_turn",
    "daily_memory_path",
]
