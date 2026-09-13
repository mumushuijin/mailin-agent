# 麦林后端

麦林后端是本地 Agent 运行时，负责会话、流式对话、LangGraph 推理、工具调用、记忆、MCP、配置和本地持久化。

## 启动

```bash
cd backend
uv sync
cp .env.example .env
uv run mailin serve
```

开发热重载：

```bash
uv run mailin serve --reload
```

可选 MCP 支持：

```bash
uv sync --extra mcp
```

## 环境变量

- `OPENAI_API_KEY` / `OPENAI_BASE_URL`：OpenAI 或兼容 API。
- `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL`：阿里云 DashScope 兼容模式。
- `TAVILY_API_KEY`：可选 Web 搜索。
- `HOST` / `PORT`：默认 `0.0.0.0:8000`。
- `LANGCHAIN_TRACING_V2` / `LANGCHAIN_API_KEY`：可选 LangSmith 追踪。

## 主要接口

- `GET /health`：健康检查。
- `/api/chat`：同步对话与 SSE 流式降级。
- `/api/ws/chat`：WebSocket 主对话通道、取消、审批、后台事件。
- `/api/session`：会话 CRUD、分页历史、工具结果按需读取。
- `/api/memory`：每日记忆读取。
- `/api/config`：配置读取、更新、重置。

## 目录

```text
backend/
├── app/
│   ├── api/          # REST / WebSocket 路由
│   ├── agent/        # LangGraph Agent 与流事件
│   ├── context/      # 上下文预算、压缩、记忆
│   ├── tools/        # 内置工具与 MCP
│   ├── services/     # chat/session/config/ws 服务
│   ├── storage/      # 工作区、checkpoint、历史投影
│   └── core/         # 设置、LLM、日志、延迟度量
├── cli/              # mailin serve
├── tests/            # pytest
└── workspace_defaults/
```

## 测试

```bash
uv run pytest
```

如果本机 `uv` 缓存目录异常，可指定仓库内缓存：

```bash
uv --cache-dir ..\.uv-cache run pytest
```
