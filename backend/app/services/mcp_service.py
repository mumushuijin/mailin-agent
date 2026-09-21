from __future__ import annotations

import logging
import re
import time
from copy import deepcopy
from typing import Any

from fastapi import HTTPException

from app.schemas.mcp import (
    McpAuthModel,
    McpConnectionModel,
    McpFieldError,
    McpListResponse,
    McpMutationResponse,
    McpRefreshResponse,
    McpRefreshServerResult,
    McpRuntimeModel,
    McpServerDetail,
    McpServerDraft,
    McpServerListItem,
    McpServerWrite,
    McpStatusView,
    McpTestResponse,
    McpTimeoutsModel,
    McpToolsModel,
    McpValidateResponse,
)
from app.tools.mcp.config import (
    definition_to_canonical,
    delete_mcp_server,
    load_mcp_catalog,
    merge_secret_maps,
    redact_mapping,
    save_mcp_server,
    set_mcp_server_enabled,
)
from app.tools.mcp.types import (
    MASK_LITERAL,
    McpCatalog,
    McpManagementState,
    McpServerDefinition,
    McpToolsFilter,
)

logger = logging.getLogger(__name__)

_ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_SERVER_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _structured_mcp_errors(
    messages: list[str],
    *,
    unresolved: list[str] | None = None,
) -> list[McpFieldError]:
    """把 MCP 校验字符串映射为与配置模块一致的 path/kind/message 结构。"""
    errors: list[McpFieldError] = []
    for msg in messages:
        lower = msg.lower()
        path = "/"
        kind = "validation"
        expected: str | None = None
        if "command" in lower:
            path = "/connection/command"
            kind = "type"
            expected = "string"
        elif "url" in lower:
            path = "/connection/url"
            kind = "type"
            expected = "string"
        elif "transport" in lower or "type" in lower and "connection" in lower:
            path = "/connection/type"
            kind = "type"
            expected = "stdio|streamable-http|sse"
        elif "未解析变量" in msg or "变量" in msg:
            path = "/connection"
            kind = "semantic"
        elif "环境变量" in msg:
            path = "/auth/required_env"
            kind = "semantic"
        errors.append(McpFieldError(path=path, kind=kind, message=msg, expected=expected))
    for var in unresolved or []:
        if any(var in e.message for e in errors):
            continue
        errors.append(
            McpFieldError(
                path="/connection",
                kind="semantic",
                message=f"未解析变量: ${{{var}}}",
            )
        )
    return errors


