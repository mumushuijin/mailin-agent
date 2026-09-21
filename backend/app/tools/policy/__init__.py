"""工具执行策略：上下文、sandbox 求值、权威 gate 与审计。"""

from __future__ import annotations

from app.tools.policy.audit import emit_policy_decision, sanitize_audit_payload
from app.tools.policy.gate import build_execution_context, evaluate_policy, gate_before_handler
from app.tools.policy.migration import (
    apply_migration_hints,
    diagnose_legacy_tool_config,
    resolve_enforcement_mode,
)
from app.tools.policy.resources import extract_resource_claims
from app.tools.policy.sandbox import (
    SNAPSHOTS_DIR_NAME,
    default_sidecar_roots,
    evaluate_sandbox_policy,
    snapshots_sidecar,
)
from app.tools.policy.types import (
    KNOWN_SANDBOX_POLICIES,
    DecisionOutcome,
    ExecutionContext,
    PolicyDecision,
    PolicyPreview,
    ResourceClaims,
    decision_allow,
    decision_deny,
    decision_needs_approval,
)

__all__ = [
    "KNOWN_SANDBOX_POLICIES",
    "SNAPSHOTS_DIR_NAME",
    "DecisionOutcome",
    "ExecutionContext",
    "PolicyDecision",
    "PolicyPreview",
    "ResourceClaims",
    "apply_migration_hints",
    "build_execution_context",
    "decision_allow",
    "decision_deny",
    "decision_needs_approval",
    "default_sidecar_roots",
    "diagnose_legacy_tool_config",
    "emit_policy_decision",
    "evaluate_policy",
    "evaluate_sandbox_policy",
    "extract_resource_claims",
    "gate_before_handler",
    "resolve_enforcement_mode",
    "sanitize_audit_payload",
    "snapshots_sidecar",
]
