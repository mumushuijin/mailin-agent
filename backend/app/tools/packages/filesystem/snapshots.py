"""项目 workspace sidecar 下的文件变更快照存储与回滚。"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.storage.workspace import MAILIN_DIR, _is_same_or_child
from app.tools.policy.sandbox import SNAPSHOTS_DIR_NAME, snapshots_sidecar
from app.tools.runtime import get_project_workspace, tool_session_id

logger = logging.getLogger(__name__)

META_FILENAME = "meta.json"
BLOB_FILENAME = "content.bin"
OpKind = Literal["write", "replace", "move", "delete", "create"]


@dataclass
class SnapshotMeta:
    snapshot_id: str
    session_id: str
    tool_call_id: str
    operation: OpKind
    target_rel: str
    source_rel: str | None = None
    destination_rel: str | None = None
    pre_fingerprint: str | None = None
    post_fingerprint: str | None = None
    existed_before: bool = False
    created_at: float = field(default_factory=time.time)
    blob_bytes: int = 0
    target_mtime: float | None = None
    target_mode: int | None = None
    retention_protect: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SnapshotMeta:
        return cls(
            snapshot_id=str(data["snapshot_id"]),
            session_id=str(data.get("session_id") or ""),
            tool_call_id=str(data.get("tool_call_id") or ""),
            operation=data.get("operation") or "write",  # type: ignore[arg-type]
            target_rel=str(data.get("target_rel") or ""),
            source_rel=data.get("source_rel"),
            destination_rel=data.get("destination_rel"),
            pre_fingerprint=data.get("pre_fingerprint"),
            post_fingerprint=data.get("post_fingerprint"),
            existed_before=bool(data.get("existed_before")),
            created_at=float(data.get("created_at") or time.time()),
            blob_bytes=int(data.get("blob_bytes") or 0),
            target_mtime=data.get("target_mtime"),
            target_mode=data.get("target_mode"),
            retention_protect=bool(data.get("retention_protect")),
        )


@dataclass(frozen=True)
class SnapshotRetention:
    max_count: int = 50
    max_age_seconds: float = 7 * 24 * 3600
    max_total_bytes: int = 200 * 1024 * 1024
    enabled: bool = True


def fingerprint_file(path: Path | None) -> str | None:
    """内容 + 大小指纹；文件不存在返回 None。"""
    if path is None or not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 64)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    h.update(b"|")
    h.update(str(size).encode("ascii"))
    return h.hexdigest()


def is_snapshot_sidecar_path(path: Path, workspace: Path | None = None) -> bool:
    root = workspace or get_project_workspace()
    if root is None:
        return False
    return _is_same_or_child(path, snapshots_sidecar(root))


def load_snapshot_config(config: dict | None = None) -> SnapshotRetention:
    try:
        if config is None:
            from app.tools.registry import load_full_config

            config = load_full_config()
        tools = (config or {}).get("tools") if isinstance(config, dict) else None
        fs = tools.get("filesystem") if isinstance(tools, dict) else None
        if fs is False or fs is None:
            return SnapshotRetention()
        if fs is True:
            return SnapshotRetention()
        if not isinstance(fs, dict):
            return SnapshotRetention()
        snaps = fs.get("snapshots") if isinstance(fs.get("snapshots"), dict) else {}
        enabled = snaps.get("enabled", True) not in (False, "false", "0", "off")
        retention = snaps.get("retention") if isinstance(snaps.get("retention"), dict) else {}
        return SnapshotRetention(
            enabled=enabled,
            max_count=int(retention.get("max_count", 50)),
            max_age_seconds=float(retention.get("max_age_seconds", 7 * 24 * 3600)),
            max_total_bytes=int(retention.get("max_total_bytes", 200 * 1024 * 1024)),
        )
    except Exception:
        return SnapshotRetention()


class SnapshotStore:
    def __init__(self, workspace: Path | None = None):
        self.workspace = workspace or get_project_workspace()
        if self.workspace is None:
            raise ValueError("未绑定项目工作区，无法创建快照")
        self.root = snapshots_sidecar(self.workspace)
        # 确保不写入 agent home：sidecar 必须在 workspace 下
        if not _is_same_or_child(self.root, self.workspace):
            raise ValueError("snapshot sidecar 必须位于项目工作区内")

    def _dir(self, snapshot_id: str) -> Path:
        return self.root / snapshot_id

    def create_opaque_id(self) -> str:
        return uuid.uuid4().hex

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        gitkeep = self.root / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")

    def save_file_blob(self, snapshot_id: str, source: Path) -> int:
        dest_dir = self._dir(snapshot_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        blob = dest_dir / BLOB_FILENAME
        shutil.copy2(source, blob)
        return blob.stat().st_size

    def write_meta(self, meta: SnapshotMeta) -> Path:
        dest_dir = self._dir(meta.snapshot_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / META_FILENAME
        path.write_text(json.dumps(meta.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_meta(self, snapshot_id: str) -> SnapshotMeta | None:
        path = self._dir(snapshot_id) / META_FILENAME
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return SnapshotMeta.from_dict(data)

    def blob_path(self, snapshot_id: str) -> Path:
        return self._dir(snapshot_id) / BLOB_FILENAME

    def create_pre_change_snapshot(
        self,
        *,
        operation: OpKind,
        target: Path,
        target_rel: str,
        session_id: str | None,
        tool_call_id: str = "",
        source: Path | None = None,
        source_rel: str | None = None,
        destination_rel: str | None = None,
    ) -> SnapshotMeta:
        """真正写入前创建快照；失败抛错，调用方不得继续 mutation。"""
        cfg = load_snapshot_config()
        if not cfg.enabled:
            # 仍返回占位 meta，但不落盘内容——调用方按 enabled 决定是否要求 snapshot
            sid = self.create_opaque_id()
            return SnapshotMeta(
                snapshot_id=sid,
                session_id=session_id or "",
                tool_call_id=tool_call_id,
                operation=operation,
                target_rel=target_rel,
                source_rel=source_rel,
                destination_rel=destination_rel,
                pre_fingerprint=fingerprint_file(target if target.exists() else None),
                existed_before=target.exists(),
            )

        self.ensure_root()
        sid = self.create_opaque_id()
        existed = target.exists() and target.is_file()
        pre_fp = fingerprint_file(target) if existed else None
        blob_bytes = 0
        mtime = None
        mode = None
        capture = source if source is not None else (target if existed else None)
        if capture is not None and capture.exists() and capture.is_file():
            blob_bytes = self.save_file_blob(sid, capture)
            st = capture.stat()
            mtime = st.st_mtime
            mode = getattr(st, "st_mode", None)
        elif operation == "delete" and existed:
            blob_bytes = self.save_file_blob(sid, target)
            st = target.stat()
            mtime = st.st_mtime
            mode = getattr(st, "st_mode", None)
        elif operation in {"write", "replace", "create"} and existed:
            blob_bytes = self.save_file_blob(sid, target)
            st = target.stat()
            mtime = st.st_mtime
            mode = getattr(st, "st_mode", None)
        elif operation == "move" and source is not None and source.exists() and source.is_file():
            blob_bytes = self.save_file_blob(sid, source)
            st = source.stat()
            mtime = st.st_mtime
            mode = getattr(st, "st_mode", None)
            pre_fp = fingerprint_file(source)
            existed = True

        meta = SnapshotMeta(
            snapshot_id=sid,
            session_id=session_id or (tool_session_id.get() or ""),
            tool_call_id=tool_call_id,
            operation=operation,
            target_rel=target_rel,
            source_rel=source_rel,
            destination_rel=destination_rel,
            pre_fingerprint=pre_fp,
            existed_before=existed,
            blob_bytes=blob_bytes,
            target_mtime=mtime,
            target_mode=mode,
        )
        try:
            self.write_meta(meta)
        except OSError as exc:
            # 清理半成品
            shutil.rmtree(self._dir(sid), ignore_errors=True)
            raise RuntimeError(f"快照元数据写入失败: {exc}") from exc
        return meta

    def finalize_post_fingerprint(self, meta: SnapshotMeta, path: Path | None) -> SnapshotMeta:
        post = fingerprint_file(path) if path is not None else None
        updated = SnapshotMeta(**{**meta.to_dict(), "post_fingerprint": post})
        if load_snapshot_config().enabled:
            self.write_meta(updated)
        return updated

    def list_metas(self) -> list[SnapshotMeta]:
        if not self.root.exists():
            return []
        items: list[SnapshotMeta] = []
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            meta = self.load_meta(child.name)
            if meta:
                items.append(meta)
        items.sort(key=lambda m: m.created_at)
        return items

    def cleanup(self, retention: SnapshotRetention | None = None) -> list[str]:
        """按数量/年龄/字节清理；失败不触及用户文件。返回已删 snapshot id。"""
        retention = retention or load_snapshot_config()
        if not retention.enabled:
            return []
        removed: list[str] = []
        try:
            metas = [m for m in self.list_metas() if not m.retention_protect]
            now = time.time()
            # 先按年龄
            for meta in list(metas):
                if retention.max_age_seconds > 0 and now - meta.created_at > retention.max_age_seconds:
                    self._remove_snapshot_dir(meta.snapshot_id)
                    removed.append(meta.snapshot_id)
                    metas.remove(meta)
            # 再按数量（旧的先删）
            while retention.max_count > 0 and len(metas) > retention.max_count:
                oldest = metas.pop(0)
                self._remove_snapshot_dir(oldest.snapshot_id)
                removed.append(oldest.snapshot_id)
            # 再按总字节
            total = sum(m.blob_bytes for m in metas)
            while retention.max_total_bytes > 0 and total > retention.max_total_bytes and metas:
                oldest = metas.pop(0)
                total -= oldest.blob_bytes
                self._remove_snapshot_dir(oldest.snapshot_id)
                removed.append(oldest.snapshot_id)
        except Exception:
            logger.exception("snapshot cleanup failed; user files untouched")
        return removed

    def _remove_snapshot_dir(self, snapshot_id: str) -> None:
        path = self._dir(snapshot_id)
        if path.exists() and _is_same_or_child(path, self.root):
            shutil.rmtree(path, ignore_errors=True)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def restore_from_snapshot(
    store: SnapshotStore,
    snapshot_id: str,
    *,
    session_id: str | None,
) -> str:
    meta = store.load_meta(snapshot_id)
    if meta is None:
        return f"快照不存在或已过期: {snapshot_id}"
    current_session = session_id or tool_session_id.get() or ""
    if meta.session_id and current_session and meta.session_id != current_session:
        return f"跨 session 拒绝 undo（snapshot 属于其他会话）"

    workspace = store.workspace
    assert workspace is not None

    def _safe(rel: str) -> Path:
        from app.storage.workspace import _resolve_safe_path, assert_not_agent_space, normalize_workspace_path

        path = _resolve_safe_path(workspace, normalize_workspace_path(rel))
        assert_not_agent_space(path)
        if is_snapshot_sidecar_path(path, workspace):
            raise ValueError("禁止对 snapshot sidecar 执行 undo 目标写入以外的操作")
        return path

    try:
        if meta.operation in {"write", "replace", "create"}:
            target = _safe(meta.target_rel)
            current_fp = fingerprint_file(target) if target.exists() else None
            if meta.post_fingerprint and current_fp and current_fp != meta.post_fingerprint:
                return "undo 冲突：目标文件在快照后已被修改，拒绝覆盖"
            if meta.post_fingerprint and not target.exists() and meta.operation != "create":
                return "undo 冲突：目标文件已不存在"
            if not meta.existed_before:
                # 创建新文件：删除之
                if target.exists():
                    if meta.post_fingerprint and fingerprint_file(target) != meta.post_fingerprint:
                        return "undo 冲突：新建文件内容已变化"
                    target.unlink()
                return f"已撤销创建: {meta.target_rel}"
            blob = store.blob_path(snapshot_id)
            if not blob.exists():
                return f"快照内容缺失: {snapshot_id}"
            atomic_write_text(target, blob.read_text(encoding="utf-8"))
            return f"已撤销写入: {meta.target_rel}"

        if meta.operation == "delete":
            target = _safe(meta.target_rel)
            if target.exists():
                return "undo 冲突：删除目标路径已重新存在内容"
            blob = store.blob_path(snapshot_id)
            if not blob.exists():
                return f"快照内容缺失: {snapshot_id}"
            atomic_write_text(target, blob.read_text(encoding="utf-8"))
            return f"已恢复删除: {meta.target_rel}"

        if meta.operation == "move":
            src_rel = meta.source_rel or meta.target_rel
            dst_rel = meta.destination_rel or meta.target_rel
            src = _safe(src_rel)
            dst = _safe(dst_rel)
            if not dst.exists():
                return "undo 冲突：移动目标已不存在"
            if meta.post_fingerprint and fingerprint_file(dst) != meta.post_fingerprint:
                return "undo 冲突：移动后的文件已被修改"
            if src.exists():
                return "undo 冲突：移动源路径已重新存在"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            return f"已撤销移动: {dst_rel} -> {src_rel}"
    except ValueError as e:
        return str(e)
    except OSError as e:
        return f"undo 失败: {e}"

    return f"不支持的 operation: {meta.operation}"
