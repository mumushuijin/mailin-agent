"""执行策略审计事件（摘要级，不含完整 secret / 文件内容）。"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.policy.types import ExecutionContext, PolicyDecision, execution_context_summary

logger = logging.getLogger("mailin.policy")

_MAX_REASON_CHARS = 500
_MAX_PREVIEW_CHARS = 300


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def sanitize_audit_payload(ctx: ExecutionContext, decision: PolicyDecision) -> dict[str, Any]:
    """构造可落日志的审计载荷：含 identity，剔除完整内容。"""
    preview = None
    if decision.preview is not None:
        raw = decision.preview.to_public_dict()
        preview = {
            "operation": raw.get("operation"),
            "target_summary": _truncate(str(raw.get("target_summary") or ""), _MAX_PREVIEW_CHARS),
            "risk_reason": _truncate(str(raw.get("risk_reason") or ""), _MAX_PREVIEW_CHARS),
            "policy_summary": raw.get("policy_summary"),
            "snapshot_status": raw.get("snapshot_status"),
            "snapshot_id": raw.get("snapshot_id"),
            "allowlist_matched": raw.get("allowlist_matched"),
            "capabilities": raw.get("capabilities"),
            "cwd": raw.get("cwd"),
            "normalized_command": _truncate(
                str(raw.get("normalized_command") or "") if raw.get("normalized_command") else None,
                _MAX_PREVIEW_CHARS,
            ),
        }
    return {
        "event": "policy_decision",
        "identity": ctx.identity(),
        "context": execution_context_summary(ctx),
        "decision": {
            "outcome": decision.outcome,
            "reason_code": decision.reason_code,
            "reason": _truncate(decision.reason, _MAX_REASON_CHARS),
            "hard_boundary": decision.hard_boundary,
            "sandbox_policy": decision.sandbox_policy,
        },
        "preview": preview,
    }


def emit_policy_decision(ctx: ExecutionContext, decision: PolicyDecision) -> dict[str, Any]:
    payload = sanitize_audit_payload(ctx, decision)
    logger.info(
        "[policy] outcome=%s reason_code=%s tool=%s session=%s run=%s call=%s",
        decision.outcome,
        decision.reason_code,
        ctx.tool_name,
        ctx.session_id,
        ctx.run_id,
        ctx.tool_call_id,
    )
    logger.debug("[policy] audit=%s", payload)
    return payload
