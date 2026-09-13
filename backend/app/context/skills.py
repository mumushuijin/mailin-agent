from __future__ import annotations

from pathlib import Path

from app.context.budget import estimate_tokens, load_context_config
from app.services.skill_service import SkillService, skills_dir
from app.tools.runtime import get_project_workspace


def load_skills_catalog(workspace: Path | None = None, project_workspace: Path | None = None) -> tuple[str, int]:
    """注入轻量 skill catalog；完整 SKILL.md 由 skill_read 渐进式加载。"""
    service = SkillService(
        workspace=workspace,
        project_workspace=project_workspace or get_project_workspace(),
    )
    content = service.build_catalog_text()
    config = load_context_config(workspace)
    budget_tokens = int(config["max_tokens"] * config["budget"].get("skills", 0.04))
    max_chars = budget_tokens * 3
    if len(content) > max_chars:
        content = content[: max_chars - 15] + "\n...（已截断）"
    return content, estimate_tokens(content)


def load_skills_index(workspace: Path | None = None) -> tuple[str, int]:
    """兼容旧调用点。"""
    return load_skills_catalog(workspace)
