from pydantic import BaseModel


class MemoryEntry(BaseModel):
    date: str
    filename: str
    content: str
    preview: str


class MemoryListResponse(BaseModel):
    memories: list[MemoryEntry]
    total: int


class MemoryDetail(BaseModel):
    filename: str
    date: str
    content: str
