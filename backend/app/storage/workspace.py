import json
import re
import shutil
import time
import uuid
from pathlib import Path

from app.core.exceptions import NotFoundError

BOOTSTRAPS_DIR = "bootstraps"
ARTIFACTS_DIR = "artifacts"
CONFIG_FILE = "CONFIG.json"

_SECTION_JSON_FILES = ("memory_sections.json", "user_sections.json")

BOOTSTRAP_NAMES = [
    "IDENTITY",
    "USER",
    "SOUL",
    "MEMORY",
    "AGENTS",
    "HEARTBEAT",
    "BOOTSTRAP",
]

CONFIG_NAMES = ["CONFIG", *BOOTSTRAP_NAMES]

ARTIFACT_EXTENSIONS = {
    ".html",
    ".htm",
    ".css",
    ".js",
    ".mjs",
    ".json",
    ".csv",
    ".svg",
    ".vue",
    ".tsx",
    ".ts",
    ".jsx",
    ".xml",
    ".yaml",
    ".yml",
}

RESERVED_TOP_LEVEL = {
    BOOTSTRAPS_DIR,
    ARTIFACTS_DIR,
    "memory",
    "sessions",
    "tool_results",
    "skills",
    CONFIG_FILE,
}


def bootstraps_dir(workspace: Path) -> Path:
    return workspace / BOOTSTRAPS_DIR


def artifacts_dir(workspace: Path) -> Path:
    return workspace / ARTIFACTS_DIR


def longterm_memory_path(workspace: Path) -> Path:
    return bootstraps_dir(workspace) / "MEMORY.md"


def config_file_path(workspace: Path) -> Path:
    return workspace / CONFIG_FILE


def _config_filename(name: str) -> str:
    if name == "CONFIG":
        return CONFIG_FILE
    return f"{name}.md"


