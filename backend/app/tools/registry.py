from __future__ import annotations

import json
import time
from functools import lru_cache
from pathlib import Path

from langchain_core.tools import BaseTool

from app.core.settings import get_settings
from app.tools.card import ToolCard
from app.tools.exposure import enrich_card_parameters
from app.tools.tool_search import assemble_bind_tools
from app.tools.packages.base import ToolPackage
from app.tools.packages import BUILTIN_PACKAGES
from app.tools.mcp.package import PACKAGE as MCP_PACKAGE

ALL_PACKAGES: list[ToolPackage] = [*BUILTIN_PACKAGES, MCP_PACKAGE]
REGISTRY_CACHE_TTL_SECONDS = 30.0


def _tool_switch_enabled(value, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        enabled = value.get("enabled", True)
        return enabled not in (False, "false", "0", "off")
    return bool(value)


def load_full_config(workspace: Path | None = None) -> dict:
    workspace = workspace or get_settings().workspace_path
    path = workspace / "CONFIG.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


class ToolRegistry:
    """内置工具包注册中心：按 CONFIG 开关解析 ToolCard 并投影为 LangChain 工具。"""

    def __init__(self, config: dict | None = None, packages: list[ToolPackage] | None = None):
        self.config = config if config is not None else load_full_config()
        self.packages = packages if packages is not None else ALL_PACKAGES
        self._cards_cache: list[ToolCard] | None = None
        self._cards_cache_at = 0.0
        self.resolve_cards_builds = 0

    def is_package_enabled(self, package: ToolPackage) -> bool:
        tools_cfg = self.config.get("tools", {})
        if package.info.config_key == "mcp":
            if "mcp" in tools_cfg:
                return bool(tools_cfg["mcp"])
            return bool(self.config.get("mcp_servers"))
        value = tools_cfg.get(package.info.config_key)
        if value is None:
            return package.info.default_enabled
        return _tool_switch_enabled(value)

    def resolve_cards(self, *, force_refresh: bool = False) -> list[ToolCard]:
        from app.tools.mcp.lifecycle import ensure_mcp_connected

        now = time.monotonic()
        if (
            not force_refresh
            and self._cards_cache is not None
            and now - self._cards_cache_at <= REGISTRY_CACHE_TTL_SECONDS
        ):
            return list(self._cards_cache)

        # 不阻塞主线程；MCP 未连接时仍返回内置工具，连接在后台或工具调用时重试
        ensure_mcp_connected(blocking=False)
        cards: list[ToolCard] = []
        for package in self.packages:
            if self.is_package_enabled(package):
                cards.extend(package.build_cards(self.config))
        self._cards_cache = enrich_card_parameters(cards)
        self._cards_cache_at = now
        self.resolve_cards_builds += 1
        return list(self._cards_cache)

    def get_langchain_tools(self) -> list[BaseTool]:
        return assemble_bind_tools(self.resolve_cards()).tools

    def list_packages(self) -> list[dict]:
        enabled_keys = {
            package.info.config_key
            for package in self.packages
            if self.is_package_enabled(package)
        }
        result: list[dict] = []
        for package in self.packages:
            info = package.info
            tool_count = len(package.build_cards(self.config)) if self.is_package_enabled(package) else 0
            result.append(
                {
                    "id": info.id,
                    "name": info.name,
                    "version": info.version,
                    "description": info.description,
                    "config_key": info.config_key,
                    "default_enabled": info.default_enabled,
                    "enabled": info.config_key in enabled_keys,
                    "tool_count": tool_count,
                }
            )
        return result

    def list_cards(self) -> list[dict]:
        cards: list[dict] = []
        for package in self.packages:
            package_cards = package.build_cards(self.config)
            enrich_card_parameters(package_cards)
            enabled = self.is_package_enabled(package)
            for card in package_cards:
                public = card.to_public_dict()
                public["enabled"] = enabled
                cards.append(public)
        return cards


@lru_cache
def get_registry() -> ToolRegistry:
    return ToolRegistry()


@lru_cache
def get_tools() -> list[BaseTool]:
    return get_registry().get_langchain_tools()


def clear_tools_cache() -> None:
    get_registry.cache_clear()
    get_tools.cache_clear()
    try:
        from app.tools.tool_search import clear_tool_search_runtime_cache

        clear_tool_search_runtime_cache()
    except Exception:
        pass
