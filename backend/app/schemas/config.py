from pydantic import BaseModel


class ConfigFile(BaseModel):
    name: str
    content: str


class ConfigUpdate(BaseModel):
    content: str


class ConfigUpdateResponse(BaseModel):
    name: str
    status: str = "ok"


class ResetResponse(BaseModel):
    status: str = "ok"
    message: str


class AgentInfo(BaseModel):
    name: str
