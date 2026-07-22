"""热层沉淀：候选提取 → Rule Router → 按节批量仲裁 → 本地状态机。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage

from app.context.api_usage import extract_api_usage
from app.context.budget import load_context_config
from app.context.memory.clauses import Clause
from app.context.memory.registry import (
    TargetFile,
    display_name,
    section_table_for_prompt,
)
from app.context.memory.router import (
    RawCandidate,
    RoutedCandidate,
    route_candidates,
    validate_raw_candidates,
)
from app.context.memory.sediment_state import SedimentState
from app.context.memory.state_machine import (
    MergeDecision,
    MergeOperation,
    apply_decisions,
)
from app.context.memory.store import HotMemoryStore, ensure_hot_layer_initialized, hot_stores
from app.core.llm import get_chat_model
from app.core.settings import get_settings
from app.storage.workspace import MemoryStore

logger = logging.getLogger(__name__)

CANDIDATE_PROMPT = """你是记忆候选提取助手。从「待沉淀每日笔记」提取值得写入长期记忆的候选事实。

## 记忆价值边界（What to keep）
目标是留下能在未来多次帮助协作的稳定事实，而不是复述当天流水账。宁可保留潜在重要的习惯、偏好、约定、环境事实，也不要因为证据还少就轻易丢弃；不确定但可能长期有用的候选用 confidence=low，让系统路由到 misc。

### 收录标准
- 用户明确要求“记住/别忘/下次还要”的内容，即使较短也应优先提取
- 长期偏好：语言、语气、输出格式、详略程度、设计/代码风格、沟通禁忌
- 稳定习惯：工作节奏、常用流程、决策偏好、反复出现的行为模式
- 长期约定：项目规范、命名规则、目录约束、测试/提交/部署流程、团队或个人规则
- 环境事实：OS、Shell、路径、技术栈、依赖工具、常用命令、MCP/外部服务配置习惯
- 身份与上下文：用户称呼、角色、长期负责领域、持续项目背景
- 对已有条款的补充、细化、例外或推翻；应填写 related_id 指向旧条款

### 排除标准
- 一次性任务进度、临时排查步骤、当天状态、短期计划、已完成的操作流水账
- 可从当前上下文直接推断、无需长期记忆的普通对话内容
- 低价值寒暄、情绪表达、泛泛评价、没有可复用信息的偏好猜测
- 密码、API Key、Token、Cookie、私钥、验证码、敏感凭据或完整隐私数据
- 与现有条款完全等价且没有新增信息的重复内容

## 固定 Section（不得新建节；无法归类 → misc）

### MEMORY (target=memory)
{memory_sections}

### USER (target=user)
{user_sections}

## 现有条款索引（仅 id + 预览，非全文）
### MEMORY
{memory_index}

### USER
{user_index}

## 待沉淀每日笔记
{daily_memories}

## 输出要求
- JSON 数组，最多 {max_candidates} 条；无候选返回 []
- 每条字段：fact, target, section, clause_id, related_id, confidence
- clause_id 格式：<prefix><slug>，如 pref.lang、env.stack、user.name
- related_id：仅当补充/推翻已有条款时填旧 id，否则 null
- confidence: high | medium | low（不确定则 low，系统会改投 misc）
- fact 写成可长期复用的原子事实，不写流水账；同一主题尽量合并为一条候选
- 不写密码/API Key/Token/私钥等敏感信息

示例：
[{{"fact":"用户偏好中文","target":"memory","section":"preference","clause_id":"pref.lang","related_id":null,"confidence":"high"}}]

仅输出 JSON 数组，不要代码块。"""

SECTION_ARBITER_PROMPT = """你是记忆 Section 仲裁助手。对本节现有条款与本节候选事实，输出批量决策。

## Section: {section_display} ({section_key}) | target={target}

## 本节现有条款
{existing_clauses}

## 本节候选（已路由）
{candidates}

## 跨节相关条款（只读参考）
{cross_refs}

## 融合逻辑（How to update）
- 优先把同一主题的候选融合进已有条款，而不是新增近义条款
- UPDATE 必须输出融合后的完整 new_text：保留旧条款仍然有效的核心信息，再加入候选的新事实、限定条件、例外或更准确表述
- 候选与旧条款部分冲突时，不要直接覆盖；若可能是时间/场景/条件差异，合并成带条件的表述
- 候选明确推翻旧条款且不可共存时才 DELETE；证据不足时 UPDATE 为更谨慎的限定表达，或 NOOP
- 候选只是例子、临时任务或当天噪音时 NOOP；但潜在长期习惯、偏好、约定、环境事实应尽量保留
- ADD 只用于本节没有可融合条款的新主题；new_text 应简洁、稳定、可复用
- 不得写入密码、API Key、Token、私钥等敏感信息

