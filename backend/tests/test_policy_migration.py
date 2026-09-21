"""配置迁移与旧配置兼容。"""

from __future__ import annotations

from app.tools.policy.migration import (
    apply_migration_hints,
    diagnose_legacy_tool_config,
    resolve_enforcement_mode,
)
from app.tools.packages.filesystem.snapshots import load_snapshot_config
from app.tools.packages.shell.config import load_shell_config


def test_new_defaults_enforce():
    assert resolve_enforcement_mode({}) == "enforce"
    assert resolve_enforcement_mode({"tools": {}}) == "enforce"


def test_old_boolean_filesystem_still_loads():
    cfg = {"tools": {"filesystem": True, "shell": True}}
    snaps = load_snapshot_config(cfg)
    assert snaps.enabled is True
    shell = load_shell_config(cfg)
    assert shell.enabled is True


def test_legacy_hints_and_audit_mode():
    cfg = {
        "tools": {
            "enforcement_mode": "audit",
            "shell": {
                "auto_approve_patterns": ["git status"],
                "safety_mode": True,
            },
        }
    }
    hints = diagnose_legacy_tool_config(cfg)
    assert hints
    summary = apply_migration_hints(cfg)
    assert summary["enforcement_mode"] == "audit"
    assert summary["hard_boundaries_always_on"] is True
