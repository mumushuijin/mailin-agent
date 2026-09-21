from fastapi import APIRouter, Query

from app.schemas.config import (
    AgentInfo,
    ConfigFile,
    ConfigModuleUpdateRequest,
    ConfigModuleUpdateResponse,
    ConfigModuleValidateRequest,
    ConfigModuleValidateResponse,
    ConfigModulesResponse,
    ConfigUpdate,
    ConfigUpdateResponse,
    ResetResponse,
)
from app.services.config_service import ConfigService

router = APIRouter()
config_service = ConfigService()


@router.get("/list")
async def list_configs() -> dict:
    return {"configs": config_service.list_configs()}


@router.get("/agent/info", response_model=AgentInfo)
async def get_agent_info():
    return config_service.get_agent_info()


@router.get("/modules", response_model=ConfigModulesResponse)
async def list_modules():
    return config_service.list_modules()


@router.post("/modules/{module}/validate", response_model=ConfigModuleValidateResponse)
async def validate_module(module: str, body: ConfigModuleValidateRequest):
    return config_service.validate_module(module, value=body.value, text=body.text)


@router.put("/modules/{module}", response_model=ConfigModuleUpdateResponse)
async def update_module(module: str, body: ConfigModuleUpdateRequest):
    return config_service.update_module(
        module,
        base_revision=body.base_revision,
        value=body.value,
        text=body.text,
    )


@router.get("/{name}", response_model=ConfigFile)
async def get_config(name: str):
    return config_service.get_config(name)


@router.put("/{name}", response_model=ConfigUpdateResponse)
async def update_config(name: str, body: ConfigUpdate):
    config_service.update_config(name, body.content)
    return ConfigUpdateResponse(name=name)


@router.post("/reset", response_model=ResetResponse)
async def reset_config(
    reset_sessions: bool = Query(False),
    reset_memory: bool = Query(False),
    reset_global_config: bool = Query(False),
):
    message = await config_service.reset(
        reset_sessions=reset_sessions,
        reset_memory=reset_memory,
        reset_global_config=reset_global_config,
    )
    return ResetResponse(message=message)