## 操作语义
- ADD: 新条款（clause_id 不存在）
- UPDATE: 补充/细化已有条款（输出融合后完整 new_text，ID 不变）
- DELETE: 彻底推翻旧条款
- NOOP: 已有等价记录

一次输出本节全部决策的 JSON 数组：
[{{"operation":"UPDATE","clause_id":"pref.lang","new_text":"..."}}, ...]"""

MISC_COMPACT_PROMPT = """以下是 misc 节全部条款。请去重合并为更精炼的条目（保留 misc. 前缀 ID），删除明显过时内容。
价值边界：保留可能长期有用的习惯、偏好、约定、环境事实；删除流水账、一次性任务、短期噪音、等价重复和敏感凭据。宁可保留潜在重要习惯，也不要轻易丢弃。
直接输出 JSON 数组：[{{"id":"misc.xxx","text":"..."}}]
最多保留 8 条。

{clauses}"""

REMEMBER_PATTERNS = re.compile(
    r"记住|记一下|下次还要|别忘了|帮我记|请记住|记下来",
    re.IGNORECASE,
)

_REMEMBER_PREFIX_RE = re.compile(
    r"^(?:请)?(?:帮我记|记住|记下来|记一下)[：:，,\s]*",
    re.IGNORECASE,
)


def extract_remember_fact(message: str) -> str:
    """去掉「请记住」等触发语，保留要沉淀的事实正文。"""
    text = (message or "").strip()
    if not text:
        return ""
    cleaned = _REMEMBER_PREFIX_RE.sub("", text).strip()
    return cleaned or text


def _memory_config(workspace) -> dict:
    cfg = load_context_config(workspace).get("memory", {})
    hot = cfg.get("hot", {})
    return {**cfg, **hot}


def _invoke_llm_json(prompt: str, source: str) -> tuple[Any, list[dict]]:
    from app.resilience import CallContext, execute_sync, llm_agent_policy

    api_usages: list[dict] = []
    model = get_chat_model()
    policy = llm_agent_policy(dependency_id=f"llm.memory.{source}")
    outcome = execute_sync(
        policy.dependency_id,
        lambda: model.invoke([HumanMessage(content=prompt)]),
        policy=policy,
        context=CallContext(metadata={"source": source}),
    )
    if not outcome.ok or outcome.value is None:
        raise RuntimeError(outcome.error or "记忆 LLM 调用失败")
    resp = outcome.value
    usage = extract_api_usage(resp, source=source)
    if usage:
        api_usages.append(usage)
    text = str(resp.content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text), api_usages


def _format_index(index: list[dict]) -> str:
    if not index:
        return "（无）"
    return "\n".join(f"- [{i['id']}] ({i['section']}) {i['text_preview']}" for i in index)


def _extract_candidates(
    stores: dict[TargetFile, HotMemoryStore],
    daily_block: str,
    *,
    max_candidates: int,
    retry_hint: str = "",
) -> list[RawCandidate]:
    prompt = CANDIDATE_PROMPT.format(
        memory_sections=section_table_for_prompt("memory"),
        user_sections=section_table_for_prompt("user"),
        memory_index=_format_index(stores["memory"].build_index()),
        user_index=_format_index(stores["user"].build_index()),
        daily_memories=daily_block,
        max_candidates=max_candidates,
    )
    if retry_hint:
        prompt += f"\n\n上次输出格式错误，请修正：{retry_hint}"
    raw, _ = _invoke_llm_json(prompt, "memory_candidates")
    if not isinstance(raw, list):
        raise ValueError("候选提取输出必须是 JSON 数组")
    valid, errors = validate_raw_candidates(raw)
    if errors and not valid:
        raise ValueError("; ".join(errors[:3]))
    return valid[:max_candidates]


def _parse_arbiter_decisions(raw: Any) -> list[MergeDecision]:
    items = raw.get("decisions", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError("仲裁输出必须是 JSON 数组")
    return [MergeDecision.model_validate(item) for item in items]


def _arbitrate_section(
    store: HotMemoryStore,
    target: TargetFile,
    section_key: str,
    routed: list[RoutedCandidate],
    cross_clauses: list[Clause],
    *,
    from_remember: bool = False,
) -> list[MergeDecision]:
    existing = store.get_section(section_key)

    def fmt_clauses(clauses: list[Clause]) -> str:
        if not clauses:
            return "（无）"
        return "\n".join(f"- [{c.clause_id}] {c.text}" for c in clauses)

    def fmt_candidates(cands: list[RoutedCandidate]) -> str:
        return "\n".join(
            f"- [{c.clause_id}] {c.fact}" + (f" (related: {c.related_id})" if c.related_id else "")
            for c in cands
        )

    prompt = SECTION_ARBITER_PROMPT.format(
        section_display=display_name(target, section_key),
        section_key=section_key,
        target=target,
        existing_clauses=fmt_clauses(existing),
        candidates=fmt_candidates(routed),
        cross_refs=fmt_clauses(cross_clauses) if cross_clauses else "（无）",
    )

    def _remember_fallback() -> list[MergeDecision]:
        return [
            MergeDecision(operation=MergeOperation.ADD, clause_id=c.clause_id, new_text=c.fact)
            for c in routed
        ]

    try:
        raw, _ = _invoke_llm_json(prompt, "memory_section_arbiter")
        return _parse_arbiter_decisions(raw)
    except Exception as first_exc:
        try:
            retry_prompt = prompt + f"\n\n上次输出格式错误，请修正：{first_exc}\n仅输出 JSON 数组。"
            raw, _ = _invoke_llm_json(retry_prompt, "memory_section_arbiter")
            return _parse_arbiter_decisions(raw)
        except Exception as second_exc:
            logger.warning("Section %s 仲裁失败: %s", section_key, second_exc)
            if from_remember:
                return _remember_fallback()
            return [MergeDecision(operation=MergeOperation.NOOP, clause_id=c.clause_id) for c in routed]


def _collect_cross_refs(
    store: HotMemoryStore,
    routed: list[RoutedCandidate],
    section_key: str,
) -> list[Clause]:
    refs: list[Clause] = []
    seen: set[str] = set()
    for c in routed:
        if not c.related_id:
            continue
        found = store.find_clause(c.related_id)
        if found and found.section != section_key and found.clause_id not in seen:
            refs.append(found)
            seen.add(found.clause_id)
    return refs


def _maybe_compact_misc(store: HotMemoryStore, sediment_state: SedimentState) -> bool:
    cfg_count = int(load_context_config(store.workspace).get("memory", {}).get("hot", {}).get("misc_compact_every", 5))
    count = sediment_state.load().get("consolidate_count", 0)
    if cfg_count <= 0 or count % cfg_count != 0:
        return False
    misc = store.get_section("misc")
    if len(misc) < 3:
        return False
    payload = json.dumps([{"id": c.clause_id, "text": c.text} for c in misc], ensure_ascii=False)
    try:
        raw, _ = _invoke_llm_json(MISC_COMPACT_PROMPT.format(clauses=payload), "misc_compact")
        if not isinstance(raw, list):
            return False
        new_misc: list[Clause] = []
        for item in raw[:8]:
            if isinstance(item, dict) and item.get("id") and item.get("text"):
                new_misc.append(Clause(str(item["id"]), "misc", str(item["text"])))
        if new_misc:
            store.set_section("misc", new_misc)
            return True
    except Exception as exc:
        logger.warning("Misc compaction 失败: %s", exc)
    return False


def _run_pipeline(
    workspace,
    raw_candidates: list[RawCandidate] | list[dict],
    *,
    from_remember: bool = False,
) -> str:
    cfg = _memory_config(workspace)
    ensure_hot_layer_initialized(workspace)
    stores = hot_stores(workspace)

    if raw_candidates and isinstance(raw_candidates[0], dict):
        valid, errors = validate_raw_candidates(raw_candidates)
        if errors and not valid:
            return f"候选格式无效：{errors[0]}"
        candidates = valid
    else:
        candidates = list(raw_candidates)  # type: ignore[arg-type]

    if not candidates:
        return "无候选可处理"

    existing_ids: dict[TargetFile, set[str]] = {
        "memory": {c.clause_id for c in stores["memory"].all_clauses()},
        "user": {c.clause_id for c in stores["user"].all_clauses()},
    }
    groups = route_candidates(candidates, existing_ids)

    op_logs: list[str] = []
    for (target, section_key), routed in groups.items():
        store = stores[target]
        cross = _collect_cross_refs(store, routed, section_key)
        try:
            decisions = _arbitrate_section(
                store, target, section_key, routed, cross, from_remember=from_remember
            )
        except Exception as exc:
            logger.warning("仲裁 %s/%s 失败: %s", target, section_key, exc)
            continue

        # 补齐 ADD 的 new_text
        routed_by_id = {c.clause_id: c for c in routed}
        normalized: list[MergeDecision] = []
        for d in decisions:
            if d.operation == MergeOperation.ADD and not d.new_text:
                rc = routed_by_id.get(d.clause_id)
                if rc:
                    d = d.model_copy(update={"new_text": rc.fact})
            if d.operation in (MergeOperation.UPDATE, MergeOperation.DELETE):
                if not store.find_clause(d.clause_id) and d.operation == MergeOperation.UPDATE:
                    rc = routed_by_id.get(d.clause_id)
                    d = MergeDecision(
                        operation=MergeOperation.ADD,
                        clause_id=d.clause_id,
                        new_text=d.new_text or (rc.fact if rc else ""),
                    )
            normalized.append(d)

        try:
            updated, warns = apply_decisions(
                store.get_section(section_key),
                normalized,
                section_key=section_key,
                section_char_limit=store.section_char_limit(),
            )
            store.set_section(section_key, updated)
            for d in normalized:
                if d.operation != MergeOperation.NOOP:
                    op_logs.append(f"{target}/{section_key}:{d.operation.value}:{d.clause_id}")
            for w in warns:
                logger.info("状态机: %s", w)
        except ValueError as exc:
            return f"沉淀失败（{target}/{section_key}）：{exc}"

    for store in stores.values():
        store.commit()

    return f"已处理 {len(candidates)} 条候选（{', '.join(op_logs) or 'NOOP'}）"


def consolidate_to_longterm(workspace=None, days: int | None = None) -> str:
    workspace = workspace or get_settings().workspace_path
    cfg = _memory_config(workspace)
    if days is None:
        days = int(cfg.get("longterm_consolidate_days", 7))

    store = MemoryStore(workspace)
    sediment_state = SedimentState(workspace)
    pending = sediment_state.pending_entries(store, days=days)
    if not pending:
        return "无待沉淀内容"

    interval_h = int(cfg.get("consolidate_interval_hours", cfg.get("longterm_consolidate_interval_hours", 24)))
    last = sediment_state.last_consolidate_at()
    if last and (datetime.now() - last).total_seconds() < interval_h * 3600:
        return f"距上次沉淀不足 {interval_h} 小时，跳过"

    daily_block = "\n\n---\n\n".join(
        f"### {e['date']}\n{e['content'][:4000]}" for e in pending[:15]
    )
    max_candidates = int(cfg.get("max_candidates_per_run", 20))

    stores = hot_stores(workspace)
    ensure_hot_layer_initialized(workspace)

    try:
        candidates = _extract_candidates(stores, daily_block, max_candidates=max_candidates)
    except Exception as first_exc:
        try:
            candidates = _extract_candidates(
                stores, daily_block, max_candidates=max_candidates, retry_hint=str(first_exc)
            )
        except Exception as second_exc:
            return f"候选提取失败：{second_exc}"

    if not candidates:
        for entry in pending:
            sediment_state.mark_consolidated(entry["filename"], entry["content"])
        sediment_state.mark_longterm_consolidated()
        return "无值得沉淀的候选，已标记每日笔记为已处理"

    result = _run_pipeline(workspace, candidates)

    for entry in pending:
        sediment_state.mark_consolidated(entry["filename"], entry["content"])
    sediment_state.mark_longterm_consolidated()

    for target in ("memory", "user"):
        if _maybe_compact_misc(stores[target], sediment_state):
            stores[target].commit()
            result += f"；已压实 {target} misc"

    return f"已沉淀 {len(pending)} 个每日文件；{result}"


def user_requested_remember(user_message: str) -> bool:
    return bool(REMEMBER_PATTERNS.search(user_message or ""))


def consolidate_user_request(
    fact: str,
    workspace=None,
    *,
    target: TargetFile = "memory",
    section: str = "misc",
) -> str:
    workspace = workspace or get_settings().workspace_path
    fact_text = extract_remember_fact(fact)
    raw = {
        "fact": fact_text,
        "target": target,
        "section": section,
        "clause_id": None,
        "related_id": None,
        "confidence": "high",
    }
    return _run_pipeline(workspace, [raw], from_remember=True)
