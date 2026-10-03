from pathlib import Path

import pytest

from app.config.persistence import read_toml
from app.core.settings import Settings, init_workspace, validate_runtime_paths
from app.storage.workspace import ensure_runtime_config


def _resources(root: Path) -> Path:
    resources = root / 'resources'
    bootstraps = resources / 'defaults' / 'workspace' / 'bootstraps'
    bootstraps.mkdir(parents=True)
    (bootstraps / 'HEARTBEAT.md').write_text('heartbeat', encoding='utf-8')
    (bootstraps / 'memory_sections.json').write_text('{}', encoding='utf-8')
    (bootstraps / 'user_sections.json').write_text('{}', encoding='utf-8')
    config = resources / 'defaults' / 'config' / 'config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('[agent]\nmodel = "default"\n', encoding='utf-8')
    return resources


def test_development_defaults_use_one_root(monkeypatch) -> None:
    monkeypatch.delenv('MAILIN_RUNTIME_ROOT')
    settings = Settings()
    assert settings.runtime_root.name == '.runtime'
    assert settings.workspace_path == settings.runtime_root / 'data' / 'agent-home'
    assert settings.cache_dir == settings.runtime_root / 'cache'
    assert settings.temp_dir == settings.runtime_root / 'tmp'
    assert settings.log_dir == settings.runtime_root / 'log'
    assert settings.config_path == settings.runtime_root / 'data' / 'config' / 'config.toml'


def test_rejects_runtime_root_inside_resources(tmp_path: Path) -> None:
    resources = _resources(tmp_path)
    settings = Settings(mode='production', runtime_root=resources / 'runtime', resources_dir=resources)
    with pytest.raises(ValueError, match='不能重叠'):
        validate_runtime_paths(settings)


def test_init_workspace_creates_four_directories_and_toml(tmp_path: Path) -> None:
    resources = _resources(tmp_path)
    settings = Settings(mode='production', runtime_root=tmp_path / 'runtime', resources_dir=resources)
    init_workspace(settings)
    assert {entry.name for entry in settings.runtime_root.iterdir()} == {'data', 'cache', 'tmp', 'log'}
    assert read_toml(settings.config_path)['agent']['model'] == 'default'
    assert (settings.workspace_path / 'bootstraps' / 'HEARTBEAT.md').is_file()


def test_legacy_json_is_not_loaded_by_normal_runtime(tmp_path: Path) -> None:
    resources = _resources(tmp_path)
    settings = Settings(mode='production', runtime_root=tmp_path / 'runtime', resources_dir=resources)
    legacy = settings.workspace_path / 'CONFIG.json'
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"agent":{"model":"legacy"}}', encoding='utf-8')
    target = ensure_runtime_config(settings)
    assert read_toml(target)['agent']['model'] == 'default'
    assert legacy.exists()


def test_reset_copies_default_without_changing_resources(tmp_path: Path) -> None:
    from app.storage.workspace import ConfigStore
    resources = _resources(tmp_path)
    settings = Settings(mode='production', runtime_root=tmp_path / 'runtime', resources_dir=resources)
    init_workspace(settings)
    default = settings.config_defaults_path / 'config.toml'
    before = default.read_bytes()
    settings.config_path.write_text('[agent]\nmodel = "changed"\n', encoding='utf-8')
    ConfigStore(settings.workspace_path, settings.workspace_defaults_path, config_defaults=settings.config_defaults_path).reset_global()
    assert read_toml(settings.config_path)['agent']['model'] == 'default'
    assert default.read_bytes() == before


def test_missing_resource_stops_before_runtime_creation(tmp_path: Path) -> None:
    settings = Settings(mode='production', runtime_root=tmp_path / 'runtime', resources_dir=tmp_path / 'missing')
    with pytest.raises(RuntimeError, match='只读资源缺失'):
        init_workspace(settings)
    assert not settings.runtime_root.exists()


def test_unwritable_runtime_path_reports_the_path(tmp_path: Path) -> None:
    resources = _resources(tmp_path)
    occupied = tmp_path / 'occupied'
    occupied.write_text('file', encoding='utf-8')
    settings = Settings(mode='production', runtime_root=occupied, resources_dir=resources)
    with pytest.raises(RuntimeError, match='运行目录不可写') as error:
        init_workspace(settings)
    assert str(occupied) in str(error.value)


def test_explicit_runtime_and_resources_roots_are_respected(tmp_path: Path) -> None:
    resources = _resources(tmp_path)
    settings = Settings(mode='production', runtime_root=tmp_path / 'custom-runtime', resources_dir=resources)
    init_workspace(settings)
    assert settings.config_path == tmp_path / 'custom-runtime' / 'data' / 'config' / 'config.toml'
    assert settings.workspace_defaults_path == resources / 'defaults' / 'workspace'
