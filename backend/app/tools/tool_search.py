"""渐进式工具披露：热工具 + 三桥接工具（tool_search / tool_describe / tool_call）。

延时（非热）工具不出现在 bind_tools 中，由模型通过桥接工具按需发现与调用。
Catalog 每轮从当前 ToolCard 列表无状态重建，避免与会话注册表漂移。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from rank_bm25 import BM25Okapi

from app.context.tool_cache import SKIP_CACHE_KWARG, is_tool_results_path
from app.tools.card import ToolCard, resolve_concurrency
from app.tools.exposure import to_langchain_tool, to_langchain_tools
from app.resilience import execute_sync, tool_error_json, tool_policy

logger = logging.getLogger(__name__)

TOOL_SEARCH_NAME = "tool_search"
TOOL_DESCRIBE_NAME = "tool_describe"
TOOL_CALL_NAME = "tool_call"

BRIDGE_TOOL_NAMES = frozenset({TOOL_SEARCH_NAME, TOOL_DESCRIBE_NAME, TOOL_CALL_NAME})

DEFAULT_HOT_TOOLS = (
    "read_file",
    "write_file",
    "replace_in_file",
    "glob_search",
    "search_files",
    "list_directory",
    "run_shell",
    "process",
    "skill_list",
    "skill_match",
    "skill_read",
    "todo",
    "ask_user",
)

KERNEL_NINE_TOOLS = (
    "read_file",
    "write_file",
    "replace_in_file",
    "glob_search",
    "search_files",
    "run_shell",
    "process",
    "todo",
    "ask_user",
)

_PRE_SKILLS_DEFAULT_HOT_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "replace_in_file",
        "glob_search",
        "search_files",
        "list_directory",
        "run_shell",
        "process",
        "todo",
        "ask_user",
    }
)

# 旧 workspace_defaults 拷贝出来的热列表；集合相等则升级为新默认。
_LEGACY_DEFAULT_HOT_TOOLS = frozenset(
    {
        "read_file",
        "list_directory",
        "run_shell",
        "memory_grep",
        "memory_add",
        "memory_consolidate",
        "python_calculator",
        "get_current_time",
    }
)


def _resolve_hot_tools(hot_raw: Any) -> tuple[str, ...]:
    if not isinstance(hot_raw, list) or not hot_raw:
        return DEFAULT_HOT_TOOLS
    hot_tools = tuple(str(n).strip() for n in hot_raw if str(n).strip())
    if not hot_tools:
        return DEFAULT_HOT_TOOLS
    if frozenset(hot_tools) in {_LEGACY_DEFAULT_HOT_TOOLS, _PRE_SKILLS_DEFAULT_HOT_TOOLS}:
        return DEFAULT_HOT_TOOLS
    return hot_tools

_TOKEN_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSearchConfig:
    enabled: bool = True
    hot_tools: tuple[str, ...] = DEFAULT_HOT_TOOLS
    search_default_limit: int = 5
    max_search_limit: int = 20
    mcp_as_hot: bool = False

    @classmethod
    def from_config(cls, config: dict | None = None) -> ToolSearchConfig:
        if config is None:
            from app.tools.registry import load_full_config

            config = load_full_config()
        full = config
        tools_cfg = full.get("tools") if isinstance(full.get("tools"), dict) else {}
        raw = tools_cfg.get("tool_search")

        if raw is False:
            return cls(enabled=False)
        if raw is True or raw is None:
            return cls()
        if not isinstance(raw, dict):
            return cls()

        enabled_raw = raw.get("enabled", True)
        enabled = enabled_raw not in (False, "false", "0", "off")

        hot_tools = _resolve_hot_tools(raw.get("hot_tools"))

        mcp_as_hot_raw = raw.get("mcp_as_hot", False)
        mcp_as_hot = mcp_as_hot_raw not in (False, "false", "0", "off")

        max_limit = max(1, min(50, _safe_int(raw.get("max_search_limit"), 20)))
        default_limit = max(1, min(max_limit, _safe_int(raw.get("search_default_limit"), 5)))

        return cls(
            enabled=enabled,
            hot_tools=hot_tools,
            search_default_limit=default_limit,
            max_search_limit=max_limit,
            mcp_as_hot=mcp_as_hot,
        )


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def load_tool_search_config(config: dict | None = None) -> ToolSearchConfig:
    return ToolSearchConfig.from_config(config)


# ---------------------------------------------------------------------------
# 分类与 catalog
# ---------------------------------------------------------------------------


def is_bridge_tool(name: str) -> bool:
    return name in BRIDGE_TOOL_NAMES


def _mcp_card_names(cards: list[ToolCard]) -> list[str]:
    return [c.name for c in cards if c.enabled and c.package.startswith("mcp-")]


def effective_hot_set(cards: list[ToolCard], config: ToolSearchConfig) -> set[str]:
    """热工具集合：CONFIG hot_tools + 可选 MCP 远程工具直绑（mcp_as_hot）。"""
    hot = set(config.hot_tools)
    if config.mcp_as_hot:
        hot.update(_mcp_card_names(cards))
    return hot


def is_hot_tool_name(name: str, cards: list[ToolCard], config: ToolSearchConfig) -> bool:
    return name in effective_hot_set(cards, config)


def is_deferred_tool_name(name: str, cards: list[ToolCard], config: ToolSearchConfig) -> bool:
    if name in BRIDGE_TOOL_NAMES:
        return False
    hot_set = effective_hot_set(cards, config)
    return any(c.name == name for c in cards) and name not in hot_set


def classify_cards(
    cards: list[ToolCard],
    config: ToolSearchConfig,
) -> tuple[list[ToolCard], list[ToolCard]]:
    hot_set = effective_hot_set(cards, config)
    hot: list[ToolCard] = []
    deferred: list[ToolCard] = []
    for card in cards:
        if not card.enabled:
            continue
        if card.name in hot_set:
            hot.append(card)
        else:
            deferred.append(card)
    return hot, deferred


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    tokens = _TOKEN_RE.findall(text.lower())
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            tokens.append(ch)
    return tokens


def _entry_search_text(card: ToolCard) -> str:
    params = (card.parameters or {}).get("properties") or {}
    param_names = " ".join(params.keys())
    name_words = card.name.replace("_", " ").replace(".", " ").replace("-", " ")
    package_hint = ""
    if card.package.startswith("mcp-"):
        server = card.package.removeprefix("mcp-")
        package_hint = f"mcp external plugin server {server}"
    return f"{name_words} {card.summary} {card.description} {package_hint} {param_names}"


@dataclass
class CatalogEntry:
    card: ToolCard
    _tokens: list[str] = field(default_factory=list)


def build_catalog(cards: list[ToolCard]) -> list[CatalogEntry]:
    return [
        CatalogEntry(card=card, _tokens=_tokenize(_entry_search_text(card)))
        for card in cards
    ]


def search_catalog(
    catalog: list[CatalogEntry],
    query: str,
    *,
    limit: int = 5,
) -> list[CatalogEntry]:
    if not catalog or limit <= 0:
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    corpus = [e._tokens for e in catalog]
    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(query_tokens)

    scored: list[tuple[float, CatalogEntry]] = []
    for idx, entry in enumerate(catalog):
        score = float(scores[idx])
        if score > 0:
            scored.append((score, entry))

    if not scored:
        ql = query.lower()
        for entry in catalog:
            blob = f"{entry.card.name} {entry.card.summary}".lower()
            if ql in blob or any(t in blob for t in query_tokens):
                scored.append((0.1, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [entry for _, entry in scored[:limit]]


def _find_card(cards: list[ToolCard], name: str) -> ToolCard | None:
    for card in cards:
        if card.name == name:
            return card
    return None


def _format_tool_not_found_error(
    name: str,
    cards: list[ToolCard],
    config: ToolSearchConfig,
) -> str:
    """未知工具：统一引导走 tool_search 披露流程。"""
    _, deferred = classify_cards(cards, config)
    deferred_names = [c.name for c in deferred]
    return json.dumps(
        {
            "error": (
                f"工具 '{name}' 未直接加载，当前不可调用。"
                "请先 tool_search 检索延时工具目录，再 tool_describe → tool_call。"
            ),
            "requested_tool": name,
            "workflow": ["tool_search", "tool_describe", "tool_call"],
            "deferred_tool_count": len(deferred_names),
            "deferred_sample": deferred_names[:8],
        },
        ensure_ascii=False,
    )


def _summarize_deferred_packages(deferred: list[ToolCard], limit: int = 8) -> str:
    if not deferred:
        return "（当前无延时工具）"
    by_pkg: dict[str, list[str]] = {}
    for card in deferred:
        by_pkg.setdefault(card.package, []).append(card.name)
    lines: list[str] = []
    for pkg in sorted(by_pkg.keys())[:limit]:
        names = by_pkg[pkg]
        if pkg == "mcp":
            label = "MCP 管理"
        elif pkg.startswith("mcp-"):
            label = f"MCP 服务「{pkg.removeprefix('mcp-')}」"
        else:
            label = f"包「{pkg}」"
        sample = names[0] if names else ""
        lines.append(f"- {label}：{len(names)} 个（如 {sample}）")
    return "\n".join(lines)


def deferred_card_names(cards: list[ToolCard], config: ToolSearchConfig) -> frozenset[str]:
    _, deferred = classify_cards(cards, config)
    return frozenset(c.name for c in deferred)


# ---------------------------------------------------------------------------
# 桥接工具描述（与 packages/*/descriptions.py 同结构）
# ---------------------------------------------------------------------------

_TOOL_DISCLOSURE_PREFIX = """【工具披露】部分能力未直接加载。除下列已列出的热工具外，其余须按顺序调用：
1. tool_search — 检索延时工具
2. tool_describe — 获取完整参数 schema
3. tool_call — 代理调用延时工具

