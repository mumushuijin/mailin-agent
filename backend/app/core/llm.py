import json
from functools import lru_cache
from pathlib import Path

from langchain_openai import ChatOpenAI

from app.core.settings import get_settings


def load_agent_config(workspace: Path | None = None) -> dict:
    workspace = workspace or get_settings().workspace_path
    config_path = workspace / "CONFIG.json"
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8"))


@lru_cache
def get_chat_model() -> ChatOpenAI:
    settings = get_settings()
    if not settings.has_llm:
        raise RuntimeError("未配置 LLM API Key")

    config = load_agent_config()
    agent_cfg = config.get("agent", {})
    return ChatOpenAI(
        model=agent_cfg.get("model", "gpt-4o-mini"),
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=agent_cfg.get("temperature", 0.7),
        streaming=True,
        stream_usage=True,
    )
