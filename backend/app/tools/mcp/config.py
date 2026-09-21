from __future__ import annotations

import json
import logging
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.core.settings import get_settings
from app.tools.mcp.types import (
    MASK_LITERAL,
    SENSITIVE_HEADER_KEYS,
    CanonicalTransport,
    McpCatalog,
    McpServerConfig,
    McpServerDefinition,
    McpToolsFilter,
    McpTransport,
    _as_str_set,
    _resolve_transport,
    _safe_float,
    transport_to_canonical,
)

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_KNOWN_CANONICAL_SERVER_KEYS = frozenset(
    {
        "display_name",
        "displayName",
        "enabled",
        "isActive",
        "connection",
        "auth",
        "timeouts",
        "tools",
        "runtime",
        # legacy flat aliases that may appear inside mcp.servers
        "type",
        "transport",
        "url",
        "baseUrl",
        "command",
        "args",
        "env",
        "headers",
        "timeout",
        "connect_timeout",
        "supports_parallel_tool_calls",
    }
)

_KNOWN_CONNECTION_KEYS = frozenset(
    {
        "type",
        "transport",
        "url",
        "baseUrl",
        "command",
        "args",
        "env",
        "headers",
    }
)


def _load_workspace_config(workspace: Path | None = None) -> dict:
    from app.storage.workspace import config_file_path

    _ = workspace
    path = config_file_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_workspace_config(data: dict, workspace: Path | None = None) -> None:
    from app.config.persistence import atomic_write_json, get_revision_tracker
    from app.storage.workspace import config_file_path

    _ = workspace
    path = config_file_path()
    atomic_write_json(path, data)
    get_revision_tracker().bump_for_path(path)


def load_mcp_catalog(workspace=None) -> McpCatalog:
    """读取并归一化 MCP 配置目录（含非法/禁用条目）。"""
    full = _load_workspace_config(workspace)
    return normalize_mcp_config(full, workspace=workspace)


def load_mcp_server_configs(workspace=None) -> dict[str, McpServerConfig]:
    """返回可连接且已启用的运行时配置（生命周期/发现使用）。"""
    catalog = load_mcp_catalog(workspace)
    result: dict[str, McpServerConfig] = {}
    for server_id, definition in catalog.servers.items():
        if not definition.enabled or not definition.can_connect:
            continue
        result[server_id] = definition.to_runtime_config()
    return result


def get_mcp_config_revision(workspace=None) -> int:
    return load_mcp_catalog(workspace).revision


def normalize_mcp_config(full: dict[str, Any], *, workspace=None) -> McpCatalog:
    """将 CONFIG 文档归一化为管理/运行时目录。"""
    _ensure_dotenv()
    workspace_path = str(workspace or get_settings().workspace_path)

    mcp_raw = full.get("mcp")
    legacy_raw = full.get("mcp_servers")
    has_mcp = isinstance(mcp_raw, dict) and _mcp_section_is_authoritative(mcp_raw)
    has_legacy = isinstance(legacy_raw, dict) and bool(legacy_raw)

    warnings: list[str] = []
    legacy_shadowed = False

    if has_mcp and has_legacy:
        legacy_shadowed = True
        warnings.append("legacy_shadowed")

    if has_mcp:
        version = _safe_int(mcp_raw.get("version"), 1)
        revision = _safe_int(mcp_raw.get("revision"), 0)
        servers_raw = mcp_raw.get("servers") if isinstance(mcp_raw.get("servers"), dict) else {}
        servers = {
            str(sid): _normalize_server_entry(
                str(sid),
                entry if isinstance(entry, dict) else {},
                source="mcp",
                workspace_path=workspace_path,
            )
            for sid, entry in servers_raw.items()
        }
        return McpCatalog(
            version=version,
            revision=revision,
            servers=servers,
            source="mcp",
            warnings=warnings,
            legacy_shadowed=legacy_shadowed,
        )

    if has_legacy:
        revision = 0
        if isinstance(mcp_raw, dict):
            revision = _safe_int(mcp_raw.get("revision"), 0)
        servers = {
            str(sid): _normalize_server_entry(
                str(sid),
                entry if isinstance(entry, dict) else {},
                source="mcp_servers",
                workspace_path=workspace_path,
            )
            for sid, entry in legacy_raw.items()
        }
        return McpCatalog(
            version=None,
            revision=revision,
            servers=servers,
            source="mcp_servers",
            warnings=warnings,
            legacy_shadowed=False,
        )

    return McpCatalog(version=None, revision=0, servers={}, source="empty", warnings=warnings)