【调用方式·必读】必须使用模型原生 function calling（tool_calls 字段）发起上述工具。
禁止在回复正文中输出 DSML、XML、Markdown 代码块或伪代码形式的工具调用（如 <｜｜DSML｜｜invoke ...>）。

"""

_HOT_TOOL_PREFIX = """【热工具·直接可用】本工具已在工具列表中，可直接调用，无需 tool_search / tool_call。

"""

_DEFERRED_TOOL_PREFIX = """【延时工具】须依次通过 tool_search → tool_describe → tool_call 调用，不可直接出现在工具列表中。

"""


def _bridge_tool_search_description(deferred: list[ToolCard]) -> str:
    catalog_summary = _summarize_deferred_packages(deferred)
    return f"""一句话功能：在 {len(deferred)} 个按需加载的延时工具目录中，按关键词检索匹配项，返回名称与摘要。

{_TOOL_DISCLOSURE_PREFIX}当前延时工具目录概览：
{catalog_summary}

适用场景：
- 需要未直接列出的文件操作（write_file、delete_file 等）
- 需要记忆管理、计算、外部集成等延时工具
- 需要 MCP 外部服务下的具体工具（与 read_file 同级；先 tool_search 按能力关键词检索）
- 能用自然语言描述所需能力，但不确定确切工具名

