from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from app.tools.packages.shell import handlers as shell_handlers
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
    set_tool_session("sess-a")
    set_tool_project(project)
    return project


def _sleep_cmd(seconds: float) -> str:
    return f'{sys.executable} -c "import time; time.sleep({seconds})"'


def test_background_list_and_kill(workspace: Path):
    started = shell_handlers.run_shell(_sleep_cmd(8), background=True)
    assert "已在后台启动作业" in started
    job_id = started.split("作业 ", 1)[1].split("（", 1)[0].strip()

    listed = shell_handlers.process("list")
    assert job_id in listed
    assert "running" in listed

    set_tool_session("sess-b")
    other = shell_handlers.process("list")
    assert job_id not in other
    assert "不存在" in shell_handlers.process("kill", job_id=job_id)

    set_tool_session("sess-a")
    killed = shell_handlers.process("kill", job_id=job_id)
    assert "已终止" in killed
    time.sleep(0.2)
    listed_after = shell_handlers.process("list")
    job_line = next(line for line in listed_after.splitlines() if line.startswith(job_id))
    assert "running" not in job_line
