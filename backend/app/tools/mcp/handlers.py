"""MCP 工具同步 handler 工厂（仿 Hermes _make_tool_handler）。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable

from app.resilience import CallContext, execute_sync
from app.resilience.policies import MCP_CALL_BUFFER_SECONDS, effective_mcp_timeout, mcp_call_policy
from app.tools.mcp.server_task import MCPServerTask

logger = logging.getLogger(__name__)

_CREDENTIAL_RE = re.compile(
    r"(Bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]+|token[=:]\s*\S+)",
    re.IGNORECASE,
)


def sanitize_error(text: str) -> str:
    return _CREDENTIAL_RE.sub("[REDACTED]", text)


def _format_tool_result(result) -> str:
    if getattr(result, "isError", False):
        parts = [
            block.text
            for block in (getattr(result, "content", None) or [])
            if hasattr(block, "text") and block.text
        ]
        return json.dumps(
            {"error": sanitize_error("\n".join(parts) or "MCP tool error")},
            ensure_ascii=False,
        )
    text_parts = [
        block.text
        for block in (getattr(result, "content", None) or [])
        if hasattr(block, "text") and block.text
    ]
    structured = getattr(result, "structuredContent", None)
    payload: dict[str, Any] = {}
    if text_parts:
        payload["result"] = "\n".join(text_parts)
    if structured is not None:
        payload["structuredContent"] = structured
    if not payload:
        payload["result"] = ""
    return json.dumps(payload, ensure_ascii=False)


async def _invoke_mcp_tool(
    server: MCPServerTask,
    tool_name: str,
    args: dict[str, Any],
    *,
    timeout: float,
) -> str:
    if not server._ready.is_set():
        ready = await server.wait_ready(min(timeout, 30.0))
        if not ready:
            raise RuntimeError(server.error or f"MCP server '{server.name}' 未就绪")

    async with server._rpc_lock:
        session = server.session
        if session is None:
            raise RuntimeError(f"MCP server '{server.name}' 会话已断开")
        result = await asyncio.wait_for(
            session.call_tool(tool_name, arguments=args or {}),
            timeout=timeout,
        )
    return _format_tool_result(result)


def make_call_tool_handler(
    server_name: str,
    tool_name: str,
    *,
    get_server: Callable[[str], MCPServerTask | None],
    timeout: float,
) -> Callable[..., str]:
    """返回符合 ToolCard.handler 签名的同步 callable。"""

    def _handler(**kwargs: Any) -> str:
        from app.tools.mcp.lifecycle import (
            ensure_mcp_connected,
            get_server_connect_error,
            server_connect_gave_up,
            reset_mcp_server,
        )
        from app.tools.mcp.loop import run_on_mcp_loop

        args = dict(kwargs)
        dependency_id = f"mcp:{server_name}"
        call_timeout = effective_mcp_timeout(timeout)
        loop_timeout = call_timeout + MCP_CALL_BUFFER_SECONDS

        if server_connect_gave_up(server_name):
            err = get_server_connect_error(server_name) or (
                f"MCP server '{server_name}' 连接失败，已暂停自动重试"
            )
            return json.dumps({"error": err}, ensure_ascii=False)

        started = time.monotonic()

        def _attempt() -> str:
            if server_connect_gave_up(server_name):
                raise RuntimeError(
                    get_server_connect_error(server_name)
                    or f"MCP server '{server_name}' 连接失败，已暂停自动重试"
                )
            ensure_mcp_connected(blocking=True)
            server = get_server(server_name)
            if server is None:
                raise RuntimeError(f"MCP server '{server_name}' 未连接")

            async def _call() -> str:
                return await _invoke_mcp_tool(server, tool_name, args, timeout=call_timeout)

            return run_on_mcp_loop(_call(), timeout=loop_timeout)

        outcome = execute_sync(
            dependency_id,
            _attempt,
            policy=mcp_call_policy(server_name, timeout=call_timeout),
            context=CallContext(metadata={"server": server_name, "tool": tool_name}),
            on_retry=lambda _attempt_no, _exc: reset_mcp_server(server_name),
        )

        if outcome.ok:
            logger.info(
                "MCP %s.%s 完成 (%.1fs, attempts=%d)",
                server_name,
                tool_name,
                (time.monotonic() - started),
                outcome.attempts,
            )
            return outcome.value  # type: ignore[return-value]

        error = sanitize_error(outcome.error or "MCP 调用失败")
        logger.warning(
            "MCP %s.%s 失败 (attempts=%d, %.1fs, circuit=%s): %s",
            server_name,
            tool_name,
            outcome.attempts,
            time.monotonic() - started,
            outcome.circuit_state.value,
            error,
        )
        return json.dumps({"error": error}, ensure_ascii=False)

    return _handler
