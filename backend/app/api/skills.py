from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from app.schemas.skill import (
    SkillDetail,
    SkillListResponse,
    SkillMatchResponse,
    SkillRefreshResponse,
    SkillToggleResponse,
)
from app.services.skill_service import SkillService
from app.storage.workspace import validate_project_workspace
from app.tools.runtime import get_project_workspace

router = APIRouter()


def _service(project_path: str | None = None) -> SkillService:
    project = validate_project_workspace(project_path) if project_path else get_project_workspace()
    return SkillService(project_workspace=project)


@router.get("/list", response_model=SkillListResponse)
async def list_skills(
    source: Literal["global", "project"] | None = Query(None),
    project_path: str | None = Query(None),
):
    return _service(project_path).list(source=source)


@router.post("/refresh", response_model=SkillRefreshResponse)
async def refresh_skills(project_path: str | None = Query(None)):
    return _service(project_path).refresh()


@router.get("/matches", response_model=SkillMatchResponse)
async def match_skills(
    query: str = Query(..., min_length=1),
    source: Literal["global", "project"] | None = Query(None),
    limit: int = Query(5, ge=1, le=20),
    project_path: str | None = Query(None),
):
    return _service(project_path).match(query, source=source, limit=limit)


@router.get("/{skill_id}", response_model=SkillDetail)
async def get_skill(
    skill_id: str,
    source: Literal["global", "project"] | None = Query(None),
    include_references: bool = Query(False),
    project_path: str | None = Query(None),
):
    return _service(project_path).detail(skill_id, source=source, include_references=include_references)


@router.post("/{skill_id}/enable", response_model=SkillToggleResponse)
async def enable_skill(
    skill_id: str,
    source: Literal["global", "project"] | None = Query(None),
    project_path: str | None = Query(None),
):
    return _service(project_path).set_enabled(skill_id, True, source=source)


@router.post("/{skill_id}/disable", response_model=SkillToggleResponse)
async def disable_skill(
    skill_id: str,
    source: Literal["global", "project"] | None = Query(None),
    project_path: str | None = Query(None),
):
    return _service(project_path).set_enabled(skill_id, False, source=source)
