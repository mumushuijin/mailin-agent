"""热层条款本地状态机：执行 ADD/UPDATE/DELETE/NOOP。"""

from __future__ import annotations

import logging
from enum import Enum

from pydantic import BaseModel, Field

from app.context.memory.clauses import Clause

logger = logging.getLogger(__name__)


class MergeOperation(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    NOOP = "NOOP"


class MergeDecision(BaseModel):
    operation: MergeOperation
    clause_id: str
    new_text: str | None = Field(default=None)
    reason: str = Field(default="")


class SectionDecisions(BaseModel):
    decisions: list[MergeDecision]


def _norm_id(clause_id: str) -> str:
    return clause_id.strip().lower()


def apply_decisions(
    clauses: list[Clause],
    decisions: list[MergeDecision],
    *,
    section_key: str,
    section_char_limit: int,
    deleted_ids: set[str] | None = None,
) -> tuple[list[Clause], list[str]]:
    """顺序执行本节决策，返回 (新条款列表, 警告日志)。"""
    working = list(clauses)
    warnings: list[str] = []
    deleted = deleted_ids or set()

    for d in decisions:
        op = d.operation
        cid = d.clause_id.strip()
        key = _norm_id(cid)

        if op == MergeOperation.NOOP:
            continue

        if op == MergeOperation.DELETE:
            if key in deleted:
                warnings.append(f"DELETE 后又有操作 {cid}，已忽略")
                continue
            before = len(working)
            working = [c for c in working if _norm_id(c.clause_id) != key]
            if len(working) < before:
                deleted.add(key)
            else:
                warnings.append(f"DELETE 目标不存在: {cid}，降级 NOOP")
            continue

        if op == MergeOperation.UPDATE:
            if key in deleted:
                warnings.append(f"DELETE 后 UPDATE {cid}，已忽略")
                continue
            text = (d.new_text or "").strip()
            if not text:
                warnings.append(f"UPDATE {cid} 无正文，降级 NOOP")
                continue
            found = False
            for i, c in enumerate(working):
                if _norm_id(c.clause_id) == key:
                    working[i] = Clause(c.clause_id, section_key, text)
                    found = True
                    break
            if not found:
                warnings.append(f"UPDATE 目标不存在 {cid}，转 ADD")
                working.append(Clause(cid, section_key, text))
            continue

        if op == MergeOperation.ADD:
            text = (d.new_text or "").strip()
            if not text:
                warnings.append(f"ADD {cid} 无正文，跳过")
                continue
            existing = next((c for c in working if _norm_id(c.clause_id) == key), None)
            if existing:
                warnings.append(f"ADD {cid} 已存在，转 UPDATE")
                for i, c in enumerate(working):
                    if _norm_id(c.clause_id) == key:
                        working[i] = Clause(c.clause_id, section_key, text)
                        break
            else:
                working.append(Clause(cid, section_key, text))
            continue

        warnings.append(f"非法 operation {op}，降级 NOOP")

    content_len = sum(len(c.render_body()) for c in working)
    if content_len > section_char_limit:
        raise ValueError(f"Section {section_key} 超出字符上限 {content_len}/{section_char_limit}")

    return working, warnings
