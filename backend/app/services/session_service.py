from app.agent.hooks import ON_SESSION_START, dispatch_observe
from app.core.settings import get_settings
from app.schemas.session import Session
from app.storage.checkpoint import delete_thread, reset_checkpointer
from app.storage.history_projection import HistoryProjectionStore
from app.storage.conversation_files import SessionConversationFiles
from app.storage.workspace import SessionStore


class SessionService:
    def __init__(self):
        settings = get_settings()
        self.store = SessionStore(settings.workspace_path)
        self.history_projection = HistoryProjectionStore(settings.workspace_path)
        self.conversation_files = SessionConversationFiles(settings.workspace_path)

    def list_sessions(self) -> list[Session]:
        return [Session(**s) for s in self.store.list()]

    def create(self, workspace_path: str) -> str:
        session_id = self.store.create(workspace_path)
        self.conversation_files.ensure_metadata(session_id)
        dispatch_observe(ON_SESSION_START, session_id=session_id)
        return session_id

    def get(self, session_id: str) -> Session:
        return Session(**self.store.get(session_id))

    def rebind(self, session_id: str, workspace_path: str) -> Session:
        return Session(**self.store.rebind(session_id, workspace_path))

    def rename(self, session_id: str, title: str) -> Session:
        return Session(**self.store.rename(session_id, title))

    def export_diagnostics(self, session_id: str) -> bytes:
        """Export one session's authoritative ledger and controlled resources."""
        import io
        import zipfile

        self.store.get(session_id)
        resources = self.conversation_files.export_files(session_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, content in resources.items():
                bundle.writestr(name, content)
        return buffer.getvalue()

    async def delete(self, session_id: str) -> None:
        thread_ids = self._checkpoint_thread_ids(session_id)
        self.store.delete(session_id)
        self.conversation_files.delete_session(session_id)
        for thread_id in thread_ids:
            await delete_thread(thread_id)

    def _checkpoint_thread_ids(self, session_id: str) -> list[str]:
        from app.agent.graph import make_thread_config

        branches = ["main"]
        try:
            branches.extend(event["branch_id"] for event in self.conversation_files.read_events(session_id)
                            if event["event_type"] == "branch.forked")
        except (OSError, ValueError):
            # Legacy or unreadable ledgers can still be explicitly deleted.
            pass
        return [make_thread_config(session_id, branch)["configurable"]["thread_id"]
                for branch in dict.fromkeys(branches)]

    async def clear_all(self) -> None:
        for session in self.store.list():
            thread_ids = self._checkpoint_thread_ids(session["id"])
            self.conversation_files.delete_session(session["id"])
            for thread_id in thread_ids:
                await delete_thread(thread_id)
        self.store.clear_all()
        await reset_checkpointer()
