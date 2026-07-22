from __future__ import annotations

import html
import re
from dataclasses import dataclass

import httpx

from app.core.settings import get_settings
from app.tools.runtime import tool_session_id

_USER_AGENT = "Mailin/0.1 (local-agent; +https://github.com/mailin)"
_SESSION_SEARCH_CACHE: dict[str, dict[str, str]] = {}


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def _format_results(query: str, results: list[SearchResult]) -> str:
    if not results:
        return f"未找到与「{query}」相关的搜索结果。"
    lines = [f"## 搜索结果（query: {query}）", ""]
    for i, item in enumerate(results, 1):
        lines.append(f"{i}. **{item.title}**")
        if item.snippet:
            lines.append(f"   摘要: {item.snippet}")
        lines.append(f"   来源: {item.url}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _search_tavily(query: str, *, max_results: int = 5) -> list[SearchResult]:
    api_key = get_settings().tavily_api_key
    if not api_key:
        return []

    resp = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "include_answer": False,
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        SearchResult(
            title=str(item.get("title") or "无标题"),
            url=str(item.get("url") or ""),
            snippet=str(item.get("content") or "")[:300],
        )
        for item in data.get("results", [])[:max_results]
        if item.get("url")
    ]


def _search_duckduckgo(query: str, *, max_results: int = 5) -> list[SearchResult]:
    resp = httpx.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
        timeout=20.0,
    )
    resp.raise_for_status()
    page = resp.text

    blocks = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>|<div[^>]+class="result__snippet"[^>]*>(.*?)</div>)',
        page,
        flags=re.DOTALL,
    )
    results: list[SearchResult] = []
    for url, title_raw, snippet_a, snippet_div in blocks:
        title = html.unescape(re.sub(r"<[^>]+>", "", title_raw)).strip()
        snippet = html.unescape(re.sub(r"<[^>]+>", "", snippet_a or snippet_div or "")).strip()
        if url.startswith("//"):
            url = "https:" + url
        results.append(SearchResult(title=title or "无标题", url=url, snippet=snippet[:300]))
        if len(results) >= max_results:
            break
    return results


def _normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip().lower())


def search_web(query: str, *, max_results: int = 5) -> str:
    """执行网络搜索，优先 Tavily，降级 DuckDuckGo。"""
    query = query.strip()
    if not query:
        return "搜索关键词不能为空。"

    session_id = tool_session_id.get() or "_global"
    cache_key = _normalize_query(query)
    session_cache = _SESSION_SEARCH_CACHE.setdefault(session_id, {})
    if cache_key in session_cache:
        return (
            session_cache[cache_key]
            + "\n\n（本会话已搜索过相同或等价 query，请基于以上结果作答，勿重复调用 web_search。）"
        )

    backend = "unknown"
    try:
        if get_settings().tavily_api_key:
            results = _search_tavily(query, max_results=max_results)
            backend = "tavily"
        else:
            results = _search_duckduckgo(query, max_results=max_results)
            backend = "duckduckgo"

        if not results and get_settings().tavily_api_key:
            results = _search_duckduckgo(query, max_results=max_results)
            backend = "duckduckgo"

        if not results:
            return (
                f"搜索「{query}」未返回结果。"
                "请检查网络连接，或在 .env 中配置 TAVILY_API_KEY 以获得更稳定的搜索。"
            )
        formatted = _format_results(query, results) + f"\n\n（搜索后端: {backend}）"
        session_cache[cache_key] = formatted
        return formatted
    except httpx.HTTPError as e:
        return f"搜索请求失败: {e}。请检查网络或 API Key 配置。"
    except Exception as e:
        return f"搜索出错: {e}"
