from __future__ import annotations

import json

from app.maintenance.types import MaintenanceResult
from app.tools.mcp.lifecycle import ensure_mcp_connected
from app.tools.mcp.status import build_mcp_status_payload

_last_health_snapshot: str | None = None


def check_mcp_health() -> MaintenanceResult | None:
    """检测 MCP 连接状态，仅在状态变化时返回需通知的结果。"""
    global _last_health_snapshot

    # 健康检查是显式 discovery 入口；mcp_status 本身只读取快照。
    ensure_mcp_connected(blocking=False)
    payload = build_mcp_status_payload()
    servers = payload.get("servers") or []
    if not payload.get("configured_servers"):
        return None

    snapshot = json.dumps(
        {srv["name"]: bool(srv.get("connected")) for srv in servers},
        sort_keys=True,
        ensure_ascii=False,
    )
    state_changed = snapshot != _last_health_snapshot
    _last_health_snapshot = snapshot

    disconnected = [srv for srv in servers if not srv.get("connected")]
    if disconnected:
        names = "、".join(srv["name"] for srv in disconnected)
        errors = [srv.get("error") for srv in disconnected if srv.get("error")]
        return MaintenanceResult(
            kind="mcp_health",
            message=f"MCP 服务未连接：{names}",
            status="done",
            success=False,
            detail={
                "state_changed": state_changed,
                "disconnected": [srv["name"] for srv in disconnected],
                "errors": errors[:3],
            },
        )

    if not state_changed:
        return None

    return MaintenanceResult(
        kind="mcp_health",
        message="MCP 服务已全部连接",
        status="done",
        success=True,
        detail={"state_changed": True, "servers": [srv["name"] for srv in servers]},
    )
