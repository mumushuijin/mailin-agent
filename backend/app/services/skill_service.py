from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.context.budget import DEFAULT_CONTEXT_CONFIG
from app.core.exceptions import AppError, NotFoundError
from app.core.settings import get_settings
from app.schemas.skill import (
    SkillDetail,
    SkillEntry,
    SkillListResponse,
    SkillMatch,
    SkillMatchResponse,
    SkillRefreshResponse,
    SkillResource,
    SkillSource,
    SkillToggleResponse,
)

DEFAULT_SKILL_CONFIG = {
    "global_roots": ["skills"],
    "project_enabled": True,
    "catalog_max_items": 80,
    "catalog_max_chars": 12_000,
    "cache_ttl_seconds": 5,
    "disabled": [],
}

_REF_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)|`([^`]+\.(?:md|txt|json|ya?ml|py|ts|js))`")
_VALID_REF_PREFIXES = ("references/", "scripts/", "assets/")


@dataclass
class _ParsedSkill:
    entry: SkillEntry
    content: str | None = None
    root: Path | None = None


_CACHE: dict[tuple[str, str | None], tuple[float, list[_ParsedSkill]]] = {}


def _slugify(raw: str) -> str:
    text = (raw or "").strip().lower()
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"[^a-z0-9:.-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "skill"


def _is_truthy(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "off", "no"}
    return bool(value)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_workspace_config(workspace: Path) -> dict[str, Any]:
    from app.storage.workspace import config_file_path

    path = config_file_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def load_skill_config(workspace: Path | None = None) -> dict[str, Any]:
    workspace = workspace or get_settings().workspace_path
    try:
        from app.tools.registry import load_full_config

        full = load_full_config(workspace)
    except Exception:
        full = _load_workspace_config(workspace)
    raw = full.get("skills", {})
    config = dict(DEFAULT_SKILL_CONFIG)
    if isinstance(raw, dict):
        config.update({k: v for k, v in raw.items() if k in config})
    if not isinstance(config.get("global_roots"), list):
        config["global_roots"] = list(DEFAULT_SKILL_CONFIG["global_roots"])
    if not isinstance(config.get("disabled"), list):
        config["disabled"] = []
    context = full.get("context", {})
    if "catalog_max_chars" not in raw and isinstance(context, dict):
        max_tokens = int(context.get("max_tokens") or DEFAULT_CONTEXT_CONFIG["max_tokens"])
        budget = context.get("budget") if isinstance(context.get("budget"), dict) else {}
        skill_ratio = float(budget.get("skills", DEFAULT_CONTEXT_CONFIG["budget"]["skills"]))
        config["catalog_max_chars"] = max(1200, int(max_tokens * skill_ratio * 3))
    return config


def skills_dir(workspace: Path | None = None) -> Path:
    workspace = workspace or get_settings().workspace_path
    path = workspace / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def clear_skill_cache() -> None:
    _CACHE.clear()


def _safe_relative_path(root: Path, relative: str) -> Path:
    rel = relative.strip().replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"^[a-zA-Z]:", rel):
        raise AppError("无效的 skill 资源路径")
    target = (root / rel).resolve()
    base = root.resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise AppError("skill 资源路径越界") from exc
    return target


def _extract_frontmatter(text: str) -> tuple[dict[str, Any], str, str | None]:
    if not text.startswith("---"):
        return {}, text, None
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text, None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            raw = "\n".join(lines[1:idx])
            body = "\n".join(lines[idx + 1 :])
            try:
                data = yaml.safe_load(raw) or {}
            except yaml.YAMLError as exc:
                return {}, body, f"frontmatter 解析失败: {exc.__class__.__name__}"
            if not isinstance(data, dict):
                return {}, body, "frontmatter 必须是对象"
            return data, body, None
    return {}, text, "frontmatter 缺少结束分隔符"


def _first_body_line(body: str) -> str:
    for line in body.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("<!--"):
            continue
        return cleaned.lstrip("#").strip()
    return ""


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _parse_skill_dir(directory: Path, source: SkillSource, disabled_keys: set[str]) -> _ParsedSkill:
    errors: list[str] = []
    content: str | None = None
    frontmatter: dict[str, Any] = {}
    body = ""
    skill_md = directory / "SKILL.md"
    meta: dict[str, Any] = {}

    try:
        meta = _read_json(directory / "_meta.json")
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"_meta.json 解析失败: {exc.__class__.__name__}")

    if skill_md.exists() and skill_md.is_file():
        try:
            content = skill_md.read_text(encoding="utf-8")
            frontmatter, body, fm_error = _extract_frontmatter(content)
            if fm_error:
                errors.append(fm_error)
        except OSError as exc:
            errors.append(f"SKILL.md 读取失败: {exc.__class__.__name__}")
    else:
        errors.append("缺少 SKILL.md")

    raw_id = (
        str(frontmatter.get("name") or "").strip()
        or str(meta.get("slug") or meta.get("name") or "").strip()
        or directory.name
    )
    skill_id = _slugify(raw_id)
    key = f"{source}:{skill_id}"
    disabled = skill_id in disabled_keys or key in disabled_keys
    metadata = frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {}
    version = (
        frontmatter.get("version")
        or metadata.get("version")
        or meta.get("version")
    )
    description = (
        str(frontmatter.get("description") or "").strip()
        or str(meta.get("description") or "").strip()
        or _first_body_line(body)
    )
    tags = _as_list(frontmatter.get("tags")) + _as_list(meta.get("tags"))
    triggers = _as_list(frontmatter.get("triggers")) + _as_list(meta.get("triggers"))
    if "allowed-tools" in frontmatter:
        tags.append("tools")

    health = "ok"
    active = True
    enabled = _is_truthy(frontmatter.get("enabled"), True) and _is_truthy(meta.get("enabled"), True)
    if disabled:
        enabled = False
    if errors:
        health = "error"
        active = False
    elif not enabled:
        health = "disabled"
        active = False

    summary = description or "（无描述）"
    entry = SkillEntry(
        id=skill_id,
        name=str(frontmatter.get("display_name") or frontmatter.get("name") or meta.get("displayName") or raw_id),
        description=description,
        version=str(version) if version is not None else None,
        source=source,
        path=str(directory),
        enabled=enabled,
        active=active,
        health=health,
        errors=errors,
        summary=summary,
        tags=sorted(set(filter(None, tags))),
        triggers=sorted(set(filter(None, triggers))),
    )
    return _ParsedSkill(entry=entry, content=content, root=directory)


def _extract_refs(text: str) -> list[str]:
    refs: list[str] = []
    for match in _REF_PATTERN.finditer(text):
        ref = (match.group(1) or match.group(2) or "").strip()
        ref = ref.split("#", 1)[0]
        ref = ref.replace("\\", "/")
        if ref.startswith(_VALID_REF_PREFIXES):
            refs.append(ref)
    return refs


class SkillService:
    def __init__(self, workspace: Path | None = None, project_workspace: Path | None = None):
        settings = get_settings()
        self.workspace = Path(workspace or settings.workspace_path)
        self.project_workspace = Path(project_workspace).resolve() if project_workspace else None

    @property
    def config(self) -> dict[str, Any]:
        return load_skill_config(self.workspace)

    def _global_roots(self) -> list[Path]:
        roots: list[Path] = []
        for raw in self.config.get("global_roots", DEFAULT_SKILL_CONFIG["global_roots"]):
            path = Path(str(raw))
            roots.append((path if path.is_absolute() else self.workspace / path).resolve())
        return roots

    def _project_root(self) -> Path | None:
        if not self.config.get("project_enabled", True) or self.project_workspace is None:
            return None
        return (self.project_workspace / ".agents" / "skills").resolve()

    def _cache_key(self) -> tuple[str, str | None]:
        project = str(self._project_root()) if self._project_root() else None
        return (str(self.workspace.resolve()), project)

    def refresh(self) -> SkillRefreshResponse:
        clear_skill_cache()
        skills = self._discover(use_cache=False)
        return SkillRefreshResponse(skills=[item.entry for item in skills], total=len(skills))

    def list(self, source: SkillSource | None = None) -> SkillListResponse:
        entries = [item.entry for item in self._discover()]
        if source:
            entries = [entry for entry in entries if entry.source == source]
        return SkillListResponse(skills=entries, total=len(entries))

    def detail(
        self,
        skill_id: str,
        *,
        source: SkillSource | None = None,
        include_references: bool = False,
    ) -> SkillDetail:
        parsed = self._find(skill_id, source=source)
        entry = parsed.entry
        resources: list[SkillResource] = []
        content = parsed.content if entry.active and entry.health == "ok" else None
        errors = list(entry.errors)
        if entry.health == "disabled":
            errors.append("skill 已禁用")
        if entry.health == "shadowed":
            errors.append("skill 已被同名项目 skill 覆盖")
        if include_references and content and parsed.root:
            resources = self._load_references(parsed.root, content)
        data = entry.model_dump()
        data["errors"] = errors
        return SkillDetail(**data, content=content, resources=resources)

    def read_resource(self, skill_id: str, relative_path: str, *, source: SkillSource | None = None) -> SkillResource:
        parsed = self._find(skill_id, source=source)
        if not parsed.root:
            raise AppError("skill 根目录不可用")
        if not parsed.entry.active or parsed.entry.health != "ok":
            raise AppError("skill 不可加载")
        target = _safe_relative_path(parsed.root, relative_path)
        if not target.is_file():
            raise NotFoundError("skill 资源不存在")
        return SkillResource(
            path=relative_path.replace("\\", "/"),
            content=target.read_text(encoding="utf-8"),
        )

    def match(self, query: str, *, source: SkillSource | None = None, limit: int = 5) -> SkillMatchResponse:
        limit = max(1, min(limit, 20))
        query_norm = query.lower()
        command = self._command_alias(query_norm)
        matches: list[SkillMatch] = []
        for entry in self.list(source=source).skills:
            if not entry.active or entry.health != "ok":
                continue
            score, reason = self._score(entry, query_norm, command)
            if score <= 0:
                continue
            source_bonus = 5 if entry.source == "project" else 0
            matches.append(SkillMatch(skill=entry, score=score + source_bonus, reason=reason))
        matches.sort(key=lambda item: (item.score, item.skill.source == "project", item.skill.id), reverse=True)
        matches = matches[:limit]
        return SkillMatchResponse(matches=matches, total=len(matches))

    def build_catalog_text(self) -> str:
        config = self.config
        max_items = int(config.get("catalog_max_items") or DEFAULT_SKILL_CONFIG["catalog_max_items"])
        lines = [
            "## 技能目录",
            "以下是可渐进加载的 skill 索引；默认仅为摘要，不含完整 SKILL.md 正文。",
            "当用户点名 skill，或任务明显匹配某个 skill 时，先调用 skill_read 读取完整说明，再执行实质任务动作。",
        ]
        entries = [
            entry
            for entry in self.list().skills
            if entry.active and entry.enabled and entry.health == "ok"
        ]
        if not entries:
            lines.append("- （暂无可用 skill）")
        for entry in entries[:max_items]:
            version = f" v{entry.version}" if entry.version else ""
            trigger = f"；触发：{', '.join(entry.triggers[:4])}" if entry.triggers else ""
            lines.append(f"- {entry.id} [{entry.source}]{version}: {entry.summary}{trigger}")
        if len(entries) > max_items:
            lines.append(f"- ...（还有 {len(entries) - max_items} 个 skill 未展示）")
        return "\n".join(lines)

    def _discover(self, *, use_cache: bool = True) -> list[_ParsedSkill]:
        config = self.config
        key = self._cache_key()
        ttl = int(config.get("cache_ttl_seconds") or 0)
        now = time.monotonic()
        if use_cache and ttl > 0 and key in _CACHE:
            created, cached = _CACHE[key]
            if now - created <= ttl:
                return cached

        disabled = {str(item).strip() for item in config.get("disabled", []) if str(item).strip()}
        parsed: list[_ParsedSkill] = []
        for root in self._global_roots():
            parsed.extend(self._scan_root(root, "global", disabled))
        project_root = self._project_root()
        if project_root is not None:
            parsed.extend(self._scan_root(project_root, "project", disabled))
        parsed = self._apply_shadowing(parsed)
        _CACHE[key] = (now, parsed)
        return parsed

    def _scan_root(self, root: Path, source: SkillSource, disabled: set[str]) -> list[_ParsedSkill]:
        if not root.exists() or not root.is_dir():
            return []
        skills: list[_ParsedSkill] = []
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            skills.append(_parse_skill_dir(child, source, disabled))
        return skills

    def _apply_shadowing(self, parsed: list[_ParsedSkill]) -> list[_ParsedSkill]:
        project_by_id = {
            item.entry.id: item
            for item in parsed
            if item.entry.source == "project" and item.entry.health == "ok" and item.entry.enabled
        }
        result: list[_ParsedSkill] = []
        for item in parsed:
            entry = item.entry.model_copy()
            if entry.id in project_by_id and entry.source == "global":
                entry.active = False
                entry.health = "shadowed"
                entry.shadowed_by = f"project:{entry.id}"
            result.append(_ParsedSkill(entry=entry, content=item.content, root=item.root))
        return sorted(
            result,
            key=lambda item: (
                item.entry.id,
                0 if item.entry.source == "project" else 1,
                item.entry.path,
            ),
        )

    def _find(self, skill_id: str, *, source: SkillSource | None = None) -> _ParsedSkill:
        wanted = _slugify(skill_id)
        candidates = [item for item in self._discover() if item.entry.id == wanted]
        if source:
            candidates = [item for item in candidates if item.entry.source == source]
        if not candidates:
            raise NotFoundError("skill 不存在")
        active = [item for item in candidates if item.entry.active]
        return active[0] if active else candidates[0]

    def _load_references(self, root: Path, content: str) -> list[SkillResource]:
        resources: list[SkillResource] = []
        visited: set[str] = set()
        stack: list[str] = []

        def visit(relative: str) -> None:
            rel = relative.replace("\\", "/")
            if rel in stack:
                resources.append(SkillResource(path=rel, error="skill 资源循环引用"))
                return
            if rel in visited:
                return
            visited.add(rel)
            stack.append(rel)
            try:
                target = _safe_relative_path(root, rel)
                if not target.is_file():
                    resources.append(SkillResource(path=rel, error="skill 资源不存在"))
                    return
                text = target.read_text(encoding="utf-8")
                resources.append(SkillResource(path=rel, content=text))
                for nested in _extract_refs(text):
                    visit(nested)
            except (OSError, UnicodeDecodeError, AppError) as exc:
                resources.append(SkillResource(path=rel, error=str(exc)))
            finally:
                stack.pop()

        for ref in _extract_refs(content):
            visit(ref)
        return resources

    def _command_alias(self, query_norm: str) -> str | None:
        match = re.search(r"/([a-z0-9_-]+)", query_norm)
        if not match:
            return None
        command = match.group(1).replace("_", "-")
        if command.startswith("opsx-"):
            return "openspec-" + command.removeprefix("opsx-")
        return command

    def _score(self, entry: SkillEntry, query_norm: str, command: str | None) -> tuple[int, str]:
        names = {entry.id.lower(), entry.name.lower()}
        if any(name and re.search(rf"(?<![a-z0-9-]){re.escape(name)}(?![a-z0-9-])", query_norm) for name in names):
            return 100, "explicit"
        if command and (command == entry.id or command in entry.triggers or command in entry.id):
            return 90, "command"
        for trigger in entry.triggers:
            trig = trigger.lower()
            if trig and trig in query_norm:
                return 80, "trigger"
        for tag in entry.tags:
            tag_norm = tag.lower()
            if tag_norm and tag_norm in query_norm:
                return 60, "tag"
        haystack = " ".join([entry.id, entry.name, entry.description, entry.summary]).lower()
        query_terms = {term for term in re.findall(r"[\w\u4e00-\u9fff]{2,}", query_norm) if len(term) > 1}
        if query_terms:
            overlap = [term for term in query_terms if term in haystack]
            if overlap:
                return min(50, 20 + len(overlap) * 5), "description"
        return 0, ""

    def _write_skill_config(self, config: dict[str, Any]) -> None:
        from app.config.persistence import atomic_write_json
        from app.storage.workspace import config_file_path

        path = config_file_path()
        full = _load_workspace_config(self.workspace)
        full["skills"] = config
        atomic_write_json(path, full)
        clear_skill_cache()

    def set_enabled(self, skill_id: str, enabled: bool, *, source: SkillSource | None = None) -> SkillToggleResponse:
        before = self._find(skill_id, source=source)
        key = f"{before.entry.source}:{before.entry.id}"
        config = self.config
        disabled = {str(item).strip() for item in config.get("disabled", []) if str(item).strip()}
        if enabled:
            disabled.discard(key)
            disabled.discard(before.entry.id)
        else:
            disabled.add(key)
        config["disabled"] = sorted(disabled)
        self._write_skill_config(config)
        after = self._find(skill_id, source=before.entry.source)
        return SkillToggleResponse(skill=after.entry)
