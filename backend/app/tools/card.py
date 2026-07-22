from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

RiskLevel = Literal["safe", "moderate", "dangerous"]
ToolSource = Literal["builtin", "user", "plugin"]


@dataclass(frozen=True)
class ToolDisplayConfig:
    name: str
    icon: str = "🔧"
    hidden: bool = False


@dataclass
class ToolCard:
    """工具统一身份卡：元数据单一来源，投影至 LLM / 前端 / 审计。"""

    id: str
    name: str
    package: str
    summary: str
    description: str
    handler: Callable[..., str]
    version: str = "1.0.0"
    parameters: dict[str, Any] = field(default_factory=dict)
    risk_level: RiskLevel = "safe"
    requires_confirmation: bool = False
    sandbox_policy: str = "none"
    display: ToolDisplayConfig | None = None
    enabled: bool = True
    source: ToolSource = "builtin"

    def to_public_dict(self) -> dict[str, Any]:
        """供 API / 前端使用的公开视图（不含 handler）。"""
        return {
            "id": self.id,
            "name": self.name,
            "package": self.package,
            "version": self.version,
            "summary": self.summary,
            "description": self.description,
            "parameters": self.parameters,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "sandbox_policy": self.sandbox_policy,
            "display": {
                "name": self.display.name,
                "icon": self.display.icon,
                "hidden": self.display.hidden,
            }
            if self.display
            else None,
            "enabled": self.enabled,
            "source": self.source,
        }


def make_card(
    *,
    package: str,
    name: str,
    handler: Callable[..., str],
    summary: str,
    description: str,
    display_name: str,
    display_icon: str,
    parameters: dict[str, Any] | None = None,
    risk_level: RiskLevel = "safe",
    requires_confirmation: bool = False,
    sandbox_policy: str = "none",
    version: str = "1.0.0",
    source: ToolSource = "builtin",
    hidden: bool = False,
) -> ToolCard:
    return ToolCard(
        id=f"{package}.{name}",
        name=name,
        package=package,
        version=version,
        summary=summary,
        description=description,
        parameters=parameters or {},
        risk_level=risk_level,
        requires_confirmation=requires_confirmation,
        sandbox_policy=sandbox_policy,
        handler=handler,
        display=ToolDisplayConfig(name=display_name, icon=display_icon, hidden=hidden),
        source=source,
    )
