from __future__ import annotations

from pathlib import Path

from app.context.budget import estimate_tokens, load_context_config
from app.core.settings import get_settings
from app.storage.workspace import ConfigStore
from app.tools.runtime import get_project_workspace

PROFILE_FILES = ("SOUL", "USER", "MEMORY", "HEARTBEAT")
REMOVED_BOOTSTRAP_FILES = ("IDENTITY", "BOOTSTRAP")
TRUNCATED_MARKER = "\n\n[TRUNCATED]"


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - len(TRUNCATED_MARKER)] + TRUNCATED_MARKER


def _append_section(sections: list[str], content: str, single_max: int, total_max: int) -> int:
    content = _truncate_text(content, single_max)
    remaining = total_max - sum(len(s) for s in sections)
    if remaining <= 0:
        return 0
    if len(content) > remaining:
        content = _truncate_text(content, remaining)
    sections.append(content)
    return len(content)


def load_bootstrap(workspace: Path | None = None, project_workspace: Path | None = None) -> tuple[str, int]:
    """从 Agent 自有空间加载人格文件，再追加项目根 AGENTS.md。"""
    workspace = workspace or get_settings().workspace_path
    project = project_workspace or get_project_workspace()
    defaults = get_settings().workspace_defaults_path
    store = ConfigStore(workspace, defaults)
    config = load_context_config(workspace)
    bootstrap_cfg = config.get("bootstrap", {})
    single_max = bootstrap_cfg.get("single_file_max_chars", 20_000)
    total_max = bootstrap_cfg.get("total_max_chars", 150_000)

    sections: list[str] = []

    for name in PROFILE_FILES:
        try:
            content = store.read(name).strip()
        except Exception:
            continue
        if not content:
            continue
        _append_section(sections, content, single_max, total_max)
        if sum(len(s) for s in sections) >= total_max:
            break

    if project is not None:
        agents_path = Path(project) / "AGENTS.md"
        try:
            if agents_path.is_file():
                agents = agents_path.read_text(encoding="utf-8").strip()
                if agents:
                    _append_section(sections, agents, single_max, total_max)
        except OSError:
            pass

    if not sections:
        default = (
            "你是麦林（Mailin），理性而温暖的数字伙伴。"
            "帮助用户推进事情、理解世界、完成创造。使用中文交流，必要时调用工具。"
        )
        return default, estimate_tokens(default)

    body = "\n\n---\n\n".join(sections)
    footer = (
        "\n\n---\n\n"
        "请根据以上设定与用户协作。目标不是回答问题，而是帮助推进事情。"
        "需要事实时优先使用工具，回答简洁、有温度、像伙伴而非客服。"
    )
    full = body + footer
    return full, estimate_tokens(full)


def build_system_prompt(workspace: Path | None = None) -> str:
    """兼容旧接口：返回 Bootstrap 文本。"""
    content, _ = load_bootstrap(workspace)
    return content
