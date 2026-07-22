from fastapi import APIRouter

from app.schemas.memory import MemoryDetail, MemoryListResponse
from app.services.memory_service import MemoryService

router = APIRouter()
memory_service = MemoryService()


@router.get("/list", response_model=MemoryListResponse)
async def list_memories():
    return memory_service.list_memories()


@router.get("/{filename}", response_model=MemoryDetail)
async def get_memory(filename: str):
    return memory_service.get(filename)
