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

## 常用脚本

- `npm run dev:web`：启动 Vite Web 开发服务。
- `npm run dev`：启动 Vite + Electron。
- `npm run type-check`：Vue / TypeScript 类型检查。
- `npm run perf:fixtures`：运行长历史和密集流式事件性能 fixture。
- `npm run build`：构建 Web 产物。
- `npm run electron:build`：构建桌面安装包。

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

开发模式默认连接 `http://localhost:8000`。Vite 会代理 `/api` 和 WebSocket 到后端；请先运行：

```bash
cd ../backend
uv run mailin serve
```
