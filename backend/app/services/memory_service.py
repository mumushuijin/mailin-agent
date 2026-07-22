from app.core.settings import get_settings
from app.schemas.memory import MemoryDetail, MemoryEntry, MemoryListResponse
from app.storage.workspace import MemoryStore


class MemoryService:
    def __init__(self):
        self.store = MemoryStore(get_settings().workspace_path)

    def list_memories(self) -> MemoryListResponse:
        entries = [MemoryEntry(**e) for e in self.store.list_entries()]
        return MemoryListResponse(memories=entries, total=len(entries))

    def get(self, filename: str) -> MemoryDetail:
        data = self.store.get(filename)
        return MemoryDetail(**data)
