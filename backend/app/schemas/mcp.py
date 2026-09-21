from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


CanonicalTransport = Literal["stdio", "streamable-http", "sse"]
McpManagementState = Literal[
    "disabled",
    "not_configured",
    "connecting",
    "ready",
    "degraded",
    "error",
    "stale",
]


class McpConnectionModel(BaseModel):
    type: CanonicalTransport = "stdio"
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class McpAuthModel(BaseModel):
    required_env: list[str] = Field(default_factory=list)


class McpTimeoutsModel(BaseModel):
    connect: float = 60.0
    call: float = 25.0


class McpToolsModel(BaseModel):
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    resources: bool = False
    prompts: bool = False


class McpRuntimeModel(BaseModel):
    supports_parallel_tool_calls: bool = False


class McpServerWrite(BaseModel):
    """创建/更新请求体（可含掩码字段）。"""

    display_name: str | None = None
    enabled: bool = True
    connection: McpConnectionModel = Field(default_factory=McpConnectionModel)
    auth: McpAuthModel = Field(default_factory=McpAuthModel)
    timeouts: McpTimeoutsModel = Field(default_factory=McpTimeoutsModel)
    tools: McpToolsModel = Field(default_factory=McpToolsModel)
    runtime: McpRuntimeModel = Field(default_factory=McpRuntimeModel)
    clear_headers: list[str] = Field(default_factory=list)
    clear_env: list[str] = Field(default_factory=list)


class McpServerDraft(McpServerWrite):
    """校验/测试草稿，可带临时 id。"""

    id: str | None = None


class McpStatusView(BaseModel):
    state: McpManagementState
    connected: bool = False
    tool_count: int = 0
    tool_names: list[str] = Field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    captured_at: float | None = None
    source: str | None = None
    stale: bool = False
    config_revision: int = 0
    applied_revision: int = 0


class McpServerListItem(BaseModel):
    id: str
    display_name: str
    enabled: bool
    transport: CanonicalTransport
    state: McpManagementState
    tool_count: int = 0
    last_checked_at: float | None = None
    error_summary: str | None = None
    can_connect: bool = False
    validation_errors: list[str] = Field(default_factory=list)
    unresolved_variables: list[str] = Field(default_factory=list)
    source: Literal["mcp", "mcp_servers"] = "mcp"


class McpServerDetail(McpServerListItem):
    connection: McpConnectionModel
    auth: McpAuthModel = Field(default_factory=McpAuthModel)
    timeouts: McpTimeoutsModel = Field(default_factory=McpTimeoutsModel)
    tools: McpToolsModel = Field(default_factory=McpToolsModel)
    runtime: McpRuntimeModel = Field(default_factory=McpRuntimeModel)
    warnings: list[str] = Field(default_factory=list)
    unknown_fields: dict[str, Any] = Field(default_factory=dict)
    status: McpStatusView | None = None
    header_refs: dict[str, str] = Field(default_factory=dict)
    env_refs: dict[str, str] = Field(default_factory=dict)


class McpListResponse(BaseModel):
    servers: list[McpServerListItem]
    total: int
    config_revision: int
    warnings: list[str] = Field(default_factory=list)
    legacy_shadowed: bool = False
    source: Literal["mcp", "mcp_servers", "empty"] = "empty"


class McpFieldError(BaseModel):
    path: str
    kind: str
    message: str
    expected: Any | None = None


class McpValidateResponse(BaseModel):
    ok: bool
    validation_errors: list[str] = Field(default_factory=list)
    errors: list[McpFieldError] = Field(default_factory=list)
    unresolved_variables: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    can_connect: bool = False
    normalized: dict[str, Any] = Field(default_factory=dict)


class McpTestResponse(BaseModel):
    ok: bool
    state: McpManagementState
    duration_ms: float
    tool_count: int = 0
    tool_names: list[str] = Field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    replaced_runtime: bool = False


class McpRefreshServerResult(BaseModel):
    id: str
    ok: bool
    state: McpManagementState
    tool_count: int = 0
    error: str | None = None
    error_code: str | None = None


class McpRefreshResponse(BaseModel):
    results: list[McpRefreshServerResult]
    config_revision: int
    applied_revision: int


class McpMutationResponse(BaseModel):
    status: str = "ok"
    server: McpServerDetail
    config_revision: int
