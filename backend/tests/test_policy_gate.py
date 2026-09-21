"""执行策略基础：allow / needs_approval / deny 与 sandbox_policy。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tools.card import make_card
from app.tools.policy import (
    build_execution_context,
    evaluate_policy,
    evaluate_sandbox_policy,
    gate_before_handler,
    sanitize_audit_payload,
)
from app.tools.policy.types import (
    REASON_APPROVAL_REQUIRED,
    REASON_MISSING_SESSION,
    REASON_OK,
    REASON_PATH_ESCAPE,
    REASON_SHELL_HARD_DENY,
    REASON_UNKNOWN_SANDBOX_POLICY,
    ResourceClaims,
)


def _card(**kwargs):
    defaults = dict(
        package="filesystem",
        name="read_file",
        handler=lambda **_kw: "ok",
        summary="s",
        description="d",
        display_name="读",
        display_icon="📄",
        sandbox_policy="workspace_only",
    )
    defaults.update(kwargs)
    return make_card(**defaults)


@pytest.fixture
def roots(tmp_path: Path, monkeypatch):
    home = tmp_path / "agent_home"
    defaults = tmp_path / "defaults"
    project = tmp_path / "project"
    home.mkdir()
    defaults.mkdir()
    project.mkdir()
    (project / "notes.txt").write_text("hi", encoding="utf-8")
    settings = type("S", (), {"workspace_path": home, "workspace_defaults_path": defaults})()
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    monkeypatch.setattr("app.tools.policy.gate.get_settings", lambda: settings)
    return project, home, defaults


def test_decision_allow_needs_approval_deny(roots):
    project, home, defaults = roots
    card = _card(requires_confirmation=True, risk_level="moderate")

    allow_ctx = build_execution_context(
        _card(requires_confirmation=False, risk_level="safe"),
        {"file_path": "notes.txt"},
        tool_call_id="c1",
        session_id="s1",
        run_id="r1",
        approval_granted=True,
        workspace_root=project,
        agent_home=home,
        template_root=defaults,
    )
    allow = evaluate_policy(allow_ctx, emit_audit=False)
    assert allow.outcome == "allow"
    assert allow.reason_code == REASON_OK or allow.allowed

    need = evaluate_policy(
        build_execution_context(
            card,
            {"file_path": "notes.txt"},
            tool_call_id="c2",
            session_id="s1",
            workspace_root=project,
            agent_home=home,
            template_root=defaults,
        ),
        emit_audit=False,
    )
    assert need.outcome == "needs_approval"
    assert need.reason_code == REASON_APPROVAL_REQUIRED
    assert need.preview is not None

    deny = evaluate_policy(
        build_execution_context(
            card,
            {"file_path": "../outside.txt"},
            tool_call_id="c3",
            session_id="s1",
            approval_granted=True,
            workspace_root=project,
            agent_home=home,
            template_root=defaults,
        ),
        emit_audit=False,
    )
    assert deny.outcome == "deny"
    assert deny.reason_code == REASON_PATH_ESCAPE


def test_unknown_sandbox_policy_fail_closed(roots):
    project, home, defaults = roots
    card = _card(sandbox_policy="totally_unknown")
    decision = gate_before_handler(
        card,
        {"file_path": "notes.txt"},
        tool_call_id="c1",
        session_id="s1",
        approval_granted=True,
    )
    # gate uses live workspace from runtime; bind via build_execution_context paths
    ctx = build_execution_context(
        card,
        {"file_path": "notes.txt"},
        session_id="s1",
        approval_granted=True,
        workspace_root=project,
        agent_home=home,
        template_root=defaults,
    )
    decision = evaluate_sandbox_policy(ctx)
    assert decision.outcome == "deny"
    assert decision.reason_code == REASON_UNKNOWN_SANDBOX_POLICY


def test_session_scoped_requires_session(roots):
    project, home, defaults = roots
    card = _card(sandbox_policy="session_scoped")
    ctx = build_execution_context(
        card,
        {"file_path": "notes.txt"},
        session_id=None,
        approval_granted=True,
        workspace_root=project,
        agent_home=home,
        template_root=defaults,
    )
    # override resources session via evaluate
    from dataclasses import replace

    ctx = replace(ctx, sandbox_policy="session_scoped", session_id=None)
    decision = evaluate_sandbox_policy(ctx)
    assert decision.outcome == "deny"
    assert decision.reason_code == REASON_MISSING_SESSION


def test_none_policy_still_allows_without_extra_boundary(roots):
    project, home, defaults = roots
    card = _card(sandbox_policy="none", requires_confirmation=False, risk_level="safe")
    ctx = build_execution_context(
        card,
        {},
        session_id="s1",
        workspace_root=project,
        agent_home=home,
        template_root=defaults,
    )
    decision = evaluate_policy(ctx, emit_audit=False)
    assert decision.outcome == "allow"


def test_external_mcp_and_agent_home_only(roots):
    project, home, defaults = roots
    mcp = _card(package="mcp-demo", name="mcp_demo_ping", sandbox_policy="external_mcp")
    ctx = build_execution_context(
        mcp,
        {},
        session_id="s1",
        approval_granted=True,
        workspace_root=project,
        agent_home=home,
        template_root=defaults,
    )
    assert evaluate_sandbox_policy(ctx).outcome == "allow"

    home_only = _card(sandbox_policy="agent_home_only", name="memory_list")
    ok = build_execution_context(
        home_only,
        {},
        session_id="s1",
        approval_granted=True,
        workspace_root=project,
        agent_home=home,
        template_root=defaults,
    )
    assert evaluate_sandbox_policy(ok).outcome == "allow"

    bad = build_execution_context(
        home_only,
        {"file_path": "anything.txt"},
        session_id="s1",
        approval_granted=True,
        workspace_root=project,
        agent_home=home,
        template_root=defaults,
    )
    assert evaluate_sandbox_policy(bad).outcome == "deny"


def test_shell_hard_deny_before_handler(roots, monkeypatch):
    project, home, defaults = roots
    card = _card(
        package="shell",
        name="run_shell",
        sandbox_policy="workspace_only",
        requires_confirmation=True,
        risk_level="moderate",
    )
    ctx = build_execution_context(
        card,
        {"command": "rm -rf /"},
        session_id="s1",
        approval_granted=True,
        workspace_root=project,
        agent_home=home,
        template_root=defaults,
    )
    decision = evaluate_policy(ctx, emit_audit=False)
    assert decision.outcome == "deny"
    assert decision.reason_code == REASON_SHELL_HARD_DENY


def test_handler_not_called_when_denied(roots, monkeypatch):
    project, home, defaults = roots
    called = {"n": 0}

    def handler(**_kwargs):
        called["n"] += 1
        return "should-not-run"

    card = _card(handler=handler, sandbox_policy="totally_unknown")
    from app.tools.tool_search import invoke_card
    from app.tools.runtime import set_tool_project, set_tool_session

    set_tool_project(project)
    set_tool_session("s1")
    result = invoke_card(card, {"file_path": "notes.txt"})
    assert called["n"] == 0
    assert "policy_denied" in result or "unknown" in result.lower()


def test_audit_payload_has_identity_without_secrets(roots):
    project, home, defaults = roots
    card = _card(requires_confirmation=True)
    ctx = build_execution_context(
        card,
        {"file_path": "notes.txt", "content": "SECRET_TOKEN_VALUE_XYZ", "command": "export KEY=secret"},
        tool_call_id="call-1",
        session_id="sess-1",
        run_id="run-1",
        workspace_root=project,
        agent_home=home,
        template_root=defaults,
    )
    decision = evaluate_policy(ctx, emit_audit=False)
    payload = sanitize_audit_payload(ctx, decision)
    assert payload["identity"]["run_id"] == "run-1"
    assert payload["identity"]["session_id"] == "sess-1"
    assert payload["identity"]["tool_call_id"] == "call-1"
    blob = str(payload)
    assert "SECRET_TOKEN_VALUE_XYZ" not in blob
    assert "content" not in payload["context"].get("arg_keys", []) or "SECRET" not in blob


def test_build_execution_context_falls_back_to_session_workspace(tmp_path, monkeypatch):
    """ContextVar 为空时，仍应按 session 元数据恢复 workspace_root。"""
    from app.core.settings import Settings, init_workspace
    from app.storage.workspace import SessionStore
    from app.tools.runtime import set_tool_project

    home = tmp_path / "agent_home"
    defaults = Path(__file__).resolve().parents[1] / "workspace_defaults"
    config_defaults = Path(__file__).resolve().parents[1] / "app" / "config" / "defaults"
    if not config_defaults.exists():
        config_defaults = defaults
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    (project / "notes.txt").write_text("hi", encoding="utf-8")
    settings = Settings(
        workspace_path=home,
        workspace_defaults_path=defaults,
        config_dir=tmp_path / "config",
        config_defaults_path=config_defaults,
    )
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    monkeypatch.setattr("app.tools.policy.gate.get_settings", lambda: settings)
    monkeypatch.setattr("app.storage.project.get_settings", lambda: settings)
    init_workspace(settings)
    sid = SessionStore(home).create(str(project))
    set_tool_project(None)

    ctx = build_execution_context(
        _card(),
        {"file_path": "notes.txt"},
        session_id=sid,
        approval_granted=True,
    )
    assert ctx.workspace_root == project.resolve()
    decision = evaluate_policy(ctx, emit_audit=False)
    assert decision.outcome == "allow"
