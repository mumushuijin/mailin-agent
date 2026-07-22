from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MaintenanceKind = Literal["sediment", "memory_nudge", "mcp_health"]
MaintenanceStatus = Literal["started", "done", "failed", "skipped"]


@dataclass
class MaintenanceResult:
    kind: MaintenanceKind
    message: str
    status: MaintenanceStatus = "done"
    success: bool = True
    session_id: str | None = None
    task_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def should_notify(self) -> bool:
        if self.status == "skipped":
            return False
        if self.kind == "mcp_health" and self.success and self.status == "done":
            # MCP 恢复连接时轻量通知一次即可
            return bool(self.detail.get("state_changed"))
        return True
