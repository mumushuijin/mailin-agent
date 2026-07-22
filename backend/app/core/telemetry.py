import os

from app.core.settings import get_settings


def setup_telemetry() -> None:
    settings = get_settings()
    config = {}
    try:
        from app.tools.registry import load_full_config

        config = load_full_config(settings.workspace_path)
    except Exception:
        pass

    telemetry = config.get("telemetry", {})
    enabled = telemetry.get("langsmith_enabled", False) or settings.langchain_tracing_v2

    if enabled and settings.langchain_api_key:
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
        os.environ.setdefault("LANGCHAIN_API_KEY", settings.langchain_api_key)
        os.environ.setdefault("LANGCHAIN_PROJECT", settings.langchain_project)
