from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class McpTransport(str, Enum):
    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"


# 规范配置 / 管理 API 使用的传输名（对外）；运行时仍用 McpTransport.HTTP="http"
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

McpConnectionState = Literal["disconnected", "connecting", "ready", "error"]

MASK_LITERAL = "********"
SENSITIVE_HEADER_KEYS = frozenset(
    {
        "authorization",
        "x-api-key",
        "api-key",
        "api_key",
        "proxy-authorization",
    }
)


@dataclass
class McpToolsFilter:
    """Per-server 工具过滤，语义对齐 Hermes tools.include/exclude。"""

    include: frozenset[str] = field(default_factory=frozenset)
    exclude: frozenset[str] = field(default_factory=frozenset)
    resources: bool = False
    prompts: bool = False

    def should_register(self, tool_name: str) -> bool:
        if self.include:
            return tool_name in self.include
        if self.exclude:
            return tool_name not in self.exclude
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "include": sorted(self.include),
            "exclude": sorted(self.exclude),
            "resources": self.resources,
            "prompts": self.prompts,
        }


@dataclass
class McpServerConfig:
    """单个 MCP Server 的运行时配置（仅可连接条目）。"""

    name: str
    enabled: bool = True
    transport: McpTransport = McpTransport.STDIO
    # stdio
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # http
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # common
    timeout: float = 25.0
    connect_timeout: float = 60.0
    supports_parallel_tool_calls: bool = False
    tools_filter: McpToolsFilter = field(default_factory=McpToolsFilter)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    display_name: str | None = None
    required_env: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> McpServerConfig:
        """兼容旧扁平条目；规范条目请走 normalize 层。"""
        tools_raw = data.get("tools") or {}
        tools_filter = McpToolsFilter(
            include=frozenset(_as_str_set(tools_raw.get("include"))),
            exclude=frozenset(_as_str_set(tools_raw.get("exclude"))),
            resources=bool(tools_raw.get("resources", False)),
            prompts=bool(tools_raw.get("prompts", False)),
        )

        transport = _resolve_transport(data)
        url = data.get("url") or data.get("baseUrl")
        enabled = data.get("enabled")
        if enabled is None and "isActive" in data:
            enabled = data.get("isActive")
        if enabled is None:
            enabled = True

        timeouts = data.get("timeouts") if isinstance(data.get("timeouts"), dict) else {}
        call_timeout = data.get("timeout")
        if call_timeout is None:
            call_timeout = timeouts.get("call")
        connect_timeout = data.get("connect_timeout")
        if connect_timeout is None:
            connect_timeout = timeouts.get("connect")

        connection = data.get("connection") if isinstance(data.get("connection"), dict) else {}
        command = data.get("command") if data.get("command") is not None else connection.get("command")
        args = data.get("args") if data.get("args") is not None else connection.get("args")
        env = data.get("env") if data.get("env") is not None else connection.get("env")
        headers = data.get("headers") if data.get("headers") is not None else connection.get("headers")
        if url is None:
            url = connection.get("url") or connection.get("baseUrl")
        if connection:
            transport = _resolve_transport({**data, **connection})

        auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
        required_env = [str(x) for x in (auth.get("required_env") or [])]
        runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
        parallel = data.get("supports_parallel_tool_calls")
        if parallel is None:
            parallel = runtime.get("supports_parallel_tool_calls", False)

        return cls(
            name=name,
            enabled=bool(enabled),
            transport=transport,
            command=command,
            args=[str(a) for a in (args or [])],
            env={str(k): str(v) for k, v in (env or {}).items()},
            url=url,
            headers={str(k): str(v) for k, v in (headers or {}).items()},
            timeout=_safe_float(call_timeout, 25.0),
            connect_timeout=_safe_float(connect_timeout, 60.0),
            supports_parallel_tool_calls=bool(parallel),
            tools_filter=tools_filter,
            raw=data,
            display_name=data.get("display_name") or data.get("displayName"),
            required_env=required_env,
        )


