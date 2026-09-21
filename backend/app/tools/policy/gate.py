"""权威工具执行策略网关。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.settings import get_settings
from app.tools.card import ToolCard
from app.tools.policy.audit import emit_policy_decision
from app.tools.policy.resources import extract_resource_claims
from app.tools.policy.sandbox import default_sidecar_roots, evaluate_sandbox_policy
from app.tools.policy.types import (
    REASON_APPROVAL_REQUIRED,
    REASON_SHELL_HARD_DENY,
    ExecutionContext,
    PolicyDecision,
    PolicyPreview,
    decision_allow,
    decision_deny,
    decision_needs_approval,
)
from app.tools.runtime import get_project_workspace, tool_session_id


def _load_enforcement_mode() -> str:
    try:
        from app.tools.registry import load_full_config
        from app.tools.policy.migration import resolve_enforcement_mode

        return resolve_enforcement_mode(load_full_config())
    except Exception:
        return "enforce"


def build_execution_context(
    card: ToolCard,
    args: dict[str, Any] | None,
    *,
    tool_call_id: str = "",
    session_id: str | None = None,
    run_id: str | None = None,
    approval_granted: bool = False,
    allowlist_matched: bool = False,
    workspace_root: Path | None = None,
    agent_home: Path | None = None,
    template_root: Path | None = None,
) -> ExecutionContext:
    settings = get_settings()
    sid = session_id if session_id is not None else tool_session_id.get()
    workspace = workspace_root if workspace_root is not None else get_project_workspace()
    # ContextVar 在线程池边界可能丢失；按会话元数据回退，避免误报 missing_workspace
    if workspace is None and sid:
        try:
            from app.storage.project import session_project_path

            workspace = session_project_path(sid)
        except Exception:
            workspace = None
    home = agent_home if agent_home is not None else getattr(settings, "workspace_path", None)
    template = (
        template_root
        if template_root is not None
        else getattr(settings, "workspace_defaults_path", None)
    )
    resources = extract_resource_claims(card.name, args, workspace_root=workspace)
    return ExecutionContext(
        tool_name=card.name,
        tool_package=card.package,
        tool_call_id=tool_call_id or "",
        session_id=sid,
        run_id=run_id,
        sandbox_policy=card.sandbox_policy or "none",
        risk_level=card.risk_level or "safe",
        requires_confirmation=bool(card.requires_confirmation),
        args=dict(args or {}),
        workspace_root=Path(workspace) if workspace else None,
        agent_home=Path(home) if home else None,
        template_root=Path(template) if template else None,
        sidecar_roots=default_sidecar_roots(Path(workspace) if workspace else None),
        resources=resources,
        approval_granted=approval_granted,
        allowlist_matched=allowlist_matched,
        enforcement_mode=_load_enforcement_mode(),  # type: ignore[arg-type]
    )


def _shell_hard_deny(ctx: ExecutionContext) -> PolicyDecision | None:
    if ctx.tool_name != "run_shell":
        return None
    command = ctx.resources.command or str(ctx.args.get("command") or "")
    try:
        from app.tools.packages.shell.guards import check_command_guard

        reason = check_command_guard(command, safety_mode=True)
    except Exception:
        return None
    if not reason:
        return None
    preview = PolicyPreview(
        operation="run_shell",
        target_summary=(command[:200] + "…") if len(command) > 200 else command,
        risk_reason=reason,
        policy_summary="shell hard deny",
        normalized_command=command[:500],
        cwd=ctx.resources.cwd,
        capabilities=ctx.resources.capabilities,
    )
    return decision_deny(
        reason=reason,
        reason_code=REASON_SHELL_HARD_DENY,
        sandbox_policy=ctx.sandbox_policy,
        preview=preview,
        hard_boundary=True,
    )


def _approval_decision(ctx: ExecutionContext) -> PolicyDecision | None:
    if ctx.approval_granted:
        return None
    if not (ctx.requires_confirmation or ctx.risk_level == "dangerous"):
        return None
    if ctx.tool_name == "run_shell" and ctx.allowlist_matched:
        return None
    preview = PolicyPreview(
        operation=ctx.tool_name,
        target_summary=", ".join(ctx.resources.paths) or ctx.resources.command or ctx.tool_name,
        risk_reason=f"工具「{ctx.tool_name}」需要确认后执行",
        impact_summary=f"risk_level={ctx.risk_level}",
        policy_summary=f"sandbox_policy={ctx.sandbox_policy}",
        cwd=ctx.resources.cwd,
        normalized_command=ctx.resources.command,
        capabilities=ctx.resources.capabilities,
        allowlist_matched=ctx.allowlist_matched,
        snapshot_status="pending" if ctx.tool_package == "filesystem" else None,
    )
    return decision_needs_approval(
        reason=preview.risk_reason,
        reason_code=REASON_APPROVAL_REQUIRED,
        sandbox_policy=ctx.sandbox_policy,
        preview=preview,
    )


def evaluate_policy(ctx: ExecutionContext, *, emit_audit: bool = True) -> PolicyDecision:
    """权威策略求值：hard deny → sandbox → approval。"""
    hard = _shell_hard_deny(ctx)
    if hard is not None:
        decision = hard
    else:
        sandbox = evaluate_sandbox_policy(ctx)
        if sandbox.outcome == "deny":
            decision = sandbox
        else:
            approval = _approval_decision(ctx)
            decision = approval or decision_allow(
                reason=sandbox.reason,
                sandbox_policy=ctx.sandbox_policy,
                preview=sandbox.preview,
            )

    # audit 模式：非硬边界的 deny/needs_approval 可降级为观察，但硬边界永不放宽
    if (
        ctx.enforcement_mode == "audit"
        and decision.outcome == "deny"
        and not decision.hard_boundary
        and decision.reason_code not in {
            "unknown_sandbox_policy",
            "path_escape",
            "agent_space_forbidden",
            "sidecar_protected",
            "missing_session",
            "shell_hard_deny",
        }
    ):
        decision = decision_allow(
            reason=f"[audit] 本应拒绝: {decision.reason}",
            sandbox_policy=ctx.sandbox_policy,
            preview=decision.preview,
        )

    if emit_audit:
        emit_policy_decision(ctx, decision)
    return decision


def gate_before_handler(
    card: ToolCard,
    args: dict[str, Any] | None,
    *,
    tool_call_id: str = "",
    session_id: str | None = None,
    run_id: str | None = None,
    approval_granted: bool = False,
    allowlist_matched: bool = False,
) -> PolicyDecision:
    """handler 调用前的 gate；仅 allow 可继续。"""
    ctx = build_execution_context(
        card,
        args,
        tool_call_id=tool_call_id,
        session_id=session_id,
        run_id=run_id,
        approval_granted=approval_granted,
        allowlist_matched=allowlist_matched,
    )
    return evaluate_policy(ctx)
