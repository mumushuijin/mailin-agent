"""候选记忆 Rule Router：确定性分流与 ID 校验。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.context.memory.registry import (
    TargetFile,
    id_prefix_for_section,
    prefix_to_section_key,
    section_keys,
)

ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
Confidence = Literal["high", "medium", "low"]


class RawCandidate(BaseModel):
    fact: str
    target: TargetFile = "memory"
    section: str | None = None
    clause_id: str | None = None
    related_id: str | None = None
    confidence: Confidence = "high"

    @field_validator("fact")
    @classmethod
    def _fact_nonempty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("fact 不能为空")
        return v


@dataclass
class RoutedCandidate:
    fact: str
    target: TargetFile
    section: str
    clause_id: str
    related_id: str | None
    confidence: Confidence
    warnings: list[str] = field(default_factory=list)


def _slugify(text: str, max_len: int = 16) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text.lower()).strip("_")
    return (s[:max_len] or "note")


def _reassign_prefix(clause_id: str, target: TargetFile, section_key: str) -> str:
    if "." not in clause_id:
        return f"{id_prefix_for_section(target, section_key).rstrip('.')}.{_slugify(clause_id)}"
    _, slug = clause_id.split(".", 1)
    prefix = id_prefix_for_section(target, section_key).rstrip(".")
    return f"{prefix}.{slug}"


def _resolve_section(candidate: RawCandidate) -> str:
    target = candidate.target
    if candidate.section and candidate.section in section_keys(target):
        return candidate.section
    if candidate.clause_id:
        key = prefix_to_section_key(target, candidate.clause_id)
        if key:
            return key
    return "misc"


def _normalize_id(
    candidate: RawCandidate,
    section_key: str,
    existing_ids: set[str],
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    target = candidate.target
    cid = (candidate.clause_id or "").strip().lower()

    if not cid or not ID_PATTERN.match(cid):
        n = 1
        base = f"{id_prefix_for_section(target, section_key).rstrip('.')}.auto"
        while f"{base}_{n}" in existing_ids:
            n += 1
        cid = f"{base}_{n}"
        warnings.append(f"ID 格式无效，分配为 {cid}")
    else:
        expected = prefix_to_section_key(target, cid)
        if expected and expected != section_key:
            cid = _reassign_prefix(cid, target, section_key)
            warnings.append(f"前缀与 section 不匹配，已纠正为 {cid}")

    if cid in existing_ids and candidate.related_id is None:
        # 新候选与已有 ID 冲突且非 UPDATE 语义
        base, slug = cid.rsplit(".", 1)
        n = 2
        while f"{base}_{n}" in existing_ids:
            n += 1
        cid = f"{base}_{n}"
        warnings.append(f"ID 冲突，分配为 {cid}")

    return cid, warnings


def validate_raw_candidates(raw_list: list) -> tuple[list[RawCandidate], list[str]]:
    """解析并校验候选 JSON；返回 (有效列表, 错误信息)。"""
    valid: list[RawCandidate] = []
    errors: list[str] = []
    if not isinstance(raw_list, list):
        return [], ["输出必须是 JSON 数组"]
    for i, item in enumerate(raw_list):
        try:
            valid.append(RawCandidate.model_validate(item))
        except Exception as exc:
            errors.append(f"候选 {i + 1}: {exc}")
    return valid, errors


def route_candidates(
    candidates: list[RawCandidate],
    existing_ids: dict[TargetFile, set[str]],
) -> dict[tuple[TargetFile, str], list[RoutedCandidate]]:
    """Rule Router：分组为 {(target, section_key): [RoutedCandidate]}。"""
    groups: dict[tuple[TargetFile, str], list[RoutedCandidate]] = {}
    for cand in candidates:
        target: TargetFile = cand.target if cand.target in ("memory", "user") else "memory"
        section = _resolve_section(cand)
        if section not in section_keys(target):
            section = "misc"
        if cand.confidence == "low":
            section = "misc"

        ids = existing_ids.setdefault(target, set())
        clause_id, warnings = _normalize_id(cand, section, ids)
        ids.add(clause_id)

        routed = RoutedCandidate(
            fact=cand.fact,
            target=target,
            section=section,
            clause_id=clause_id,
            related_id=(cand.related_id or "").strip() or None,
            confidence=cand.confidence,
            warnings=warnings,
        )
        groups.setdefault((target, section), []).append(routed)
    return groups
