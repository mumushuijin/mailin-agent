from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.agent.graph import get_graph
from app.config import (
    MODULE_DISPLAY_NAMES,
    USER_VISIBLE_MODULE_KEYS,
    is_user_visible_module,
    merge_module_into_raw,
    normalize_config_document,
    project_module_for_api,
    validate_module_value,
)
from app.config.persistence import atomic_write_json, get_revision_tracker
from app.core.exceptions import ConfigConflictError, ConfigValidationError, NotFoundError
from app.core.llm import get_chat_model
from app.core.settings import get_settings
from app.schemas.config import (
    AgentInfo,
    ConfigFieldError,
    ConfigFile,
    ConfigModuleSummary,
    ConfigModulesResponse,
    ConfigModuleUpdateResponse,
    ConfigModuleValidateResponse,
)
from app.services.session_service import SessionService
from app.storage.workspace import ConfigStore, MemoryStore, config_file_path
from app.tools.registry import clear_tools_cache


def _validation_errors_from_exc(exc: ValidationError) -> list[ConfigFieldError]:
    errors: list[ConfigFieldError] = []
    for item in exc.errors():
        loc = item.get("loc") or ()
        path = "/" + "/".join(str(part) for part in loc) if loc else "/"
        errors.append(
            ConfigFieldError(
                path=path,
                kind=str(item.get("type") or "validation"),
                message=str(item.get("msg") or "校验失败"),
                expected=item.get("ctx", {}).get("expected") if isinstance(item.get("ctx"), dict) else None,
            )
        )
    return errors


def _parse_module_input(
    *,
    module: str,
    value: dict[str, Any] | None,
    text: str | None,
) -> dict[str, Any]:
    if text is not None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigValidationError(
                "JSON 语法错误",
                errors=[
                    {
                        "path": "/",
                        "kind": "syntax",
                        "message": f"JSON 无法解析: {exc.msg}",
                        "expected": "json",
                    }
                ],
                module=module,
            ) from exc
        if not isinstance(parsed, dict):
            raise ConfigValidationError(
                "模块值必须是 JSON 对象",
                errors=[{"path": "/", "kind": "type", "message": "必须是对象", "expected": "object"}],
                module=module,
            )
        return parsed
    if value is None:
        raise ConfigValidationError(
            "缺少模块值",
            errors=[{"path": "/", "kind": "validation", "message": "需要提供 value 或 text"}],
            module=module,
        )
    if not isinstance(value, dict):
        raise ConfigValidationError(
            "模块值必须是 JSON 对象",
            errors=[{"path": "/", "kind": "type", "message": "必须是对象", "expected": "object"}],
            module=module,
        )
    return value