@dataclass
class McpServerDefinition:
    """归一化后的管理/运行时定义（含非法条目元数据）。"""

    id: str
    display_name: str
    enabled: bool
    connection_type: CanonicalTransport
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    required_env: list[str] = field(default_factory=list)
    timeouts_connect: float = 60.0
    timeouts_call: float = 25.0
    tools_filter: McpToolsFilter = field(default_factory=McpToolsFilter)
    supports_parallel_tool_calls: bool = False
    source: Literal["mcp", "mcp_servers"] = "mcp"
    validation_errors: list[str] = field(default_factory=list)
    unresolved_variables: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    can_connect: bool = False
    unknown_fields: dict[str, Any] = field(default_factory=dict)
    raw_canonical: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_runtime_config(self) -> McpServerConfig:
        transport = {
            "stdio": McpTransport.STDIO,
            "streamable-http": McpTransport.HTTP,
            "sse": McpTransport.SSE,
        }[self.connection_type]
        return McpServerConfig(
            name=self.id,
            enabled=self.enabled,
            transport=transport,
            command=self.command,
            args=list(self.args),
            env=dict(self.env),
            url=self.url,
            headers=dict(self.headers),
            timeout=self.timeouts_call,
            connect_timeout=self.timeouts_connect,
            supports_parallel_tool_calls=self.supports_parallel_tool_calls,
            tools_filter=self.tools_filter,
            raw=dict(self.raw_canonical),
            display_name=self.display_name,
            required_env=list(self.required_env),
        )


@dataclass
class McpCatalog:
    """工作区 MCP 配置目录（归一化结果）。"""

    version: int | None
    revision: int
    servers: dict[str, McpServerDefinition] = field(default_factory=dict)
    source: Literal["mcp", "mcp_servers", "empty"] = "empty"
    warnings: list[str] = field(default_factory=list)
    legacy_shadowed: bool = False


@dataclass
class McpServerStatus:
    """对外暴露的连接状态。"""

    name: str
    connected: bool
    transport: McpTransport
    tool_count: int = 0
    tool_names: list[str] = field(default_factory=list)
    error: str | None = None
    supports_parallel_tool_calls: bool = False
    enabled: bool = True
    state: McpManagementState = "error"
    error_code: str | None = None
    display_name: str | None = None
    normalized_transport: CanonicalTransport | None = None
    config_revision: int = 0
    can_connect: bool = False


@dataclass
class McpStatusSnapshot:
    """最近一次提交完成的 MCP 状态快照。"""

    statuses: list[McpServerStatus] = field(default_factory=list)
    captured_at: float = 0.0
    source: str = "runtime"
    stale: bool = False
    config_revision: int = 0
    applied_revision: int = 0
    captured_monotonic: float = field(default=0.0, repr=False, compare=False)


_STREAMABLE_HTTP_ALIASES = frozenset(
    {
        "http",
        "https",
        "streamable_http",
        "streamablehttp",
        "streamable-http",
    }
)

_SSE_ALIASES = frozenset({"sse"})


def transport_to_canonical(transport: McpTransport) -> CanonicalTransport:
    if transport == McpTransport.SSE:
        return "sse"
    if transport == McpTransport.HTTP:
        return "streamable-http"
    return "stdio"


def canonical_to_transport(value: str) -> McpTransport:
    raw = str(value or "").lower().strip().replace("-", "_")
    if raw in _SSE_ALIASES:
        return McpTransport.SSE
    if raw in _STREAMABLE_HTTP_ALIASES:
        return McpTransport.HTTP
    return McpTransport.STDIO


def _resolve_transport(data: dict[str, Any]) -> McpTransport:
    """从 transport / type 字段推断传输方式。"""
    raw = str(data.get("transport") or data.get("type") or "").lower().strip()
    raw_norm = raw.replace("-", "_")
    url = str(data.get("url") or data.get("baseUrl") or "").strip()
    has_url = bool(url)
    has_command = bool(data.get("command"))

    if raw_norm in _SSE_ALIASES or (has_url and url.rstrip("/").endswith("/sse")):
        return McpTransport.SSE
    if raw_norm in _STREAMABLE_HTTP_ALIASES or (has_url and not has_command):
        return McpTransport.HTTP
    if has_command:
        return McpTransport.STDIO
    if has_url:
        return McpTransport.HTTP
    return McpTransport.STDIO


def _as_str_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value}
    return set()


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
