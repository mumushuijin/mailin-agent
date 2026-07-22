from pathlib import Path

from app.context.bootstrap import build_system_prompt as _build_bootstrap

# 兼容旧导入路径
def build_system_prompt(workspace: Path | None = None) -> str:
    return _build_bootstrap(workspace)