不适用场景：
- 目标工具已在工具列表中（热工具）→ 直接调用
- 已通过 tool_describe 确认工具名且参数 schema 已知 → 可直接 tool_call
- 仅需再次调用刚 describe 过的同一工具 → 直接 tool_call

参数说明：
- query（必填）：描述所需能力的关键词或短语
- limit（可选，默认 5，上限见配置）：最多返回条数

返回格式：
- 成功：JSON 对象，含 query、total_available、matches（每项含 name、package、summary）
- 无匹配：matches 为空数组，可换关键词重试
- 失败：{{"error": "中文说明"}}

风险限制：
- 仅搜索当前会话已启用工具包中的延时工具
- 结果不含完整 parameters → 调用前须 tool_describe
- 标准流程：tool_search → tool_describe → tool_call"""


def _bridge_tool_describe_description() -> str:
    return f"""一句话功能：加载 tool_search 返回的某个延时工具的完整 ToolCard（含 parameters schema 与使用说明）。

{_TOOL_DISCLOSURE_PREFIX}适用场景：
- tool_search 命中目标工具后，需要确认参数名、类型与必填项
- 准备 tool_call 前参数形状不明确
- 需要阅读完整适用/不适用场景与风险限制

不适用场景：
- 工具是热工具（已在工具列表中）→ 直接调用，本工具会返回错误
- 尚未 tool_search 或工具名拼写不确定 → 应先 tool_search
- 刚 describe 过且 schema 未变 → 直接 tool_call，无需重复 describe

