"""structlog 结构化日志：trace_id / run_id / task_id 上下文传播。"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from structlog.contextvars import (
    bind_contextvars,
    get_contextvars,
    reset_contextvars,
)

_configured = False
_runtime_handler: logging.FileHandler | None = None
_SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|authorization)", re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"((?:api[_-]?key|token|secret|password|authorization)\s*[:=]\s*)([^\s,;]+)",
    re.IGNORECASE,
)


def redact_log_value(value: Any) -> Any:
    """递归脱敏日志字段，避免配置诊断和异常文本泄漏凭证。"""
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, child in value.items():
            redacted[key] = "***" if _SECRET_KEY_RE.search(str(key)) else redact_log_value(child)
        return redacted
    if isinstance(value, (list, tuple)):
        return type(value)(redact_log_value(item) for item in value)
    if isinstance(value, str):
        return _SECRET_ASSIGNMENT_RE.sub(r"\1***", value)
    return value


def _log_timezone() -> ZoneInfo:
    from app.core.settings import get_settings

    return ZoneInfo(get_settings().mailin_timezone)


def _add_timestamp(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """统一使用项目时区（默认 Asia/Shanghai / UTC+8）。"""
    event_dict["timestamp"] = datetime.now(_log_timezone()).isoformat(timespec="milliseconds")
    return event_dict


def _redact_event(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return redact_log_value(event_dict)


def setup_logging(*, level: str = "INFO", log_format: str = "console") -> None:
    """初始化 structlog + stdlib 桥接，进程内只执行一次。"""
    global _configured
    if _configured:
        return
    _configured = True

    log_level = getattr(logging, level.upper(), logging.INFO)

    foreign_pre_chain = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _redact_event,
        _add_timestamp,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            _redact_event,
            _add_timestamp,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    if log_format == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=foreign_pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)


def attach_runtime_log_file(log_dir: Any) -> None:
    """Send backend logs to the resolved writable runtime log directory."""
    global _runtime_handler
    from pathlib import Path

    path = Path(log_dir) / "backend.log"
    if _runtime_handler and _runtime_handler.baseFilename == str(path):
        return
    root = logging.getLogger()
    if _runtime_handler:
        root.removeHandler(_runtime_handler)
        _runtime_handler.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(root.handlers[0].formatter if root.handlers else logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(handler)
    _runtime_handler = handler


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def bind_context(**fields: Any) -> dict[str, contextlib.Token[Any]]:
    return bind_contextvars(**fields)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


def get_current_trace_id() -> str | None:
    value = get_contextvars().get("trace_id")
    return value if isinstance(value, str) else None


def new_trace_id() -> str:
    return str(uuid.uuid4())


@contextlib.contextmanager
def log_scope(**fields: Any) -> Iterator[None]:
    """绑定日志上下文，退出时恢复（支持嵌套）。"""
    tokens = bind_contextvars(**fields)
    try:
        yield
    finally:
        reset_contextvars(**tokens)
