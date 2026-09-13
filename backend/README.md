# 麦林 Mailin 后端

本地 AI Agent 后端，基于 **FastAPI + LangGraph**，对接 `frontend/` 全部 API。

## 快速开始

```bash
cd backend

# 安装依赖（uv）
uv sync

# 配置 LLM
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY 或 DASHSCOPE_API_KEY

# 启动服务（默认 http://localhost:8000）
uv run mailin serve
# 或开发热重载
uv run mailin serve --reload
```

可选依赖：

```bash
uv sync --extra mcp    # MCP 协议支持
```

## API 概览

| 模块 | 路径前缀 | 说明 |
|------|----------|------|
| Chat | `/api/chat` | 同步 / 流式对话（SSE） |
| Session | `/api/session` | 会话 CRUD + 历史 |
| Memory | `/api/memory` | 每日工作记忆 |
| Config | `/api/config` | Agent 配置读写 + 重置 |
| WebSocket | `/api/ws` | 实时对话、审批、后台事件 |

健康检查：`GET /health` → `{ "status": "ok", "has_llm": bool }`

### Chat（REST）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat/send` | 同步对话（排障 / 强类型 ChatResponse） |
| POST | `/api/chat/send/stream` | SSE 流式对话（WebSocket 失败时的降级路径） |

### Session

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/session/list` | 列出所有会话 |
| POST | `/api/session/create` | 创建会话 |
| GET | `/api/session/{id}` | 获取会话详情 |
| DELETE | `/api/session/{id}` | 删除会话 |
| GET | `/api/session/{id}/history` | 获取会话消息历史 |

### Memory

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/memory/list` | 列出每日记忆文件 |
| GET | `/api/memory/{filename}` | 读取指定记忆 |

### Config

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/config/list` | 可编辑配置项列表 |
| GET | `/api/config/agent/info` | Agent 元信息 |
| GET | `/api/config/{name}` | 读取配置（如 `CONFIG`、`IDENTITY`） |
| PUT | `/api/config/{name}` | 更新配置内容 |
| POST | `/api/config/reset` | 重置（可选 `reset_sessions` / `reset_memory` / `reset_global_config`） |

### WebSocket

连接：`ws://localhost:8000/api/ws/chat`

客户端 JSON 消息 `op` 字段：

| op | 说明 |
|----|------|
| `ping` | 心跳，返回 `pong` |
| `session.subscribe` | 订阅会话级事件 |
| `chat.send` | 发起对话（`message`、`session_id`、`run_id`） |
| `chat.cancel` | 取消进行中的 run |
| `approve` | 工具审批（`decision`: `allow` / `deny`） |

服务端推送 `AgentEvent` 类型包括 `token`、`tool_call`、`tool_result`、`done`、`error`、`background` 等。

## 架构

```
app/
├── main.py              # FastAPI 入口 + 生命周期
├── api/                 # REST / WebSocket 路由
├── agent/               # LangGraph ReAct 图（agent ↔ tools 循环）
├── context/             # 上下文预算、压缩、记忆热层与沉淀
├── tools/               # 内置工具包 + MCP 集成
├── services/            # 业务逻辑（chat / session / config / ws）
├── maintenance/         # 后台维护调度
├── storage/             # 工作区 + SQLite checkpoint
└── core/                # 设置、LLM、遥测、异常
cli/                     # `mailin serve` 命令行
tests/                   # pytest 测试
```

**Agent 图**：`StateGraph(AgentState)` — `agent` 节点调用 LLM，`tools` 节点执行工具，通过 `should_continue` 路由循环或结束。会话状态持久化到 SQLite checkpoint（`langgraph-checkpoint-sqlite`）。

**上下文引擎**：按 `CONFIG.json` 中的 token 预算分配 bootstrap、skills、摘要、近期轮次、工具结果等；超阈值时自动压缩。

**记忆系统**：每日热层笔记（`memory/`）→ 候选提取 → Rule Router → LLM 仲裁 → 沉淀到 `bootstraps/MEMORY.md` 长期记忆。

