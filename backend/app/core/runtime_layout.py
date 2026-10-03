"""The single source of writable runtime and read-only resource paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = BACKEND_ROOT.parent


def _absolute(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} 必须是绝对路径: {path}")
    return path.resolve()


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@dataclass(frozen=True)
class RuntimeLayout:
    runtime_root: Path
    resources_root: Path

    @classmethod
    def resolve(
        cls,
        runtime_root: str | Path | None = None,
        resources_root: str | Path | None = None,
    ) -> RuntimeLayout:
        runtime = _absolute(
            runtime_root or os.getenv("MAILIN_RUNTIME_ROOT") or REPOSITORY_ROOT / ".runtime",
            "runtime-root",
        )
        resources = _absolute(
            resources_root or os.getenv("MAILIN_RESOURCES_DIR") or BACKEND_ROOT / "resources",
            "resources-root",
        )
        if _within(runtime, resources) or _within(resources, runtime):
            raise ValueError(f"运行根与只读资源目录不能重叠: {runtime} / {resources}")
        return cls(runtime, resources)

    @property
    def data(self) -> Path:
        return self.runtime_root / "data"

    @property
    def cache(self) -> Path:
        return self.runtime_root / "cache"

    @property
    def tmp(self) -> Path:
        return self.runtime_root / "tmp"

    @property
    def log(self) -> Path:
        return self.runtime_root / "log"

    @property
    def agent_home(self) -> Path:
        return self.data / "agent-home"

    @property
    def config_dir(self) -> Path:
        return self.data / "config"

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def workspace_defaults(self) -> Path:
        return self.resources_root / "defaults" / "workspace"

    @property
    def config_defaults(self) -> Path:
        return self.resources_root / "defaults" / "config"

    def ensure_writable(self) -> None:
        for directory in (self.data, self.cache, self.tmp, self.log):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                probe = directory / ".mailin-write-probe"
                with probe.open("w", encoding="utf-8") as handle:
                    handle.write("ok")
                probe.unlink()
            except OSError as exc:
                raise RuntimeError(f"运行目录不可写: {directory}: {exc}") from exc

    def validate_resources(self) -> None:
        for required in (self.workspace_defaults, self.config_defaults / "config.toml"):
            if not required.exists():
                raise RuntimeError(f"只读资源缺失: {required}")
