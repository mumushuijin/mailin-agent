<div align="center">

<img src="./cover.png" alt="麦林 Mailin 界面预览" width="820" />

# 🌾 麦林 Mailin

**本地优先的个人 AI Agent 应用**

多轮对话 · 工具调用 · 记忆沉淀 · 配置化管理

[![Status](https://img.shields.io/badge/status-开发中-orange.svg)](#-项目状态)
[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](#)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.11-3776AB.svg?logo=python&logoColor=white)](#)
[![Vue](https://img.shields.io/badge/Vue-3.5-42b883.svg?logo=vuedotjs&logoColor=white)](#)
[![Electron](https://img.shields.io/badge/Electron-35-47848F.svg?logo=electron&logoColor=white)](#)
[![License](https://img.shields.io/badge/license-未定-lightgrey.svg)](#)

</div>

---

> [!WARNING]
> ## 🚧 项目状态：开发中（Work In Progress）
>
> 麦林当前处于 **早期开发阶段（v0.1.0）**，功能与接口仍在快速迭代，可能存在不兼容变更、未完成模块以及不稳定行为。
>
> - ⚠️ **API / 配置结构可能随时调整**，暂不保证向后兼容。
> - ⚠️ **部分子系统仍为 MVP**（如评估套件、多 Agent 协作等，详见 [已知限制](#-已知限制与路线图)）。
> - ⚠️ 仅面向 **本地单机场景**，无用户认证与多用户支持，请勿用于生产环境。
> - ✅ 欢迎试用、反馈与共建，但请谨慎在重要数据上运行。

---

## ✨ 项目简介

**麦林 Mailin** 是一款面向本地的 AI Agent 应用，采用前后端分离架构：后端负责 Agent 推理、工具执行与会话持久化，前端提供 **Web 与 Electron 桌面双形态** 交互界面。数据完全本地存储，无外部数据库依赖。

| 维度 | 说明 |
|------|------|
| 定位 | 本地优先的个人 AI 助手，支持多轮对话、工具调用、记忆沉淀与配置化管理 |
| 架构模式 | 前后端分离 + WebSocket / SSE 实时通信 |
| 部署形态 | Web 开发模式 / Electron 桌面安装包（Windows NSIS、macOS DMG、Linux AppImage） |
| 数据存储 | 本地文件系统工作区 + SQLite Checkpoint |

```
┌─────────────────────────────────────────────────────────────────────┐
│                         麦林 Mailin 系统                              │
├──────────────────────────────┬──────────────────────────────────────┤
│         frontend/            │              backend/                 │
│  Vue 3 + Vite + Electron     │  FastAPI + LangGraph + LangChain      │
│  Ant Design Vue              │  SQLite Checkpoint + 本地工作区        │
├──────────────────────────────┴──────────────────────────────────────┤
│  通信：REST API · SSE 流式 · WebSocket（聊天 / 审批 / 后台推送）       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 🎯 核心特性

- 🧠 **ReAct Agent 内核** — 基于 LangGraph `StateGraph` 的 Reasoning + Acting 循环，支持工具调用与人工审批中断。
- 🗂️ **三层记忆系统** — 热层（每日工作记忆）→ 温层（沉淀）→ 冷层（长期记忆 `MEMORY.md`），后台自动 consolidate。
- 📏 **上下文引擎** — Token 预算分配 + 四层压缩策略，超限自动摘要，超大工具结果落盘引用。
- 🔧 **可插拔工具系统** — 文件系统、Shell（含安全守卫与审批）、记忆、计算器、时间、Web 搜索、MCP 集成。
- 🔌 **MCP 集成** — 对接外部 Model Context Protocol 服务器，含健康检查与状态推送。
- ⚡ **实时通信** — WebSocket 流式对话（主路径）+ SSE 降级，30s 心跳与指数退避重连。
- 🛡️ **韧性设计** — 统一的超时 / 重试（tenacity）/ 熔断（pybreaker）包装。
- 🖥️ **桌面 + Web 双形态** — Electron 自定义标题栏无边框窗口，或纯浏览器访问。

---

## 🧰 技术栈

### 后端

| 类别 | 技术 | 版本要求 |
|------|------|----------|
| 语言 | Python | ≥ 3.11 |
| Web 框架 | FastAPI + Uvicorn | ≥ 0.115 / ≥ 0.32 |
| Agent 框架 | LangGraph + LangChain Core | ≥ 1.0 / ≥ 0.3 |
| LLM 接入 | langchain-openai（OpenAI 兼容 API） | ≥ 0.3 |
| 状态持久化 | langgraph-checkpoint-sqlite | ≥ 2.0 |
| 数据校验 | Pydantic + pydantic-settings | ≥ 2.0 |
| 韧性 | tenacity + pybreaker | — |
| 日志 | structlog | ≥ 24.0 |
| 包管理 | uv | — |

### 前端

| 类别 | 技术 | 版本 |
|------|------|------|
| 框架 | Vue 3（Composition API） | ^3.5 |
| 构建 | Vite | ^7.3 |
| UI 库 | Ant Design Vue | ^4.2 |
| 路由 | Vue Router | ^5.0 |
| 桌面壳 | Electron + electron-builder | ^35 / ^26 |
| 语言 | TypeScript | ~5.9 |
| Node.js | — | ^20.19 或 ≥ 22.12 |

---

## 📁 仓库结构

```
mailin125/
├── cover.png                   # 项目封面图
├── .docs/                      # 项目文档（含《技术完整介绍》）
├── .cursor/                    # Cursor Agent Skills 配置
├── backend/                    # Python 后端
│   ├── app/                    # 应用核心代码
│   │   ├── main.py             # FastAPI 入口与生命周期
│   │   ├── api/                # REST / WebSocket 路由
│   │   ├── agent/              # LangGraph ReAct 图
│   │   ├── context/            # 上下文引擎与记忆子系统
│   │   ├── tools/              # 工具注册与内置工具包
│   │   ├── services/           # 业务服务层
│   │   ├── storage/            # 工作区与 Checkpoint
│   │   ├── maintenance/        # 后台维护调度
│   │   ├── resilience/         # 超时 / 重试 / 熔断
│   │   ├── core/               # 设置、LLM、日志、遥测
│   │   └── schemas/            # Pydantic 请求/响应模型
│   ├── cli/                    # `mailin serve` / `mailin eval` CLI
│   ├── eval/                   # 评估子系统
│   ├── tests/                  # pytest 测试套件
│   ├── workspace/              # 运行时工作区（首次启动从 defaults 复制）
│   └── workspace_defaults/     # 工作区默认模板
└── frontend/                   # Vue + Electron 前端
    ├── src/                    # 页面 / API 客户端 / 组件 / 主题
    ├── electron/               # Electron 主进程与 preload
    └── package.json
```

---

## 🚀 快速开始

### 环境要求

- Python ≥ 3.11 与 [uv](https://github.com/astral-sh/uv)
- Node.js ^20.19 或 ≥ 22.12
- 一个 OpenAI 兼容的 LLM API Key（如 OpenAI、阿里云 DashScope 等）

### 1. 启动后端

```bash
cd backend

# 安装依赖
uv sync

# 可选：MCP 支持
uv sync --extra mcp

# 配置 LLM
cp .env.example .env
# 编辑 .env，填入 API Key（OPENAI_API_KEY 或 DASHSCOPE_API_KEY）

# 启动服务（默认 http://localhost:8000）
uv run mailin serve

# 或开发热重载
uv run mailin serve --reload
```

### 2. 启动前端

```bash
cd frontend

# 安装依赖
npm install

# Web 开发模式（浏览器访问）
npm run dev:web

# Electron 桌面开发
npm run dev

# 构建桌面安装包
npm run electron:build
```

### 3. 联调

```bash
# 终端 1：后端
cd backend && uv run mailin serve

# 终端 2：前端
cd frontend && npm run dev:web
```

前端 Vite 代理会将 `/api` 与 WebSocket 转发到 `http://localhost:8000`。

---

## 🔑 环境变量

后端 `.env`（关键项）：

| 变量 | 必填 | 说明 |
|------|------|------|
| `OPENAI_API_KEY` | 二选一 | OpenAI 或兼容 API 密钥 |
| `OPENAI_BASE_URL` | 否 | 兼容 API 地址 |
| `DASHSCOPE_API_KEY` | 二选一 | 阿里云 DashScope 密钥 |
| `TAVILY_API_KEY` | 否 | Tavily 搜索（未设置回退 DuckDuckGo） |
| `HOST` / `PORT` | 否 | 默认 `0.0.0.0:8000` |
| `LOG_LEVEL` / `LOG_FORMAT` | 否 | 默认 `INFO` / `console` |

> 完整环境变量与工作区 `CONFIG.json` 说明见 [`.docs/技术介绍.md`](./.docs/技术介绍.md)。

---

## 🧪 测试与评估

```bash
cd backend

# 单元测试
uv run pytest

# 评估套件（依赖 uv sync --extra eval）
uv run mailin eval --suite smoke
```

---

## 🗺️ 已知限制与路线图

| 项目 | 现状 |
|------|------|
| 用户认证 | ❌ 无（本地单机场景） |
| 多用户 | ❌ 不支持 |
| 云同步 | ❌ 不支持，数据纯本地 |
| 评估系统 | 🚧 MVP 阶段，retrieval 套件待完善 |
| 多 Agent 协作 | 🚧 引导文件已预留（`AGENTS.md`），运行时未实现 |
| API / 配置稳定性 | 🚧 开发中，可能存在不兼容变更 |

---

## 📚 相关文档

| 文档 | 路径 |
|------|------|
| 技术完整介绍 | [`.docs/技术介绍.md`](./.docs/技术介绍.md) |
| 后端 README | [`backend/README.md`](./backend/README.md) |
| Bootstrap 说明 | `backend/workspace_defaults/bootstraps/README.md` |
| MCP 工具目录 | `backend/app/tools/mcp/catalog/README.md` |

---

<div align="center">

**麦林 Mailin** · 版本 0.1.0 · 🚧 开发中

*本项目仍在积极开发，文档与代码可能随时更新，如有出入请以实际代码为准。*

</div>
