"""导出配置文档 JSON Schema 与契约样例。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config.document import CanonicalConfigDocument, McpServerCanonicalModel
from app.config.modules import (
    MODULE_DISPLAY_NAMES,
    MODULE_MODELS,
    ORDINARY_MODULE_KEYS,
    USER_VISIBLE_MODULE_KEYS,
    default_module_value,
)

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"


def build_module_contract() -> dict[str, Any]:
    modules: dict[str, Any] = {}
    for key in ORDINARY_MODULE_KEYS:
        model_cls = MODULE_MODELS[key]
        schema = model_cls.model_json_schema(mode="serialization")
        modules[key] = {
            "key": key,
            "display_name": MODULE_DISPLAY_NAMES[key],
            "defaults": default_module_value(key),
            "json_schema": schema,
        }
    return {
        "schema_version": 1,
        "ordinary_modules": list(ORDINARY_MODULE_KEYS),
        "user_visible_modules": list(USER_VISIBLE_MODULE_KEYS),
        "enums": {
            "enforcement_mode": ["audit", "enforce"],
            "mcp_transport": ["stdio", "streamable-http", "sse"],
        },
        "modules": modules,
        "document_json_schema": CanonicalConfigDocument.model_json_schema(mode="serialization"),
        "mcp_server_json_schema": McpServerCanonicalModel.model_json_schema(mode="serialization"),
        "example": CanonicalConfigDocument().model_dump(mode="json"),
    }


def write_contracts(target_dir: Path | None = None) -> Path:
    target = target_dir or CONTRACTS_DIR
    target.mkdir(parents=True, exist_ok=True)
    payload = build_module_contract()
    path = target / "config-document.contract.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    example_path = target / "canonical-config.example.json"
    example_path.write_text(
        json.dumps(payload["example"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    out = write_contracts()
    print(f"wrote {out}")
