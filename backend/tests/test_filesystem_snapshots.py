"""文件系统快照、mutation protocol 与 undo。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.tools.packages.filesystem import handlers as fs
from app.tools.packages.filesystem.snapshots import (
    SnapshotRetention,
    SnapshotStore,
    fingerprint_file,
    snapshots_sidecar,
)
from app.tools.policy.sandbox import snapshots_sidecar as policy_sidecar
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
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)
    monkeypatch.setattr("app.tools.packages.filesystem.handlers.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.tools.registry.load_full_config",
        lambda workspace=None: {
            "tools": {
                "filesystem": {
                    "snapshots": {
                        "enabled": True,
                        "retention": {
                            "max_count": 10,
                            "max_age_seconds": 86400,
                            "max_total_bytes": 10_000_000,
                        },
                    }
                }
            }
        },
    )
    set_tool_project(project)
    set_tool_session("session-a")
    return project, home


def _snapshot_id(text: str) -> str:
    m = re.search(r"\[snapshot_id=([0-9a-f]+)\]", text)
    assert m, text
    return m.group(1)


def test_snapshot_lands_in_project_sidecar_not_agent_home(workspace):
    project, home = workspace
    result = fs.write_file("notes/a.txt", "hello")
    sid = _snapshot_id(result)
    store = SnapshotStore(project)
    assert (store.root / sid / "meta.json").exists()
    assert str(store.root).startswith(str(project))
    assert not any(home.rglob(sid))
    assert policy_sidecar(project) == snapshots_sidecar(project)


def test_write_replace_snapshot_then_undo(workspace):
    _project, _home = workspace
    r1 = fs.write_file("doc.txt", "v1")
    sid1 = _snapshot_id(r1)
    assert "1|v1" in fs.read_file("doc.txt") or "v1" in fs.read_file("doc.txt")

    r2 = fs.replace_in_file("doc.txt", "v1", "v2")
    sid2 = _snapshot_id(r2)
    assert "v2" in fs.read_file("doc.txt")

    assert "已撤销" in fs.undo_file_change(sid2)
    assert "v1" in fs.read_file("doc.txt")
    assert sid1  # created


def test_snapshot_failure_leaves_file_unchanged(workspace, monkeypatch):
    project, _home = workspace
    fs.write_file("keep.txt", "original")
    # keep.txt 可能落在根或需显式路径
    path = project / "keep.txt"
    if not path.exists():
        # 若被重定向则找实际文件
        matches = list(project.rglob("keep.txt"))
        assert matches
        path = matches[0]
    original = path.read_text(encoding="utf-8")

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(SnapshotStore, "create_pre_change_snapshot", boom)
    result = fs.write_file("keep.txt", "should-not-apply")
    assert "快照创建失败" in result
    assert path.read_text(encoding="utf-8") == original


def test_move_delete_return_snapshot_and_undo(workspace):
    project, _home = workspace
    (project / "src").mkdir(exist_ok=True)
    (project / "src" / "x.txt").write_text("payload", encoding="utf-8")

    moved = fs.move_file("src/x.txt", "src/y.txt")
    move_sid = _snapshot_id(moved)
    assert (project / "src" / "y.txt").exists()
    assert not (project / "src" / "x.txt").exists()
    assert "已撤销移动" in fs.undo_file_change(move_sid)
    assert (project / "src" / "x.txt").exists()

    deleted = fs.delete_file("src/x.txt")
    del_sid = _snapshot_id(deleted)
    assert not (project / "src" / "x.txt").exists()
    assert "已恢复删除" in fs.undo_file_change(del_sid)
    assert (project / "src" / "x.txt").read_text(encoding="utf-8") == "payload"


def test_undo_conflict_and_cross_session(workspace):
    _project, _home = workspace
    r = fs.write_file("c.txt", "one")
    sid = _snapshot_id(r)
    fs.write_file("c.txt", "two")
    conflict = fs.undo_file_change(sid)
    assert "冲突" in conflict

    set_tool_session("session-b")
    other = fs.undo_file_change(sid)
    assert "跨 session" in other or "其他会话" in other


def test_retention_cleanup_and_sidecar_excluded(workspace):
    project, _home = workspace
    store = SnapshotStore(project)
    for i in range(3):
        fs.write_file(f"n{i}.md", f"content-{i}")
    removed = store.cleanup(
        SnapshotRetention(max_count=2, max_age_seconds=10**9, max_total_bytes=10**12)
    )
    assert isinstance(removed, list)
    assert len(store.list_metas()) <= 2

    r = fs.write_file("visible.md", "VISIBLE_MARKER_ONLY")
    sid = _snapshot_id(r)
    blob = store.blob_path(sid)
    if blob.exists():
        blob.write_text("SECRET_SNAPSHOT_BLOB_XYZ", encoding="utf-8")
    search = fs.search_files("SECRET_SNAPSHOT_BLOB_XYZ", ".")
    assert "未找到" in search or "L1:" not in search
    assert "禁止读取" in fs.read_file(f".mailin/snapshots/{sid}/meta.json")


def test_fingerprint_stable(workspace):
    project, _ = workspace
    p = project / "f.bin"
    p.write_bytes(b"abc")
    assert fingerprint_file(p) == fingerprint_file(p)
    assert fingerprint_file(None) is None
