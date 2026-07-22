"""热层固定 Section 注册表（系统维护，LLM 不得新建节）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TargetFile = Literal["memory", "user"]


@dataclass(frozen=True)
class SectionDef:
    key: str
    display_name: str
    id_prefix: str
    description: str


MEMORY_SECTIONS: tuple[SectionDef, ...] = (
    SectionDef("preference", "Preference", "pref.", "用户偏好、沟通风格、输出格式"),
    SectionDef("environment", "Environment", "env.", "技术栈、OS、路径、运行环境"),
    SectionDef("tools", "Tools", "tool.", "常用工具、命令、MCP 习惯"),
    SectionDef("convention", "Convention", "conv.", "长期约定、流程、项目规范"),
    SectionDef("misc", "Misc", "misc.", "无法归类但值得保留的事实"),
)

USER_SECTIONS: tuple[SectionDef, ...] = (
    SectionDef("identity", "Identity", "user.", "称呼、角色、长期身份"),
    SectionDef("style", "Style", "style.", "沟通风格、语气"),
    SectionDef("habit", "Habit", "habit.", "工作习惯、节奏"),
    SectionDef("misc", "Misc", "misc.", "兜底"),
)

REGISTRY: dict[TargetFile, tuple[SectionDef, ...]] = {
    "memory": MEMORY_SECTIONS,
    "user": USER_SECTIONS,
}

SECTION_JSON_FILES: dict[TargetFile, str] = {
    "memory": "memory_sections.json",
    "user": "user_sections.json",
}

# display_name → key（解析旧 MEMORY.md 用）
_DISPLAY_TO_KEY: dict[TargetFile, dict[str, str]] = {
    target: {s.display_name: s.key for s in sections}
    for target, sections in REGISTRY.items()
}

# id 前缀 → section key
_PREFIX_TO_KEY: dict[TargetFile, dict[str, str]] = {}
for _target, _sections in REGISTRY.items():
    mapping: dict[str, str] = {}
    for s in _sections:
        mapping[s.id_prefix.rstrip(".")] = s.key
        mapping[s.id_prefix] = s.key
    _PREFIX_TO_KEY[_target] = mapping


def section_keys(target: TargetFile) -> list[str]:
    return [s.key for s in REGISTRY[target]]


def section_def(target: TargetFile, key: str) -> SectionDef | None:
    for s in REGISTRY[target]:
        if s.key == key:
            return s
    return None


def display_name(target: TargetFile, key: str) -> str:
    s = section_def(target, key)
    return s.display_name if s else key


def key_from_display(target: TargetFile, name: str) -> str | None:
    return _DISPLAY_TO_KEY[target].get(name.strip())


def prefix_to_section_key(target: TargetFile, clause_id: str) -> str | None:
    if "." not in clause_id:
        return None
    prefix = clause_id.split(".", 1)[0]
    return _PREFIX_TO_KEY[target].get(prefix) or _PREFIX_TO_KEY[target].get(prefix + ".")


def id_prefix_for_section(target: TargetFile, section_key: str) -> str:
    s = section_def(target, section_key)
    if not s:
        return "misc."
    return s.id_prefix


def section_table_for_prompt(target: TargetFile) -> str:
    lines = []
    for s in REGISTRY[target]:
        lines.append(f"- {s.key} ({s.display_name}): 前缀 {s.id_prefix} — {s.description}")
    return "\n".join(lines)
