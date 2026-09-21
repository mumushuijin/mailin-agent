"""sandbox_policy 显式策略表求值。"""

from __future__ import annotations

from pathlib import Path

from app.storage.workspace import MAILIN_DIR, _is_same_or_child
from app.tools.policy.types import (
    KNOWN_SANDBOX_POLICIES,
    REASON_AGENT_SPACE,
    REASON_CONFIG_ERROR,
    REASON_MISSING_SESSION,
    REASON_MISSING_WORKSPACE,
    REASON_PATH_ESCAPE,
    REASON_RESOURCE_UNRESOLVED,
    REASON_SIDECAR_PROTECTED,
    REASON_UNKNOWN_SANDBOX_POLICY,
    ExecutionContext,
    PolicyDecision,
    PolicyPreview,
    decision_allow,
    decision_deny,
)

SNAPSHOTS_DIR_NAME = "snapshots"


def snapshots_sidecar(workspace: Path) -> Path:
    return workspace / MAILIN_DIR / SNAPSHOTS_DIR_NAME


def default_sidecar_roots(workspace: Path | None) -> tuple[Path, ...]:
    if workspace is None:
        return ()
    return (snapshots_sidecar(workspace),)


def _preview_for(ctx: ExecutionContext, risk_reason: str) -> PolicyPreview:
    target = ", ".join(ctx.resources.paths) if ctx.resources.paths else ctx.tool_name
    return PolicyPreview(
        operation=ctx.tool_name,
        target_summary=target,
        risk_reason=risk_reason,
        impact_summary="; ".join(ctx.resources.notes) if ctx.resources.notes else "",
        policy_summary=f"sandbox_policy={ctx.sandbox_policy}",
        cwd=ctx.resources.cwd,
        normalized_command=ctx.resources.command,
        capabilities=ctx.resources.capabilities,
        allowlist_matched=ctx.allowlist_matched,
    )


def _resolve_claimed_path(workspace: Path, relative: str) -> Path | None:
    rel = relative.strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    try:
        target = (workspace / rel).resolve()
    except OSError:
        return None
    root = workspace.resolve()
    if not str(target).startswith(str(root)):
        return None
    return target


def _path_hits_sidecar(path: Path, sidecars: tuple[Path, ...]) -> bool:
    for root in sidecars:
        if _is_same_or_child(path, root):
            return True
    return False


def _check_paths_in_workspace(ctx: ExecutionContext) -> PolicyDecision | None:
    if ctx.workspace_root is None:
        return decision_deny(
            reason="未绑定项目工作区，拒绝执行",
            reason_code=REASON_MISSING_WORKSPACE,
            sandbox_policy=ctx.sandbox_policy,
            preview=_preview_for(ctx, "缺少项目工作区"),
        )

    workspace = ctx.workspace_root
    sidecars = ctx.sidecar_roots or default_sidecar_roots(workspace)

    candidates = list(ctx.resources.paths)
    if ctx.resources.cwd:
        candidates.append(ctx.resources.cwd)

    for rel in candidates:
        resolved = _resolve_claimed_path(workspace, rel)
        if resolved is None:
            return decision_deny(
                reason=f"路径越界或无法解析: {rel}",
                reason_code=REASON_PATH_ESCAPE,
                sandbox_policy=ctx.sandbox_policy,
                preview=_preview_for(ctx, f"路径越界: {rel}"),
            )
        if ctx.agent_home and _is_same_or_child(resolved, Path(ctx.agent_home)):
            return decision_deny(
                reason="禁止访问 agent home",
                reason_code=REASON_AGENT_SPACE,
                sandbox_policy=ctx.sandbox_policy,
                preview=_preview_for(ctx, "触及 agent home"),
            )
        if ctx.template_root and _is_same_or_child(resolved, Path(ctx.template_root)):
            return decision_deny(
                reason="禁止访问模板目录",
                reason_code=REASON_AGENT_SPACE,
                sandbox_policy=ctx.sandbox_policy,
                preview=_preview_for(ctx, "触及模板目录"),
            )
        if _path_hits_sidecar(resolved, sidecars):
            return decision_deny(
                reason="禁止访问受保护的 runtime sidecar",
                reason_code=REASON_SIDECAR_PROTECTED,
                sandbox_policy=ctx.sandbox_policy,
                preview=_preview_for(ctx, "触及 snapshot sidecar"),
            )
    return None


