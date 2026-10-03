import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from app.core.runtime_layout import BACKEND_ROOT, REPOSITORY_ROOT, RuntimeLayout

_ENV_FILE = BACKEND_ROOT / ".env"


load_dotenv(_ENV_FILE)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(default_factory=lambda: os.getenv("MAILIN_HOST", "0.0.0.0"))
    port: int = Field(default_factory=lambda: int(os.getenv("MAILIN_PORT", "8000")))
    mode: str = Field(default_factory=lambda: os.getenv("MAILIN_MODE", "development"))
    runtime_root: Path = Field(default_factory=lambda: Path(os.getenv("MAILIN_RUNTIME_ROOT", str(REPOSITORY_ROOT / ".runtime"))))
    resources_dir: Path = Field(default_factory=lambda: Path(os.getenv("MAILIN_RESOURCES_DIR", str(BACKEND_ROOT / "resources"))))

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
    def config_path(self) -> Path:
        return self.layout.config_path

    @property
    def layout(self) -> RuntimeLayout:
        return RuntimeLayout.resolve(self.runtime_root, self.resources_dir)

    @property
    def data_root(self) -> Path:
        return self.layout.data

    @property
    def cache_dir(self) -> Path:
        return self.layout.cache

    @property
    def temp_dir(self) -> Path:
        return self.layout.tmp

    @property
    def log_dir(self) -> Path:
        return self.layout.log

    @property
    def workspace_path(self) -> Path:
        return self.layout.agent_home

    @property
    def config_dir(self) -> Path:
        return self.layout.config_dir

    @property
    def workspace_defaults_path(self) -> Path:
        return self.layout.workspace_defaults

    @property
    def config_defaults_path(self) -> Path:
        return self.layout.config_defaults

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


def _is_same_or_child(path: Path, root: Path) -> bool:
    try:
        path = path.resolve()
        root = root.resolve()
    except OSError:
        return False
    return path == root or root in path.parents


def validate_runtime_paths(settings: Settings | None = None) -> None:
    """校验并创建运行目录，同时检查只读资源。"""
    settings = settings or get_settings()
    settings.layout.validate_resources()
    settings.layout.ensure_writable()


def init_workspace(settings: Settings | None = None) -> None:
    from app.storage.workspace import (
        BOOTSTRAP_NAMES,
        CONFIG_FILE,
        _config_filename,
        bootstraps_dir,
        ensure_runtime_config,
        migrate_workspace_layout,
    )

    settings = settings or get_settings()
    if settings.mode == "development":
        from app.core.runtime_migration import migrate_development_layout

        migrate_development_layout(settings.layout)
    validate_runtime_paths(settings)
    workspace = settings.workspace_path
    defaults = settings.workspace_defaults_path

    settings.data_root.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    (workspace / "sessions").mkdir(parents=True, exist_ok=True)
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    bootstraps_dir(workspace).mkdir(parents=True, exist_ok=True)

    ensure_runtime_config(settings)

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

    migrate_workspace_layout(workspace, defaults)