def _mcp_section_is_authoritative(mcp_raw: dict[str, Any]) -> bool:
    version = mcp_raw.get("version")
    if version is None:
        return False
    try:
        int(version)
    except (TypeError, ValueError):
        return False
    return True


def _normalize_server_entry(
    server_id: str,
    data: dict[str, Any],
    *,
    source: str,
    workspace_path: str,
) -> McpServerDefinition:
    # 先收集未插值的变量引用，再插值用于可执行配置
    unresolved = _collect_unresolved_variables(data)
    interpolated = _interpolate(data, workspace_path=workspace_path)

    connection_raw = interpolated.get("connection") if isinstance(interpolated.get("connection"), dict) else {}
    flat_for_transport = {**interpolated, **connection_raw}
    transport = _resolve_transport(flat_for_transport)
    connection_type = transport_to_canonical(transport)

    display_name = (
        interpolated.get("display_name")
        or interpolated.get("displayName")
        or server_id
    )
    enabled = interpolated.get("enabled")
    if enabled is None and "isActive" in interpolated:
        enabled = interpolated.get("isActive")
    if enabled is None:
        enabled = True

    command = connection_raw.get("command") if "command" in connection_raw else interpolated.get("command")
    args_raw = connection_raw.get("args") if "args" in connection_raw else interpolated.get("args")
    env_raw = connection_raw.get("env") if "env" in connection_raw else interpolated.get("env")
    headers_raw = connection_raw.get("headers") if "headers" in connection_raw else interpolated.get("headers")
    url = connection_raw.get("url") or connection_raw.get("baseUrl") or interpolated.get("url") or interpolated.get("baseUrl")

    timeouts = interpolated.get("timeouts") if isinstance(interpolated.get("timeouts"), dict) else {}
    call_timeout = timeouts.get("call", interpolated.get("timeout"))
    connect_timeout = timeouts.get("connect", interpolated.get("connect_timeout"))

    auth = interpolated.get("auth") if isinstance(interpolated.get("auth"), dict) else {}
    required_env = [str(x) for x in (auth.get("required_env") or [])]

    tools_raw = interpolated.get("tools") if isinstance(interpolated.get("tools"), dict) else {}
    tools_filter = McpToolsFilter(
        include=frozenset(_as_str_set(tools_raw.get("include"))),
        exclude=frozenset(_as_str_set(tools_raw.get("exclude"))),
        resources=bool(tools_raw.get("resources", False)),
        prompts=bool(tools_raw.get("prompts", False)),
    )

    runtime = interpolated.get("runtime") if isinstance(interpolated.get("runtime"), dict) else {}
    parallel = interpolated.get("supports_parallel_tool_calls")
    if parallel is None:
        parallel = runtime.get("supports_parallel_tool_calls", False)

    unknown_fields = {
        k: v
        for k, v in interpolated.items()
        if k not in _KNOWN_CANONICAL_SERVER_KEYS and not _looks_sensitive_key(k)
    }
    if connection_raw:
        unknown_connection = {
            f"connection.{k}": v
            for k, v in connection_raw.items()
            if k not in _KNOWN_CONNECTION_KEYS and not _looks_sensitive_key(k)
        }
        unknown_fields.update(unknown_connection)

    validation_errors = _validate_definition_fields(
        connection_type=connection_type,
        command=command,
        url=url,
        args=args_raw,
    )
    for var in unresolved:
        if var != "WORKSPACE":
            validation_errors.append(f"未解析变量: ${{{var}}}")

    for env_name in required_env:
        if not os.environ.get(env_name):
            unresolved.append(env_name)
            validation_errors.append(f"缺少必需环境变量: {env_name}")

    # 去重保持顺序
    unresolved = list(dict.fromkeys(unresolved))
    validation_errors = list(dict.fromkeys(validation_errors))

    can_connect = bool(enabled) and not validation_errors

    return McpServerDefinition(
        id=server_id,
        display_name=str(display_name),
        enabled=bool(enabled),
        connection_type=connection_type,
        command=str(command) if command else None,
        args=[str(a) for a in (args_raw or [])],
        env={str(k): str(v) for k, v in (env_raw or {}).items()},
        url=str(url) if url else None,
        headers={str(k): str(v) for k, v in (headers_raw or {}).items()},
        required_env=required_env,
        timeouts_connect=_safe_float(connect_timeout, 60.0),
        timeouts_call=_safe_float(call_timeout, 25.0),
        tools_filter=tools_filter,
        supports_parallel_tool_calls=bool(parallel),
        source=source,  # type: ignore[arg-type]
        validation_errors=validation_errors,
        unresolved_variables=unresolved,
        warnings=[],
        can_connect=can_connect,
        unknown_fields=unknown_fields,
        raw_canonical=_to_canonical_dict(
            server_id=server_id,
            display_name=str(display_name),
            enabled=bool(enabled),
            connection_type=connection_type,
            command=str(command) if command else None,
            args=[str(a) for a in (args_raw or [])],
            env={str(k): str(v) for k, v in (env_raw or {}).items()},
            url=str(url) if url else None,
            headers={str(k): str(v) for k, v in (headers_raw or {}).items()},
            required_env=required_env,
            timeouts_connect=_safe_float(connect_timeout, 60.0),
            timeouts_call=_safe_float(call_timeout, 25.0),
            tools_filter=tools_filter,
            supports_parallel_tool_calls=bool(parallel),
            unknown_fields=unknown_fields,
        ),
    )


