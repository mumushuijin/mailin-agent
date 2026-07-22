from __future__ import annotations

import json
from typing import Callable

from app.resilience.policy import ResiliencePolicy
from app.resilience.registry import get_registry

GRAPH_REQUEST_TIMEOUT_SECONDS = 300.0
AGENT_TOOLS_TIMEOUT_SECONDS = 45.0

LLM_TRANSIENT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "502",
    "503",
    "504",
    "timeout",
    "timed out",
    "connection",
    "overloaded",
    "temporarily unavailable",
)

TOOL_DEFAULT_TIMEOUT_SECONDS = 15.0
MCP_DEFAULT_TIMEOUT_SECONDS = 25.0
MCP_MAX_TIMEOUT_SECONDS = 45.0
MCP_CALL_BUFFER_SECONDS = 5.0
LLM_AGENT_TIMEOUT_SECONDS = 60.0
CONTEXT_STEP_TIMEOUT_SECONDS = 10.0

AGENT_FAILURE_MESSAGE = "抱歉，本轮推理遇到问题，请稍后重试。"


def tool_error_json(message: str = "工具暂时不可用") -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def llm_agent_policy(*, dependency_id: str = "llm.agent") -> ResiliencePolicy:
    return ResiliencePolicy(
        dependency_id=dependency_id,
        timeout_seconds=LLM_AGENT_TIMEOUT_SECONDS,
        max_retries=2,
        retry_backoff_seconds=1.0,
        retry_jitter_seconds=0.5,
        transient_markers=LLM_TRANSIENT_MARKERS,
        circuit_failure_threshold=8,
        circuit_open_seconds=45.0,
    )


def llm_grace_policy() -> ResiliencePolicy:
    return ResiliencePolicy(
        dependency_id="llm.grace_summary",
        timeout_seconds=30.0,
        max_retries=1,
        retry_backoff_seconds=1.0,
        transient_markers=LLM_TRANSIENT_MARKERS,
    )


def tool_policy(tool_name: str, *, timeout: float | None = None) -> ResiliencePolicy:
    return ResiliencePolicy(
        dependency_id=f"tool:{tool_name}",
        timeout_seconds=timeout or TOOL_DEFAULT_TIMEOUT_SECONDS,
        max_retries=0,
        circuit_failure_threshold=10,
        circuit_open_seconds=30.0,
        fallback=lambda: tool_error_json(f"工具 '{tool_name}' 暂时不可用"),
    )


def mcp_tool_policy(tool_name: str, *, timeout: float | None = None) -> ResiliencePolicy:
    """MCP 工具外层包装策略：与 MCP handler 内层超时对齐。"""
    effective = min(timeout or MCP_DEFAULT_TIMEOUT_SECONDS, MCP_MAX_TIMEOUT_SECONDS)
    outer_timeout = effective + MCP_CALL_BUFFER_SECONDS
    return ResiliencePolicy(
        dependency_id=f"tool:{tool_name}",
        timeout_seconds=outer_timeout,
        max_retries=0,
        circuit_failure_threshold=10,
        circuit_open_seconds=30.0,
        fallback=lambda: tool_error_json(f"工具 '{tool_name}' 暂时不可用"),
    )


def effective_mcp_timeout(configured: float | None = None) -> float:
    """将 MCP 配置超时限制在用户可接受范围内（默认 25s，上限 45s）。"""
    base = configured if configured and configured > 0 else MCP_DEFAULT_TIMEOUT_SECONDS
    return min(base, MCP_MAX_TIMEOUT_SECONDS)


def mcp_call_deadline_seconds(configured: float | None = None) -> float:
    """单次 MCP 调用（含 wait_for / loop buffer）的最大等待秒数。"""
    return effective_mcp_timeout(configured) + MCP_CALL_BUFFER_SECONDS


def agent_tools_batch_timeout(tool_count: int) -> float:
    """工具节点一批 tool_call 的总超时（按个数线性扩展，上限 120s）。"""
    per_tool = max(TOOL_DEFAULT_TIMEOUT_SECONDS, mcp_call_deadline_seconds()) + 2.0
    return min(per_tool * max(1, tool_count), 120.0)


def context_step_policy(step: str, *, fallback: Callable[[], object]) -> ResiliencePolicy:
    return ResiliencePolicy(
        dependency_id=f"context.{step}",
        timeout_seconds=CONTEXT_STEP_TIMEOUT_SECONDS,
        max_retries=0,
        circuit_failure_threshold=5,
        circuit_open_seconds=20.0,
        fallback=fallback,
    )


def _mcp_retryable(exc: BaseException) -> bool:
    text = str(exc).lower()
    if any(marker in text for marker in ("401", "403", "unauthorized", "forbidden")):
        return False
    return any(marker.lower() in text for marker in MCP_TRANSIENT_MARKERS)


def mcp_call_policy(server_name: str, *, timeout: float) -> ResiliencePolicy:
    """MCP 工具调用的默认韧性策略（单点边界：熔断 + 超时，不重试避免与外层叠加）。"""
    effective = effective_mcp_timeout(timeout)
    return ResiliencePolicy(
        dependency_id=f"mcp:{server_name}",
        timeout_seconds=effective + MCP_CALL_BUFFER_SECONDS,
        max_retries=0,
        retry_on=_mcp_retryable,
        circuit_failure_threshold=5,
        circuit_open_seconds=30.0,
    )


MCP_TRANSIENT_MARKERS = (
    "RemoteProtocolError",
    "peer closed connection",
    "Error parsing JSON",
    "TimeoutError",
    "timed out",
    "连接",
)


def register_default_policies() -> None:
    registry = get_registry()
    registry.register_policy(llm_agent_policy())
    registry.register_policy(llm_grace_policy())
