from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.tools.packages.shell.executor import execute_shell, format_shell_result, resolve_workdir
from app.tools.runtime import set_tool_project, set_tool_session


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    home = tmp_path / "agent_home"
    defaults = tmp_path / "defaults"
    project = tmp_path / "project"
    home.mkdir()
    defaults.mkdir()
    project.mkdir()
    settings = type("S", (), {"workspace_path": home, "workspace_defaults_path": defaults})()
    monkeypatch.setattr("app.tools.packages.shell.executor.get_settings", lambda: settings)
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    set_tool_project(project)
    return project


def test_resolve_workdir_rejects_escape(workspace: Path):
    path, err = resolve_workdir("../../etc")
    assert path is None
    assert err is not None


def test_execute_shell_echo(workspace: Path):
    set_tool_session("test-session")
    cmd = "echo hello" if sys.platform != "win32" else "echo hello"
    cwd, err = resolve_workdir(".")
    assert err is None
    assert cwd is not None

    output, code, error = execute_shell(
        cmd,
        cwd=cwd,
        timeout=10,
        env={"PATH": __import__("os").environ.get("PATH", "")},
        session_id="test-session",
    )
    assert error is None
    assert code == 0
    assert "hello" in output.lower()


def test_format_shell_result_includes_exit_code():
    result = format_shell_result("false", "err", 1, session_id=None)
    assert "退出码 1" in result


def test_registry_includes_shell_when_enabled():
    from app.tools.registry import ToolRegistry

    registry = ToolRegistry(
        config={
            "tools": {
                "filesystem": False,
                "memory": False,
                "calculator": False,
                "datetime": False,
                "web_search": False,
                "shell": True,
            }
        }
    )
    names = {card.name for card in registry.resolve_cards()}
    assert "run_shell" in names


def test_registry_excludes_shell_when_disabled():
    from app.tools.registry import ToolRegistry

    registry = ToolRegistry(
        config={
            "tools": {
                "filesystem": False,
                "memory": False,
                "calculator": False,
                "datetime": False,
                "web_search": False,
                "shell": {"enabled": False},
            }
        }
    )
    names = {card.name for card in registry.resolve_cards()}
    assert "run_shell" not in names
