"""CONFIG.json 规范根文档模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config.modules import (
    MODULE_MODELS,
    AgentModule,
    ContextModule,
    MiddlewareModule,
    SkillsModule,
    TelemetryModule,
    ToolsModule,
    default_module_value,
)
from app.schemas.mcp import (
    McpAuthModel,
    McpConnectionModel,
    McpRuntimeModel,
    McpTimeoutsModel,
    McpToolsModel,
)


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class McpServerCanonicalModel(FlexibleModel):
    """规范 mcp.servers.<id> 条目（与管理 API 契约对齐）。"""

    display_name: str | None = None
    enabled: bool = True
    connection: McpConnectionModel = Field(default_factory=McpConnectionModel)
    auth: McpAuthModel = Field(default_factory=McpAuthModel)
    timeouts: McpTimeoutsModel = Field(default_factory=McpTimeoutsModel)
    tools: McpToolsModel = Field(default_factory=McpToolsModel)
    runtime: McpRuntimeModel = Field(default_factory=McpRuntimeModel)


class McpDomainModel(FlexibleModel):
    version: int = 1
    revision: int = 0
    servers: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("mcp.version 必须 >= 1")
        return value

    @field_validator("revision")
    @classmethod
    def _revision_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("mcp.revision 必须 >= 0")
        return value


class ConfigDomainModel(FlexibleModel):
    agent: AgentModule = Field(default_factory=AgentModule)
    tools: ToolsModule = Field(default_factory=ToolsModule)
    middleware: MiddlewareModule = Field(default_factory=MiddlewareModule)
    skills: SkillsModule = Field(default_factory=SkillsModule)
    telemetry: TelemetryModule = Field(default_factory=TelemetryModule)
    context: ContextModule = Field(default_factory=ContextModule)


class CanonicalConfigDocument(FlexibleModel):
    """规范 CONFIG.json 根结构。"""

    schema_version: int = 1
    config: ConfigDomainModel = Field(default_factory=ConfigDomainModel)
    mcp: McpDomainModel = Field(default_factory=McpDomainModel)

    @field_validator("schema_version")
    @classmethod
    def _schema_version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("schema_version 必须 >= 1")
        return value

    def module_dict(self, module: str) -> dict[str, Any]:
        if module not in MODULE_MODELS:
            raise KeyError(f"未知配置模块: {module}")
        value = getattr(self.config, module)
        return value.model_dump(mode="json")

    def replace_module(self, module: str, value: dict[str, Any]) -> CanonicalConfigDocument:
        if module not in MODULE_MODELS:
            raise KeyError(f"未知配置模块: {module}")
        model_cls = MODULE_MODELS[module]
        validated = model_cls.model_validate(value)
        config_data = self.config.model_dump(mode="python")
        config_data[module] = validated.model_dump(mode="python")
        return self.model_copy(
            update={"config": ConfigDomainModel.model_validate(config_data)},
            deep=True,
        )

    def to_storage_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def empty_canonical_document() -> CanonicalConfigDocument:
    return CanonicalConfigDocument()


def default_config_domain_dict() -> dict[str, Any]:
    return {key: default_module_value(key) for key in MODULE_MODELS}
