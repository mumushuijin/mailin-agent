from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from app.tools.packages.filesystem import handlers as fs
from app.tools.packages.shell.handlers import run_shell
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
    monkeypatch.setattr("app.tools.packages.filesystem.handlers.get_settings", lambda: settings)
    monkeypatch.setattr("app.tools.packages.shell.executor.get_settings", lambda: settings)
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    set_tool_session("eval-loop")
    set_tool_project(project)
    return project


def test_kernel_eval_loop_without_mcp(workspace: Path):
    (workspace / "sample.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (workspace / "test_sample.py").write_text(
        "from sample import add\n\ndef test_add():\n    assert add(1, 1) == 3\n",
        encoding="utf-8",
    )

    found = fs.glob_search("**/test_*.py")
    assert "test_sample.py" in found

    grep = fs.search_files(r"assert add\(1, 1\) == 3", ".")
    assert "test_sample.py" in grep
    assert "L4:" in grep or "L3:" in grep

    before = fs.read_file("test_sample.py")
    assert "assert add(1, 1) == 3" in before

    edited = fs.replace_in_file("test_sample.py", "assert add(1, 1) == 3", "assert add(1, 1) == 2")
    assert "替换 1 处" in edited

    result = run_shell(f"{sys.executable} -m pytest test_sample.py -q", timeout=60)
    assert "passed" in result.lower() or "1 passed" in result

    after = fs.read_file("test_sample.py")
    assert "assert add(1, 1) == 2" in after
    assert "assert add(1, 1) == 3" not in after
    diff_line = next(
        line for line in after.splitlines() if re.search(r"assert add\(1, 1\) == 2", line)
    )
    assert "== 2" in diff_line
