from pydantic import BaseModel, Field
from typing import Any, Literal


class ConfigFile(BaseModel):
    name: str
    content: str


class ConfigUpdate(BaseModel):
    content: str


class ConfigUpdateResponse(BaseModel):
    name: str
    status: str = "ok"


class ResetResponse(BaseModel):
    status: str = "ok"
    message: str


class AgentInfo(BaseModel):
    name: str


# --- 结构化模块 API ---

ConfigErrorKind = Literal[
    "syntax",
    "type",
    "validation",
    "semantic",
    "conflict",
    "io",
    "draft",
]


class ConfigFieldError(BaseModel):
    path: str
    kind: str
    message: str
    expected: Any | None = None


class ConfigModuleSummary(BaseModel):
    key: str
    display_name: str
    value: dict[str, Any] = Field(default_factory=dict)
    source: str = "default"
    warnings: list[str] = Field(default_factory=list)
    errors: list[ConfigFieldError] = Field(default_factory=list)
    has_draft_error: bool = False


class ConfigModulesResponse(BaseModel):
    revision: int
    schema_version: int = 1
    modules: list[ConfigModuleSummary]
    warnings: list[str] = Field(default_factory=list)
    mcp_revision: int = 0
    mcp_source: str = "empty"


class ConfigModuleValidateRequest(BaseModel):
    value: dict[str, Any] | None = None
    text: str | None = None


class ConfigModuleValidateResponse(BaseModel):
    ok: bool
    module: str
    errors: list[ConfigFieldError] = Field(default_factory=list)
    normalized: dict[str, Any] | None = None
    revision: int | None = None


class ConfigModuleUpdateRequest(BaseModel):
    base_revision: int
    value: dict[str, Any] | None = None
    text: str | None = None


class ConfigModuleUpdateResponse(BaseModel):
    status: str = "ok"
    module: str
    revision: int
    value: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class ConfigConflictPayload(BaseModel):
    code: Literal["CONFIG_REVISION_CONFLICT"] = "CONFIG_REVISION_CONFLICT"
    message: str = "配置已被其他请求更新，请重新加载后合并"
    revision: int
    module: str | None = None
    current: dict[str, Any] | None = None


class ConfigValidationPayload(BaseModel):
    code: Literal["CONFIG_VALIDATION_ERROR"] = "CONFIG_VALIDATION_ERROR"
    message: str = "配置校验失败"
    errors: list[ConfigFieldError] = Field(default_factory=list)
    revision: int | None = None
    module: str | None = None
