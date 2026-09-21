"""配置文档归一化、有效运行时投影与脱敏。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.config.document import (
    CanonicalConfigDocument,
    ConfigDomainModel,
    McpDomainModel,
    empty_canonical_document,
)
from app.config.modules import (
    MODULE_MODELS,
    ORDINARY_MODULE_KEYS,
    default_module_value,
    validate_module_value,
)
from app.tools.mcp.config import normalize_mcp_config
from app.tools.mcp.types import MASK_LITERAL, SENSITIVE_HEADER_KEYS

LEGACY_ORDINARY_KEYS = ORDINARY_MODULE_KEYS


@dataclass
class ConfigWarning:
    code: str
    message: str
    path: str | None = None


@dataclass
class NormalizedConfigResult:
    document: CanonicalConfigDocument
    warnings: list[ConfigWarning] = field(default_factory=list)
    module_errors: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    source: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def _is_mapping(value: Any) -> bool:
    return isinstance(value, dict)


def _module_error_from_validation(exc: ValidationError) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for item in exc.errors():
        loc = item.get("loc") or ()
        path = "/" + "/".join(str(part) for part in loc) if loc else "/"
        errors.append(
            {
                "path": path,
                "kind": item.get("type", "validation"),
                "message": item.get("msg", "校验失败"),
                "expected": item.get("expected"),
            }
        )
    return errors


def _pick_module_raw(raw: dict[str, Any], module: str) -> tuple[Any | None, str | None, ConfigWarning | None]:
    """按优先级选取模块原始值：合法 config.<module> > 旧顶层字段。"""
    config_domain = raw.get("config")
    canonical_value = None
    if _is_mapping(config_domain) and module in config_domain:
        canonical_value = config_domain.get(module)

    legacy_value = raw.get(module) if module in raw else None

    if _is_mapping(canonical_value):
        warning = None
        if _is_mapping(legacy_value):
            warning = ConfigWarning(
                code="legacy_module_shadowed",
                message=f"config.{module} 优先于旧版顶层 {module}，旧字段未被合并",
                path=f"/config/{module}",
            )
        return canonical_value, "config", warning

    if _is_mapping(legacy_value):
        return legacy_value, "legacy", None

    if canonical_value is not None and not _is_mapping(canonical_value):
        return canonical_value, "config", ConfigWarning(
            code="invalid_module_shape",
            message=f"config.{module} 必须是对象",
            path=f"/config/{module}",
        )

    return None, None, None


def normalize_config_document(raw: dict[str, Any] | None) -> NormalizedConfigResult:
    """将旧顶层或规范根确定性归一化为 CanonicalConfigDocument。

    规则：
    1. 存在合法 config.<module> 时优先于同名旧顶层字段。
    2. 缺少规范模块时从旧顶层补齐。
    3. 存在合法 mcp 时优先于 mcp_servers；两者并存时不合并并 warning。
    4. 读取过程不写回文件。
    """
    raw = deepcopy(raw) if isinstance(raw, dict) else {}
    warnings: list[ConfigWarning] = []
    module_errors: dict[str, list[dict[str, Any]]] = {}
    source: dict[str, str] = {}

    schema_version = raw.get("schema_version", 1)
    try:
        schema_version = int(schema_version)
        if schema_version < 1:
            schema_version = 1
            warnings.append(
                ConfigWarning(
                    code="invalid_schema_version",
                    message="schema_version 非法，已回退为 1",
                    path="/schema_version",
                )
            )
    except (TypeError, ValueError):
        schema_version = 1
        warnings.append(
            ConfigWarning(
                code="invalid_schema_version",
                message="schema_version 非法，已回退为 1",
                path="/schema_version",
            )
        )

    config_data: dict[str, Any] = {}
    for module in ORDINARY_MODULE_KEYS:
        picked, src, warning = _pick_module_raw(raw, module)
        if warning is not None:
            warnings.append(warning)
        if picked is None:
            config_data[module] = default_module_value(module)
            source[module] = "default"
            continue
        try:
            validated = validate_module_value(module, picked)
            # model_dump 后合并未知字段：Pydantic extra=allow 已保留在 model
            config_data[module] = validated.model_dump(mode="json")
            source[module] = src or "default"
        except (ValidationError, TypeError) as exc:
            config_data[module] = default_module_value(module)
            source[module] = "default"
            if isinstance(exc, ValidationError):
                module_errors[module] = _module_error_from_validation(exc)
            else:
                module_errors[module] = [
                    {
                        "path": f"/{module}",
                        "kind": "type",
                        "message": str(exc),
                        "expected": "object",
                    }
                ]
            warnings.append(
                ConfigWarning(
                    code="module_validation_failed",
                    message=f"模块 {module} 校验失败，已使用默认值；原始草稿保留在错误中",
                    path=f"/config/{module}",
                )
            )
            # 保留非法草稿供 API 展示（不写入 document 合法值）
            module_errors[module].append(
                {
                    "path": "/",
                    "kind": "draft",
                    "message": "invalid_draft",
                    "draft": picked,
                }
            )

    mcp_catalog = normalize_mcp_config(raw)
    for msg in mcp_catalog.warnings:
        warnings.append(
            ConfigWarning(
                code="mcp_warning",
                message=msg,
                path="/mcp",
            )
        )
    if mcp_catalog.legacy_shadowed:
        warnings.append(
            ConfigWarning(
                code="mcp_legacy_shadowed",
                message="规范 mcp 与旧 mcp_servers 同时存在，已使用 mcp，未合并 mcp_servers",
                path="/mcp",
            )
        )

    mcp_servers: dict[str, Any] = {}
    for server_id, definition in mcp_catalog.servers.items():
        if definition.raw_canonical:
            mcp_servers[server_id] = deepcopy(definition.raw_canonical)
        else:
            mcp_servers[server_id] = {
                "display_name": definition.display_name,
                "enabled": definition.enabled,
                "connection": {
                    "type": definition.connection_type,
                    "command": definition.command,
                    "args": list(definition.args),
                    "env": dict(definition.env),
                    "url": definition.url,
                    "headers": dict(definition.headers),
                },
                "auth": {"required_env": list(definition.required_env)},
                "timeouts": {
                    "connect": definition.connect_timeout,
                    "call": definition.timeout,
                },
                "tools": definition.tools_filter.to_dict(),
                "runtime": {
                    "supports_parallel_tool_calls": definition.supports_parallel_tool_calls,
                },
                **(definition.unknown_fields or {}),
            }

    document = CanonicalConfigDocument(
        schema_version=schema_version,
        config=ConfigDomainModel.model_validate(config_data),
        mcp=McpDomainModel(
            version=mcp_catalog.version or 1,
            revision=mcp_catalog.revision or 0,
            servers=mcp_servers,
        ),
    )
    source["mcp"] = mcp_catalog.source
    return NormalizedConfigResult(
        document=document,
        warnings=warnings,
        module_errors=module_errors,
        source=source,
        raw=raw,
    )


def project_effective_runtime_config(
    document: CanonicalConfigDocument,
    *,
    preserve_legacy_keys: bool = True,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把规范文档投影为现有运行时期望的顶层视图。

    运行时 loader 仍读顶层 agent/tools/...；MCP 同时提供规范 mcp。
    未识别的顶层键（如 hooks）在 preserve 时从 raw 透传。
    """
    effective: dict[str, Any] = {}
    config_dump = document.config.model_dump(mode="json")
    for module in ORDINARY_MODULE_KEYS:
        effective[module] = deepcopy(config_dump[module])

    effective["mcp"] = document.mcp.model_dump(mode="json")
    effective["schema_version"] = document.schema_version

    if preserve_legacy_keys and isinstance(raw, dict):
        for key, value in raw.items():
            if key in ORDINARY_MODULE_KEYS:
                continue
            if key in {"schema_version", "config", "mcp", "mcp_servers"}:
                continue
            if key not in effective:
                effective[key] = deepcopy(value)
        # 若 raw 仍有 mcp_servers 且未被规范 mcp 取代展示需要，保留给兼容消费者
        if "mcp_servers" in raw and "mcp_servers" not in effective:
            # 运行时优先规范 mcp；仍保留影子字段供诊断，不覆盖
            effective["mcp_servers"] = deepcopy(raw["mcp_servers"])

    return effective


