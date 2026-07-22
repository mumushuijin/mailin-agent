"""热层分节 JSON 存储 + 组装 MEMORY.md / USER.md。

JSON 为唯一数据源；MD 为组装视图。每次成功落盘后写入 last-good 快照，
启动时校验并在损坏/漂移时自动恢复。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.context.budget import load_context_config
from app.context.memory.clauses import (
    DEFAULT_TITLE,
    USER_TITLE,
    Clause,
    parse_clauses,
    render_sections,
)
from app.context.memory.registry import (
    SECTION_JSON_FILES,
    TargetFile,
    key_from_display,
    prefix_to_section_key,
    section_keys,
)
from app.core.settings import get_settings

logger = logging.getLogger(__name__)

_LOCK_SUFFIX = ".lock"
_SNAPSHOT_ROOT = ".hot_memory_snapshots"
_SNAPSHOT_JSON = "sections.json"
_SNAPSHOT_MD = "view.md"


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + _LOCK_SUFFIX)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()


class HotMemoryStore:
    """单 target 的分节存储：JSON 为源，Markdown 为组装视图。"""

    def __init__(self, workspace: Path, target: TargetFile):
        self.workspace = workspace
        self.target = target
        self.bootstraps = workspace / "bootstraps"
        self.bootstraps.mkdir(parents=True, exist_ok=True)
        self.json_path = self.bootstraps / SECTION_JSON_FILES[target]
        self.md_path = self.bootstraps / ("USER.md" if target == "user" else "MEMORY.md")
        self._sections: dict[str, list[Clause]] | None = None
        self._dirty: set[str] = set()

    @property
    def _snapshot_dir(self) -> Path:
        return self.bootstraps / _SNAPSHOT_ROOT / self.target

    def _defaults_bootstraps(self) -> Path:
        return get_settings().workspace_defaults_path / "bootstraps"

    def _hot_cfg(self) -> dict:
        return load_context_config(self.workspace).get("memory", {}).get("hot", {})

    def _empty_sections(self) -> dict[str, list[Clause]]:
        return {k: [] for k in section_keys(self.target)}

    def _render_sections(self, sections: dict[str, list[Clause]]) -> str:
        cfg = self._hot_cfg()
        placeholder = cfg.get("empty_section_placeholder", "（空）")
        title = USER_TITLE if self.target == "user" else DEFAULT_TITLE
        return render_sections(
            sections,
            target=self.target,
            title=title,
            empty_placeholder=placeholder,
        )

    def _expected_md(self) -> str:
        return self._render_sections(self.load())

    def _parse_json_payload(self, raw: object) -> dict[str, list[Clause]] | None:
        if not isinstance(raw, dict):
            return None
        sections = self._empty_sections()
        for key in section_keys(self.target):
            items = raw.get(key, [])
            if not isinstance(items, list):
                return None
            for item in items:
                if not isinstance(item, dict):
                    continue
                cid = (item.get("id") or "").strip()
                text = (item.get("text") or "").strip()
                if cid and text:
                    sections[key].append(Clause(cid, key, text))
        return sections

    def _try_parse_json_file(self, path: Path) -> dict[str, list[Clause]] | None:
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning("解析 %s 失败: %s", path.name, exc)
            return None
        return self._parse_json_payload(raw)

    def _seed_json_from_defaults(self, *, force: bool = False) -> bool:
        if self.json_path.exists() and not force:
            return False
        src = self._defaults_bootstraps() / SECTION_JSON_FILES[self.target]
        if not src.exists():
            self._persist_sections(self._empty_sections())
            return True
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, self.json_path)
        return True

    def _migrate_from_md(self) -> dict[str, list[Clause]]:
        sections = self._empty_sections()
        if not self.md_path.exists():
            return sections
        title = USER_TITLE if self.target == "user" else DEFAULT_TITLE
        try:
            raw_md = self.md_path.read_text(encoding="utf-8")
        except OSError:
            return sections
        for clause in parse_clauses(raw_md, default_title=title, target=self.target):
            key = key_from_display(self.target, clause.section)
            if not key:
                key = prefix_to_section_key(self.target, clause.clause_id) or "misc"
            if key not in sections:
                key = "misc"
            sections[key].append(Clause(clause.clause_id, key, clause.text))
        return sections

    def _read_json_or_recover(self) -> dict[str, list[Clause]]:
        parsed = self._try_parse_json_file(self.json_path)
        if parsed is not None:
            return parsed

        logger.warning("%s 损坏或不可读，尝试恢复快照", self.json_path.name)
        if self._restore_snapshot():
            parsed = self._try_parse_json_file(self.json_path)
            if parsed is not None:
                return parsed

        logger.warning("%s 快照恢复失败，回退到 workspace_defaults", self.json_path.name)
        self._seed_json_from_defaults(force=True)
        parsed = self._try_parse_json_file(self.json_path)
        return parsed if parsed is not None else self._empty_sections()

    def _save_snapshot(self) -> None:
        """落盘 last-good：JSON + 由 JSON 组装的 MD。"""
        sections = self.load()
        snap = self._snapshot_dir
        snap.mkdir(parents=True, exist_ok=True)
        payload = {
            key: [{"id": c.clause_id, "text": c.text} for c in sections.get(key, [])]
            for key in section_keys(self.target)
        }
        rendered = self._render_sections(sections)
        self._atomic_write(snap / _SNAPSHOT_JSON, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        self._atomic_write(snap / _SNAPSHOT_MD, rendered)

    def _restore_snapshot(self) -> bool:
        snap_json = self._snapshot_dir / _SNAPSHOT_JSON
        if not snap_json.exists():
            return False
        try:
            shutil.copy2(snap_json, self.json_path)
            self._sections = None
            parsed = self._try_parse_json_file(self.json_path)
            if parsed is None:
                return False
            self._sections = parsed
            self.assemble_md()
            logger.info("已从快照恢复 %s", self.json_path.name)
            return True
        except OSError as exc:
            logger.warning("恢复快照失败: %s", exc)
            return False

    def _sync_md_from_json_unlocked(self) -> bool:
        """确保 MD 与当前 JSON 内存视图一致（调用方需已持锁）。"""
        expected = self._render_sections(self.load())
        if not self.md_path.exists():
            self.assemble_md()
            return True
        try:
            current = self.md_path.read_text(encoding="utf-8").strip()
        except OSError:
            self.assemble_md()
            return True
        if current != expected.strip():
            self.assemble_md()
            return True
        return False

    def _sync_md_from_json(self) -> bool:
        with _file_lock(self.json_path):
            return self._sync_md_from_json_unlocked()

    def initialize(self) -> list[str]:
        """启动/首次加载：播种、恢复、迁移、对齐 MD、写快照。"""
        logs: list[str] = []
        with _file_lock(self.json_path):
            if not self.json_path.exists():
                if self._seed_json_from_defaults():
                    logs.append(f"{self.target}:seed_defaults")

            self._sections = None
            sections = self._read_json_or_recover()
            self._sections = sections

            if not any(sections.values()) and self.md_path.exists():
                migrated = self._migrate_from_md()
                if any(migrated.values()):
                    self._sections = migrated
                    self._persist_sections(migrated)
                    logs.append(f"{self.target}:migrated_from_md")

            if not self.json_path.exists():
                self._persist_sections(self._sections or self._empty_sections())

            if self._sync_md_from_json_unlocked():
                logs.append(f"{self.target}:md_synced")

            self._save_snapshot()
            logs.append(f"{self.target}:snapshot_saved")
        return logs

    def load(self) -> dict[str, list[Clause]]:
        if self._sections is not None:
            return self._sections
        self._sections = self._read_json_or_recover()
        return self._sections

    def _persist_sections(self, sections: dict[str, list[Clause]]) -> None:
        payload = {
            key: [{"id": c.clause_id, "text": c.text} for c in sections.get(key, [])]
            for key in section_keys(self.target)
        }
        self._atomic_write(self.json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def _persist_json(self) -> None:
        self._persist_sections(self._sections or self._empty_sections())

    def assemble_md(self) -> str:
        rendered = self._render_sections(self.load())
        self._atomic_write(self.md_path, rendered)
        return rendered

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".mem_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def backup(self, path: Path) -> str:
        ts = int(time.time())
        bak = path.with_suffix(path.suffix + f".bak.{ts}")
        if path.exists():
            shutil.copy2(path, bak)
        return str(bak)

    def get_section(self, section_key: str) -> list[Clause]:
        return list(self.load().get(section_key, []))

    def set_section(self, section_key: str, clauses: list[Clause]) -> None:
        sections = self.load()
        sections[section_key] = list(clauses)
        self._dirty.add(section_key)

    def all_clauses(self) -> list[Clause]:
        out: list[Clause] = []
        for key in section_keys(self.target):
            out.extend(self.load().get(key, []))
        return out

    def build_index(self, preview_len: int = 80) -> list[dict]:
        index: list[dict] = []
        for key in section_keys(self.target):
            for c in self.load().get(key, []):
                text = c.text.replace("\n", " ")
                index.append(
                    {
                        "id": c.clause_id,
                        "section": key,
                        "text_preview": text[:preview_len] + ("…" if len(text) > preview_len else ""),
                    }
                )
        return index

    def find_clause(self, clause_id: str) -> Clause | None:
        key = clause_id.strip().lower()
        for c in self.all_clauses():
            if c.clause_id.lower() == key:
                return c
        return None

    def char_count(self) -> int:
        return len(self._expected_md())

    def file_char_limit(self) -> int:
        cfg = self._hot_cfg()
        if self.target == "user":
            return int(cfg.get("user_char_limit", 1500))
        return int(cfg.get("memory_char_limit", 3000))

    def section_char_limit(self) -> int:
        return int(self._hot_cfg().get("section_char_limit", 800))

    def repair_stale_md(self) -> bool:
        """运行期：将陈旧 MD 对齐到 JSON（不修改 JSON）。"""
        return self._sync_md_from_json()

    def commit(self) -> list[str]:
        """写回脏 Section JSON，组装 MD，更新 last-good 快照。"""
        if not self._dirty and self.json_path.exists() and self.md_path.exists():
            if self._sync_md_from_json():
                self._save_snapshot()
                return [f"{self.target}:md_repair"]
            return []

        with _file_lock(self.json_path):
            if self._dirty or not self.json_path.exists():
                self._persist_json()
            rendered = self.assemble_md()
            if len(rendered) > self.file_char_limit():
                raise ValueError(
                    f"{self.md_path.name} 超出字符上限 {len(rendered)}/{self.file_char_limit()}"
                )
            self._save_snapshot()
        logs = [f"{self.target}:{k}" for k in sorted(self._dirty)]
        self._dirty.clear()
        return logs


def hot_stores(workspace: Path) -> dict[TargetFile, HotMemoryStore]:
    return {
        "memory": HotMemoryStore(workspace, "memory"),
        "user": HotMemoryStore(workspace, "user"),
    }


def ensure_hot_layer_initialized(workspace: Path) -> None:
    """确保 JSON/MD 成对一致，并写入可恢复快照。"""
    for store in hot_stores(workspace).values():
        store.initialize()
