from app.agent.hooks import ON_SESSION_START, dispatch_observe
from app.core.settings import get_settings
from app.schemas.session import Session
from app.storage.checkpoint import delete_thread, reset_checkpointer
from app.storage.history_projection import HistoryProjectionStore
from app.storage.workspace import SessionStore


class SessionService:
    def __init__(self):
        settings = get_settings()
        self.store = SessionStore(settings.workspace_path)
        self.history_projection = HistoryProjectionStore(settings.workspace_path)

    def list_sessions(self) -> list[Session]:
        return [Session(**s) for s in self.store.list()]

    def create(self, workspace_path: str) -> str:
        session_id = self.store.create(workspace_path)
        dispatch_observe(ON_SESSION_START, session_id=session_id)
        return session_id

    def get(self, session_id: str) -> Session:
        return Session(**self.store.get(session_id))

    def rebind(self, session_id: str, workspace_path: str) -> Session:
        return Session(**self.store.rebind(session_id, workspace_path))

    def rename(self, session_id: str, title: str) -> Session:
        return Session(**self.store.rename(session_id, title))

    async def delete(self, session_id: str) -> None:
        self.store.delete(session_id)
        self.history_projection.delete(session_id)
        await delete_thread(session_id)

    async def clear_all(self) -> None:
        for session in self.store.list():
            self.history_projection.delete(session["id"])
            await delete_thread(session["id"])
        self.store.clear_all()
        await reset_checkpointer()
