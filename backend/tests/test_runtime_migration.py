import json
from pathlib import Path

import pytest

from app.config.persistence import read_toml
from app.core.runtime_layout import RuntimeLayout
from app.core.runtime_migration import migrate_legacy_layout


def _sources(parent: Path) -> dict[str, Path]:
    return {kind: parent / old for kind, old in (("data", ".devdata"), ("cache", ".devcache"), ("tmp", ".devtemp"), ("log", ".devlogs"))}


def test_migrates_four_directories_and_config_then_cleans_sources(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    for kind, source in sources.items():
        source.mkdir()
        (source / f"{kind}.txt").write_text(kind, encoding="utf-8")
    config = sources["data"] / "config" / "CONFIG.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"agent": {"model": "测试"}}), encoding="utf-8")
    layout = RuntimeLayout.resolve(tmp_path / ".runtime", tmp_path / "resources")

    assert migrate_legacy_layout(layout, legacy_roots=sources)
    assert all((layout.runtime_root / kind / f"{kind}.txt").read_text(encoding="utf-8") == kind for kind in sources)
    assert read_toml(layout.config_path)["agent"]["model"] == "测试"
    assert layout.config_path.with_name("config.toml.bak").is_file()
    assert not any(source.exists() for source in sources.values())


def test_invalid_json_preserves_all_sources(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    config = sources["data"] / "config" / "CONFIG.json"
    config.parent.mkdir(parents=True)
    config.write_text("{broken", encoding="utf-8")
    layout = RuntimeLayout.resolve(tmp_path / ".runtime", tmp_path / "resources")
    with pytest.raises(RuntimeError, match="旧配置迁移失败"):
        migrate_legacy_layout(layout, legacy_roots=sources)
    assert config.read_text(encoding="utf-8") == "{broken"
    assert not layout.runtime_root.exists()


def test_target_conflict_preserves_source(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    sources["data"].mkdir()
    layout = RuntimeLayout.resolve(tmp_path / ".runtime", tmp_path / "resources")
    layout.data.mkdir(parents=True)
    (layout.data / "existing.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(RuntimeError, match="目标冲突"):
        migrate_legacy_layout(layout, legacy_roots=sources)
    assert sources["data"].exists()
    assert (layout.data / "existing.txt").is_file()


def test_explicit_external_config_preference_preserves_alternate(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    internal = sources["data"] / "config" / "CONFIG.json"
    internal.parent.mkdir(parents=True)
    internal.write_text('{"agent":{"model":"alternate"}}', encoding="utf-8")
    external = tmp_path / "CONFIG.json"
    external.write_text('{"agent":{"model":"selected"}}', encoding="utf-8")
    layout = RuntimeLayout.resolve(tmp_path / ".runtime", tmp_path / "resources")

    migrate_legacy_layout(layout, legacy_roots=sources, legacy_config=external, prefer_legacy_config=True)
    assert read_toml(layout.config_path)["agent"]["model"] == "selected"
    assert read_toml(layout.config_dir / "legacy-alternate.toml.bak")["agent"]["model"] == "alternate"
    assert not external.exists()
    assert not (layout.config_dir / "CONFIG.json").exists()


def test_release_data_maps_logs_without_copying_install_resources(tmp_path: Path) -> None:
    install = tmp_path / "install" / "resources"
    template = install / "defaults" / "workspace" / "SOUL.md"
    template.parent.mkdir(parents=True)
    template.write_text("read-only", encoding="utf-8")
    old_data = tmp_path / "old-user-data"
    (old_data / "logs").mkdir(parents=True)
    (old_data / "logs" / "backend.log").write_text("log", encoding="utf-8")
    (old_data / "sessions").mkdir()
    (old_data / "sessions" / "index.json").write_text("{}", encoding="utf-8")
    layout = RuntimeLayout.resolve(tmp_path / "chosen-runtime", install)
    migrate_legacy_layout(layout, legacy_roots={"data": old_data})
    assert (layout.log / "backend.log").read_text(encoding="utf-8") == "log"
    assert (layout.data / "sessions" / "index.json").is_file()
    assert template.read_text(encoding="utf-8") == "read-only"
    assert not (layout.data / "defaults").exists()


def test_copy_failure_keeps_source_and_does_not_commit(tmp_path: Path, monkeypatch) -> None:
    sources = _sources(tmp_path)
    sources["data"].mkdir()
    (sources["data"] / "session.txt").write_text("keep", encoding="utf-8")
    layout = RuntimeLayout.resolve(tmp_path / ".runtime", tmp_path / "resources")

    def fail_copy(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("app.core.runtime_migration.shutil.copytree", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        migrate_legacy_layout(layout, legacy_roots=sources)
    assert (sources["data"] / "session.txt").read_text(encoding="utf-8") == "keep"
    assert not layout.runtime_root.exists()


def test_insufficient_space_keeps_source(tmp_path: Path, monkeypatch) -> None:
    sources = _sources(tmp_path)
    sources["data"].mkdir()
    (sources["data"] / "state.txt").write_text("keep", encoding="utf-8")
    layout = RuntimeLayout.resolve(tmp_path / ".runtime", tmp_path / "resources")
    monkeypatch.setattr("app.core.runtime_migration.shutil.disk_usage", lambda _path: type("U", (), {"free": 0})())
    with pytest.raises(RuntimeError, match="磁盘空间不足"):
        migrate_legacy_layout(layout, legacy_roots=sources)
    assert (sources["data"] / "state.txt").is_file()
    assert not layout.runtime_root.exists()


def test_invalid_model_keeps_source(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    config = sources["data"] / "config" / "CONFIG.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"agent":{"temperature":100}}', encoding="utf-8")
    layout = RuntimeLayout.resolve(tmp_path / ".runtime", tmp_path / "resources")
    with pytest.raises(RuntimeError, match="字段校验失败"):
        migrate_legacy_layout(layout, legacy_roots=sources)
    assert config.is_file()
    assert not layout.runtime_root.exists()


def test_permission_failure_keeps_source(tmp_path: Path, monkeypatch) -> None:
    sources = _sources(tmp_path)
    sources["data"].mkdir()
    (sources["data"] / "state.txt").write_text("keep", encoding="utf-8")
    layout = RuntimeLayout.resolve(tmp_path / ".runtime", tmp_path / "resources")

    def denied(*_args, **_kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr("app.core.runtime_migration.shutil.copytree", denied)
    with pytest.raises(PermissionError, match="access denied"):
        migrate_legacy_layout(layout, legacy_roots=sources)
    assert (sources["data"] / "state.txt").is_file()
    assert not layout.runtime_root.exists()


def test_stale_legacy_path_after_marker_is_rejected(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    sources["data"].mkdir()
    layout = RuntimeLayout.resolve(tmp_path / ".runtime", tmp_path / "resources")
    layout.data.mkdir(parents=True)
    (layout.data / ".runtime-migration-complete.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="旧路径仍存在"):
        migrate_legacy_layout(layout, legacy_roots=sources)
    assert sources["data"].exists()


def test_corrupt_toml_in_legacy_data_preserves_source(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    corrupt = sources["data"] / "config" / "config.toml"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("[broken", encoding="utf-8")
    layout = RuntimeLayout.resolve(tmp_path / ".runtime", tmp_path / "resources")
    with pytest.raises(RuntimeError, match="TOML 损坏"):
        migrate_legacy_layout(layout, legacy_roots=sources)
    assert corrupt.is_file()
    assert not layout.runtime_root.exists()


def test_release_source_cannot_be_install_resources(tmp_path: Path) -> None:
    resources = tmp_path / "install" / "resources"
    (resources / "defaults").mkdir(parents=True)
    layout = RuntimeLayout.resolve(tmp_path / "runtime", resources)
    with pytest.raises(RuntimeError, match="安装资源目录重叠"):
        migrate_legacy_layout(layout, legacy_roots={"data": resources / "defaults"})


def test_migration_accepts_new_nested_runtime_parent(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    sources["data"].mkdir()
    (sources["data"] / "session.txt").write_text("preserved", encoding="utf-8")
    layout = RuntimeLayout.resolve(tmp_path / "new" / "nested" / "runtime", tmp_path / "resources")

    assert migrate_legacy_layout(layout, legacy_roots=sources)
    assert (layout.data / "session.txt").read_text(encoding="utf-8") == "preserved"
