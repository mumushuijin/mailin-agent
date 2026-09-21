"""规范配置模型与归一化适配层测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import (
    MODULE_MODELS,
    ORDINARY_MODULE_KEYS,
    AgentModule,
    CanonicalConfigDocument,
    SkillsModule,
    TelemetryModule,
    ToolsModule,
    default_module_value,
    merge_module_into_raw,
    normalize_config_document,
    preserve_unknown_fields,
    project_effective_runtime_config,
    project_module_for_api,
    redact_mapping_secrets,
    validate_module_value,
)
from app.tools.mcp.types import MASK_LITERAL


class TestModuleModels:
    def test_agent_defaults_and_required_shape(self):
        agent = AgentModule()
        dumped = agent.model_dump()
        assert dumped["model"] == "qwen-plus"
        assert dumped["temperature"] == 0.7
        assert dumped["max_steps"] == 48
        assert "graph_version" in dumped

    def test_agent_rejects_invalid_enum_like_ranges(self):
        with pytest.raises(ValidationError):
            AgentModule(temperature=3.0)
        with pytest.raises(ValidationError):
            AgentModule(max_steps=0)

    def test_tools_enforcement_mode_enum(self):
        tools = ToolsModule(enforcement_mode="audit")
        assert tools.enforcement_mode == "audit"
        with pytest.raises(ValidationError):
            ToolsModule(enforcement_mode="strict")

    def test_tools_shell_timeout_constraint(self):
        with pytest.raises(ValidationError):
            ToolsModule(
                shell={
                    "enabled": True,
                    "default_timeout": 100,
                    "max_timeout": 50,
                }
            )

    def test_skills_and_telemetry_defaults(self):
        skills = SkillsModule()
        assert skills.global_roots == ["skills"]
        assert skills.disabled == []
        telemetry = TelemetryModule()
        assert telemetry.langsmith_enabled is False
        with pytest.raises(ValidationError):
            TelemetryModule(sample_rate=1.5)

    def test_registry_covers_ordinary_modules(self):
        assert set(MODULE_MODELS) == set(ORDINARY_MODULE_KEYS)
        for key in ORDINARY_MODULE_KEYS:
            value = default_module_value(key)
            assert isinstance(value, dict)
            validated = validate_module_value(key, value)
            assert validated is not None

    def test_unknown_fields_preserved_on_module(self):
        agent = AgentModule.model_validate({"model": "x", "custom_flag": True})
        assert agent.model_dump()["custom_flag"] is True


class TestNormalize:
    def test_legacy_top_level_projects_into_config(self):
        raw = {
            "agent": {"model": "legacy-model", "temperature": 0.2, "max_steps": 10},
            "tools": {"enforcement_mode": "audit", "memory": False},
            "mcp_servers": {
                "demo": {
                    "enabled": True,
                    "type": "sse",
                    "url": "https://example.com/sse",
                }
            },
        }
        result = normalize_config_document(raw)
        assert result.document.config.agent.model == "legacy-model"
        assert result.document.config.tools.enforcement_mode == "audit"
        assert result.source["agent"] == "legacy"
        assert "demo" in result.document.mcp.servers
        assert result.source["mcp"] == "mcp_servers"

    def test_canonical_config_preferred_over_legacy(self):
        raw = {
            "schema_version": 1,
            "config": {
                "agent": {"model": "canonical", "temperature": 0.5, "max_steps": 20},
            },
            "agent": {"model": "legacy", "temperature": 0.1, "max_steps": 5},
            "mcp": {
                "version": 1,
                "servers": {
                    "http1": {
                        "enabled": True,
                        "connection": {
                            "type": "streamable-http",
                            "url": "https://example.com/mcp",
                        },
                    }
                },
            },
            "mcp_servers": {
                "legacy1": {
                    "enabled": True,
                    "type": "sse",
                    "url": "https://example.com/legacy",
                }
            },
        }
        result = normalize_config_document(raw)
        assert result.document.config.agent.model == "canonical"
        assert any(w.code == "legacy_module_shadowed" for w in result.warnings)
        assert "http1" in result.document.mcp.servers
        assert "legacy1" not in result.document.mcp.servers
        assert result.document.mcp.servers["http1"]["connection"]["type"] == "streamable-http"
        assert any(w.code == "mcp_legacy_shadowed" for w in result.warnings) or any(
            "shadow" in w.message.lower() or "mcp_servers" in w.message for w in result.warnings
        )

    def test_invalid_module_isolated_with_defaults(self):
        raw = {
            "agent": {"model": "ok", "temperature": 0.5, "max_steps": 8},
            "tools": {"enforcement_mode": "not-a-mode"},
        }
        result = normalize_config_document(raw)
        assert result.document.config.agent.model == "ok"
        assert result.document.config.tools.enforcement_mode == "enforce"
        assert "tools" in result.module_errors


class TestProjectionAndRedaction:
    def test_effective_runtime_projection_exposes_top_level_modules(self):
        doc = CanonicalConfigDocument()
        doc = doc.replace_module(
            "agent",
            {"model": "runtime-model", "temperature": 0.3, "max_steps": 12},
        )
        effective = project_effective_runtime_config(
            doc,
            raw={"hooks": {"enabled": True}, "mcp_servers": {}},
        )
        assert effective["agent"]["model"] == "runtime-model"
        assert effective["hooks"]["enabled"] is True
        assert "mcp" in effective
        assert "tools" in effective

    def test_save_one_module_preserves_unknown_and_other_modules(self):
        raw = {
            "agent": {"model": "a", "temperature": 0.1, "max_steps": 3, "extra_agent": 1},
            "tools": {"enforcement_mode": "audit", "memory": False, "custom_tool_flag": True},
            "hooks": {"enabled": False},
            "mcp_servers": {
                "s1": {"enabled": True, "type": "sse", "url": "https://example.com/sse"}
            },
        }
        merged = merge_module_into_raw(
            raw,
            "agent",
            {"model": "b", "temperature": 0.9, "max_steps": 9, "extra_agent": 1},
        )
        assert merged["config"]["agent"]["model"] == "b"
        assert merged["config"]["agent"]["extra_agent"] == 1
        assert merged["config"]["tools"]["custom_tool_flag"] is True
        assert merged["hooks"]["enabled"] is False
        # 保存后只保留规范 mcp，旧顶层普通字段与 mcp_servers 不再回写
        assert "agent" not in merged or "config" in merged
        assert "mcp_servers" not in merged
        assert "s1" in merged["mcp"]["servers"]
        assert merged["config"]["tools"]["enforcement_mode"] == "audit"

    def test_preserve_unknown_fields_nested(self):
        base = {"known": 1, "nested": {"a": 1, "keep": True}}
        update = {"known": 2, "nested": {"a": 3}}
        result = preserve_unknown_fields(base, update)
        assert result["known"] == 2
        assert result["nested"]["a"] == 3
        assert result["nested"]["keep"] is True

    def test_redact_sensitive_headers_and_env(self):
        payload = {
            "connection": {
                "headers": {"Authorization": "Bearer secret", "X-Trace": "1"},
                "env": {"API_KEY": "abc", "PATH": "/usr/bin"},
            }
        }
        redacted = redact_mapping_secrets(payload)
        assert redacted["connection"]["headers"]["Authorization"] == MASK_LITERAL
        assert redacted["connection"]["headers"]["X-Trace"] == "1"
        assert redacted["connection"]["env"]["API_KEY"] == MASK_LITERAL
        assert redacted["connection"]["env"]["PATH"] == "/usr/bin"

    def test_module_api_projection_uses_draft_when_provided(self):
        doc = CanonicalConfigDocument()
        projected = project_module_for_api(
            doc,
            "agent",
            draft={"model": "draft", "temperature": 0.1, "max_steps": 2},
        )
        assert projected["model"] == "draft"
