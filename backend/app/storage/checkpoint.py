from pathlib import Path

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.settings import get_settings

_checkpointer: AsyncSqliteSaver | None = None
_conn: aiosqlite.Connection | None = None


def _checkpoint_path() -> Path:
    settings = get_settings()
    path = settings.workspace_path / "sessions" / "checkpoints.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def init_checkpointer() -> AsyncSqliteSaver:
    global _checkpointer, _conn
    if _checkpointer is not None:
        return _checkpointer
    _conn = await aiosqlite.connect(str(_checkpoint_path()))
    _checkpointer = AsyncSqliteSaver(_conn)
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