def _to_canonical_dict(
    *,
    server_id: str,
    display_name: str,
    enabled: bool,
    connection_type: CanonicalTransport,
    command: str | None,
    args: list[str],
    env: dict[str, str],
    url: str | None,
    headers: dict[str, str],
    required_env: list[str],
    timeouts_connect: float,
    timeouts_call: float,
    tools_filter: McpToolsFilter,
    supports_parallel_tool_calls: bool,
    unknown_fields: dict[str, Any],
) -> dict[str, Any]:
    connection: dict[str, Any] = {"type": connection_type}
    if connection_type == "stdio":
        connection["command"] = command
        connection["args"] = list(args)
        connection["env"] = dict(env)
        connection["url"] = None
        connection["headers"] = {}
    else:
        connection["url"] = url
        connection["headers"] = dict(headers)
        connection["command"] = None
        connection["args"] = []
        connection["env"] = {}

    payload: dict[str, Any] = {
        "display_name": display_name,
        "enabled": enabled,
        "connection": connection,
        "auth": {"required_env": list(required_env)},
        "timeouts": {"connect": timeouts_connect, "call": timeouts_call},
        "tools": tools_filter.to_dict(),
        "runtime": {"supports_parallel_tool_calls": supports_parallel_tool_calls},
    }
    # 保留非敏感未知字段（顶层）
    for key, value in unknown_fields.items():
        if key.startswith("connection."):
            continue
        if key not in payload:
            payload[key] = value
    return payload


def definition_to_canonical(definition: McpServerDefinition) -> dict[str, Any]:
    return _to_canonical_dict(
        server_id=definition.id,
        display_name=definition.display_name,
        enabled=definition.enabled,
        connection_type=definition.connection_type,
        command=definition.command,
        args=definition.args,
        env=definition.env,
        url=definition.url,
        headers=definition.headers,
        required_env=definition.required_env,
        timeouts_connect=definition.timeouts_connect,
        timeouts_call=definition.timeouts_call,
        tools_filter=definition.tools_filter,
        supports_parallel_tool_calls=definition.supports_parallel_tool_calls,
        unknown_fields=definition.unknown_fields,
    )


