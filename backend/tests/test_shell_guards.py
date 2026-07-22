from __future__ import annotations

import pytest

from app.tools.packages.shell.approval import should_auto_approve_run_shell
from app.tools.packages.shell.config import load_shell_config, matches_auto_approve
from app.tools.packages.shell.guards import check_command_guard


def test_guard_blocks_empty_command():
    assert check_command_guard("") is not None


def test_guard_blocks_rm_rf_root():
    reason = check_command_guard("rm -rf /")
    assert reason is not None
    assert "rm" in reason.lower() or "文件系统" in reason


def test_guard_blocks_credential_paths():
    assert check_command_guard("cat .env") is not None
    assert check_command_guard("type bootstraps\\MEMORY.md") is not None


def test_guard_allows_safe_command_when_safety_on():
    assert check_command_guard("git status", safety_mode=True) is None


def test_guard_skips_hardline_when_safety_off():
    assert check_command_guard("rm -rf /", safety_mode=False) is None


def test_auto_approve_patterns():
    patterns = ("git status", "git diff*")
    assert matches_auto_approve("git status", patterns)
    assert matches_auto_approve("git diff HEAD~1", patterns)
    assert not matches_auto_approve("git push", patterns)


def test_should_auto_approve_from_config(monkeypatch):
    config = {
        "tools": {
            "shell": {
                "enabled": True,
                "auto_approve_patterns": ["echo hello"],
            }
        }
    }
    monkeypatch.setattr(
        "app.tools.registry.load_full_config",
        lambda workspace=None: config,
    )
    assert should_auto_approve_run_shell("echo hello") is True
    assert should_auto_approve_run_shell("echo world") is False


def test_shell_disabled_in_config():
    cfg = load_shell_config({"tools": {"shell": False}})
    assert cfg.enabled is False