**工具包**（可在 `CONFIG.json` 的 `tools` 节开关）：

| 包 | config_key | 能力 |
|----|------------|------|
| filesystem | `filesystem` | 读写、精确替换、glob、正则 grep |
| shell | `shell` | `run_shell`（可后台）与 `process`（列/等/杀） |
| session | `session` | `todo` 会话待办、`ask_user` 向用户提问 |
| memory | `memory` | 记忆 grep / add / consolidate（默认冷工具） |
| calculator | `calculator` | Python 计算器（默认冷工具） |
| datetime | `datetime` | 当前时间（默认冷工具） |
| web_search | `web_search` | 网络搜索（默认冷工具） |
| MCP | `mcp` | 外部 MCP 服务器工具（默认冷工具） |
| tool_search | `tool_search` | 按需检索工具（hot tools + 搜索） |

默认热绑定（无需 `tool_search`）：`read_file`、`write_file`、`replace_in_file`、`glob_search`、`search_files`、`list_directory`、`run_shell`、`process`、`todo`、`ask_user`。关闭 MCP 后这九件内核工具仍可完成「读测试 → 改断言 → 跑测试」。

**后台维护**（服务启动后自动运行）：

- 记忆沉淀 — 每 5 分钟检查并自动 consolidate
- MCP 健康检查 — 每 10 分钟探测 MCP 连接

维护结果通过 WebSocket `background` 事件推送给前端。

## 工作区

麦林区分三空间：`workspace_defaults/` 只作模板；`workspace/` 是 Agent 自有空间（人格、记忆、会话、全局技能、`CONFIG.json`）；对话前指定的项目文件夹才是文件/shell 沙箱。

```
backend/workspace_defaults/          # 只读模板
├── CONFIG.json
├── bootstraps/                      # SOUL / USER / MEMORY / HEARTBEAT
└── project/AGENTS.md                # 项目规则种子

backend/workspace/                  # Agent 自有空间（不是对话 cwd）
├── CONFIG.json
├── bootstraps/
│   ├── SOUL.md
│   ├── USER.md
│   ├── MEMORY.md
│   └── HEARTBEAT.md
├── memory/
├── sessions/
└── skills/

<项目工作区>/                       # 用户指定，先绑定再对话
├── AGENTS.md
└── .mailin/                         # 建议加入项目 .gitignore
    ├── tool_results/
    └── artifacts/
```

`CONFIG.json` 关键字段：

- `agent` — 模型名、温度、`max_steps`
- `tools` — 各工具包开关 + `tool_search` 热工具列表
- `mcp_servers` — MCP 服务器连接配置
- `context` — token 上限、压缩阈值、预算分配比例

## 环境变量

| 变量 | 说明 |
|------|------|
| `OPENAI_API_KEY` | OpenAI 或兼容 API 密钥 |
| `OPENAI_BASE_URL` | 兼容 API 地址（可选） |
| `DASHSCOPE_API_KEY` | 阿里云 DashScope 密钥 |
| `DASHSCOPE_BASE_URL` | DashScope 兼容模式地址 |
| `TAVILY_API_KEY` | Tavily 搜索（未设置时回退 DuckDuckGo） |
| `HOST` / `PORT` | 服务监听地址（默认 `0.0.0.0:8000`） |
| `LANGCHAIN_TRACING_V2` | 启用 LangSmith 追踪 |
| `LANGCHAIN_API_KEY` | LangSmith API 密钥 |
| `LANGCHAIN_PROJECT` | LangSmith 项目名（默认 `mailin`） |

## 测试

```bash
uv run pytest
```

## 与前端联调

```bash
# 终端 1：后端
uv run mailin serve

# 终端 2：前端
cd ../frontend && npm run dev:web
```

前端 Vite 代理将 `/api` 转发到 `http://localhost:8000`，WebSocket 同理。

## LangGraph CLI

项目包含 `langgraph.json`，可使用 LangGraph CLI 独立调试 Agent 图：

```bash
uv run langgraph dev
```
