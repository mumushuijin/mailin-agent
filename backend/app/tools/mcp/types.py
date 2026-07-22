from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class McpTransport(str, Enum):
    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"


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


@dataclass
class McpServerConfig:
    """单个 MCP Server 的运行时配置。"""

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

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> McpServerConfig:
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

        return cls(
            name=name,
            enabled=bool(enabled),
            transport=transport,
            command=data.get("command"),
            args=[str(a) for a in (data.get("args") or [])],
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            url=url,
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            timeout=_safe_float(data.get("timeout"), 25.0),
            connect_timeout=_safe_float(data.get("connect_timeout"), 60.0),
            supports_parallel_tool_calls=bool(data.get("supports_parallel_tool_calls", False)),
            tools_filter=tools_filter,
            raw=data,
        )


_STREAMABLE_HTTP_ALIASES = frozenset({
    "http",
    "https",
    "streamable_http",
    "streamablehttp",
    "streamable-http",
})

_SSE_ALIASES = frozenset({"sse"})


def _resolve_transport(data: dict[str, Any]) -> McpTransport:
    """从 transport / type 字段推断传输方式。"""
    raw = str(data.get("transport") or data.get("type") or "").lower().strip()
    raw = raw.replace("-", "_")
    url = str(data.get("url") or data.get("baseUrl") or "").strip()
    has_url = bool(url)
    has_command = bool(data.get("command"))

    if raw in _SSE_ALIASES or (has_url and url.rstrip("/").endswith("/sse")):
        return McpTransport.SSE
    if raw in _STREAMABLE_HTTP_ALIASES or (has_url and not has_command):
        return McpTransport.HTTP
    if has_command:
        return McpTransport.STDIO
    if has_url:
        return McpTransport.HTTP
    return McpTransport.STDIO


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


McpConnectionState = Literal["disconnected", "connecting", "ready", "error"]


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
