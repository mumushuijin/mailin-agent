from __future__ import annotations

from pathlib import Path

from app.context.budget import estimate_tokens, load_context_config
from app.core.settings import get_settings
from app.storage.workspace import ConfigStore

BOOTSTRAP_FILES = ("IDENTITY", "USER", "SOUL", "MEMORY", "AGENTS", "HEARTBEAT", "BOOTSTRAP")
TRUNCATED_MARKER = "\n\n[TRUNCATED]"


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - len(TRUNCATED_MARKER)] + TRUNCATED_MARKER


def load_bootstrap(workspace: Path | None = None) -> tuple[str, int]:
    """从磁盘加载 Bootstrap 文件，应用单文件与总量截断。返回 (content, token_count)。"""
    workspace = workspace or get_settings().workspace_path
    defaults = get_settings().workspace_defaults_path
    store = ConfigStore(workspace, defaults)
    config = load_context_config(workspace)
    bootstrap_cfg = config.get("bootstrap", {})
    single_max = bootstrap_cfg.get("single_file_max_chars", 20_000)
    total_max = bootstrap_cfg.get("total_max_chars", 150_000)

    sections: list[str] = []
    total_chars = 0

    for name in BOOTSTRAP_FILES:
        try:
            content = store.read(name).strip()
        except Exception:
            continue
        if not content:
            continue
        content = _truncate_text(content, single_max)
        remaining = total_max - total_chars
        if remaining <= 0:
            break
        if len(content) > remaining:
            content = _truncate_text(content, remaining)
        sections.append(content)
        total_chars += len(content)
        if total_chars >= total_max:
            break

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
