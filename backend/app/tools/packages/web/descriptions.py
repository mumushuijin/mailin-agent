from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDoc:
    summary: str
    description: str


TOOL_DOCS: dict[str, ToolDoc] = {
    "web_search": ToolDoc(
        summary="检索互联网实时信息，返回带标题、摘要与来源链接的搜索结果",
        description="""一句话功能：根据 query 在互联网上搜索，返回最多 5 条结构化结果（标题、摘要、URL）。

适用场景：
- 用户询问今日新闻、实时股价、最新政策、刚发布的技术动态等时效性信息
- 需要核实近期事实、版本号、活动日期等可能已变化的信息
- 用户明确要求「搜一下」「查最新」「上网找」
- 本地工作区（search_files / read_file）中没有相关资料，且问题依赖外部信息

不适用场景：
- 通用知识、概念解释、代码写法等模型已有可靠知识的问题 → 直接回答
- 查找用户工作区内的本地文件或笔记 → 用 search_files、glob_search、read_file
- 需要读取某个已知 URL 的全文内容 → 本工具只返回搜索摘要，不抓取网页正文
- 用户未要求联网，且问题不依赖实时信息 → 不要主动搜索
- 搜索失败或结果不足 → 不要编造答案，应如实告知用户并说明原因

参数说明：
- query（必填）：搜索关键词或自然语言查询，宜具体明确
  - 好例子：「Rust 1.85 2024 edition 新特性」「2026年6月 杭州 天气」
  - 差例子：「新闻」「帮我查」→ 应结合用户上下文补全后再搜

返回格式：
- 成功：Markdown 列表，每条含
  - 序号与标题
  - 摘要（snippet，最多约 300 字）
  - 来源 URL
  - 末尾标注搜索后端（tavily 或 duckduckgo）
- 无结果：说明未找到，并提示检查网络或配置 TAVILY_API_KEY
- 失败：返回中文错误说明（网络错误、API 异常等）；不得虚构搜索结果

风险限制：
- 需要出站网络；无网络或 API 不可用时将失败
- 搜索后端：优先使用 .env 中配置的 TAVILY_API_KEY（Tavily）；未配置时降级 DuckDuckGo
- Tavily 无结果时会自动降级 DuckDuckGo 重试一次
- 单次最多返回 5 条结果；请求超时约 20 秒
- 同一 query 在本会话内只会真正搜索一次，重复调用会返回缓存并提示直接作答
- 已有足够搜索结果时，应综合已有摘要回答用户，勿用相同或极相似 query 反复搜索
- 摘要来自第三方页面，引用时应说明来源，并提醒用户核实
- 不产生本地文件副作用；不访问工作区外本地路径""",
    ),
}
