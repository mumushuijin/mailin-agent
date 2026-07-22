from app.core.settings import get_settings
from app.schemas.config import AgentInfo, ConfigFile
from app.services.session_service import SessionService
from app.storage.workspace import ConfigStore, MemoryStore
from app.tools.registry import clear_tools_cache


class ConfigService:
    def __init__(self):
        settings = get_settings()
        self.store = ConfigStore(settings.workspace_path, settings.workspace_defaults_path)
        self.workspace = settings.workspace_path
        self.session_service = SessionService()

    def list_configs(self) -> list[str]:
        return self.store.list_configs()

    def get_config(self, name: str) -> ConfigFile:
        return ConfigFile(name=name, content=self.store.read(name))

    def update_config(self, name: str, content: str) -> None:
        self.store.write(name, content)
        clear_tools_cache()
        from app.core.llm import get_chat_model
        from app.agent.graph import get_graph

        get_chat_model.cache_clear()
        get_graph.cache_clear()

        import asyncio

        try:
            loop = asyncio.get_running_loop()
            from app.services.push_service import push_config_updated

            loop.create_task(push_config_updated(name))
        except RuntimeError:
            pass

    def get_agent_info(self) -> AgentInfo:
        return AgentInfo(name=self.store.get_agent_name())

    async def reset(
        self,
        *,
        reset_sessions: bool = False,
        reset_memory: bool = False,
        reset_global_config: bool = False,
    ) -> str:
        parts: list[str] = []
        if reset_sessions:
            await self.session_service.clear_all()
            parts.append("已清除所有会话")
        if reset_memory:
            memory_store = MemoryStore(self.workspace)
            memory_store.clear_daily()
            memory_store.reset_longterm()
            parts.append("已清除记忆")
        if reset_global_config:
            self.store.reset_global()
            clear_tools_cache()
            from app.core.llm import get_chat_model
            from app.agent.graph import get_graph

            get_chat_model.cache_clear()
            get_graph.cache_clear()
            parts.append("已重置全局配置")
        if not parts:
            return "未执行任何重置操作"
        return "；".join(parts)
