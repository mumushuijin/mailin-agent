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

仅支持 checkpoint `schema_version=2`、账本元数据 `schema_version=2`、历史投影 `version=3`。
应用用户轮次统一使用 `turn_id`；WS/SSE/同步发送、取消和交互答复采用相同轮次关联。
checkpoint 的 scope 仅含 workspace/session/turn，当前 task、step 和模型 request 分别位于
`current_task`、`current_step.step_id`、`current_step.model.request_id`，快照 ID 为独立 `cp_...`。
正式 agent 迭代预算使用 `max_step_every_turn`；普通问候允许 `tasks=[]`、`current_task=null`。

已解析运行目录的 `sessions/<session_id>/ledger_meta.json` 保存版本和容器归属；
`ledger.jsonl` 每行是一条消息，必需 seq/message_id/timestamp/message，适用时带 turn_id。
所有持久时间均为 UTC Unix 毫秒。框架原始消息 ID（如 `run--...`）保存在 `message.id`，
不用于判断应用 turn；框架内部 callback/config run_id 保留原含义。示例：

```json
{"seq":1,"message_id":"msg_a1","turn_id":"turn_1","timestamp":1791964800000,"message":{"role":"user","content":"你好"}}
```

模型请求重建的 bootstrap、参考块和摘要视图只进入工作窗口，不作为普通对话事件追加。
关键模型/工具尝试、交互、节点/任务、turn 终态和摘要维护以结构化 system 消息审计，
不会进入默认模型提示或聊天正文。工具定义参数和完整结果以账本为权威，checkpoint 只存
definition_seq/result_seq 与恢复必要状态。大型结果引用包含受控相对路径、bytes、media_type、SHA-256。
诊断导出通过 `SessionConversationFiles.export_files` 包含一次元数据、账本和所有引用/摘要资源。
checkpoint 只保存模型工作窗口及账本已应用序号；历史投影位于
同目录的 `history_projection.json`，损坏或缺失时可由账本重建。上下文压缩摘要保存在同目录的
`summaries/`，checkpoint 只保存摘要指针。共享 `sessions/checkpoints.sqlite` 和
`sessions/index.json` 保持在 sessions 根目录。

旧 checkpoint、账本、投影和 `run_id` 请求明确拒绝，应用不转换、覆盖或自动清理旧会话。
用户备份并主动清理旧会话后新建。回滚须配套应用版本和对应数据备份；旧版不能读取新格式。
恢复发现已启动却没有可靠结果的工具时追加一次 unknown 结算，失败结束原 turn，禁止自动重放。
完整协议、字段清单及恢复规则见 [checkpoint-ledger-v2.md](../.docs/checkpoint-ledger-v2.md)。

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
