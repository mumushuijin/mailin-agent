from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDoc:
    summary: str
    description: str


TOOL_DOCS: dict[str, ToolDoc] = {
    "skill_list": ToolDoc(
        summary="列出可用 skill 的轻量目录",
        description="""列出全局与当前项目 `.agents/skills/` 中可用的 skill 摘要。

适用场景：需要确认有哪些 skill、来源是 global 还是 project、是否被覆盖或禁用。
注意：只返回摘要，不返回完整 `SKILL.md`；执行匹配任务前应调用 `skill_read` 加载完整说明。""",
    ),
    "skill_match": ToolDoc(
        summary="按用户请求匹配候选 skill",
        description="""根据用户请求匹配候选 skill，返回可解释的命中原因与排序。

匹配优先级：显式名称/命令前缀 > trigger > tag > 描述关键词；项目 skill 优先于同名全局 skill。""",
    ),
    "skill_read": ToolDoc(
        summary="读取指定 skill 的完整说明",
        description="""按 id 读取指定 skill 的完整 `SKILL.md`，必要时可带上直接引用资源。

适用场景：用户点名 skill，或 `skill_match` 显示任务明显匹配某个 skill 后，在执行实质任务动作前加载完整说明。
安全边界：只能读取已发现 skill 目录内的 `SKILL.md` 与允许的相对资源，不授予任何额外工具权限。""",
    ),
}
