"""Verified one-time migration from legacy writable directories."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from app.config import normalize_config_document
from app.config.persistence import atomic_write_toml, read_toml
from app.core.runtime_layout import RuntimeLayout

LEGACY_NAMES = {".devdata": "data", ".devcache": "cache", ".devtemp": "tmp", ".devlogs": "log"}
MARKER = ".runtime-migration-complete.json"


def migrate_legacy_layout(
    layout: RuntimeLayout,
    *,
    legacy_roots: dict[str, Path],
    legacy_config: Path | None = None,
    prefer_legacy_config: bool = False,
    dry_run: bool = False,
) -> bool:
    """Stage, verify and commit an old layout; never overwrite a nonempty target."""
    sources = {kind: Path(path) for kind, path in legacy_roots.items() if Path(path).exists()}
    if legacy_config is not None and legacy_config.exists():
        legacy_config = Path(legacy_config)
    else:
        legacy_config = None
    if not sources and legacy_config is None:
        return False

    target = layout.runtime_root
    marker = target / "data" / MARKER
    if marker.exists():
        raise RuntimeError(f"迁移已完成但旧路径仍存在，需清理: {', '.join(map(str, [*sources.values(), *([legacy_config] if legacy_config else [])]))}")
    if not dry_run and target.exists() and any(target.iterdir()):
        raise RuntimeError(f"迁移目标冲突，请选择空的运行根: {target}")
    for source in sources.values():
        resolved = source.resolve()
        if resolved == target or resolved in target.parents or target in resolved.parents:
            raise RuntimeError(f"迁移源与目标重叠: {source} / {target}")
        resources = layout.resources_root
        if resolved == resources or resources in resolved.parents or resolved in resources.parents:
            raise RuntimeError(f"迁移源不能与安装资源目录重叠: {source} / {resources}")

    bytes_needed = sum(file.stat().st_size for source in sources.values() if source.is_dir() for file in source.rglob("*") if file.is_file())
    if legacy_config is not None:
        bytes_needed += legacy_config.stat().st_size
    usage_root = target.parent
    while not usage_root.exists():
        usage_root = usage_root.parent
    if shutil.disk_usage(usage_root).free < bytes_needed * 2 + 1024 * 1024:
        raise RuntimeError(f"迁移目标磁盘空间不足: {target}")

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mailin-migrate-", dir=target.parent) as staging_name:
        stage = Path(staging_name)
        for name in ("data", "cache", "tmp", "log"):
            (stage / name).mkdir()
        for kind, source in sources.items():
            destination = stage / kind
            if not source.is_dir():
                raise RuntimeError(f"迁移源不是目录: {source}")
            shutil.copytree(source, destination, dirs_exist_ok=True)
            _verify_tree(source, destination)
        embedded_logs = stage / "data" / "logs"
        if "log" not in sources and embedded_logs.is_dir():
            shutil.copytree(embedded_logs, stage / "log", dirs_exist_ok=True)
            shutil.rmtree(embedded_logs)

        candidates = [stage / "data" / "config" / "CONFIG.json", stage / "data" / "agent-home" / "CONFIG.json"]
        candidates.extend([legacy_config] if legacy_config is not None else [])
        existing = [candidate for candidate in candidates if candidate.is_file()]
        if len(existing) > 1 and not (prefer_legacy_config and legacy_config in existing):
            raise RuntimeError(f"发现多个旧配置，无法决定迁移来源: {', '.join(map(str, existing))}")
        if existing:
            source = legacy_config if prefer_legacy_config and legacy_config in existing else existing[0]
            try:
                for alternate in existing:
                    if alternate == source:
                        continue
                    backup = stage / "data" / "config" / "legacy-alternate.toml.bak"
                    atomic_write_toml(backup, json.loads(alternate.read_text(encoding="utf-8")))
                legacy = json.loads(source.read_text(encoding="utf-8"))
                if not isinstance(legacy, dict):
                    raise ValueError("配置根必须是对象")
                normalized = normalize_config_document(legacy)
                if normalized.module_errors:
                    raise ValueError(f"配置字段校验失败: {', '.join(normalized.module_errors)}")
                normalized.document.model_validate(normalized.document.to_storage_dict())
                config_target = stage / "data" / "config" / "config.toml"
                if config_target.exists():
                    raise RuntimeError(f"配置迁移目标冲突: {config_target}")
                atomic_write_toml(config_target, legacy)
                if read_toml(config_target) != legacy:
                    raise RuntimeError(f"配置重载校验失败: {config_target}")
                if normalize_config_document(read_toml(config_target)).module_errors:
                    raise RuntimeError(f"配置模型重载校验失败: {config_target}")
                shutil.copy2(config_target, config_target.with_name("config.toml.bak"))
                for old in existing:
                    if old.is_relative_to(stage):
                        old.unlink()
            except (OSError, ValueError, RuntimeError) as exc:
                raise RuntimeError(f"旧配置迁移失败: {source}: {exc}") from exc

        for previous in (stage / "data" / "config").glob("CONFIG.json.bak"):
            try:
                value = json.loads(previous.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("备份配置根必须是对象")
                atomic_write_toml(previous.with_name("legacy-previous.toml.bak"), value)
                previous.unlink()
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"旧配置备份迁移失败: {previous}: {exc}") from exc

        existing_toml = stage / "data" / "config" / "config.toml"
        if existing_toml.exists():
            try:
                parsed = read_toml(existing_toml)
                if normalize_config_document(parsed).module_errors:
                    raise ValueError("配置字段校验失败")
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"迁移目标 TOML 损坏: {existing_toml}: {exc}") from exc

        (stage / "data" / MARKER).write_text(
            json.dumps({"version": 1, "sources": {kind: str(path) for kind, path in sources.items()}}, ensure_ascii=False),
            encoding="utf-8",
        )
        for kind, source in sources.items():
            _verify_tree(source, stage / kind, excluded={"CONFIG.json", "logs"} if kind == "data" else {"CONFIG.json"})

        if dry_run:
            return True
        if target.exists():
            target.rmdir()
        stage.replace(target)
        for source in sources.values():
            shutil.rmtree(source)
        if legacy_config is not None:
            legacy_config.unlink(missing_ok=True)
    return True


def _verify_tree(source: Path, target: Path, *, excluded: set[str] | None = None) -> None:
    excluded = excluded or set()
    for file in source.rglob("*"):
        if not file.is_file() or any(part in excluded for part in file.relative_to(source).parts):
            continue
        copied = target / file.relative_to(source)
        if not copied.is_file() or copied.read_bytes() != file.read_bytes():
            raise RuntimeError(f"迁移文件校验失败: {file} -> {copied}")


def migrate_development_layout(layout: RuntimeLayout) -> bool:
    repo = layout.runtime_root.parent
    if layout.runtime_root.name != ".runtime":
        return False
    roots = {new: repo / old for old, new in LEGACY_NAMES.items()}
    legacy_config = repo / "backend" / "app" / "config" / "CONFIG.json"
    return migrate_legacy_layout(layout, legacy_roots=roots, legacy_config=legacy_config, prefer_legacy_config=True)
