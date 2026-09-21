from __future__ import annotations

from fastapi import APIRouter

from app.schemas.mcp import (
    McpListResponse,
    McpMutationResponse,
    McpRefreshResponse,
    McpServerDetail,
    McpServerDraft,
    McpServerWrite,
    McpTestResponse,
    McpValidateResponse,
)
from app.services.mcp_service import McpService

router = APIRouter()
_service = McpService()


@router.get("/list", response_model=McpListResponse)
async def list_mcp_servers():
    return _service.list_servers()


@router.post("/validate", response_model=McpValidateResponse)
async def validate_mcp_server(body: McpServerDraft):
    return _service.validate_draft(body)


@router.post("/test", response_model=McpTestResponse)
async def test_mcp_server(body: McpServerDraft):
    return _service.test_draft(body)


@router.post("/refresh", response_model=McpRefreshResponse)
async def refresh_mcp_servers():
    return _service.refresh()


@router.get("/{server_id}", response_model=McpServerDetail)
async def get_mcp_server(server_id: str):
    return _service.get_server(server_id)


@router.put("/{server_id}", response_model=McpMutationResponse)
async def upsert_mcp_server(server_id: str, body: McpServerWrite):
    return _service.upsert_server(server_id, body)


@router.delete("/{server_id}")
async def delete_mcp_server(server_id: str):
    return _service.delete_server(server_id)


@router.post("/{server_id}/enable", response_model=McpMutationResponse)
async def enable_mcp_server(server_id: str):
    return _service.enable_server(server_id, True)


@router.post("/{server_id}/disable", response_model=McpMutationResponse)
async def disable_mcp_server(server_id: str):
    return _service.enable_server(server_id, False)
