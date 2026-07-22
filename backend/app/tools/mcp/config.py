from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from app.core.settings import get_settings
from app.tools.mcp.types import McpServerConfig

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _load_workspace_config(workspace: Path | None = None) -> dict:
    workspace = workspace or get_settings().workspace_path
    path = workspace / "CONFIG.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_mcp_server_configs(workspace=None) -> dict[str, McpServerConfig]:
    """从 workspace CONFIG.json 读取 mcp_servers 段。"""
    full = _load_workspace_config(workspace)
    raw_servers = full.get("mcp_servers")
    if not raw_servers or not isinstance(raw_servers, dict):
        return {}

    _ensure_dotenv()
    # 避免 Path.resolve()（会调用 os.getcwd），langgraph dev 的 blockbuster 会拦截
    workspace_path = str(workspace or get_settings().workspace_path)

    result: dict[str, McpServerConfig] = {}
    for name, cfg in raw_servers.items():
        if not isinstance(cfg, dict):
            logger.warning("mcp_servers.%s 必须是对象，已跳过", name)
            continue
        interpolated = _interpolate(cfg, workspace_path=workspace_path)
        server = McpServerConfig.from_dict(str(name), interpolated)
        if not server.enabled:
            logger.debug("MCP server '%s' 已禁用，跳过", name)
            continue
        issues = validate_server_config(name, server)
        if issues:
            logger.warning("跳过可疑 MCP server '%s': %s", name, "; ".join(issues))
            continue
        result[str(name)] = server
    return result


def validate_server_config(name: str, config: McpServerConfig) -> list[str]:
    """启动前安全校验（v0.1 最小规则）。"""
    issues: list[str] = []
    if config.transport.value == "stdio":
        if not config.command:
            issues.append("stdio 模式缺少 command")
        elif _looks_like_shell_injection(config.command):
            issues.append(f"command 含可疑字符: {config.command!r}")
    elif config.transport.value in ("http", "sse"):
        if not config.url:
            issues.append(f"{config.transport.value} 模式缺少 url")
    if config.command and config.url:
        issues.append("不能同时配置 command 与 url")
    return issues


def _looks_like_shell_injection(command: str) -> bool:
    return any(ch in command for ch in ("|", "&", ";", "`", "$("))


def _interpolate(value: Any, *, workspace_path: str) -> Any:
    """递归解析 ${VAR}；内置 ${WORKSPACE}。"""
    if isinstance(value, str):
        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key == "WORKSPACE":
                return workspace_path
            return os.environ.get(key, match.group(0))

        return _ENV_VAR_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _interpolate(v, workspace_path=workspace_path) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(item, workspace_path=workspace_path) for item in value]
    return value


def _ensure_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        env_path = get_settings().workspace_path / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
    except Exception:
        pass
