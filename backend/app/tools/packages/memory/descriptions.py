from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDoc:
    summary: str
    description: str


_MEMORY_ARCHITECTURE = """记忆体系（三层，无向量检索）：
- **热层**（`bootstraps/MEMORY.md` + `bootstraps/USER.md`）：结构化条款，每轮 Bootstrap 全量注入
- **温层**（`memory/YYYY-MM-DD.md`）：当日工作笔记，宽松追加；用 memory_grep / memory_list / memory_get 读取
- **冷层**：历史会话 checkpoint（勿用记忆工具查对话原文）"""

_MEMORY_MD_CRITERIA = """写入准则：

**温层 memory_add 应当写入**：
- 对话中产生的跨会话要点、项目背景、决策线索（待批量沉淀升热层）
- 系统维护轮次触发的要点沉淀

**温层不应写入**：
- 用户明确要求「记住 / 请记住 / 帮我记」的内容（应走 memory_consolidate 直写热层）
- 密码、API Key、令牌
- 完整对话流水账
- 明显一次性、易过时信息

**热层（MEMORY/USER）**：
- 源数据在 `bootstraps/memory_sections.json` 与 `bootstraps/user_sections.json`（按固定 Section 存 Clause）
- `MEMORY.md` / `USER.md` 为组装视图，Bootstrap 读取；**优先改 JSON，不要手改 MD**（会被覆盖）
- 固定 Section 不可增删；条款 ID 格式 `prefix.slug`（如 pref.lang）
- 由 memory_consolidate 仲裁写入；手动应急可编辑 JSON 后重启会话"""

_MEMORY_TRIGGERS = """触发机制（系统自动，无需用户手动）：
- 每 5 轮真实用户对话 → 系统维护消息提示 memory_add（写温层）
- 每 24h 且有未沉淀温层 → 提示 memory_consolidate 批量沉淀
- 用户说「请记住 / 帮我记 / 记下来」等 → **立即调用 memory_consolidate(fact=...)** 直写热层，勿用 memory_add
- 上下文压缩时 → 自动抢救摘要到温层"""


TOOL_DOCS: dict[str, ToolDoc] = {
    "memory_list": ToolDoc(
        summary="列出所有每日工作记忆文件及内容预览",
        description=f"""一句话功能：列出 `memory/` 目录下全部每日记忆文件（按日期倒序），并显示每条约 120 字的预览。

{_MEMORY_ARCHITECTURE}

适用场景：了解近期有哪些工作记忆、哪天记了什么
不适用：查长期记忆 → Bootstrap 已注入 MEMORY.md / USER.md""",
    ),
    "memory_get": ToolDoc(
        summary="读取指定日期的每日工作记忆全文",
        description=f"""一句话功能：读取 `memory/YYYY-MM-DD.md` 的完整 Markdown 内容。

{_MEMORY_ARCHITECTURE}

参数：filename（必填，格式 `YYYY-MM-DD.md`）""",
    ),
    "memory_grep": ToolDoc(
        summary="在每日工作记忆中关键词搜索",
        description=f"""一句话功能：在近 30 日 `memory/*.md` 中做纯文本关键词搜索（无 embedding）。

{_MEMORY_ARCHITECTURE}

注意：长期记忆 **不在搜索范围**，请直接参考 Bootstrap 中的 MEMORY.md / USER.md。

适用场景：按关键词找某天记过什么
不适用：查热层长期事实、查历史对话原文

参数：keyword（必填）""",
    ),
    "memory_add": ToolDoc(
        summary="向今日工作记忆追加一条笔记",
        description=f"""一句话功能：向当日 `memory/YYYY-MM-DD.md` 追加一条带时间戳的 Markdown 段落。

{_MEMORY_ARCHITECTURE}

{_MEMORY_MD_CRITERIA}

{_MEMORY_TRIGGERS}

**自主调用时机**：对话中产生值得跨会话保留的要点时主动写入温层；收到系统维护提示时回顾并沉淀。

**不要用于**：用户明确说「请记住 / 帮我记」时 — 应调用 memory_consolidate 直写热层。

参数：content（必填，简洁 Markdown 事实/结论）""",
    ),
    "memory_consolidate": ToolDoc(
        summary="将记忆仲裁写入热层 MEMORY.md / USER.md",
        description=f"""一句话功能：把事实沉淀到热层结构化条款（`MEMORY.md` / `USER.md`），或批量从温层升热层。

{_MEMORY_ARCHITECTURE}

{_MEMORY_MD_CRITERIA}

{_MEMORY_TRIGGERS}

**两种模式**：
1. **用户明确要求记住**（「请记住…」「帮我记…」）→ 传 `fact`，立即仲裁写入热层
2. **批量沉淀**（无 fact）→ 从近 7 日温层每日笔记提取候选后按 Section 仲裁

流程：候选提取(1次LLM) → Rule Router → 按节仲裁(每节1次) → 本地状态机落盘

固定 Section（MEMORY）：preference / environment / tools / convention / misc
固定 Section（USER）：identity / style / habit / misc

仲裁：ADD / UPDATE / DELETE / NOOP

参数：
- fact（可选）：用户要记住的事实正文；有则直写热层，无则批量沉淀温层
- target（可选）：`memory` 或 `user`，默认 `memory`
- section（可选）：固定节 key，默认 `misc`（不确定时由仲裁归类）""",
    ),
}
