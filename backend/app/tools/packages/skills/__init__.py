from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from app.services.skill_service import SkillService
from app.tools.card import ToolCard, make_card
from app.tools.packages.base import ToolPackage
from app.tools.packages.skills.descriptions import TOOL_DOCS
from app.tools.runtime import get_project_workspace


def _service() -> SkillService:
    return SkillService(project_workspace=get_project_workspace())


def _skill_list(source: Literal["global", "project"] | None = None) -> str:
    response = _service().list(source=source)
    return json.dumps(response.model_dump(), ensure_ascii=False, indent=2)


def _skill_match(query: str, source: Literal["global", "project"] | None = None, limit: int = 5) -> str:
    response = _service().match(query, source=source, limit=limit)
    return json.dumps(response.model_dump(), ensure_ascii=False, indent=2)


def _skill_read(
    skill_id: str,
    source: Literal["global", "project"] | None = None,
    include_references: bool = False,
) -> str:
    response = _service().detail(
        skill_id,
        source=source,
        include_references=include_references,
    )
    return json.dumps(response.model_dump(), ensure_ascii=False, indent=2)


def _card(name: str, handler, *, parameters: dict | None = None) -> ToolCard:
    doc = TOOL_DOCS[name]
    return make_card(
        package="skills",
        name=name,
        handler=handler,
        summary=doc.summary,
        description=doc.description,
        display_name=_DISPLAY_NAMES[name],
        display_icon=_DISPLAY_ICONS[name],
        parameters=parameters,
        concurrency="safe",
    )


_DISPLAY_NAMES = {
    "skill_list": "列出技能",
    "skill_match": "匹配技能",
    "skill_read": "读取技能",
}

_DISPLAY_ICONS = {
    "skill_list": "🧩",
    "skill_match": "🎯",
    "skill_read": "📘",
}


class SkillsPackage(ToolPackage):
    def build_cards(self, config: dict | None = None) -> list[ToolCard]:
        source_schema = {
            "type": "string",
            "enum": ["global", "project"],
            "description": "可选：只查看 global 或 project 来源",
        }
        return [
            _card(
                "skill_list",
                _skill_list,
                parameters={
                    "type": "object",
                    "properties": {
                        "source": source_schema,
                    },
                },
            ),
            _card(
                "skill_match",
                _skill_match,
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "用户请求或任务描述",
                        },
                        "source": source_schema,
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                            "description": "返回候选数量，默认 5",
                        },
                    },
                    "required": ["query"],
                },
            ),
            _card(
                "skill_read",
                _skill_read,
                parameters={
                    "type": "object",
                    "properties": {
                        "skill_id": {
                            "type": "string",
                            "description": "skill id，例如 openspec-propose 或 backend",
                        },
                        "source": source_schema,
                        "include_references": {
                            "type": "boolean",
                            "description": "是否带上 SKILL.md 直接引用的 references/scripts/assets 资源",
                        },
                    },
                    "required": ["skill_id"],
                },
            ),
        ]


PACKAGE = SkillsPackage(Path(__file__).parent)
