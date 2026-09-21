"""普通配置模块的 Pydantic 类型与注册表。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EnforcementMode = Literal["audit", "enforce"]
ORDINARY_MODULE_KEYS = (
    "agent",
    "tools",
    "middleware",
    "skills",
    "telemetry",
    "context",
)
# 配置页 /modules API 仅暴露用户可编辑模块；其余为系统内部经验配置
USER_VISIBLE_MODULE_KEYS = (
    "agent",
    "tools",
)


class FlexibleModel(BaseModel):
    """允许未知字段，便于兼容旧配置扩展。"""

    model_config = ConfigDict(extra="allow")


class AgentModule(FlexibleModel):
    model: str = "qwen-plus"
    temperature: float = 0.7
    max_steps: int = 48
    graph_version: str = "0.1.0-mvp"

    @field_validator("temperature")
    @classmethod
    def _temperature_range(cls, value: float) -> float:
        if not 0.0 <= value <= 2.0:
            raise ValueError("temperature 必须在 0~2 之间")
        return value

    @field_validator("max_steps")
    @classmethod
    def _max_steps_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_steps 必须 >= 1")
        return value


class SnapshotRetentionModel(FlexibleModel):
    max_count: int = 50
    max_age_seconds: int = 604800
    max_total_bytes: int = 209715200


class SnapshotConfigModel(FlexibleModel):
    enabled: bool = True
    retention: SnapshotRetentionModel = Field(default_factory=SnapshotRetentionModel)


class FilesystemPackageModel(FlexibleModel):
    enabled: bool = True
    snapshots: SnapshotConfigModel = Field(default_factory=SnapshotConfigModel)


class ShellCommandPolicyModel(FlexibleModel):
    executable: str
    args_prefix: list[str] = Field(default_factory=list)
    allowed_roots: list[str] = Field(default_factory=lambda: ["."])
    capabilities: list[str] = Field(default_factory=list)


class ShellPolicyModel(FlexibleModel):
    allowed_roots: list[str] = Field(default_factory=lambda: ["."])
    capabilities: list[str] = Field(default_factory=list)
    commands: list[ShellCommandPolicyModel] = Field(default_factory=list)


class ShellPackageModel(FlexibleModel):
    enabled: bool = True
    default_timeout: int = 60
    max_timeout: int = 300
    safety_mode: bool = True
    workdir: str = "."
    auto_approve_patterns: list[str] = Field(default_factory=list)
    policy: ShellPolicyModel | None = None
    extra_env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _timeout_order(self) -> ShellPackageModel:
        if self.max_timeout < self.default_timeout:
            raise ValueError("max_timeout 必须 >= default_timeout")
        return self


class ToolSearchPackageModel(FlexibleModel):
    enabled: bool = True
    hot_tools: list[str] = Field(default_factory=list)
    search_default_limit: int = 5
    max_search_limit: int = 20
    mcp_as_hot: bool = False

    @model_validator(mode="after")
    def _limit_order(self) -> ToolSearchPackageModel:
        if self.search_default_limit < 1 or self.max_search_limit < 1:
            raise ValueError("tool_search limit 必须 >= 1")
        if self.search_default_limit > self.max_search_limit:
            raise ValueError("search_default_limit 不能大于 max_search_limit")
        return self


PackageToggle = Annotated[bool | dict[str, Any], Field(union_mode="smart")]


class ToolsModule(FlexibleModel):
    enforcement_mode: EnforcementMode = "enforce"
    filesystem: bool | FilesystemPackageModel = True
    shell: bool | ShellPackageModel = True
    memory: PackageToggle = True
    session: PackageToggle = True
    skills: PackageToggle = True
    calculator: PackageToggle = True
    datetime: PackageToggle = True
    web_search: PackageToggle = False
    mcp: PackageToggle = True
    tool_search: bool | ToolSearchPackageModel = True

    @field_validator("enforcement_mode", mode="before")
    @classmethod
    def _normalize_enforcement(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class MiddlewareModule(FlexibleModel):
    """占位模块：当前运行时未消费，保留对象形状供后续扩展。"""

    pass


class SkillsModule(FlexibleModel):
    global_roots: list[str] = Field(default_factory=lambda: ["skills"])
    project_enabled: bool = True
    catalog_max_items: int = 80
    catalog_max_chars: int = 12_000
    cache_ttl_seconds: int = 5
    disabled: list[str] = Field(default_factory=list)


class TelemetryModule(FlexibleModel):
    langsmith_enabled: bool = False
    sample_rate: float = 0.01

    @field_validator("sample_rate")
    @classmethod
    def _sample_rate_range(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("sample_rate 必须在 0~1 之间")
        return value


class ContextBootstrapModel(FlexibleModel):
    single_file_max_chars: int = 20_000
    total_max_chars: int = 150_000


class ContextBudgetModel(FlexibleModel):
    bootstrap: float = 0.18
    skills: float = 0.04
    summary: float = 0.12
    recent_turns: float = 0.28
    tool_results: float = 0.25
    current_message: float = 0.05
    reserved: float = 0.08


class ContextHotMemoryModel(FlexibleModel):
    memory_char_limit: int = 3000
    user_char_limit: int = 1500
    section_char_limit: int = 800
    consolidate_interval_hours: int = 24
    max_candidates_per_run: int = 20
    arbiter_batch_by_section: bool = True
    empty_section_placeholder: str = "（空）"
    misc_compact_every: int = 5


class ContextWarmMemoryModel(FlexibleModel):
    nudge_every_user_turns: int = 5
    daily_file_max_chars: int = 8000


class ContextMemoryModel(FlexibleModel):
    daily_sediment_every_turns: int = 5
    longterm_consolidate_interval_hours: int = 24
    longterm_consolidate_days: int = 7
    hot: ContextHotMemoryModel = Field(default_factory=ContextHotMemoryModel)
    warm: ContextWarmMemoryModel = Field(default_factory=ContextWarmMemoryModel)


class ContextModule(FlexibleModel):
    max_tokens: int = 128_000
    compress_threshold_ratio: float = 0.75
    bootstrap: ContextBootstrapModel = Field(default_factory=ContextBootstrapModel)
    tool_result_max_tokens: int = 4_000
    tool_summary_max_chars: int = 500
    budget: ContextBudgetModel = Field(default_factory=ContextBudgetModel)
    recent_turn_pairs: int = 5
    recent_tail_max_tokens: int = 20_000
    summary_max_chars: int = 3_000
    summary_chunk_max_chars: int = 12_000
    max_compression_attempts: int = 3
    memory: ContextMemoryModel = Field(default_factory=ContextMemoryModel)


MODULE_MODELS: dict[str, type[BaseModel]] = {
    "agent": AgentModule,
    "tools": ToolsModule,
    "middleware": MiddlewareModule,
    "skills": SkillsModule,
    "telemetry": TelemetryModule,
    "context": ContextModule,
}

MODULE_DISPLAY_NAMES: dict[str, str] = {
    "agent": "Agent",
    "tools": "工具",
    "middleware": "中间件",
    "skills": "技能",
    "telemetry": "遥测",
    "context": "上下文",
}


def default_module_value(module: str) -> dict[str, Any]:
    model_cls = MODULE_MODELS.get(module)
    if model_cls is None:
        raise KeyError(f"未知配置模块: {module}")
    return model_cls().model_dump(mode="json")


def validate_module_value(module: str, value: Any) -> BaseModel:
    model_cls = MODULE_MODELS.get(module)
    if model_cls is None:
        raise KeyError(f"未知配置模块: {module}")
    if not isinstance(value, dict):
        raise TypeError(f"模块 {module} 的值必须是 JSON 对象")
    return model_cls.model_validate(value)


def is_ordinary_module(module: str) -> bool:
    return module in MODULE_MODELS


def is_user_visible_module(module: str) -> bool:
    return module in USER_VISIBLE_MODULE_KEYS
