"""热层结构化条款：硬分隔符 + 固定 Section 组装。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.context.memory.registry import (
    TargetFile,
    display_name,
    key_from_display,
    prefix_to_section_key,
    section_keys,
)

CLAUSE_DELIMITER = "\n◆\n"
SECTION_MARKER = "§§§"
SECTION_PATTERN = re.compile(
    rf"{re.escape(SECTION_MARKER)}\s*(.+?)\s*{re.escape(SECTION_MARKER)}"
)
CLAUSE_ID_PATTERN = re.compile(r"^\[([^\]]+)\]\s*(.*)$", re.DOTALL)
CLAUSE_SPLIT_RE = re.compile(r"\n*◆\n*")
EMPTY_PLACEHOLDER_DEFAULT = "（空）"

DEFAULT_TITLE = "# 长期记忆"
USER_TITLE = "# 用户画像"


@dataclass(frozen=True)
class Clause:
    clause_id: str
    section: str  # section key，如 preference / identity
    text: str

    def render_body(self) -> str:
        return f"[{self.clause_id}] {self.text.strip()}"


def _normalize_id(clause_id: str) -> str:
    return clause_id.strip().lower()


def parse_clauses(content: str, *, default_title: str = DEFAULT_TITLE, target: TargetFile = "memory") -> list[Clause]:
    """解析组装视图 Markdown 为条款列表。"""
    text = (content or "").strip()
    if not text:
        return []

    lines = text.splitlines()
    if lines and lines[0].startswith("#"):
        text = "\n".join(lines[1:]).strip()

    clauses: list[Clause] = []
    if SECTION_MARKER in text:
        matches = list(SECTION_PATTERN.finditer(text))
        if matches:
            for i, match in enumerate(matches):
                disp = match.group(1).strip()
                section_key = key_from_display(target, disp) or disp.lower()
                body_start = match.end()
                body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                body = text[body_start:body_end]
                clauses.extend(_parse_section_body(section_key, body, target))
    else:
        clauses.extend(_parse_legacy_body(text, target))

    seen: set[str] = set()
    unique: list[Clause] = []
    for c in clauses:
        key = _normalize_id(c.clause_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique


def _parse_section_body(section_key: str, body: str, target: TargetFile) -> list[Clause]:
    body = body.strip()
    if not body or body == EMPTY_PLACEHOLDER_DEFAULT:
        return []
    if "◆" not in body:
        clause = _parse_single_clause(body, section_key, target)
        return [clause] if clause else []
    chunks = CLAUSE_SPLIT_RE.split(body)
    out: list[Clause] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk or chunk == EMPTY_PLACEHOLDER_DEFAULT:
            continue
        clause = _parse_single_clause(chunk, section_key, target)
        if clause:
            out.append(clause)
    return out


def _parse_single_clause(chunk: str, section_key: str, target: TargetFile) -> Clause | None:
    lines = [ln for ln in chunk.splitlines() if not ln.strip().startswith("## ")]
    chunk = "\n".join(lines).strip()
    if not chunk:
        return None

    m = CLAUSE_ID_PATTERN.match(chunk)
    if m:
        clause_id, text = m.group(1).strip(), m.group(2).strip()
        if clause_id and text:
            key = prefix_to_section_key(target, clause_id) or section_key
            return Clause(clause_id=clause_id, section=key, text=text)

    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "_", chunk[:24]).strip("_").lower() or "legacy"
    return Clause(clause_id=f"misc.legacy_{slug}", section="misc", text=chunk)


def _parse_legacy_body(text: str, target: TargetFile) -> list[Clause]:
    clauses: list[Clause] = []
    current = "misc"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            disp = stripped[3:].strip()
            current = key_from_display(target, disp) or prefix_to_section_key(target, disp) or "misc"
            continue
        if stripped.startswith("- "):
            body = stripped[2:].strip()
            clause = _parse_single_clause(body, current, target)
            if clause:
                clauses.append(clause)
    return clauses


def render_sections(
    sections: dict[str, list[Clause]],
    *,
    target: TargetFile,
    title: str = DEFAULT_TITLE,
    empty_placeholder: str = EMPTY_PLACEHOLDER_DEFAULT,
) -> str:
    """按注册表顺序组装 Markdown（含空节占位）。"""
    parts = [title, ""]
    for key in section_keys(target):
        disp = display_name(target, key)
        parts.append(f"{SECTION_MARKER} {disp} {SECTION_MARKER}")
        clauses = sections.get(key, [])
        if clauses:
            bodies = [c.render_body() for c in clauses]
            parts.append(CLAUSE_DELIMITER.join(bodies))
        else:
            parts.append(empty_placeholder)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def render_clauses(
    clauses: list[Clause],
    *,
    target: TargetFile = "memory",
    title: str = DEFAULT_TITLE,
    empty_placeholder: str = EMPTY_PLACEHOLDER_DEFAULT,
) -> str:
    sections: dict[str, list[Clause]] = {k: [] for k in section_keys(target)}
    for c in clauses:
        key = c.section if c.section in sections else "misc"
        sections[key].append(Clause(c.clause_id, key, c.text))
    return render_sections(sections, target=target, title=title, empty_placeholder=empty_placeholder)


def char_count(sections: dict[str, list[Clause]], *, target: TargetFile, title: str = DEFAULT_TITLE) -> int:
    return len(render_sections(sections, target=target, title=title))


def find_clause(clauses: list[Clause], clause_id: str) -> Clause | None:
    key = _normalize_id(clause_id)
    for c in clauses:
        if _normalize_id(c.clause_id) == key:
            return c
    return None


def detect_drift(raw: str, *, target: TargetFile = "memory", title: str = DEFAULT_TITLE) -> bool:
    if not raw.strip():
        return False
    parsed = parse_clauses(raw, default_title=title, target=target)
    sections: dict[str, list[Clause]] = {k: [] for k in section_keys(target)}
    for c in parsed:
        sections.setdefault(c.section, []).append(c)
    rendered = render_sections(sections, target=target, title=title)
    return raw.strip() != rendered.strip()
