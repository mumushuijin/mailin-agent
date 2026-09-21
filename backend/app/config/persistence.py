"""CONFIG.json revision 跟踪与原子写入。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


class ConfigRevisionTracker:
    """进程内 revision：结合文件摘要检测外部变更。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revision = 0
        self._digest: str | None = None

    @staticmethod
    def digest_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def digest_obj(data: dict[str, Any]) -> str:
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def current_for_path(self, path: Path) -> int:
        with self._lock:
            if not path.exists():
                if self._digest is None:
                    self._digest = ""
                return self._revision
            digest = self.digest_bytes(path.read_bytes())
            if self._digest is None:
                self._digest = digest
                return self._revision
            if digest != self._digest:
                self._digest = digest
                self._revision += 1
            return self._revision

    def bump_for_path(self, path: Path) -> int:
        with self._lock:
            digest = self.digest_bytes(path.read_bytes()) if path.exists() else ""
            self._digest = digest
            self._revision += 1
            return self._revision

    def reset(self) -> None:
        with self._lock:
            self._revision = 0
            self._digest = None


_TRACKER = ConfigRevisionTracker()


def get_revision_tracker() -> ConfigRevisionTracker:
    return _TRACKER


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """写入临时文件后原子替换，失败时保留旧文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise
