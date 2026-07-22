# MCP 客户端对接方案（v0.1）

> 仿照 [Hermes Agent `mcp_tool.py`](https://github.com/NousResearch/hermes-agent/blob/main/tools/mcp_tool.py) 设计，适配麦林现有 `ToolCard` + `ToolRegistry` + `tool_search` 架构。

## 1. 目标

- 作为 **MCP Client（Host）**，连接外部 MCP Server，将远端工具投影为本地 `ToolCard`
- 支持 **stdio 子进程** 与 **HTTP/StreamableHTTP** 两种传输（SSE 列为 v0.2）
- 配置驱动、按需启用，**默认不预装任何 MCP Server**
- 与内置 `packages/` 工具包并列，统一走 Agent 工具调度链路

## 2. 非目标（v0.1）

- 不做 MCP Catalog 一键安装（`hermes mcp install` 等价物，放 v0.2）
- 不做 Sampling / Elicitation / OAuth 自动刷新（放 v0.2+）
- 不让 Hermes 反向 `mcp serve`（麦林已有 FastAPI，非本阶段范围）

## 3. 与现有架构的关系

```
workspace/CONFIG.json
    └── mcp_servers: { ... }          # 新增配置段
            │
            ▼
app/tools/mcp/                        # 本模块（MCP Client）
    ├── lifecycle.discover()          # 启动时连接 + 发现工具
    ├── bridge.to_tool_cards()        # MCP Tool → ToolCard
    └── server_task.MCPServerTask     # 长连接 + 子进程/HTTP
            │
            ▼
app/tools/registry.ToolRegistry
    resolve_cards() = builtin_cards + mcp_cards
            │
            ▼
app/tools/tool_search.py
    hot / deferred 分类（MCP 工具默认 deferred）
            │
            ▼
LangGraph agent → call_tools → invoke_card
```

### 3.1 与内置工具包的差异

| 维度 | `packages/*`（内置） | `mcp/*`（外部） |
|------|----------------------|-----------------|
| 来源 | Python handler 硬编码 | MCP Server 动态发现 |
| 进程模型 | 同进程函数调用 | stdio 独立子进程 / HTTP 远程 |
| 配置键 | `tools.filesystem` 等 | `mcp_servers.<name>` |
| ToolCard.source | `builtin` | `plugin` |
| 命名 | `read_file` | `mcp_<server>_<tool>` |

## 4. 目录结构

```
backend/app/tools/mcp/
├── DESIGN.md              # 本文档
├── __init__.py            # 对外 API：discover / shutdown / reload / status
├── config.py              # 读取 mcp_servers、环境变量插值、安全过滤
├── types.py               # McpServerConfig、McpServerStatus 等
├── loop.py                # 后台专用 asyncio 事件循环（daemon 线程）
├── server_task.py         # 单 Server 长连接 Task（stdio / HTTP）
├── discovery.py           # connect → initialize → list_tools
├── handlers.py            # call_tool 同步 handler 工厂
├── bridge.py              # MCP schema → ToolCard 投影
├── lifecycle.py           # 启动 / 关闭 / 重载编排
├── security.py            # stdio env 过滤、错误脱敏、可疑配置拦截
└── catalog/               # v0.2：官方 MCP 清单（manifest only）
    └── README.md
```

## 5. 配置设计

在 `workspace/CONFIG.json`（及 `workspace_defaults/CONFIG.json`）新增顶层段：

```json
{
  "mcp_servers": {
    "filesystem": {
      "enabled": true,
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "${WORKSPACE}"],
      "env": {},
      "timeout": 120,
      "connect_timeout": 60,
      "supports_parallel_tool_calls": false,
      "tools": {
        "include": [],
        "exclude": [],
        "resources": false,
        "prompts": false
      }
    },
    "linear": {
      "enabled": false,
      "transport": "http",
      "url": "https://mcp.linear.app/mcp",
      "headers": {},
      "auth": "bearer",
      "timeout": 180,
      "connect_timeout": 30
    }
  }
}
```

### 5.1 配置键说明

| 键 | 适用 | 说明 |
|----|------|------|
| `enabled` | 全部 | `false` 时跳过连接 |
| `transport` | 全部 | `stdio` \| `http`（默认按字段推断：`url`→http，否则 stdio） |
| `command` / `args` / `env` | stdio | 子进程启动参数；env 不继承完整 shell，仅白名单基线 + 显式配置 |
| `url` / `headers` | http | 远程 MCP 端点 |
| `timeout` | 全部 | 单次 `call_tool` 超时（秒），默认 300 |
| `connect_timeout` | 全部 | 首次连接超时，默认 60 |
| `supports_parallel_tool_calls` | 全部 | 是否允许同 Server 工具并发，默认 false |
| `tools.include` | 全部 | 白名单（非空时仅注册列表内工具） |
| `tools.exclude` | 全部 | 黑名单（include 优先） |
| `tools.resources` | 全部 | 是否注册 resource 工具，默认 false（v0.1 简化） |
| `tools.prompts` | 全部 | 是否注册 prompt 工具，默认 false（v0.1 简化） |

### 5.2 环境变量插值

- 字符串值支持 `${VAR}`，从 `os.environ` 解析（含 `.env`）
- 内置占位符：`${WORKSPACE}` → `get_settings().workspace_path`

## 6. 核心运行时设计（仿 Hermes）

### 6.1 后台事件循环

LangGraph Agent 主线程是同步 + 多线程 checkpointer；MCP SDK 是 asyncio。

```
主线程 / Agent 线程                    MCP 后台线程
      │                                      │
      │  run_coroutine_threadsafe()          │  _mcp_loop (asyncio)
      ├─────────────────────────────────────►│
      │  handler(args) → JSON str            │  MCPServerTask.run()
      │◄─────────────────────────────────────┤  session.call_tool()
```

- `loop.py`：`threading.Thread(daemon=True)` + `asyncio.new_event_loop()`
- 所有 `ClientSession` 在同一 loop 的 Task 内创建与销毁（满足 anyio cancel scope 约束）
- 工具 handler 对外仍是 **同步** `Callable[..., str]`，与现有 `ToolCard.handler` 一致

### 6.2 MCPServerTask（单 Server）

职责对标 Hermes `MCPServerTask`：

1. 根据 config 选择 `_run_stdio` 或 `_run_http`
2. `session.initialize()` → 缓存 `initialize_result`（能力探测）
3. `session.list_tools()` → 缓存 `_tools`
4. 维持长连接；断线指数退避重连（最多 5 次）
5. 每 Server 一个 `_rpc_lock`，串行化 JSON-RPC（避免 stdio 流竞态）
6. shutdown 时优雅退出 `async with` 块

**stdio 子进程**：`command` + `args` 拉起独立进程；stderr 重定向到 `workspace/logs/mcp-stderr.log`。

**HTTP**：使用 `mcp` SDK 的 `streamablehttp_client`（或 httpx 回退）。

### 6.3 工具命名与 ToolCard 投影

```python
# bridge.py
prefixed_name = f"mcp_{sanitize(server_name)}_{sanitize(tool_name)}"
# filesystem.read_file → mcp_filesystem_read_file

ToolCard(
    id=f"mcp.{server_name}.{tool_name}",
    name=prefixed_name,
    package=f"mcp-{server_name}",
    source="plugin",
    handler=make_call_handler(server_name, tool_name, timeout),
    parameters=normalize_input_schema(mcp_tool.inputSchema),
    risk_level="moderate",  # 外部工具默认 moderate
    ...
)
```

### 6.4 与 tool_search 集成

MCP 工具 **默认作为延时工具**，经 `tool_search → tool_describe → tool_call` 披露（`tools.tool_search.mcp_as_hot: false`）。若需直绑，可显式设 `mcp_as_hot: true`。

可选：在 `CONFIG.json` 增加：

```json
"tools": {
  "tool_search": {
    "hot_tools": ["mcp_filesystem_read_file"]
  }
}
```

`bridge.py` 可为每个 MCP Server 生成 `package` 元数据，供前端/API `list_packages` 展示。

## 7. Registry 集成改动（待实现）

```python
# registry.py（计划改动）

class ToolRegistry:
    def resolve_cards(self) -> list[ToolCard]:
        cards: list[ToolCard] = []
        for package in self.packages:
            if self.is_package_enabled(package):
                cards.extend(package.build_cards())
        cards.extend(get_mcp_cards())  # ← 新增
        return enrich_card_parameters(cards)
```

```python
# 应用启动（main.py 或 lifespan）
from app.tools.mcp import discover_mcp_servers

@asynccontextmanager
async def lifespan(app):
    discover_mcp_servers()
    yield
    shutdown_mcp_servers()
```

```python
# 配置热重载 API（可选）
POST /api/tools/mcp/reload  →  reload_mcp_servers() + clear_tools_cache()
GET  /api/tools/mcp/status  →  get_mcp_status()
```

## 8. 生命周期

```
应用启动
  → load_mcp_config()
  → filter_suspicious_servers()
  → _ensure_mcp_loop()
  → asyncio.gather(connect each enabled server)   # 并行
  → bridge.register_cards()                       # 写入内存 registry
  → clear_tools_cache()

用户修改 CONFIG.json
  → POST /reload 或 CLI
  → shutdown removed servers
  → discover new/changed servers
  → 刷新 mcp_cards + clear_tools_cache()

应用退出
  → shutdown_mcp_servers()
  → _stop_mcp_loop()
  → 清理子进程
```

## 9. 安全（v0.1 最小集）

| 措施 | 说明 |
|------|------|
| stdio env 过滤 | 仅传递 `config.env` + 安全基线（PATH、SYSTEMROOT 等），不传 API Key |
| 错误脱敏 | handler 返回前 strip Bearer/token 字样 |
| 可疑配置拦截 | `command` 含 shell 元字符、`curl\|bash` 等拒绝启动 |
| 工具描述扫描 | 可选：检测 prompt injection 模式并 warn |
| 熔断器 | 连续 N 次失败后短路，返回结构化 error JSON |

## 10. 依赖

```toml
# pyproject.toml
[project.optional-dependencies]
mcp = ["mcp>=1.0"]
```

- `mcp` 为 **可选依赖**；未安装时 `discover_mcp_servers()` no-op，打 debug 日志
- stdio 场景需本机 `npx` / `node`（用户自行安装 MCP Server 包）

## 11. 分阶段交付

### Phase 1 — HTTP 传输 + 注册（当前）

- [x] 目录与 DESIGN.md
- [x] `config.py` / `types.py`（含 `type: streamable_http`、`baseUrl` 别名）
- [x] `loop.py` + `server_task.py`（**HTTP / Streamable HTTP**）
- [x] `discovery.py` + `bridge.py` + `handlers.py`
- [x] `registry.py` 接入 `get_mcp_cards()`
- [x] FastAPI lifespan `discover` / `shutdown`
- [ ] `server_task.py` stdio 子进程（见 `LOCAL_MCP.md`）
- [ ] 单元测试：mock stdio server

### Phase 2 — HTTP + 运维

- [ ] HTTP/StreamableHTTP 传输
- [ ] `GET /api/tools/mcp/status`、`POST /reload`
- [ ] 动态 `tools/list_changed` 通知刷新
- [ ] `workspace_defaults/CONFIG.json` 示例段

### Phase 3 — Catalog + OAuth

- [ ] `mcp/catalog/` manifest 格式
- [ ] CLI：`mailin mcp list|install|configure`
- [ ] OAuth token 缓存（`workspace/mcp-tokens/`）

## 12. 配置示例

### 本地文件系统（stdio）

```json
"mcp_servers": {
  "project_fs": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/projects/demo"],
    "tools": {
      "include": ["read_file", "list_directory"]
    }
  }
}
```

### 远程 HTTP

```json
"mcp_servers": {
  "company_api": {
    "url": "https://mcp.internal.example.com/mcp",
    "headers": {
      "Authorization": "Bearer ${COMPANY_MCP_TOKEN}"
    },
    "timeout": 180
  }
}
```

## 13. 调用链时序

```mermaid
sequenceDiagram
    participant App as FastAPI lifespan
    participant MCP as mcp.lifecycle
    participant Loop as mcp.loop
    participant Task as MCPServerTask
    participant Reg as ToolRegistry
    participant Agent as LangGraph agent

    App->>MCP: discover_mcp_servers()
    MCP->>Loop: ensure background loop
    loop each enabled server
        MCP->>Task: start(config)
        Task->>Task: spawn stdio / connect http
        Task->>Task: list_tools()
        MCP->>Reg: bridge.to_tool_cards()
    end
    Agent->>Reg: get_tools()
  Reg-->>Agent: hot tools + bridge tools
    Agent->>Agent: tool_search (find mcp_*)
    Agent->>MCP: handler → call_tool
    MCP-->>Agent: JSON result
```

## 14. 与 Hermes 对照

| Hermes | 麦林 v0.1 |
|--------|-----------|
| `~/.hermes/config.yaml` | `workspace/CONFIG.json` → `mcp_servers` |
| `tools.registry.register()` | `ToolCard` 列表合并进 `resolve_cards()` |
| `mcp-<server>` toolset | `package=f"mcp-{server}"` |
| `hermes mcp install` | Phase 3 `catalog/` |
| Sampling/Elicitation | 不做 |
| `/reload-mcp` | `POST /api/tools/mcp/reload` |

---

**下一步**：按 Phase 1 实现 `server_task.py`（stdio）+ `registry` 接入，用 `@modelcontextprotocol/server-filesystem` 做端到端验证。
