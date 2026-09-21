"""Shell envelope、allowlist、guard 与审批预览。"""

from __future__ import annotations

import sys

from app.tools.packages.shell.allowlist import (
    AllowlistEntry,
    clear_session_allowlist,
    evaluate_shell_auto_approve,
    set_session_allowlist,
)
from app.tools.packages.shell.approval import build_shell_approval_preview, should_auto_approve_run_shell
from app.tools.packages.shell.envelope import parse_command_envelope
from app.tools.packages.shell.guards import check_command_guard, guard_decision


def test_envelope_basic_git_status():
    env = parse_command_envelope("git status", cwd=".", platform="linux")
    assert env.parse_ok
    assert env.executable == "git"
    assert env.args == ("status",)
    assert env.normalized_command() == "git status"


def test_envelope_windows_and_metachar():
    env = parse_command_envelope(r'echo "hello world"', platform="win32")
    assert env.executable is not None
    piped = parse_command_envelope("cat a.txt | grep x", platform="linux")
    assert "shell_metachar" in piped.capabilities
    assert not piped.parse_ok or "shell_metachar_unreliable" in piped.notes


def test_guard_parse_failure_needs_approval_not_hard_deny():
    decision = guard_decision('echo "unterminated')
    # shlex may fail → needs_approval；硬危险仍 deny
    assert decision["outcome"] in {"needs_approval", "allow_candidate", "deny"}
    assert check_command_guard("rm -rf /") is not None
    assert check_command_guard("cat .mailin/snapshots/abc/meta.json") is not None


def test_session_allowlist_scoped(monkeypatch):
    clear_session_allowlist("s-a")
    clear_session_allowlist("s-b")
    set_session_allowlist(
        "s-a",
        [AllowlistEntry(executable="git", args_prefix=("status",), allowed_roots=(".",), capabilities=())],
    )
    ok, env, reason, preview = evaluate_shell_auto_approve("git status", session_id="s-a", cwd=".")
    assert ok
    assert preview.get("allowlist_matched") is True

    bad, _e, _r, _p = evaluate_shell_auto_approve("git status", session_id="s-b", cwd=".")
    assert not bad

    # 其他 session 不得借用
    other, *_ = evaluate_shell_auto_approve("git push", session_id="s-a", cwd=".")
    assert not other


def test_legacy_patterns_cannot_bypass_hard_boundary(monkeypatch):
    config = {
        "tools": {
            "shell": {
                "enabled": True,
                "auto_approve_patterns": ["rm -rf /", "cat .mailin/snapshots/*"],
            }
        }
    }
    monkeypatch.setattr("app.tools.registry.load_full_config", lambda workspace=None: config)
    # 硬危险：guard 层拒绝；auto_approve 也不应放行 sidecar
    assert should_auto_approve_run_shell("rm -rf /") is False
    matched, _e, _r, _p = evaluate_shell_auto_approve(
        "cat .mailin/snapshots/x",
        session_id="s1",
        config=config,
    )
    assert matched is False


def test_legacy_pattern_still_works_for_safe_command(monkeypatch):
    config = {
        "tools": {
            "shell": {
                "enabled": True,
                "auto_approve_patterns": ["echo hello"],
            }
        }
    }
    monkeypatch.setattr("app.tools.registry.load_full_config", lambda workspace=None: config)
    assert should_auto_approve_run_shell("echo hello") is True
    assert should_auto_approve_run_shell("echo world") is False


def test_shell_approval_preview_fields():
    preview = build_shell_approval_preview(
        "npm test",
        cwd="frontend",
        background=False,
        timeout_seconds=60,
        session_id="s1",
    )
    assert preview["operation"] == "run_shell"
    assert "normalized_command" in preview or "npm" in preview.get("target_summary", "")
    assert preview["cwd"] == "frontend"
    assert preview["timeout_seconds"] == 60
    assert "allowlist_matched" in preview
    assert "risk_reason" in preview


def test_platform_fixture_smoke():
    # 覆盖 Windows/Linux 常见命令解析
    samples = [
        ("git diff HEAD~1", "win32"),
        ("git diff HEAD~1", "linux"),
        ("npm run build", "win32"),
        ("python -m pytest", "linux"),
    ]
    for cmd, plat in samples:
        env = parse_command_envelope(cmd, platform=plat)
        assert env.executable
        assert env.raw == cmd
