from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _BACKEND_ROOT / ".env"

load_dotenv(_ENV_FILE)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000
    workspace_path: Path = _BACKEND_ROOT / "workspace"
    workspace_defaults_path: Path = _BACKEND_ROOT / "workspace_defaults"

    openai_api_key: str | None = None
    openai_base_url: str | None = None
    dashscope_api_key: str | None = None
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    langchain_tracing_v2: bool = False
    langchain_project: str = "mailin"
    langchain_api_key: str | None = None

    tavily_api_key: str | None = None

    log_level: str = "INFO"
    log_format: str = "console"
    mailin_timezone: str = "Asia/Shanghai"

    @property
    def llm_api_key(self) -> str | None:
        return self.openai_api_key or self.dashscope_api_key

    @property
    def llm_base_url(self) -> str | None:
        if self.openai_base_url:
            return self.openai_base_url
        if self.dashscope_api_key:
            return self.dashscope_base_url
        return None

    @property
    def has_llm(self) -> bool:
        return bool(self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def init_workspace(settings: Settings | None = None) -> None:
    from app.storage.workspace import (
        BOOTSTRAP_NAMES,
        CONFIG_FILE,
        _config_filename,
        artifacts_dir,
        bootstraps_dir,
        migrate_workspace_layout,
    )

    settings = settings or get_settings()
    workspace = settings.workspace_path
    defaults = settings.workspace_defaults_path

    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    (workspace / "sessions").mkdir(parents=True, exist_ok=True)
    (workspace / "tool_results").mkdir(parents=True, exist_ok=True)
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    bootstraps_dir(workspace).mkdir(parents=True, exist_ok=True)
    artifacts_dir(workspace).mkdir(parents=True, exist_ok=True)

    config_target = workspace / CONFIG_FILE
    if not config_target.exists():
        config_src = defaults / CONFIG_FILE
        if config_src.exists():
            config_target.write_text(config_src.read_text(encoding="utf-8"), encoding="utf-8")

    defaults_bootstraps = defaults / "bootstraps"
    _skip_md_bootstrap = {"MEMORY", "USER"}
    for name in BOOTSTRAP_NAMES:
        if name in _skip_md_bootstrap:
            continue
        target = bootstraps_dir(workspace) / _config_filename(name)
        if target.exists():
            continue
        source = defaults_bootstraps / _config_filename(name)
        if not source.exists():
            source = defaults / _config_filename(name)
        if source.exists():
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    from app.storage.workspace import _SECTION_JSON_FILES

    for fname in _SECTION_JSON_FILES:
        target = bootstraps_dir(workspace) / fname
        if target.exists():
            continue
        source = defaults_bootstraps / fname
        if source.exists():
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    from app.context.memory.store import ensure_hot_layer_initialized

    ensure_hot_layer_initialized(workspace)

    readme = artifacts_dir(workspace) / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Artifacts\n\n"
            "此目录存放 Agent 生成的产物文件（HTML 页面、报告、导出数据等）。\n"
            "示例：`artifacts/weather.html`\n",
            encoding="utf-8",
        )

    migrate_workspace_layout(workspace, defaults)