参数说明：
- name（必填）：延时工具的精确名称，须与 tool_search 返回的 name 一致

返回格式：
- 成功：JSON ToolCard 公开视图（name、summary、description、parameters、risk_level 等）
- 失败：{{"error": "中文说明"}}（热工具、名称无效、工具不可用等）

风险限制：
- 仅可 describe 延时工具，不可 describe 桥接工具本身
- describe 不产生副作用；实际执行须 tool_call"""


def _bridge_tool_call_description() -> str:
    return f"""一句话功能：代理调用一个延时工具，arguments 须符合 tool_describe 返回的 parameters schema。

{_TOOL_DISCLOSURE_PREFIX}适用场景：
- 已通过 tool_describe 确认工具名与参数
- 执行任意延时工具（含 MCP 外部工具）的实际操作

不适用场景：
- 工具是热工具 → 直接调用该工具，勿经 tool_call
- 未执行 tool_describe 且参数不确定 → 应先 describe
- 试图调用 tool_search / tool_describe / tool_call 自身 → 禁止
- 在正文手写 DSML / JSON 工具调用 → 禁止，必须用 tool_calls

参数说明：
- name（必填）：延时工具精确名称
- arguments（必填）：JSON 对象，键名与 describe 返回的 parameters.properties 一致

正确示例（须通过 tool_calls 发起，勿写入正文）：
- tool_search(query="八字")
- tool_describe(name="mcp_Bazi_MCP_getBaziDetail")
- tool_call(name="mcp_Bazi_MCP_getBaziDetail", arguments={{"solarDatetime": "2002-03-08T17:00:00+08:00", "gender": 1}})

返回格式：
- 成功：与直接调用该工具相同（文本或结构化结果）
- 失败：{{"error": "中文说明"}}（参数非法、工具不存在、热工具误用等）