def _promote_legacy_mcp_servers(full: dict[str, Any]) -> dict[str, Any]:
    """把旧 mcp_servers 中尚未出现在 mcp.servers 的条目提升进规范域，并删除 mcp_servers。"""
    legacy = full.get("mcp_servers")
    if not isinstance(legacy, dict) or not legacy:
        full.pop("mcp_servers", None)
        return full

    mcp_section = full.get("mcp") if isinstance(full.get("mcp"), dict) else {}
    servers = dict(mcp_section.get("servers") or {}) if isinstance(mcp_section.get("servers"), dict) else {}
    legacy_catalog = normalize_mcp_config({"mcp_servers": legacy})
    for sid, definition in legacy_catalog.servers.items():
        if sid not in servers:
            servers[sid] = definition_to_canonical(definition)

    full["mcp"] = {
        "version": int(mcp_section.get("version") or legacy_catalog.version or 1),
        "revision": int(mcp_section.get("revision") or legacy_catalog.revision or 0),
        "servers": servers,
    }
    full.pop("mcp_servers", None)
    return full


def save_mcp_server(
    server_id: str,
    canonical: dict[str, Any],
    *,
    workspace=None,
    bump_revision: bool = True,
) -> McpCatalog:
    """写入/更新单个 Server 到规范 `mcp` 段；写出后去掉旧 mcp_servers。"""
    workspace = workspace or get_settings().workspace_path
    full = _promote_legacy_mcp_servers(_load_workspace_config(workspace))
    catalog = normalize_mcp_config(full, workspace=workspace)

    mcp_section = full.get("mcp") if isinstance(full.get("mcp"), dict) else {}
    servers = dict(mcp_section.get("servers") or {}) if isinstance(mcp_section.get("servers"), dict) else {}
    servers[server_id] = deepcopy(canonical)

    revision = catalog.revision
    if bump_revision:
        revision = revision + 1

    full["mcp"] = {
        "version": int(mcp_section.get("version") or catalog.version or 1),
        "revision": revision,
        "servers": servers,
    }
    full.pop("mcp_servers", None)
    _write_workspace_config(full, workspace)
    return normalize_mcp_config(full, workspace=workspace)


def delete_mcp_server(server_id: str, *, workspace=None) -> McpCatalog:
    workspace = workspace or get_settings().workspace_path
    full = _promote_legacy_mcp_servers(_load_workspace_config(workspace))
    catalog = normalize_mcp_config(full, workspace=workspace)

    mcp_section = full.get("mcp") if isinstance(full.get("mcp"), dict) else {}
    servers = dict(mcp_section.get("servers") or {}) if isinstance(mcp_section.get("servers"), dict) else {}

    if server_id not in servers and server_id not in catalog.servers:
        raise KeyError(server_id)

    servers.pop(server_id, None)
    revision = catalog.revision + 1
    full["mcp"] = {
        "version": int(mcp_section.get("version") or catalog.version or 1),
        "revision": revision,
        "servers": servers,
    }
    full.pop("mcp_servers", None)
    _write_workspace_config(full, workspace)
    return normalize_mcp_config(full, workspace=workspace)


def set_mcp_server_enabled(server_id: str, enabled: bool, *, workspace=None) -> McpCatalog:
    catalog = load_mcp_catalog(workspace)
    if server_id not in catalog.servers:
        raise KeyError(server_id)
    definition = catalog.servers[server_id]
    canonical = definition_to_canonical(definition)
    canonical["enabled"] = bool(enabled)
    return save_mcp_server(server_id, canonical, workspace=workspace, bump_revision=True)


def bump_mcp_revision(*, workspace=None) -> int:
    """仅递增 revision（例如显式 refresh 后标记已应用）。"""
    workspace = workspace or get_settings().workspace_path
    full = _load_workspace_config(workspace)
    catalog = normalize_mcp_config(full, workspace=workspace)
    mcp_section = full.get("mcp") if isinstance(full.get("mcp"), dict) else {}
    servers = mcp_section.get("servers") if isinstance(mcp_section.get("servers"), dict) else None
    if servers is None and catalog.source == "mcp_servers":
        servers = {sid: definition_to_canonical(defn) for sid, defn in catalog.servers.items()}
    elif servers is None:
        servers = {}
    revision = catalog.revision + 1
    full["mcp"] = {
        "version": int(mcp_section.get("version") or catalog.version or 1),
        "revision": revision,
        "servers": servers,
    }
    _write_workspace_config(full, workspace)
    return revision


