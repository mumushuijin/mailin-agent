from app.core.settings import get_settings
from app.schemas.session import Session
from app.storage.checkpoint import delete_thread, reset_checkpointer
from app.storage.workspace import SessionStore


class SessionService:
    def __init__(self):
        self.store = SessionStore(get_settings().workspace_path)

    def list_sessions(self) -> list[Session]:
        return [Session(**s) for s in self.store.list()]

    def create(self) -> str:
        return self.store.create()

    def get(self, session_id: str) -> Session:
        return Session(**self.store.get(session_id))

    async def delete(self, session_id: str) -> None:
        self.store.delete(session_id)
        await delete_thread(session_id)

    async def clear_all(self) -> None:
        for session in self.store.list():
            await delete_thread(session["id"])
        self.store.clear_all()
        await reset_checkpointer()
