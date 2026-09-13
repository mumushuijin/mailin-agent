from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SkillSource = Literal["global", "project"]
SkillHealth = Literal["ok", "disabled", "error", "shadowed"]


class SkillEntry(BaseModel):
    id: str
    name: str
    description: str = ""
    version: str | None = None
    source: SkillSource
    path: str
    enabled: bool = True
    active: bool = True
    health: SkillHealth = "ok"
    shadowed_by: str | None = None
    errors: list[str] = Field(default_factory=list)
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)


class SkillResource(BaseModel):
    path: str
    content: str | None = None
    error: str | None = None


class SkillDetail(SkillEntry):
    content: str | None = None
    resources: list[SkillResource] = Field(default_factory=list)


class SkillListResponse(BaseModel):
    skills: list[SkillEntry]
    total: int


class SkillRefreshResponse(BaseModel):
    status: str = "ok"
    skills: list[SkillEntry]
    total: int


class SkillToggleResponse(BaseModel):
    status: str = "ok"
    skill: SkillEntry


class SkillMatch(BaseModel):
    skill: SkillEntry
    score: int
    reason: str


class SkillMatchResponse(BaseModel):
    matches: list[SkillMatch]
    total: int
