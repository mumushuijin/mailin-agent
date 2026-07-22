from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import BaseMessage


@dataclass
class ContextBreakdown:
    bootstrap: int = 0
    skills: int = 0
    summary: int = 0
    recent_turns: int = 0
    tool_results: int = 0
    current_message: int = 0
    reserved: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "bootstrap": self.bootstrap,
            "skills": self.skills,
            "summary": self.summary,
            "recent_turns": self.recent_turns,
            "tool_results": self.tool_results,
            "current_message": self.current_message,
            "reserved": self.reserved,
        }

    @property
    def total(self) -> int:
        return sum(self.to_dict().values())


@dataclass
class ContextUsage:
    total_tokens: int
    max_tokens: int
    ratio: float
    breakdown: ContextBreakdown
    compression_layer: int | None = None
    warning: str | None = None
    # 本地粗估；仅在与 total_tokens（有效用量）不同时输出
    estimated_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "total_tokens": self.total_tokens,
            "max_tokens": self.max_tokens,
            "ratio": round(self.ratio, 4),
            "breakdown": self.breakdown.to_dict(),
            "compression_layer": self.compression_layer,
            "warning": self.warning,
        }
        if self.estimated_tokens is not None and self.estimated_tokens != self.total_tokens:
            payload["estimated_tokens"] = self.estimated_tokens
        return payload


@dataclass
class CompressionResult:
    summary: str
    kept_messages: list[BaseMessage]
    layer: int
    success: bool
    message: str | None = None
    layer3_strategy: str | None = None
    api_usages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AssembleResult:
    messages: list[BaseMessage]
    usage: ContextUsage
    context_summary: str
    compression_count: int
    rejected: bool = False
    reject_message: str | None = None
    compressing: bool = False
    compression_layer: int | None = None
    compression_api_usages: list[dict[str, Any]] = field(default_factory=list)
    memory_turn_counter: int = 0
    memory_nudge_pending: bool = False


class ContextOverflowError(Exception):
    """上下文已满，无法继续对话。"""

    def __init__(self, message: str = "上下文已满，请新建会话或清理历史。"):
        super().__init__(message)
        self.user_message = message