class ConfigService:
    def __init__(self):
        self.session_service = SessionService()
        self._revisions = get_revision_tracker()

    @property
    def workspace(self):
        return get_settings().workspace_path

    @property
    def store(self) -> ConfigStore:
        settings = get_settings()
        return ConfigStore(
            settings.workspace_path,
            settings.workspace_defaults_path,
            config_defaults=settings.config_defaults_path,
        )

    def list_configs(self) -> list[str]:
        return self.store.list_configs()

    def get_config(self, name: str) -> ConfigFile:
        return ConfigFile(name=name, content=self.store.read(name))

    def update_config(self, name: str, content: str) -> None:
        self.store.write(name, content)
        if name == "CONFIG":
            self._revisions.bump_for_path(config_file_path())
            self._invalidate_runtime(ordinary=True, mcp=True)
        else:
            self._notify_config_updated(name)

    def get_agent_info(self) -> AgentInfo:
        return AgentInfo(name=self.store.get_agent_name())

    def _load_raw_config(self) -> dict[str, Any]:
        path = config_file_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigValidationError(
                "CONFIG.json 无法解析",
                errors=[
                    {
                        "path": "/",
                        "kind": "syntax",
                        "message": f"JSON 无法解析: {exc.msg}",
                        "expected": "json",
                    }
                ],
            ) from exc
        if not isinstance(data, dict):
            raise ConfigValidationError(
                "CONFIG.json 根必须是对象",
                errors=[{"path": "/", "kind": "type", "message": "必须是对象", "expected": "object"}],
            )
        return data

    def current_revision(self) -> int:
        return self._revisions.current_for_path(config_file_path())

    def list_modules(self) -> ConfigModulesResponse:
        raw = self._load_raw_config()
        normalized = normalize_config_document(raw)
        revision = self.current_revision()
        modules: list[ConfigModuleSummary] = []
        for key in USER_VISIBLE_MODULE_KEYS:
            errors_raw = normalized.module_errors.get(key) or []
            draft = None
            field_errors: list[ConfigFieldError] = []
            for item in errors_raw:
                if item.get("kind") == "draft":
                    draft = item.get("draft")
                    continue
                field_errors.append(
                    ConfigFieldError(
                        path=str(item.get("path") or "/"),
                        kind=str(item.get("kind") or "validation"),
                        message=str(item.get("message") or "校验失败"),
                        expected=item.get("expected"),
                    )
                )
            value = project_module_for_api(
                normalized.document,
                key,
                draft=draft if isinstance(draft, dict) else None,
            )
            modules.append(
                ConfigModuleSummary(
                    key=key,
                    display_name=MODULE_DISPLAY_NAMES[key],
                    value=value,
                    source=normalized.source.get(key, "default"),
                    warnings=[
                        w.message
                        for w in normalized.warnings
                        if w.path and w.path.startswith(f"/config/{key}")
                    ],
                    errors=field_errors,
                    has_draft_error=bool(field_errors),
                )
            )
        return ConfigModulesResponse(
            revision=revision,
            schema_version=normalized.document.schema_version,
            modules=modules,
            warnings=[w.message for w in normalized.warnings],
            mcp_revision=normalized.document.mcp.revision,
            mcp_source=normalized.source.get("mcp", "empty"),
        )

    def validate_module(
        self,
        module: str,
        *,
        value: dict[str, Any] | None = None,
        text: str | None = None,
    ) -> ConfigModuleValidateResponse:
        if not is_user_visible_module(module):
            raise NotFoundError(f"未知或不可编辑的配置模块: {module}")
        parsed = _parse_module_input(module=module, value=value, text=text)
        try:
            validated = validate_module_value(module, parsed)
        except ValidationError as exc:
            return ConfigModuleValidateResponse(
                ok=False,
                module=module,
                errors=_validation_errors_from_exc(exc),
                revision=self.current_revision(),
            )
        except TypeError as exc:
            return ConfigModuleValidateResponse(
                ok=False,
                module=module,
                errors=[ConfigFieldError(path="/", kind="type", message=str(exc), expected="object")],
                revision=self.current_revision(),
            )
        return ConfigModuleValidateResponse(
            ok=True,
            module=module,
            normalized=validated.model_dump(mode="json"),
            revision=self.current_revision(),
        )

    def update_module(
        self,
        module: str,
        *,
        base_revision: int,
        value: dict[str, Any] | None = None,
        text: str | None = None,
    ) -> ConfigModuleUpdateResponse:
        if not is_user_visible_module(module):
            raise NotFoundError(f"未知或不可编辑的配置模块: {module}")

        current_revision = self.current_revision()
        if base_revision != current_revision:
            listing = self.list_modules()
            current_module = next((m for m in listing.modules if m.key == module), None)
            raise ConfigConflictError(
                revision=current_revision,
                module=module,
                current=current_module.value if current_module else None,
            )

        parsed = _parse_module_input(module=module, value=value, text=text)
        try:
            validated = validate_module_value(module, parsed)
        except ValidationError as exc:
            raise ConfigValidationError(
                "配置校验失败",
                errors=[e.model_dump() for e in _validation_errors_from_exc(exc)],
                revision=current_revision,
                module=module,
            ) from exc

        raw = self._load_raw_config()
        try:
            merged = merge_module_into_raw(raw, module, validated.model_dump(mode="json"))
            candidate = normalize_config_document(merged)
            validate_module_value(module, candidate.document.module_dict(module))
        except ValidationError as exc:
            raise ConfigValidationError(
                "完整配置文档校验失败",
                errors=[e.model_dump() for e in _validation_errors_from_exc(exc)],
                revision=current_revision,
                module=module,
            ) from exc
        except ConfigValidationError:
            raise
        except Exception as exc:
            raise ConfigValidationError(
                f"合并配置失败: {exc}",
                errors=[{"path": "/", "kind": "semantic", "message": str(exc)}],
                revision=current_revision,
                module=module,
            ) from exc

        path = config_file_path()
        try:
            atomic_write_json(path, merged)
        except OSError as exc:
            raise ConfigValidationError(
                "配置写入失败，已保留旧文件",
                errors=[{"path": "/", "kind": "io", "message": str(exc)}],
                revision=current_revision,
                module=module,
            ) from exc

        new_revision = self._revisions.bump_for_path(path)
        self._invalidate_runtime(ordinary=True, mcp=False)
        value_out = project_module_for_api(candidate.document, module)
        return ConfigModuleUpdateResponse(
            module=module,
            revision=new_revision,
            value=value_out,
            warnings=[w.message for w in candidate.warnings],
        )

    def _invalidate_runtime(self, *, ordinary: bool, mcp: bool) -> None:
        if ordinary or mcp:
            clear_tools_cache()
        if ordinary:
            get_chat_model.cache_clear()
            get_graph.cache_clear()
        self._notify_config_updated("CONFIG")

    def _notify_config_updated(self, name: str) -> None:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            from app.services.push_service import push_config_updated

            loop.create_task(push_config_updated(name))
        except RuntimeError:
            pass

    async def reset(
        self,
        *,
        reset_sessions: bool = False,
        reset_memory: bool = False,
        reset_global_config: bool = False,
    ) -> str:
        parts: list[str] = []
        if reset_sessions:
            await self.session_service.clear_all()
            parts.append("已清除所有会话")
        if reset_memory:
            memory_store = MemoryStore(self.workspace)
            memory_store.clear_daily()
            memory_store.reset_longterm()
            parts.append("已清除记忆")
        if reset_global_config:
            self.store.reset_global()
            self._revisions.bump_for_path(config_file_path())
            self._invalidate_runtime(ordinary=True, mcp=True)
            parts.append("已重置全局配置（含规范 mcp 域）")
        if not parts:
            return "未执行任何重置操作"
        return "；".join(parts)
