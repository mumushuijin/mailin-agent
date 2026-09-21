"""跨 session 安全回归：snapshot / approval identity / sidecar。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tools.card import make_card
from app.tools.packages.filesystem import handlers as fs
from app.tools.packages.filesystem.snapshots import SnapshotStore
from app.tools.policy import build_execution_context, evaluate_policy
from app.tools.runtime import set_tool_project, set_tool_session


@pytest.fixture
def roots(tmp_path: Path, monkeypatch):
    home = tmp_path / "agent_home"
    defaults = tmp_path / "defaults"
    project = tmp_path / "project"
    home.mkdir()
    defaults.mkdir()
    project.mkdir()
    settings = type("S", (), {"workspace_path": home, "workspace_defaults_path": defaults})()
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    monkeypatch.setattr("app.tools.packages.filesystem.handlers.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.tools.registry.load_full_config",
        lambda workspace=None: {"tools": {"filesystem": {"snapshots": {"enabled": True}}}},
    )
    set_tool_project(project)
    return project, home, defaults


def test_path_escape_and_agent_home_denied(roots):
    project, home, defaults = roots
    set_tool_session("s1")
    card = make_card(
        package="filesystem",
        name="read_file",
        handler=lambda **_k: "x",
        summary="s",
        description="d",
        display_name="r",
        display_icon="📄",
        sandbox_policy="workspace_only",
    )
    deny = evaluate_policy(
        build_execution_context(
            card,
            {"file_path": "../outside.txt"},
            session_id="s1",
            approval_granted=True,
            workspace_root=project,
            agent_home=home,
            template_root=defaults,
        ),
        emit_audit=False,
    )
    assert deny.outcome == "deny"


def test_snapshot_cross_session_and_sidecar(roots):
    project, _home, _defaults = roots
    set_tool_session("s-a")
    result = fs.write_file("iso.txt", "a")
    assert "snapshot_id=" in result
    store = SnapshotStore(project)
    metas = store.list_metas()
    assert metas
    sid = metas[-1].snapshot_id

    set_tool_session("s-b")
    assert "跨 session" in fs.undo_file_change(sid) or "其他会话" in fs.undo_file_change(sid)
    assert "禁止" in fs.read_file(f".mailin/snapshots/{sid}/meta.json")
