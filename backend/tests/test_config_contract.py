"""前后端配置契约一致性测试。"""

from __future__ import annotations

import json
from pathlib import Path

from app.config.modules import (
    MODULE_DISPLAY_NAMES,
    MODULE_MODELS,
    ORDINARY_MODULE_KEYS,
    default_module_value,
)
from app.config.schema_export import build_module_contract, write_contracts

BACKEND_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "config"
    / "contracts"
    / "config-document.contract.json"
)
FRONTEND_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "contracts"
    / "config-document.contract.json"
)


def test_contract_files_exist_and_match_runtime_export():
    write_contracts()
    payload = build_module_contract()
    assert BACKEND_CONTRACT.exists()
    on_disk = json.loads(BACKEND_CONTRACT.read_text(encoding="utf-8"))
    assert on_disk["ordinary_modules"] == list(ORDINARY_MODULE_KEYS)
    assert on_disk["user_visible_modules"] == ["agent", "tools"]
    assert on_disk["enums"]["enforcement_mode"] == ["audit", "enforce"]
    assert on_disk["enums"]["mcp_transport"] == ["stdio", "streamable-http", "sse"]
    for key in ORDINARY_MODULE_KEYS:
        assert on_disk["modules"][key]["display_name"] == MODULE_DISPLAY_NAMES[key]
        assert on_disk["modules"][key]["defaults"] == default_module_value(key)
        assert "json_schema" in on_disk["modules"][key]
    assert on_disk["example"]["schema_version"] == 1
    assert set(on_disk["example"]["config"]) == set(MODULE_MODELS)
    assert payload["modules"]["agent"]["defaults"]["model"] == "qwen-plus"


def test_frontend_contract_copy_stays_in_sync():
    write_contracts()
    backend = json.loads(BACKEND_CONTRACT.read_text(encoding="utf-8"))
    assert FRONTEND_CONTRACT.exists(), "缺少 frontend/src/contracts 契约副本"
    frontend = json.loads(FRONTEND_CONTRACT.read_text(encoding="utf-8"))
    assert frontend["ordinary_modules"] == backend["ordinary_modules"]
    assert frontend["enums"] == backend["enums"]
    for key in ORDINARY_MODULE_KEYS:
        assert frontend["modules"][key]["defaults"] == backend["modules"][key]["defaults"]
        assert frontend["modules"][key]["display_name"] == backend["modules"][key]["display_name"]


def test_example_round_trips_through_canonical_document():
    from app.config import CanonicalConfigDocument, normalize_config_document

    example = json.loads(
        (BACKEND_CONTRACT.parent / "canonical-config.example.json").read_text(encoding="utf-8")
    )
    doc = CanonicalConfigDocument.model_validate(example)
    assert doc.schema_version == 1
    normalized = normalize_config_document(example)
    assert normalized.document.config.agent.model == doc.config.agent.model
