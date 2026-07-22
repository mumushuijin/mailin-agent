from __future__ import annotations

import os

_SENSITIVE_EXACT = frozenset(
    {
        "OPENAI_API_KEY",
        "DASHSCOPE_API_KEY",
        "TAVILY_API_KEY",
        "ANTHROPIC_API_KEY",
        "LANGCHAIN_API_KEY",
    }
)


def build_subprocess_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """构建子进程环境：过滤 LLM/API 凭证，保留常用系统变量。"""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _SENSITIVE_EXACT:
            continue
        if upper.endswith("_API_KEY") or upper.endswith("_SECRET"):
            continue
        env[key] = value
    if extra_env:
        env.update(extra_env)
    return env
