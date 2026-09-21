"""统一工具执行上下文、策略决策与预览结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

DecisionOutcome = Literal["allow", "needs_approval", "deny"]

SandboxPolicyName = Literal[
    "workspace_only",
    "session_scoped",
    "agent_home_only",
    "external_mcp",
    "none",
]

KNOWN_SANDBOX_POLICIES: frozenset[str] = frozenset(
    {
        "workspace_only",
        "session_scoped",
        "agent_home_only",
        "external_mcp",
        "none",
    }
)

# 稳定 reason_code：审计、前端与契约测试共用
REASON_OK = "ok"
REASON_UNKNOWN_SANDBOX_POLICY = "unknown_sandbox_policy"
REASON_MISSING_SESSION = "missing_session"
REASON_MISSING_WORKSPACE = "missing_workspace"
REASON_PATH_ESCAPE = "path_escape"
REASON_AGENT_SPACE = "agent_space_forbidden"
REASON_SIDECAR_PROTECTED = "sidecar_protected"
REASON_APPROVAL_REQUIRED = "approval_required"
REASON_SHELL_HARD_DENY = "shell_hard_deny"
REASON_SHELL_PARSE_UNRELIABLE = "shell_parse_unreliable"
REASON_EXTERNAL_MCP_DENIED = "external_mcp_denied"
REASON_RESOURCE_UNRESOLVED = "resource_unresolved"
REASON_CONFIG_ERROR = "config_error"


@dataclass(frozen=True)
class ResourceClaims:
    """规范化后的资源声明（路径 / cwd / 命令摘要等）。"""

    paths: tuple[str, ...] = ()
    cwd: str | None = None
    command: str | None = None
    executable: str | None = None
    args: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    external_endpoint: str | None = None
    unresolved: bool = False
    notes: tuple[str, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "paths": list(self.paths),
            "cwd": self.cwd,
            "command": self.command,
            "executable": self.executable,
            "args": list(self.args),
            "capabilities": list(self.capabilities),
            "external_endpoint": self.external_endpoint,
            "unresolved": self.unresolved,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class PolicyPreview:
    """可渲染、可审计的审批/拒绝预览（不含完整敏感内容）。"""

    operation: str
    target_summary: str
    risk_reason: str
    impact_summary: str = ""
    policy_summary: str = ""
    snapshot_status: str | None = None
    snapshot_id: str | None = None
    allowlist_matched: bool | None = None
    capabilities: tuple[str, ...] = ()
    cwd: str | None = None
    normalized_command: str | None = None
    timeout_seconds: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        data = {
            "operation": self.operation,
            "target_summary": self.target_summary,
            "risk_reason": self.risk_reason,
            "impact_summary": self.impact_summary,
            "policy_summary": self.policy_summary,
            "snapshot_status": self.snapshot_status,
            "snapshot_id": self.snapshot_id,
            "allowlist_matched": self.allowlist_matched,
            "capabilities": list(self.capabilities),
            "cwd": self.cwd,
            "normalized_command": self.normalized_command,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.extras:
            data["extras"] = dict(self.extras)
        return data


@dataclass(frozen=True)
class ExecutionContext:
    """不可变执行上下文：policy gate 的唯一输入。"""

    tool_name: str
    tool_package: str
    tool_call_id: str
    session_id: str | None
    run_id: str | None
    sandbox_policy: str
    risk_level: str
    requires_confirmation: bool
    args: dict[str, Any] = field(default_factory=dict)
    workspace_root: Path | None = None
    agent_home: Path | None = None
    template_root: Path | None = None
    sidecar_roots: tuple[Path, ...] = ()
    resources: ResourceClaims = field(default_factory=ResourceClaims)
    approval_granted: bool = False
    allowlist_matched: bool = False
    policy_version: str = "1"
    enforcement_mode: Literal["audit", "enforce"] = "enforce"

    def identity(self) -> dict[str, str | None]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_package": self.tool_package,
        }


@dataclass(frozen=True)
class PolicyDecision:
    outcome: DecisionOutcome
    reason_code: str
    reason: str
    preview: PolicyPreview | None = None
    sandbox_policy: str = "none"
    hard_boundary: bool = False

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "sandbox_policy": self.sandbox_policy,
            "hard_boundary": self.hard_boundary,
            "preview": self.preview.to_public_dict() if self.preview else None,
        }

    def to_tool_error_message(self) -> str:
        return (
            f"[policy_denied] {self.reason_code}: {self.reason}"
            if self.outcome == "deny"
            else self.reason
        )


def decision_allow(
    *,
    reason: str = "策略检查通过",
    reason_code: str = REASON_OK,
    sandbox_policy: str = "none",
    preview: PolicyPreview | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        outcome="allow",
        reason_code=reason_code,
        reason=reason,
        preview=preview,
        sandbox_policy=sandbox_policy,
        hard_boundary=False,
    )


def decision_needs_approval(
    *,
    reason: str,
    reason_code: str = REASON_APPROVAL_REQUIRED,
    sandbox_policy: str = "none",
    preview: PolicyPreview | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        outcome="needs_approval",
        reason_code=reason_code,
        reason=reason,
        preview=preview,
        sandbox_policy=sandbox_policy,
        hard_boundary=False,
    )


def decision_deny(
    *,
    reason: str,
    reason_code: str,
    sandbox_policy: str = "none",
    preview: PolicyPreview | None = None,
    hard_boundary: bool = True,
) -> PolicyDecision:
    return PolicyDecision(
        outcome="deny",
        reason_code=reason_code,
        reason=reason,
        preview=preview,
        sandbox_policy=sandbox_policy,
        hard_boundary=hard_boundary,
    )


def preview_to_dict(preview: PolicyPreview | None) -> dict[str, Any] | None:
    if preview is None:
        return None
    return preview.to_public_dict()


def execution_context_summary(ctx: ExecutionContext) -> dict[str, Any]:
    """审计用摘要，不含完整 args / secrets。"""
    return {
        **ctx.identity(),
        "sandbox_policy": ctx.sandbox_policy,
        "risk_level": ctx.risk_level,
        "requires_confirmation": ctx.requires_confirmation,
        "approval_granted": ctx.approval_granted,
        "allowlist_matched": ctx.allowlist_matched,
        "enforcement_mode": ctx.enforcement_mode,
        "policy_version": ctx.policy_version,
        "workspace": str(ctx.workspace_root) if ctx.workspace_root else None,
        "resources": ctx.resources.to_public_dict(),
        "arg_keys": sorted(ctx.args.keys()),
    }