def _resolve_safe_path(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise ValueError("路径越界")
    return target


def normalize_workspace_path(relative: str, *, for_write: bool = False) -> str:
    """规范化相对路径；写入时裸文件名默认落入 artifacts/。"""
    rel = relative.strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if rel in ("", "."):
        if for_write:
            raise ValueError("路径不能为空")
        return ""
    if not rel:
        raise ValueError("路径不能为空")

    first = rel.split("/", 1)[0]
    if first in RESERVED_TOP_LEVEL or first == CONFIG_FILE:
        return rel

    if for_write and "/" not in rel:
        suffix = Path(rel).suffix.lower()
        if suffix in ARTIFACT_EXTENSIONS:
            return f"{ARTIFACTS_DIR}/{rel}"
    return rel


def migrate_workspace_layout(workspace: Path, defaults: Path) -> None:
    """将旧版工作区根目录布局迁移到 bootstraps/ 与 artifacts/。"""
    bootstraps_dir(workspace).mkdir(parents=True, exist_ok=True)
    artifacts_dir(workspace).mkdir(parents=True, exist_ok=True)

    for name in BOOTSTRAP_NAMES:
        legacy = workspace / _config_filename(name)
        target = bootstraps_dir(workspace) / _config_filename(name)
        if legacy.exists() and legacy.is_file():
            if not target.exists():
                shutil.move(str(legacy), str(target))
            else:
                legacy.unlink()

    artifact_patterns = ("*.html", "*.htm", "*.css", "*.js", "*.json", "*.csv", "*.txt", "*.md")
    skip_names = {CONFIG_FILE, *(_config_filename(n) for n in BOOTSTRAP_NAMES)}
    for pattern in artifact_patterns:
        for path in workspace.glob(pattern):
            if not path.is_file() or path.name in skip_names:
                continue
            target = artifacts_dir(workspace) / path.name
            if target.exists():
                continue
            shutil.move(str(path), str(target))


class SessionStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = workspace / "sessions"
        self.index_path = self.sessions_dir / "index.json"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self.index_path.write_text("{}", encoding="utf-8")

    def _load(self) -> dict:
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _save(self, data: dict) -> None:
        self.index_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(self) -> list[dict]:
        data = self._load()
        sessions = [
            {"id": sid, **meta}
            for sid, meta in data.items()
        ]
        sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return sessions

    def create(self) -> str:
        sid = str(uuid.uuid4())
        now = int(time.time())
        data = self._load()
        data[sid] = {"created_at": now, "updated_at": now}
        self._save(data)
        return sid

    def get(self, session_id: str) -> dict:
        data = self._load()
        if session_id not in data:
            raise NotFoundError("会话不存在")
        return {"id": session_id, **data[session_id]}

    def touch(self, session_id: str) -> None:
        data = self._load()
        if session_id in data:
            data[session_id]["updated_at"] = int(time.time())
            self._save(data)

    def delete(self, session_id: str) -> None:
        data = self._load()
        if session_id not in data:
            raise NotFoundError("会话不存在")
        del data[session_id]
        self._save(data)

    def clear_all(self) -> None:
        self._save({})


class ConfigStore:
    def __init__(self, workspace: Path, defaults: Path):
        self.workspace = workspace
        self.defaults = defaults

    def list_configs(self) -> list[str]:
        return [n for n in CONFIG_NAMES if self.get_path(n).exists()]

    def get_path(self, name: str) -> Path:
        if name not in CONFIG_NAMES:
            raise NotFoundError(f"未知配置: {name}")
        if name == "CONFIG":
            return config_file_path(self.workspace)
        return bootstraps_dir(self.workspace) / _config_filename(name)

    def read(self, name: str) -> str:
        path = self.get_path(name)
        if not path.exists():
            raise NotFoundError(f"配置不存在: {name}")
        return path.read_text(encoding="utf-8")

    def write(self, name: str, content: str) -> None:
        if name == "CONFIG":
            json.loads(content)
        path = self.get_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def get_agent_name(self) -> str:
        try:
            content = self.read("IDENTITY")
        except NotFoundError:
            return "麦林"
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("名称：") or line.startswith("名称:"):
                return line.split("：", 1)[-1].split(":", 1)[-1].strip()
            if line.startswith("# "):
                name = line[2:].strip()
                if name:
                    return name
        return "麦林"

    def reset_global(self) -> None:
        bootstraps_dir(self.workspace).mkdir(parents=True, exist_ok=True)
        defaults_bootstraps = self.defaults / BOOTSTRAPS_DIR
        for name in BOOTSTRAP_NAMES:
            src = defaults_bootstraps / _config_filename(name)
            if not src.exists():
                src = self.defaults / _config_filename(name)
            if src.exists():
                shutil.copy2(src, bootstraps_dir(self.workspace) / _config_filename(name))
        config_src = self.defaults / CONFIG_FILE
        if config_src.exists():
            shutil.copy2(config_src, config_file_path(self.workspace))


class MemoryStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory_dir = workspace / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def list_entries(self) -> list[dict]:
        entries = []
        for path in sorted(self.memory_dir.glob("*.md"), reverse=True):
            content = path.read_text(encoding="utf-8")
            date = path.stem
            preview = content.strip().replace("\n", " ")[:120]
            entries.append(
                {
                    "date": date,
                    "filename": path.name,
                    "content": content,
                    "preview": preview,
                }
            )
        return entries

    def get(self, filename: str) -> dict:
        if not re.match(r"^\d{4}-\d{2}-\d{2}\.md$", filename):
            raise NotFoundError("无效的记忆文件名")
        path = _resolve_safe_path(self.memory_dir, filename)
        if not path.exists():
            raise NotFoundError("记忆文件不存在")
        return {
            "filename": filename,
            "date": path.stem,
            "content": path.read_text(encoding="utf-8"),
        }

    def clear_daily(self) -> None:
        for path in self.memory_dir.glob("*.md"):
            path.unlink()

    def reset_longterm(self) -> None:
        path = longterm_memory_path(self.workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# 长期记忆\n\n（助手可在此记录跨会话的重要信息。）\n",
            encoding="utf-8",
        )

    def append_daily(self, content: str, source: str = "agent") -> str:
        from datetime import date

        today = date.today().isoformat()
        path = self.memory_dir / f"{today}.md"
        header = f"# {today}\n\n"
        existing = path.read_text(encoding="utf-8") if path.exists() else header
        path.write_text(existing.rstrip() + "\n\n" + content.strip() + "\n", encoding="utf-8")
        return today


class ArtifactStore:
    """Agent 生成产物目录（HTML、报告、导出文件等）。"""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.root = artifacts_dir(workspace)
        self.root.mkdir(parents=True, exist_ok=True)

    def list_entries(self) -> list[dict]:
        entries: list[dict] = []
        if not self.root.exists():
            return entries
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            rel = str(path.relative_to(self.workspace)).replace("\\", "/")
            entries.append(
                {
                    "path": rel,
                    "name": path.name,
                    "size": path.stat().st_size,
                    "modified_at": int(path.stat().st_mtime),
                }
            )
        return entries
