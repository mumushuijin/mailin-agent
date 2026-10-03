# 麦林前端

麦林前端提供聊天、会话、项目绑定、工具审批、设置和后台状态展示。它既可以作为 Web 应用运行，也可以通过 Electron 打包成桌面应用。

## 启动

```bash
cd frontend
npm install
npm run dev:web
```

Electron 开发模式：

```bash
npm run dev
```

构建桌面包：

```bash
npm run electron:build
```

`electron:build` 需要构建机预先安装 PyInstaller。它会把 `backend/cli/__main__.py` 打包为 `onedir` 后端，并通过 `extraResources` 放入 Electron 的 `resources/backend`。安装器允许选择安装目录；发布应用首次启动时选择独立的运行根，自动派生 `data/cache/tmp/log`。

发布资源位于 `resources/backend`、`resources/defaults/config/config.toml`、`resources/defaults/workspace`。设置页展示安装目录、运行根与四个派生目录；后端不可用时顶部横幅提供重试、换运行根和打开日志操作。升级旧版数据时，迁移失败会阻止后端启动并显示诊断，旧源数据保留。迁移成功后的旧路径清理不可逆，升级前请备份旧数据目录。

## 常用脚本

- `npm run dev:web`：启动 Vite Web 开发服务。
- `npm run dev`：启动 Vite + Electron。
- `npm run type-check`：Vue / TypeScript 类型检查。
- `npm run perf:fixtures`：运行长历史和密集流式事件性能 fixture。
- `npm run build`：构建 Web 产物。
- `npm run electron:build`：构建桌面安装包。
- `npm run electron:build:mirror`：使用清华 npm 镜像和 npmmirror Electron 二进制镜像构建，适合网络受限环境。

Windows 构建会在开始前清理 `release` 输出目录；如果 electron-builder 在退出前遇到
`.tmp` 目录重命名锁，包装脚本会在子进程退出后自动恢复该目录，再继续执行资源校验。

如果仍出现 `EPERM`，优先确认没有残留的 `electron.exe` / 项目相关 `node.exe`，不要让资源管理器、IDE 索引器或同步软件打开 `release` 目录；必要时请将 `frontend/release`、`frontend/build` 加入 Windows Defender 排除项。

## 目录

```text
frontend/
├── src/
│   ├── api/          # REST / WebSocket 客户端
│   ├── components/   # 通用组件
│   ├── composables/  # 组合式逻辑
│   ├── views/        # Chat / Settings / Memory 等页面
│   ├── types/        # 前端类型
│   └── utils/        # Markdown、工具展示等
├── electron/         # Electron 主进程与 preload
├── scripts/          # 辅助脚本
└── package.json
```

## 后端连接

聊天状态快照使用 Agent canonical state（`scope`、`context`、任务、step、工具和 memory），
并以 `state_revision` 标识可恢复状态版本。WebSocket 与 SSE 共用该结构；消息增量、心跳等
临时事件不作为 checkpoint 状态。
`scope.run_id` 表示一轮用户指令；`task_id` 表示明确任务项，`step_id` 表示节点执行，
`request_id` 表示一次实际模型请求。三者不适用时为 `null`。传输 `event_id` 与
历史消息的 `message_id`、供应商 `vendor_message_id` 和工具 `tool_call_id` 各自独立。
历史 API 从新格式账本投影这些显式身份，按 `message_id` 去重。

开发模式默认连接 `http://localhost:8000`。Vite 会代理 `/api` 和 WebSocket 到后端；请先运行：

```bash
cd ../backend
uv run mailin serve --mode development
```
