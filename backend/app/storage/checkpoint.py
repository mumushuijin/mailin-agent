import asyncio
import copy
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.base import CheckpointTuple

from app.core.settings import get_settings

_checkpointer: AsyncSqliteSaver | None = None
_conn: aiosqlite.Connection | None = None


class CanonicalAsyncSqliteSaver(AsyncSqliteSaver):
    """Persist checkpoint_uid under the public canonical checkpoint_id channel name."""

    async def aput(self, config, checkpoint, metadata, new_versions):
        payload = copy.deepcopy(checkpoint)
        channels = payload.get("channel_values") or {}
        if "checkpoint_uid" in channels:
            channels["checkpoint_id"] = channels.pop("checkpoint_uid")
            payload["channel_values"] = channels
        return await super().aput(config, payload, metadata, new_versions)

    async def aget_tuple(self, config):
        value = await super().aget_tuple(config)
        if value is None:
            return None
        payload = copy.deepcopy(value.checkpoint)
        channels = payload.get("channel_values") or {}
        if "checkpoint_id" in channels:
            channels["checkpoint_uid"] = channels.pop("checkpoint_id")
            payload["channel_values"] = channels
        return CheckpointTuple(
            config=value.config,
            checkpoint=payload,
            metadata=value.metadata,
            parent_config=value.parent_config,
            pending_writes=value.pending_writes,
        )


def _checkpoint_path() -> Path:
    settings = get_settings()
    return settings.workspace_path / "sessions" / "checkpoints.sqlite"


async def init_checkpointer() -> AsyncSqliteSaver:
    global _checkpointer, _conn
    if _checkpointer is not None:
        return _checkpointer
    path = _checkpoint_path()
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(str(path))
    _checkpointer = CanonicalAsyncSqliteSaver(_conn)
    return _checkpointer


def get_checkpointer() -> AsyncSqliteSaver:
    if _checkpointer is None:
        raise RuntimeError("Checkpointer 未初始化，请确认应用 lifespan 已启动")
    return _checkpointer


async def close_checkpointer() -> None:
    global _checkpointer, _conn
    if _conn is not None:
        await _conn.close()
    _checkpointer = None
    _conn = None


async def delete_thread(session_id: str) -> None:
    try:
        await get_checkpointer().adelete_thread(session_id)
    except Exception:
        pass


async def reset_checkpointer() -> None:
    path = _checkpoint_path()
    await close_checkpointer()
    from app.agent.graph import get_graph

    get_graph.cache_clear()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    await init_checkpointer()
