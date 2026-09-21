"""测试用 settings 构造：CONFIG 落在独立 config_dir。"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def fake_settings(
    workspace: Path,
    *,
    defaults: Path | None = None,
    config_dir: Path | None = None,
    config_defaults: Path | None = None,
) -> Any:
    cfg_dir = config_dir or (workspace / "config")
    cfg_dir.mkdir(parents=True, exist_ok=True)
    defs = defaults or workspace
    cfg_defs = config_defaults or defs
    return type(
        "S",
        (),
        {
            "workspace_path": workspace,
            "workspace_defaults_path": defs,
            "config_dir": cfg_dir,
            "config_defaults_path": cfg_defs,
            "config_path": cfg_dir / "CONFIG.json",
        },
    )()


def write_config(settings: Any, data: dict) -> Path:
    import json

    path = Path(settings.config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_config(settings: Any) -> dict:
    import json

    return json.loads(Path(settings.config_path).read_text(encoding="utf-8"))
