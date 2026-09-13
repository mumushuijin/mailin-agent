import json
import re
import shutil
import time
import uuid
from pathlib import Path

from app.core.exceptions import AppError, NotFoundError
from app.schemas.session import (
    PLACEHOLDER_TITLE,
    TITLE_SOURCE_AUTO,
    TITLE_SOURCE_PLACEHOLDER,
    TITLE_SOURCE_USER,
)

TITLE_MAX_LEN = 16
_TITLE_PREFIXES = (
    "请你帮我",
    "请帮我",
    "麻烦你",
    "麻烦",
    "请问一下",
    "请问",
    "帮我",
    "我想",
)


def derive_session_title(message: str) -> str:
    text = (message or "").replace("\r\n", "\n").replace("\r", "\n")
    first_line = next((line.strip() for line in text.split("\n") if line.strip()), "")
    if not first_line:
        return PLACEHOLDER_TITLE
    for prefix in _TITLE_PREFIXES:
        if first_line.startswith(prefix):
            first_line = first_line[len(prefix) :].lstrip(" ，,：:、")
            break
    first_line = first_line.strip()
    if not first_line:
        return PLACEHOLDER_TITLE
    if len(first_line) > TITLE_MAX_LEN:
        return first_line[:TITLE_MAX_LEN].rstrip()
    return first_line

BOOTSTRAPS_DIR = "bootstraps"
MAILIN_DIR = ".mailin"
ARTIFACTS_DIR = f"{MAILIN_DIR}/artifacts"
CONFIG_FILE = "CONFIG.json"

_SECTION_JSON_FILES = ("memory_sections.json", "user_sections.json")

BOOTSTRAP_NAMES = [
    "USER",
    "SOUL",
    "MEMORY",
    "HEARTBEAT",
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
    MAILIN_DIR,
}


def bootstraps_dir(workspace: Path) -> Path:
    return workspace / BOOTSTRAPS_DIR


def artifacts_dir(workspace: Path) -> Path:
    return workspace / MAILIN_DIR / "artifacts"


def mailin_dir(project: Path) -> Path:
    return project / MAILIN_DIR


def project_tool_results_dir(project: Path, session_id: str) -> Path:
    return mailin_dir(project) / "tool_results" / session_id


