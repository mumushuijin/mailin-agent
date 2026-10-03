"""TOML configuration serialization, revision tracking and atomic writes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

_NULL = "\u0000mailin:null"
_ESCAPE = "\u0000mailin:escape:"


def _encode(value: Any) -> Any:
    if value is None:
        return _NULL
    if isinstance(value, str) and (value == _NULL or value.startswith(_ESCAPE)):
        return _ESCAPE + value
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    return value


def _decode(value: Any) -> Any:
    if value == _NULL:
        return None
    if isinstance(value, str) and value.startswith(_ESCAPE):
        return value[len(_ESCAPE):]
    if isinstance(value, dict):
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def dumps_toml(data: dict[str, Any]) -> str:
    return tomli_w.dumps(_encode(data))


def loads_toml(content: str) -> dict[str, Any]:
    result = _decode(tomllib.loads(content))
    if not isinstance(result, dict):
        raise ValueError("配置根必须是表")
    return result


def read_toml(path: Path, *, recover: bool = False) -> dict[str, Any]:
    try:
        return loads_toml(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        backup = path.with_name(f"{path.name}.bak")
        if not recover or not backup.exists():
            raise
        return loads_toml(backup.read_text(encoding="utf-8"))


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


def atomic_write_toml(path: Path, data: dict[str, Any]) -> None:
    """写入临时文件后原子替换，失败时保留旧文件并保留 .bak。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dumps_toml(data)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    backup_path = path.with_name(f"{path.name}.bak")
    try:
        if path.exists():
            shutil.copy2(path, backup_path)
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
