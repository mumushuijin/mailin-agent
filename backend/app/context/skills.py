from __future__ import annotations

from pathlib import Path

from app.context.budget import estimate_tokens, load_context_config
from app.core.settings import get_settings


def skills_dir(workspace: Path | None = None) -> Path:
    workspace = workspace or get_settings().workspace_path
    path = workspace / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_skills_catalog(workspace: Path | None = None) -> tuple[str, int]:
    """扫描 skills 目录，仅注入技能目录（名称 + 一行描述），供渐进式加载。"""
    directory = skills_dir(workspace)
    lines: list[str] = [
        "## 技能目录",
        "以下为可渐进加载的技能索引，不含完整技能正文。",
    ]
    found = False

    for skill_md in sorted(directory.glob("*/SKILL.md")):
        found = True
        name = skill_md.parent.name
        first_line = ""
        try:
            text = skill_md.read_text(encoding="utf-8")
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("---"):
                    first_line = line.lstrip("#").strip()
                    break
        except OSError:
            first_line = "（无描述）"
        lines.append(f"- {name}: {first_line or '（无描述）'}")

    from app.tools.registry import get_registry

    registry = get_registry()
    for pkg in registry.packages:
        if registry.is_package_enabled(pkg):
            info = pkg.info
            lines.append(f"- {info.id}: {info.description or info.name}")

    if not found and len(lines) == 2:
        lines.append("- （暂无额外技能，工具包见上方列表）")

    content = "\n".join(lines)
    config = load_context_config(workspace)
    budget_tokens = int(load_context_config(workspace)["max_tokens"] * config["budget"].get("skills", 0.04))
    max_chars = budget_tokens * 3
    if len(content) > max_chars:
        content = content[: max_chars - 15] + "\n...（已截断）"
    return content, estimate_tokens(content)


def load_skills_index(workspace: Path | None = None) -> tuple[str, int]:
    """兼容旧调用点。"""
    return load_skills_catalog(workspace)