def merge_module_into_raw(
    raw: dict[str, Any],
    module: str,
    value: dict[str, Any],
) -> dict[str, Any]:
    """将单个普通模块合并进完整原始文档，写出规范根。

    - 先归一化现有文档
    - 替换目标模块
    - 写出 schema_version + config + mcp（不再回写旧顶层 agent/tools/...）
    - 保留未识别顶层键（hooks 等）
    - 提升并去掉旧 mcp_servers，避免双源 warning / 丢 Server
    """
    if module not in MODULE_MODELS:
        raise KeyError(f"未知配置模块: {module}")

    # 先把旧 mcp_servers 提升进规范 mcp，再归一化，避免双源时丢掉真实 Server
    from app.tools.mcp.config import _promote_legacy_mcp_servers

    promoted = _promote_legacy_mcp_servers(deepcopy(raw) if isinstance(raw, dict) else {})
    validated = validate_module_value(module, value)
    normalized = normalize_config_document(promoted)
    updated = normalized.document.replace_module(module, validated.model_dump(mode="json"))

    result: dict[str, Any] = updated.to_storage_dict()

    for key, item in promoted.items():
        if key in {"schema_version", "config", "mcp", "mcp_servers"}:
            continue
        if key in ORDINARY_MODULE_KEYS:
            continue
        result[key] = deepcopy(item)

    result.pop("mcp_servers", None)
    return result


