from __future__ import annotations

from contextvars import ContextVar

# 仅 WebSocket 聊天路径开启工具审批；SSE 降级路径保持直通。
approval_enabled: ContextVar[bool] = ContextVar("approval_enabled", default=False)
