from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.tools.card import ToolCard


@dataclass
class ToolPackageInfo:
    id: str
    name: str
    version: str
    description: str
    config_key: str
    default_enabled: bool = True


class ToolPackage(ABC):
    def __init__(self, package_dir: Path):
        self.package_dir = package_dir
        self.info = self._load_manifest()

    def _load_manifest(self) -> ToolPackageInfo:
        manifest_path = self.package_dir / "manifest.yaml"
        data: dict[str, Any] = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        return ToolPackageInfo(
            id=data["id"],
            name=data["name"],
            version=str(data.get("version", "1.0.0")),
            description=data.get("description", ""),
            config_key=data["config_key"],
            default_enabled=bool(data.get("default_enabled", True)),
        )

    @abstractmethod
    def build_cards(self, config: dict | None = None) -> list[ToolCard]:
        raise NotImplementedError