def redact_mapping_secrets(data: Any) -> Any:
    """递归脱敏常见秘密字段；用于模块/Server 投影。"""
    if isinstance(data, list):
        return [redact_mapping_secrets(item) for item in data]
    if not isinstance(data, dict):
        return data

    result: dict[str, Any] = {}
    for key, value in data.items():
        key_l = str(key).lower()
        if key_l in SENSITIVE_HEADER_KEYS or key_l in {
            "authorization",
            "api_key",
            "apikey",
            "token",
            "secret",
            "password",
        }:
            if value in (None, ""):
                result[key] = value
            else:
                result[key] = MASK_LITERAL
            continue
        if key_l in {"headers", "env", "extra_env"} and isinstance(value, dict):
            redacted_child: dict[str, Any] = {}
            for child_key, child_val in value.items():
                child_l = str(child_key).lower()
                if child_l in SENSITIVE_HEADER_KEYS or child_l.endswith("_token") or child_l.endswith("_key"):
                    redacted_child[child_key] = MASK_LITERAL if child_val not in (None, "") else child_val
                else:
                    redacted_child[child_key] = redact_mapping_secrets(child_val)
            result[key] = redacted_child
            continue
        result[key] = redact_mapping_secrets(value)
    return result


def project_module_for_api(
    document: CanonicalConfigDocument,
    module: str,
    *,
    draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """API 读取用的模块脱敏投影。非法草稿优先于合法 document 值。"""
    if draft is not None:
        return redact_mapping_secrets(deepcopy(draft))
    return redact_mapping_secrets(document.module_dict(module))


def preserve_unknown_fields(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """合并更新时保留 base 中 update 未提及的未知键。"""
    result = deepcopy(base)
    for key, value in update.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
            and key not in {"headers", "env", "extra_env"}
        ):
            result[key] = preserve_unknown_fields(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result
