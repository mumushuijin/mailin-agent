from pathlib import Path

import pytest

from app.config.persistence import atomic_write_toml, dumps_toml, loads_toml, read_toml


def test_nested_arrays_null_unknown_and_unicode_round_trip() -> None:
    document = {
        "schema_version": 1,
        "config": {"agent": {"model": "测试模型", "extra": None}},
        "unknown": {"array": [1, "汉字", None, {"future": True}]},
    }
    assert loads_toml(dumps_toml(document)) == document


def test_atomic_write_and_backup_recovery(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    atomic_write_toml(path, {"version": 1})
    atomic_write_toml(path, {"version": 2})
    assert read_toml(path) == {"version": 2}
    assert read_toml(path.with_name("config.toml.bak")) == {"version": 1}
    path.write_text("[broken\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_toml(path)
    assert read_toml(path, recover=True) == {"version": 1}