def _is_same_or_child(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    if resolved == base:
        return True
    try:
        resolved.relative_to(base)
        return True
    except ValueError:
        return False


def validate_project_workspace(raw: str | Path | None) -> Path:
    """校验并返回绝对项目工作区路径。失败抛 AppError。"""
    if raw is None or str(raw).strip() == "":
        raise AppError("请先选择项目文件夹")
    path = Path(str(raw).strip()).expanduser()
    try:
        path = path.resolve()
    except OSError as exc:
        raise AppError("无效的工作区路径") from exc
    if not path.exists():
        raise AppError("工作区路径不存在")
    if not path.is_dir():
        raise AppError("工作区路径必须是已存在的文件夹")

    from app.core.settings import get_settings

    settings = get_settings()
    agent_home = Path(settings.workspace_path)
    defaults = Path(settings.workspace_defaults_path)
    if _is_same_or_child(path, agent_home):
        raise AppError("不能将 Agent 自有空间作为项目工作区")
    if _is_same_or_child(path, defaults):
        raise AppError("不能将模板目录作为项目工作区")
    return path


def seed_project_agents(project: Path) -> None:
    """项目根没有 AGENTS.md 时，从模板复制一份种子。"""
    target = project / "AGENTS.md"
    if target.exists():
        return
    from app.core.settings import get_settings

    source = get_settings().workspace_defaults_path / "project" / "AGENTS.md"
    if source.exists():
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def assert_not_agent_space(path: Path) -> None:
    """文件/shell 解析后的路径不得落入 agent home 或模板。"""
    from app.core.settings import get_settings

    settings = get_settings()
    home = getattr(settings, "workspace_path", None)
    defaults = getattr(settings, "workspace_defaults_path", None)
    if home and _is_same_or_child(path, Path(home)):
        raise ValueError("路径越界")
    if defaults and _is_same_or_child(path, Path(defaults)):
        raise ValueError("路径越界")


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
    """将旧版工作区根目录布局迁移到 bootstraps/。不再在 agent home 创建 artifacts。"""
    bootstraps_dir(workspace).mkdir(parents=True, exist_ok=True)

    for name in BOOTSTRAP_NAMES:
        legacy = workspace / _config_filename(name)
        target = bootstraps_dir(workspace) / _config_filename(name)
        if legacy.exists() and legacy.is_file():
            if not target.exists():
                shutil.move(str(legacy), str(target))
            else:
                legacy.unlink()


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

    def _hydrate(self, session_id: str, meta: dict) -> dict:
        record = {"id": session_id, **meta}
        title = str(record.get("title") or "").strip()
        record["title"] = title or PLACEHOLDER_TITLE
        source = str(record.get("title_source") or "").strip()
        if source not in {TITLE_SOURCE_PLACEHOLDER, TITLE_SOURCE_AUTO, TITLE_SOURCE_USER}:
            if record["title"] == PLACEHOLDER_TITLE:
                source = TITLE_SOURCE_PLACEHOLDER
            else:
                source = TITLE_SOURCE_AUTO
        record["title_source"] = source
        return record

    def list(self) -> list[dict]:
        data = self._load()
        sessions = [self._hydrate(sid, meta) for sid, meta in data.items()]
        sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return sessions

    def create(self, workspace_path: str) -> str:
        project = validate_project_workspace(workspace_path)
        seed_project_agents(project)
        sid = str(uuid.uuid4())
        now = int(time.time())
        data = self._load()
        data[sid] = {
            "created_at": now,
            "updated_at": now,
            "workspace_path": str(project),
            "title": PLACEHOLDER_TITLE,
            "title_source": TITLE_SOURCE_PLACEHOLDER,
        }
        self._save(data)
        return sid

    def get(self, session_id: str) -> dict:
        data = self._load()
        if session_id not in data:
            raise NotFoundError("会话不存在")
        return self._hydrate(session_id, data[session_id])

    def rebind(self, session_id: str, workspace_path: str) -> dict:
        data = self._load()
        if session_id not in data:
            raise NotFoundError("会话不存在")
        project = validate_project_workspace(workspace_path)
        seed_project_agents(project)
        data[session_id]["workspace_path"] = str(project)
        data[session_id]["updated_at"] = int(time.time())
        self._save(data)
        return self._hydrate(session_id, data[session_id])

    def rename(self, session_id: str, title: str) -> dict:
        cleaned = (title or "").strip()
        if not cleaned:
            raise AppError("标题不能为空")
        data = self._load()
        if session_id not in data:
            raise NotFoundError("会话不存在")
        data[session_id]["title"] = cleaned
        data[session_id]["title_source"] = TITLE_SOURCE_USER
        data[session_id]["updated_at"] = int(time.time())
        self._save(data)
        return self._hydrate(session_id, data[session_id])

    def maybe_set_title_from_message(self, session_id: str, message: str) -> dict | None:
        data = self._load()
        if session_id not in data:
            raise NotFoundError("会话不存在")
        hydrated = self._hydrate(session_id, data[session_id])
        source = hydrated["title_source"]
        current_title = hydrated["title"]
        if source == TITLE_SOURCE_USER:
            return hydrated
        if source == TITLE_SOURCE_AUTO:
            if len(current_title) <= TITLE_MAX_LEN:
                return hydrated
            compressed = derive_session_title(current_title)
            if compressed != PLACEHOLDER_TITLE:
                data[session_id]["title"] = compressed
                data[session_id]["title_source"] = TITLE_SOURCE_AUTO
                self._save(data)
            return self._hydrate(session_id, data[session_id])
        derived = derive_session_title(message)
        if derived == PLACEHOLDER_TITLE:
            return hydrated
        data[session_id]["title"] = derived
        data[session_id]["title_source"] = TITLE_SOURCE_AUTO
        self._save(data)
        return self._hydrate(session_id, data[session_id])

    def apply_auto_title(self, session_id: str, title: str) -> dict | None:
        cleaned = derive_session_title(title)
        if cleaned == PLACEHOLDER_TITLE:
            return self.get(session_id)
        data = self._load()
        if session_id not in data:
            raise NotFoundError("会话不存在")
        hydrated = self._hydrate(session_id, data[session_id])
        if hydrated["title_source"] != TITLE_SOURCE_AUTO:
            return hydrated
        data[session_id]["title"] = cleaned
        data[session_id]["title_source"] = TITLE_SOURCE_AUTO
        self._save(data)
        return self._hydrate(session_id, data[session_id])

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
            content = self.read("SOUL")
        except NotFoundError:
            return "麦林"
        for line in content.splitlines():
            line = line.strip()
            if "麦林" in line or "Mailin" in line:
                return "麦林"
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
