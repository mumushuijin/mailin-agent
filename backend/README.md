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
- `MAILIN_MODE`：`development` 或 `production`。
- `MAILIN_RUNTIME_ROOT`：统一可写运行根；开发默认是仓库根 `.runtime`。
- `MAILIN_RESOURCES_DIR`：只读发布资源目录。

也可以通过 CLI 显式传入这些路径：

```bash
uv run mailin serve --mode development --runtime-root D:\\MailinDevRuntime
uv run mailin serve --mode production --host 127.0.0.1 --port 0 --runtime-root D:\\MailinRuntime --resources-dir <resources-dir>
```

生产模式不会把可写数据回退到当前目录或 Python 代码目录。

运行根只管理四个一级目录：`data`（配置、agent-home、会话、记忆、checkpoint）、`cache`、`tmp`、`log`。全局配置是 `data/config/config.toml`，首次启动从只读 `resources/defaults/config/config.toml` 初始化；更新采用临时文件和原子替换，上一版位于 `config.toml.bak`。项目 workspace 的选择不会改变全局配置或 agent-home。

旧数据迁移命令：`uv run mailin migrate --runtime-root <新运行根> --resources-dir <资源根> --legacy-data-root <旧数据目录>`；旧缓存、临时与日志可分别用 `--legacy-cache-dir`、`--legacy-temp-dir`、`--legacy-log-dir` 指定。目标必须为空。迁移会校验复制结果和转换后的 TOML，成功后清除旧源；失败则保留旧源且不启动服务。清理不可逆，升级前应备份旧目录。

## 主要接口

- `GET /health`：健康检查。
- `/api/chat`：同步对话与 SSE 流式降级。
- `/api/ws/chat`：WebSocket 主对话通道、取消、审批、后台事件。
- `/api/session`：会话 CRUD、分页历史、工具结果按需读取。
- `/api/memory`：每日记忆读取。
- `/api/config`：配置读取、更新、重置。

## Agent checkpoint 与会话账本

Agent checkpoint 使用固定的 canonical state：`checkpoint_id`、`scope`、`context`、
`max_step_every_run`、`tasks`、`current_task`、`current_step`、`memory`，并带有状态修订、
终态和账本游标等恢复字段。WebSocket 与 SSE 在状态事件中共享该快照；LLM 文本增量仍作为
临时事件发送。

每个 session 的完整消息账本位于 `.runtime/data/agent-home/sessions/<session_id>/ledger.jsonl`，
每行严格包含 `seq`、`message_id`、`scope`、`origin`、`message`。`scope` 的六种身份分别是
`workspace_id`（工作空间）、`session_id`（会话）、`run_id`（一轮用户指令）、
`task_id`（任务项）、`request_id`（一次实际模型请求，包括重试）、`step_id`（一次节点执行）。
没有对应任务项、请求或节点执行时使用 JSON `null`。`origin` 只接受 `user`、`assistant`、
`tool`、`system_maintenance`。`message_id` 是应用账本 ID；供应商消息 ID 留在
`message.data.id`，工具调用 ID 留在 `message.data.tool_calls[*].id`，传输 `event_id` 不入账本。
例如：

```json
{"seq":1,"message_id":"msg_a1","scope":{"workspace_id":"ws_1","session_id":"00000000-0000-4000-8000-000000000001","run_id":"run_1","task_id":"task_1","request_id":null,"step_id":null},"origin":"user","message":{"type":"human","data":{"content":"你好"}}}
```

模型请求重建的 bootstrap、参考块和摘要视图只进入工作窗口，不作为普通对话事件追加。
诊断导出直接保留上述新格式 JSONL 行，并分别标注 checkpoint 的 `context.working_message`
为可恢复工作窗口、某次模型请求输入为运行时临时组装结果；两者不能当作相同的持久消息序列。
checkpoint 只保存模型工作窗口及账本已应用序号；历史投影位于
同目录的 `history_projection.json`，损坏或缺失时可由账本重建。上下文压缩摘要保存在同目录的
`summaries/`，checkpoint 只保存摘要指针。共享 `sessions/checkpoints.sqlite` 和
`sessions/index.json` 保持在 sessions 根目录。

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
└── resources/defaults/ # 只读 workspace 模板与 config/config.toml
```

## 测试

```bash
uv run pytest
```

如果本机 `uv` 缓存目录异常，可指定仓库内缓存：

```bash
uv --cache-dir ..\.uv-cache run pytest
```