def evaluate_sandbox_policy(ctx: ExecutionContext) -> PolicyDecision:
    """解释 ToolCard.sandbox_policy；未知策略 fail-closed。"""
    policy = (ctx.sandbox_policy or "none").strip() or "none"

    if policy not in KNOWN_SANDBOX_POLICIES:
        return decision_deny(
            reason=f"未知 sandbox_policy: {policy}",
            reason_code=REASON_UNKNOWN_SANDBOX_POLICY,
            sandbox_policy=policy,
            preview=_preview_for(ctx, f"未知策略 {policy}"),
            hard_boundary=True,
        )

    if ctx.resources.unresolved and policy in {"workspace_only", "session_scoped"}:
        return decision_deny(
            reason="无法解析工具资源声明",
            reason_code=REASON_RESOURCE_UNRESOLVED,
            sandbox_policy=policy,
            preview=_preview_for(ctx, "资源声明 unresolved"),
        )

    if policy == "none":
        # 无额外资源边界，但仍需基础 identity（由 gate 层检查 session/approval）
        return decision_allow(
            reason="无额外 sandbox 边界",
            sandbox_policy=policy,
            preview=_preview_for(ctx, "sandbox_policy=none"),
        )

    if policy == "workspace_only":
        denied = _check_paths_in_workspace(ctx)
        if denied:
            return denied
        return decision_allow(
            reason="路径落在项目工作区内",
            sandbox_policy=policy,
            preview=_preview_for(ctx, "workspace_only 通过"),
        )

    if policy == "session_scoped":
        if not ctx.session_id:
            return decision_deny(
                reason="session_scoped 工具需要有效 session",
                reason_code=REASON_MISSING_SESSION,
                sandbox_policy=policy,
                preview=_preview_for(ctx, "缺少 session"),
            )
        denied = _check_paths_in_workspace(ctx)
        if denied:
            return denied
        return decision_allow(
            reason="session 与工作区约束通过",
            sandbox_policy=policy,
            preview=_preview_for(ctx, "session_scoped 通过"),
        )

    if policy == "agent_home_only":
        # 仅允许声明的受控子目录；当前未声明受控子目录时 fail-closed，避免泛化任意 agent-home
        if not ctx.agent_home:
            return decision_deny(
                reason="agent_home_only 缺少 agent home 配置",
                reason_code=REASON_CONFIG_ERROR,
                sandbox_policy=policy,
                preview=_preview_for(ctx, "缺少 agent home"),
            )
        # 本 change 未引入泛化 agent-home 路径工具；若声明了路径且不在受控根则拒绝
        if ctx.resources.paths:
            return decision_deny(
                reason="agent_home_only 不允许任意路径声明",
                reason_code=REASON_AGENT_SPACE,
                sandbox_policy=policy,
                preview=_preview_for(ctx, "禁止泛化 agent-home 路径"),
            )
        return decision_allow(
            reason="agent_home_only 受控调用",
            sandbox_policy=policy,
            preview=_preview_for(ctx, "agent_home_only 通过"),
        )

    if policy == "external_mcp":
        # 外部调用策略：本地文件/进程声明不得借 MCP 绕过
        if ctx.resources.paths or ctx.resources.cwd or ctx.resources.command:
            denied = None
            if ctx.workspace_root is not None and (ctx.resources.paths or ctx.resources.cwd):
                denied = _check_paths_in_workspace(ctx)
            if denied:
                return denied
        return decision_allow(
            reason="external_mcp 策略已记录",
            sandbox_policy=policy,
            preview=_preview_for(ctx, "external_mcp 通过"),
        )

    return decision_deny(
        reason=f"未实现的 sandbox_policy: {policy}",
        reason_code=REASON_UNKNOWN_SANDBOX_POLICY,
        sandbox_policy=policy,
        preview=_preview_for(ctx, f"未实现策略 {policy}"),
    )