class McpService:
    def list_servers(self) -> McpListResponse:
        catalog = load_mcp_catalog()
        snapshot = self._status_snapshot()
        by_id = {s.name: s for s in snapshot.statuses}
        items: list[McpServerListItem] = []
        for server_id, definition in catalog.servers.items():
            status = by_id.get(server_id)
            state = self._resolve_state(definition, status, snapshot)
            items.append(
                McpServerListItem(
                    id=server_id,
                    display_name=definition.display_name,
                    enabled=definition.enabled,
                    transport=definition.connection_type,
                    state=state,
                    tool_count=status.tool_count if status else 0,
                    last_checked_at=snapshot.captured_at or None,
                    error_summary=(status.error if status else None)
                    or (definition.validation_errors[0] if definition.validation_errors else None),
                    can_connect=definition.can_connect,
                    validation_errors=list(definition.validation_errors),
                    unresolved_variables=list(definition.unresolved_variables),
                    source=definition.source,
                )
            )
        return McpListResponse(
            servers=items,
            total=len(items),
            config_revision=catalog.revision,
            warnings=list(catalog.warnings),
            legacy_shadowed=catalog.legacy_shadowed,
            source=catalog.source,
        )

    def get_server(self, server_id: str) -> McpServerDetail:
        catalog = load_mcp_catalog()
        definition = catalog.servers.get(server_id)
        if definition is None:
            raise HTTPException(status_code=404, detail=f"MCP server '{server_id}' 不存在")
        return self._to_detail(definition, catalog)

    def upsert_server(self, server_id: str, body: McpServerWrite) -> McpMutationResponse:
        self._validate_server_id(server_id)
        catalog = load_mcp_catalog()
        existing = catalog.servers.get(server_id)
        merged = self._merge_write(existing, body)
        errors = [
            e
            for e in self._validate_canonical(merged)
            if not str(e).startswith("未解析变量") and not str(e).startswith("缺少必需环境变量")
        ]
        if errors:
            raise HTTPException(status_code=400, detail={"validation_errors": errors})

        new_catalog = save_mcp_server(server_id, merged)
        self._invalidate_runtime_caches(reason="save")
        definition = new_catalog.servers[server_id]
        return McpMutationResponse(
            server=self._to_detail(definition, new_catalog),
            config_revision=new_catalog.revision,
        )

    def delete_server(self, server_id: str) -> dict[str, Any]:
        catalog = load_mcp_catalog()
        if server_id not in catalog.servers:
            raise HTTPException(status_code=404, detail=f"MCP server '{server_id}' 不存在")
        new_catalog = delete_mcp_server(server_id)
        self._close_server_runtime(server_id)
        self._invalidate_runtime_caches(reason="delete")
        return {"status": "ok", "id": server_id, "config_revision": new_catalog.revision}

    def enable_server(self, server_id: str, enabled: bool) -> McpMutationResponse:
        catalog = load_mcp_catalog()
        if server_id not in catalog.servers:
            raise HTTPException(status_code=404, detail=f"MCP server '{server_id}' 不存在")
        new_catalog = set_mcp_server_enabled(server_id, enabled)
        if not enabled:
            self._close_server_runtime(server_id)
        self._invalidate_runtime_caches(reason="enable" if enabled else "disable")
        definition = new_catalog.servers[server_id]
        return McpMutationResponse(
            server=self._to_detail(definition, new_catalog),
            config_revision=new_catalog.revision,
        )

    def validate_draft(self, draft: McpServerDraft) -> McpValidateResponse:
        catalog_before = load_mcp_catalog()
        revision_before = catalog_before.revision
        canonical = self._draft_to_canonical(draft)
        # 用临时目录校验，不落盘
        definition = self._definition_from_canonical(draft.id or "draft", canonical)
        ok = not definition.validation_errors
        catalog_after = load_mcp_catalog()
        if catalog_after.revision != revision_before:
            logger.error("validate_draft unexpectedly mutated config revision")
        return McpValidateResponse(
            ok=ok,
            validation_errors=list(definition.validation_errors),
            errors=_structured_mcp_errors(
                definition.validation_errors,
                unresolved=definition.unresolved_variables,
            ),
            unresolved_variables=list(definition.unresolved_variables),
            warnings=list(definition.warnings),
            can_connect=definition.can_connect,
            normalized=definition_to_canonical(definition),
        )

    def test_draft(self, draft: McpServerDraft) -> McpTestResponse:
        started = time.monotonic()
        catalog_before = load_mcp_catalog()
        cards_before = self._runtime_card_names()
        canonical = self._draft_to_canonical(draft)
        definition = self._definition_from_canonical(draft.id or "draft-test", canonical)
        if definition.validation_errors:
            return McpTestResponse(
                ok=False,
                state="not_configured" if not definition.can_connect else "error",
                duration_ms=(time.monotonic() - started) * 1000,
                error="; ".join(definition.validation_errors),
                error_code="validation_error",
                replaced_runtime=False,
            )

        try:
            from app.tools.mcp import lifecycle as life

            result = life.test_mcp_server_connection(definition.to_runtime_config())
        except Exception as exc:
            logger.exception("MCP test failed")
            result = {
                "ok": False,
                "state": "error",
                "tool_count": 0,
                "tool_names": [],
                "error": str(exc),
                "error_code": "test_failed",
            }

        cards_after = self._runtime_card_names()
        catalog_after = load_mcp_catalog()
        replaced = cards_before != cards_after or catalog_after.revision != catalog_before.revision
        return McpTestResponse(
            ok=bool(result.get("ok")),
            state=result.get("state") or "error",  # type: ignore[arg-type]
            duration_ms=(time.monotonic() - started) * 1000,
            tool_count=int(result.get("tool_count") or 0),
            tool_names=list(result.get("tool_names") or []),
            error=result.get("error"),
            error_code=result.get("error_code"),
            replaced_runtime=replaced,
        )

    def refresh(self) -> McpRefreshResponse:
        from app.tools.mcp import lifecycle as life

        catalog = load_mcp_catalog()
        results_raw = life.refresh_mcp_servers()
        snapshot = self._status_snapshot()
        by_id = {s.name: s for s in snapshot.statuses}
        results: list[McpRefreshServerResult] = []
        for server_id, definition in catalog.servers.items():
            raw = results_raw.get(server_id) or {}
            status = by_id.get(server_id)
            state = self._resolve_state(definition, status, snapshot)
            if not definition.enabled:
                state = "disabled"
            results.append(
                McpRefreshServerResult(
                    id=server_id,
                    ok=bool(raw.get("ok", status.connected if status else False)),
                    state=state,
                    tool_count=int(raw.get("tool_count") or (status.tool_count if status else 0)),
                    error=raw.get("error") or (status.error if status else None),
                    error_code=raw.get("error_code") or (status.error_code if status else None),
                )
            )
        return McpRefreshResponse(
            results=results,
            config_revision=catalog.revision,
            applied_revision=snapshot.applied_revision,
        )

    def _to_detail(self, definition: McpServerDefinition, catalog: McpCatalog) -> McpServerDetail:
        snapshot = self._status_snapshot()
        status = next((s for s in snapshot.statuses if s.name == definition.id), None)
        state = self._resolve_state(definition, status, snapshot)
        redacted_headers = redact_mapping(definition.headers)
        redacted_env = redact_mapping(definition.env, treat_all_as_secret=True)
        header_refs = {
            k: v for k, v in definition.headers.items() if _ENV_REF_RE.match(str(v) or "")
        }
        env_refs = {
            k: v for k, v in definition.env.items() if _ENV_REF_RE.match(str(v) or "")
        }
        return McpServerDetail(
            id=definition.id,
            display_name=definition.display_name,
            enabled=definition.enabled,
            transport=definition.connection_type,
            state=state,
            tool_count=status.tool_count if status else 0,
            last_checked_at=snapshot.captured_at or None,
            error_summary=(status.error if status else None)
            or (definition.validation_errors[0] if definition.validation_errors else None),
            can_connect=definition.can_connect,
            validation_errors=list(definition.validation_errors),
            unresolved_variables=list(definition.unresolved_variables),
            source=definition.source,
            connection=McpConnectionModel(
                type=definition.connection_type,
                url=definition.url,
                headers=redacted_headers,
                command=definition.command,
                args=list(definition.args),
                env=redacted_env,
            ),
            auth=McpAuthModel(required_env=list(definition.required_env)),
            timeouts=McpTimeoutsModel(
                connect=definition.timeouts_connect,
                call=definition.timeouts_call,
            ),
            tools=McpToolsModel(
                include=sorted(definition.tools_filter.include),
                exclude=sorted(definition.tools_filter.exclude),
                resources=definition.tools_filter.resources,
                prompts=definition.tools_filter.prompts,
            ),
            runtime=McpRuntimeModel(
                supports_parallel_tool_calls=definition.supports_parallel_tool_calls,
            ),
            warnings=list(definition.warnings) + list(catalog.warnings),
            unknown_fields=dict(definition.unknown_fields),
            status=McpStatusView(
                state=state,
                connected=bool(status.connected) if status else False,
                tool_count=status.tool_count if status else 0,
                tool_names=list(status.tool_names) if status else [],
                error=status.error if status else None,
                error_code=status.error_code if status else None,
                captured_at=snapshot.captured_at,
                source=snapshot.source,
                stale=snapshot.stale or state == "stale",
                config_revision=catalog.revision,
                applied_revision=snapshot.applied_revision,
            ),
            header_refs=header_refs,
            env_refs=env_refs,
        )

    def _merge_write(
        self,
        existing: McpServerDefinition | None,
        body: McpServerWrite,
    ) -> dict[str, Any]:
        existing_headers = dict(existing.headers) if existing else {}
        existing_env = dict(existing.env) if existing else {}
        headers = merge_secret_maps(
            existing_headers,
            body.connection.headers,
            cleared_keys=body.clear_headers,
        )
        env = merge_secret_maps(
            existing_env,
            body.connection.env,
            cleared_keys=body.clear_env,
        )
        display_name = body.display_name or (existing.display_name if existing else None)
        unknown = dict(existing.unknown_fields) if existing else {}
        return {
            "display_name": display_name,
            "enabled": body.enabled,
            "connection": {
                "type": body.connection.type,
                "url": body.connection.url,
                "headers": headers,
                "command": body.connection.command,
                "args": list(body.connection.args),
                "env": env,
            },
            "auth": {"required_env": list(body.auth.required_env)},
            "timeouts": {
                "connect": body.timeouts.connect,
                "call": body.timeouts.call,
            },
            "tools": {
                "include": list(body.tools.include),
                "exclude": list(body.tools.exclude),
                "resources": body.tools.resources,
                "prompts": body.tools.prompts,
            },
            "runtime": {
                "supports_parallel_tool_calls": body.runtime.supports_parallel_tool_calls,
            },
            **{k: v for k, v in unknown.items() if not str(k).startswith("connection.")},
        }

    def _draft_to_canonical(self, draft: McpServerDraft) -> dict[str, Any]:
        return {
            "display_name": draft.display_name or draft.id or "draft",
            "enabled": draft.enabled,
            "connection": {
                "type": draft.connection.type,
                "url": draft.connection.url,
                "headers": {
                    k: v
                    for k, v in draft.connection.headers.items()
                    if v != MASK_LITERAL
                },
                "command": draft.connection.command,
                "args": list(draft.connection.args),
                "env": {
                    k: v for k, v in draft.connection.env.items() if v != MASK_LITERAL
                },
            },
            "auth": {"required_env": list(draft.auth.required_env)},
            "timeouts": {"connect": draft.timeouts.connect, "call": draft.timeouts.call},
            "tools": {
                "include": list(draft.tools.include),
                "exclude": list(draft.tools.exclude),
                "resources": draft.tools.resources,
                "prompts": draft.tools.prompts,
            },
            "runtime": {
                "supports_parallel_tool_calls": draft.runtime.supports_parallel_tool_calls,
            },
        }

    def _definition_from_canonical(self, server_id: str, canonical: dict[str, Any]) -> McpServerDefinition:
        from app.tools.mcp.config import _normalize_server_entry
        from app.core.settings import get_settings

        return _normalize_server_entry(
            server_id,
            deepcopy(canonical),
            source="mcp",
            workspace_path=str(get_settings().workspace_path),
        )

    def _validate_canonical(self, canonical: dict[str, Any]) -> list[str]:
        definition = self._definition_from_canonical("tmp", canonical)
        return list(definition.validation_errors)

    def _validate_server_id(self, server_id: str) -> None:
        if not _SERVER_ID_RE.match(server_id):
            raise HTTPException(
                status_code=400,
                detail="server id 须以字母开头，仅含字母数字_-，最长 64",
            )

    def _resolve_state(
        self,
        definition: McpServerDefinition,
        status,
        snapshot,
    ) -> McpManagementState:
        from app.tools.mcp.lifecycle import resolve_management_state

        return resolve_management_state(definition, status, snapshot)

    def _status_snapshot(self):
        from app.tools.mcp.lifecycle import get_mcp_status_snapshot

        return get_mcp_status_snapshot()

    def _invalidate_runtime_caches(self, *, reason: str) -> None:
        from app.tools.mcp.lifecycle import mark_mcp_config_stale
        from app.tools.registry import clear_tools_cache
        from app.agent.graph import get_graph
        from app.core.llm import get_chat_model

        mark_mcp_config_stale(reason=reason)
        clear_tools_cache()
        get_chat_model.cache_clear()
        get_graph.cache_clear()

    def _close_server_runtime(self, server_id: str) -> None:
        try:
            from app.tools.mcp.lifecycle import close_mcp_server

            close_mcp_server(server_id)
        except Exception as exc:
            logger.debug("close MCP server '%s' failed: %s", server_id, exc)

    def _runtime_card_names(self) -> set[str]:
        try:
            from app.tools.mcp.bridge import get_mcp_cards

            return {c.name for c in get_mcp_cards()}
        except Exception:
            return set()
