from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ShellConfig:
    enabled: bool = True
    default_timeout: int = 60
    max_timeout: int = 300
    safety_mode: bool = True
    workdir: str = "."
    auto_approve_patterns: tuple[str, ...] = ()
    extra_env: dict[str, str] = field(default_factory=dict)


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def load_shell_config(config: dict | None = None) -> ShellConfig:
    if config is None:
        from app.tools.registry import load_full_config

        config = load_full_config()

    tools_cfg = config.get("tools") if isinstance(config.get("tools"), dict) else {}
    raw = tools_cfg.get("shell")

    if raw is False:
        return ShellConfig(enabled=False)
    if raw is True or raw is None:
        return ShellConfig()
    if not isinstance(raw, dict):
        return ShellConfig()

    enabled = raw.get("enabled", True) not in (False, "false", "0", "off")
    default_timeout = max(1, _safe_int(raw.get("default_timeout"), 60))
    max_timeout = max(default_timeout, _safe_int(raw.get("max_timeout"), 300))
    safety_mode = raw.get("safety_mode", True) not in (False, "false", "0", "off")
    workdir = str(raw.get("workdir") or ".").strip() or "."

    patterns_raw = raw.get("auto_approve_patterns") or ()
    if isinstance(patterns_raw, list):
        auto_approve_patterns = tuple(str(p).strip() for p in patterns_raw if str(p).strip())
    else:
        auto_approve_patterns = ()

    extra_env: dict[str, str] = {}
    env_raw = raw.get("extra_env")
    if isinstance(env_raw, dict):
        extra_env = {str(k): str(v) for k, v in env_raw.items()}

    return ShellConfig(
        enabled=enabled,
        default_timeout=default_timeout,
        max_timeout=max_timeout,
        safety_mode=safety_mode,
        workdir=workdir,
        auto_approve_patterns=auto_approve_patterns,
        extra_env=extra_env,
    )


def matches_auto_approve(command: str, patterns: tuple[str, ...]) -> bool:
    command = (command or "").strip()
    if not command or not patterns:
        return False
    for pattern in patterns:
        if fnmatch.fnmatchcase(command, pattern):
            return True
    return False