def validate_server_config(name: str, config: McpServerConfig) -> list[str]:
    """启动前安全校验（v0.1 最小规则）。"""
    return _validate_definition_fields(
        connection_type=transport_to_canonical(config.transport),
        command=config.command,
        url=config.url,
        args=config.args,
    )


def _validate_definition_fields(
    *,
    connection_type: CanonicalTransport,
    command: Any,
    url: Any,
    args: Any,
) -> list[str]:
    issues: list[str] = []
    if connection_type == "stdio":
        if not command:
            issues.append("stdio 模式缺少 command")
        elif _looks_like_shell_injection(str(command)):
            issues.append(f"command 含可疑字符: {command!r}")
        if url:
            issues.append("不能同时配置 command 与 url")
    elif connection_type in ("streamable-http", "sse"):
        if not url:
            issues.append(f"{connection_type} 模式缺少 url")
        if command:
            issues.append("不能同时配置 command 与 url")
    if args is not None and not isinstance(args, (list, tuple)):
        issues.append("args 必须是数组")
    return issues


def _looks_like_shell_injection(command: str) -> bool:
    return any(ch in command for ch in ("|", "&", ";", "`", "$("))


def _looks_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in SENSITIVE_HEADER_KEYS:
        return True
    return any(token in lowered for token in ("token", "secret", "password", "passwd", "api_key", "apikey"))


def _collect_unresolved_variables(value: Any) -> list[str]:
    found: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            for match in _ENV_VAR_RE.finditer(node):
                key = match.group(1)
                if key == "WORKSPACE":
                    continue
                if key not in os.environ:
                    found.append(key)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _ensure_dotenv()
    _walk(value)
    return list(dict.fromkeys(found))


def _interpolate(value: Any, *, workspace_path: str) -> Any:
    """递归解析 ${VAR}；内置 ${WORKSPACE}。未定义变量保留原样。"""
    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key == "WORKSPACE":
                return workspace_path
            return os.environ.get(key, match.group(0))

        return _ENV_VAR_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _interpolate(v, workspace_path=workspace_path) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(item, workspace_path=workspace_path) for item in value]
    return value


def _ensure_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        env_path = get_settings().workspace_path / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
    except Exception:
        pass


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def is_mask_literal(value: Any) -> bool:
    return isinstance(value, str) and value.strip() == MASK_LITERAL


def redact_mapping(values: dict[str, str] | None, *, treat_all_as_secret: bool = False) -> dict[str, str]:
    """返回脱敏后的映射；敏感键替换为掩码，纯引用或 Bearer ${VAR} 保留引用形态。"""
    result: dict[str, str] = {}
    for key, value in (values or {}).items():
        text = str(value)
        if _is_reference_only_secret(text):
            result[str(key)] = text
        elif treat_all_as_secret or _looks_sensitive_key(key) or _value_looks_secret(text):
            result[str(key)] = MASK_LITERAL
        else:
            result[str(key)] = text
    return result


def _is_reference_only_secret(value: str) -> bool:
    text = value.strip()
    if _ENV_VAR_RE.fullmatch(text):
        return True
    # 常见 Authorization: Bearer ${TOKEN}
    if text.lower().startswith("bearer "):
        rest = text[7:].strip()
        return bool(_ENV_VAR_RE.fullmatch(rest))
    return False


def _value_looks_secret(value: str) -> bool:
    lowered = value.lower()
    if lowered.startswith("bearer "):
        return True
    if value.startswith("${") and value.endswith("}"):
        return False
    return False


def merge_secret_maps(
    existing: dict[str, str],
    incoming: dict[str, str] | None,
    *,
    cleared_keys: list[str] | None = None,
) -> dict[str, str]:
    """合并脱敏保存语义：掩码保留原值；显式清除删除；新值替换。"""
    result = dict(existing)
    for key in cleared_keys or []:
        result.pop(key, None)
    if incoming is None:
        return result
    for key, value in incoming.items():
        if is_mask_literal(value):
            continue
        if value is None or value == "":
            result.pop(key, None)
            continue
        result[key] = str(value)
    return result