风险限制：
- 权限、沙箱与风险级别与原工具一致（如 delete_file 仍须用户明确授权）
- 执行不可自动撤销；写入/删除类操作前宜先 read_file 确认
- UI 与轨迹记录会显示底层真实工具名，而非 tool_call"""


def _hot_tool_description(card: ToolCard) -> str:
    return f"{_HOT_TOOL_PREFIX}{card.description}"


def _to_hot_langchain_tool(card: ToolCard) -> BaseTool:
    return to_langchain_tool(card, description=_hot_tool_description(card))


def _make_bridge_tools(deferred: list[ToolCard]) -> list[BaseTool]:
    def _search(query: str, limit: int | None = None) -> str:
        return dispatch_tool_search({"query": query, "limit": limit})

    def _describe(name: str) -> str:
        return dispatch_tool_describe({"name": name})

    def _call(name: str, arguments: dict[str, Any]) -> str:
        return dispatch_tool_call_proxy({"name": name, "arguments": arguments})

    return [
        StructuredTool.from_function(
            func=_search,
            name=TOOL_SEARCH_NAME,
            description=_bridge_tool_search_description(deferred),
        ),
        StructuredTool.from_function(
            func=_describe,
            name=TOOL_DESCRIBE_NAME,
            description=_bridge_tool_describe_description(),
        ),
        StructuredTool.from_function(
            func=_call,
            name=TOOL_CALL_NAME,
            description=_bridge_tool_call_description(),
        ),
    ]


# ---------------------------------------------------------------------------
# 对外组装：bind_tools 可见列表
# ---------------------------------------------------------------------------


@dataclass
class AssemblyResult:
    tools: list[BaseTool]
    activated: bool = False
    hot_count: int = 0
    deferred_count: int = 0


def assemble_bind_tools(
    cards: list[ToolCard],
    config: ToolSearchConfig | None = None,
) -> AssemblyResult:
    """返回模型应 bind 的工具：热工具 + 桥接工具，或全部工具（未启用 / 无延时工具）。"""
    config = config or load_tool_search_config()
    enabled_cards = [c for c in cards if c.enabled]

    if not config.enabled or not enabled_cards:
        tools = to_langchain_tools(enabled_cards)
        return AssemblyResult(tools=tools, activated=False, hot_count=len(tools))

    hot, deferred = classify_cards(enabled_cards, config)
    if not deferred:
        tools = to_langchain_tools(enabled_cards)
        return AssemblyResult(tools=tools, activated=False, hot_count=len(tools))

    visible = [_to_hot_langchain_tool(c) for c in hot] + _make_bridge_tools(deferred)
    logger.info(
        "tool_search 已激活：%d 热工具，%d 延时工具经桥接暴露",
        len(hot),
        len(deferred),
    )
    return AssemblyResult(
        tools=visible,
        activated=True,
        hot_count=len(hot),
        deferred_count=len(deferred),
    )


# ---------------------------------------------------------------------------
# 桥接分发
# ---------------------------------------------------------------------------


def _cards_for_dispatch() -> tuple[list[ToolCard], ToolSearchConfig]:
    from app.tools.registry import get_registry

    config = load_tool_search_config(get_registry().config)
    return get_registry().resolve_cards(), config


def _format_search_hit(entry: CatalogEntry) -> dict[str, Any]:
    card = entry.card
    return {
        "name": card.name,
        "package": card.package,
        "summary": card.summary,
    }


def dispatch_tool_search(
    args: dict[str, Any],
    *,
    cards: list[ToolCard] | None = None,
    config: ToolSearchConfig | None = None,
) -> str:
    cards, config = (cards, config) if cards is not None else _cards_for_dispatch()
    if config is None:
        config = load_tool_search_config()

    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query 为必填"}, ensure_ascii=False)

    raw_limit = args.get("limit")
    if raw_limit is None:
        limit = config.search_default_limit
    else:
        limit = max(1, min(config.max_search_limit, _safe_int(raw_limit, config.search_default_limit)))

    _, deferred = classify_cards(cards, config)
    catalog = build_catalog(deferred)
    hits = search_catalog(catalog, query, limit=limit)
    return json.dumps(
        {
            "query": query,
            "total_available": len(catalog),
            "matches": [_format_search_hit(h) for h in hits],
        },
        ensure_ascii=False,
    )


def dispatch_tool_describe(
    args: dict[str, Any],
    *,
    cards: list[ToolCard] | None = None,
    config: ToolSearchConfig | None = None,
) -> str:
    cards, config = (cards, config) if cards is not None else _cards_for_dispatch()
    if config is None:
        config = load_tool_search_config()

    name = str(args.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "name 为必填"}, ensure_ascii=False)

    if name in effective_hot_set(cards, config):
        return json.dumps(
            {
                "error": (
                    f"'{name}' 是热工具，已直接列在工具列表中。"
                    "请直接调用该工具，无需 tool_describe。"
                ),
            },
            ensure_ascii=False,
        )

    if not is_deferred_tool_name(name, cards, config):
        return json.dumps(
            {
                "error": (
                    f"'{name}' 不是可用的延时工具。"
                    "请检查拼写，或通过 tool_search 重新检索。"
                ),
            },
            ensure_ascii=False,
        )

    card = _find_card(cards, name)
    if card is None:
        return json.dumps({"error": f"未找到工具 '{name}'"}, ensure_ascii=False)

    public = card.to_public_dict()
    public["description"] = f"{_DEFERRED_TOOL_PREFIX}{public.get('description', '')}"
    return json.dumps(public, ensure_ascii=False)


def resolve_tool_call(
    args: dict[str, Any],
    *,
    cards: list[ToolCard],
    config: ToolSearchConfig,
) -> tuple[str | None, dict[str, Any], str | None]:
    name = str(args.get("name") or "").strip()
    if not name:
        return None, {}, "tool_call 需要 name 参数"
    if name in BRIDGE_TOOL_NAMES:
        return None, {}, f"不能通过 tool_call 调用桥接工具 '{name}'"

    raw_args = args.get("arguments")
    if raw_args is None:
        raw_args = {}
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            return None, {}, f"arguments 不是合法 JSON: {exc}"
    if not isinstance(raw_args, dict):
        return None, {}, "arguments 必须是对象"

    if name in effective_hot_set(cards, config):
        return None, {}, f"'{name}' 是热工具，请直接调用，不要通过 tool_call"

    if not is_deferred_tool_name(name, cards, config):
        return None, {}, f"'{name}' 不是可代理的延时工具，请先用 tool_search 确认名称"

    if _find_card(cards, name) is None:
        return None, {}, f"工具 '{name}' 当前不可用"

    return name, raw_args, None


def invoke_card(card: ToolCard, arguments: dict[str, Any]) -> str:
    def _run() -> str:
        if card.source == "plugin":
            result = card.handler(**(arguments or {}))
            if isinstance(result, str):
                return result
            return json.dumps(result, ensure_ascii=False, default=str)
        tool = to_langchain_tool(card)
        result = tool.invoke(arguments)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

    # MCP handler 内已有 execute_sync（mcp:{server}），此处不再套 tool:{name} 层，避免熔断/超时嵌套。
    if card.package.startswith("mcp-"):
        try:
            return _run()
        except Exception as exc:
            return tool_error_json(str(exc))

    outcome = execute_sync(
        f"tool:{card.name}",
        _run,
        policy=tool_policy(card.name),
    )
    if outcome.ok and outcome.value is not None:
        return outcome.value
    return tool_error_json(outcome.error or f"工具 '{card.name}' 调用失败")


def resolve_tool_display(
    name: str,
    args: dict[str, Any],
    *,
    cards: list[ToolCard] | None = None,
    config: ToolSearchConfig | None = None,
) -> tuple[str, dict[str, Any]]:
    """解析 UI 应展示的工具名与参数（tool_call 解包为底层延时/MCP 工具）。"""
    if cards is None or config is None:
        cards, config = _cards_for_dispatch()
    if name == TOOL_CALL_NAME:
        underlying, underlying_args, _err = resolve_tool_call(args, cards=cards, config=config)
        if underlying:
            return underlying, underlying_args
    return name, dict(args or {})


def should_show_tool_in_ui(name: str) -> bool:
    """桥接披露工具仅内部使用，不在 UI 展示。"""
    return name not in {TOOL_SEARCH_NAME, TOOL_DESCRIBE_NAME, TOOL_CALL_NAME}


def dispatch_tool_call_proxy(args: dict[str, Any]) -> str:
    """供桥接 StructuredTool 直接调用时使用（测试 / 非 graph 路径）。"""
    cards, config = _cards_for_dispatch()
    underlying, underlying_args, err = resolve_tool_call(args, cards=cards, config=config)
    if err:
        return json.dumps({"error": err}, ensure_ascii=False)
    card = _find_card(cards, underlying or "")
    if card is None:
        return json.dumps({"error": f"未找到工具 '{underlying}'"}, ensure_ascii=False)
    return invoke_card(card, underlying_args)


def execute_single_tool_call(
    name: str,
    args: dict[str, Any],
    *,
    cards: list[ToolCard] | None = None,
    config: ToolSearchConfig | None = None,
) -> tuple[str, str]:
    """执行一次工具调用。返回 (展示用工具名, 结果文本)。"""
    if cards is None or config is None:
        cards, config = _cards_for_dispatch()
    elif config is None:
        config = load_tool_search_config()

    if name == TOOL_CALL_NAME:
        underlying, underlying_args, err = resolve_tool_call(args, cards=cards, config=config)
        if err:
            return name, json.dumps({"error": err}, ensure_ascii=False)
        card = _find_card(cards, underlying or "")
        if card is None:
            return underlying or name, _format_tool_not_found_error(underlying or name, cards, config)
        return underlying or name, invoke_card(card, underlying_args)

    if is_bridge_tool(name):
        if name == TOOL_SEARCH_NAME:
            return name, dispatch_tool_search(args, cards=cards, config=config)
        if name == TOOL_DESCRIBE_NAME:
            return name, dispatch_tool_describe(args, cards=cards, config=config)
        return name, json.dumps({"error": f"未知桥接工具 {name}"}, ensure_ascii=False)

    card = _find_card(cards, name)
    if card is None:
        return name, _format_tool_not_found_error(name, cards, config)
    return name, invoke_card(card, args)


def _parse_tool_call_args(tc: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    name = tc.get("name", "")
    args = tc.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if not isinstance(args, dict):
        args = {}
    return name, args, tc.get("id") or ""


def _execute_parsed_call(
    name: str,
    args: dict[str, Any],
    tool_call_id: str,
    *,
    cards: list[ToolCard],
    config: ToolSearchConfig,
    session_id: str,
    process_for_cache,
) -> ToolMessage:
    display_name, content = execute_single_tool_call(name, args, cards=cards, config=config)

    from app.agent.hooks import dispatch_transform_tool_result

    transformed = dispatch_transform_tool_result(
        tool_name=display_name,
        arguments=args,
        result=content,
        tool_call_id=tool_call_id,
        session_id=session_id,
    )
    if transformed is not None:
        content = transformed

    extra_kwargs: dict[str, Any] = {}
    if display_name == "read_file":
        file_path = args.get("file_path")
        if isinstance(file_path, str) and is_tool_results_path(file_path):
            extra_kwargs[SKIP_CACHE_KWARG] = True
    msg = ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        name=display_name,
        additional_kwargs=extra_kwargs,
    )
    return process_for_cache(msg, session_id)


def run_tool_calls(
    tool_calls: list[dict[str, Any]],
    session_id: str,
    *,
    process_for_cache,
) -> list[ToolMessage]:
    """执行 AIMessage.tool_calls：连续 safe 调用并行，barrier 前 drain 且互不重叠。"""
    from concurrent.futures import ThreadPoolExecutor

    cards, config = _cards_for_dispatch()
    parsed: list[tuple[str, dict[str, Any], str]] = [_parse_tool_call_args(tc) for tc in tool_calls]
    messages: list[ToolMessage] = []
    i = 0
    while i < len(parsed):
        name, args, _tc_id = parsed[i]
        card = _find_card(cards, name)
        if name == TOOL_CALL_NAME:
            underlying, _, _ = resolve_tool_call(args, cards=cards, config=config)
            card = _find_card(cards, underlying or "")
            conc = resolve_concurrency(card, args.get("arguments") if isinstance(args.get("arguments"), dict) else args)
        else:
            conc = resolve_concurrency(card, args)

        if conc == "safe":
            batch: list[tuple[str, dict[str, Any], str]] = []
            while i < len(parsed):
                n, a, tid = parsed[i]
                c = _find_card(cards, n)
                call_args = a
                if n == TOOL_CALL_NAME:
                    underlying, _, _ = resolve_tool_call(a, cards=cards, config=config)
                    c = _find_card(cards, underlying or "")
                    if isinstance(a.get("arguments"), dict):
                        call_args = a["arguments"]
                if resolve_concurrency(c, call_args) != "safe":
                    break
                batch.append(parsed[i])
                i += 1
            if len(batch) == 1:
                n, a, tid = batch[0]
                messages.append(
                    _execute_parsed_call(
                        n, a, tid, cards=cards, config=config, session_id=session_id, process_for_cache=process_for_cache
                    )
                )
            else:
                with ThreadPoolExecutor(max_workers=min(8, len(batch))) as pool:
                    futs = [
                        pool.submit(
                            _execute_parsed_call,
                            n,
                            a,
                            tid,
                            cards=cards,
                            config=config,
                            session_id=session_id,
                            process_for_cache=process_for_cache,
                        )
                        for n, a, tid in batch
                    ]
                    messages.extend(fut.result() for fut in futs)
            continue

        n, a, tid = parsed[i]
        messages.append(
            _execute_parsed_call(
                n, a, tid, cards=cards, config=config, session_id=session_id, process_for_cache=process_for_cache
            )
        )
        i += 1
    return messages
