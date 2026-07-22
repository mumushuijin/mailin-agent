# 本地 stdio MCP 管理指南

stdio 传输（子进程模式）**尚未在代码中实现**，但配置格式与目录约定已确定。本文说明用户如何自行下载 MCP 工具、放在哪、麦林如何找到并拉起子进程。

---

## 1. 总体模型

```
用户下载 / 安装 MCP Server（npm 包、Python 脚本、二进制）
        ↓
在 workspace/CONFIG.json 的 mcp_servers 中声明 command + args
        ↓
麦林启动时 discover_mcp_servers()
        ↓
spawn 子进程（stdin/stdout JSON-RPC）
        ↓
list_tools → 注册为 mcp_<server>_<tool>
        ↓
Agent 通过 MCP Client 调用
```

与云端 HTTP MCP 的区别：**Server 代码跑在用户机器上**，麦林只负责拉起进程和维护长连接。

---

## 2. MCP 工具放哪里

推荐三种方式，按常见程度排序：

### 方式 A：npx 按需拉取（推荐，无需手动下载）

适合已发布到 npm 的官方 MCP Server（如 `@modelcontextprotocol/server-filesystem`）。

```json
"mcp_servers": {
  "project_fs": {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "${WORKSPACE}"]
  }
}
```

- **存放位置**：由 `npx` 缓存到本机 npm cache（如 `%LOCALAPPDATA%\npm-cache`），用户无需关心
- **前提**：本机已安装 Node.js + npx

### 方式 B：git clone 到 workspace 固定目录（推荐用于自定义/私有 MCP）

约定目录：

```
workspace/
  mcp-servers/           ← 用户自行 clone 或解压 MCP 项目
    my-custom-mcp/
      server.py
      requirements.txt
      .venv/
  CONFIG.json
```

配置示例：

```json
"mcp_servers": {
  "my_custom": {
    "type": "stdio",
    "command": "${WORKSPACE}/mcp-servers/my-custom-mcp/.venv/Scripts/python.exe",
    "args": ["${WORKSPACE}/mcp-servers/my-custom-mcp/server.py"],
    "env": {
      "API_KEY": "${MY_API_KEY}"
    }
  }
}
```

- **存放位置**：`workspace/mcp-servers/<name>/`
- **管理方式**：用户自行 `git clone`、`pip install -r requirements.txt`、建 venv
- **麦林如何找到**：完全由 `CONFIG.json` 里的 `command` + `args` 绝对/相对路径决定

### 方式 C：系统 PATH 中的全局命令

适合已通过 `pip install`、`cargo install`、系统包管理器安装的工具。

```json
"mcp_servers": {
  "codex": {
    "command": "codex",
    "args": ["mcp-server"]
  }
}
```

- **存放位置**：系统 PATH 任意位置
- **麦林如何找到**：启动子进程时按 PATH 解析 `command`

---

## 3. CONFIG.json 配置格式（stdio）

| 字段 | 必填 | 说明 |
|------|------|------|
| `command` | 是 | 可执行文件路径或命令名 |
| `args` | 否 | 参数列表 |
| `env` | 否 | 传给子进程的环境变量（不继承完整 shell） |
| `enabled` / `isActive` | 否 | 默认 true |
| `tools.include` | 否 | 只注册列出的工具 |
| `tools.exclude` | 否 | 排除列出的工具 |
| `timeout` | 否 | 单次 call_tool 超时（秒） |

完整示例：

```json
"mcp_servers": {
  "github": {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": {
      "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
    },
    "tools": {
      "include": ["list_issues", "create_issue"]
    }
  }
}
```

---

## 4. 麦林如何 spawn 子进程（实现规划）

stdio 实现将放在 `server_task.py` 的 `_run_stdio_once()`，流程：

1. 解析 `command`（支持 `${WORKSPACE}`、`${ENV_VAR}` 插值）
2. 安全校验：拒绝 `|`、`&`、`;` 等 shell 注入
3. 构建子进程环境：`config.env` + 安全基线（PATH、SYSTEMROOT 等），**不传**完整父进程环境
4. 使用 MCP SDK `stdio_client(StdioServerParameters(...))` 建立连接
5. stderr 重定向到 `workspace/logs/mcp-stderr.log`（避免污染日志）
6. `initialize()` → `list_tools()` → 注册 ToolCard
7. 子进程随 `shutdown_mcp_servers()` 或应用退出而终止

---

## 5. 与云端 HTTP MCP 的对比

| | 本地 stdio | 云端 HTTP |
|--|-----------|-----------|
| 配置 | `command` + `args` | `url` + `headers` |
| 进程 | 麦林 spawn 子进程 | 无本地进程 |
| 工具代码位置 | 用户机器 | 远程服务器 |
| 典型场景 | 文件系统、本地 DB、私有工具 | ModelScope、Linear、SaaS API |
| 当前状态 | **待实现** | **已实现** |

---

## 6. 环境变量与密钥

敏感信息放 `workspace/.env`，在 CONFIG 中用 `${VAR}` 引用：

```env
GITHUB_TOKEN=ghp_xxx
MY_API_KEY=sk-xxx
```

```json
"env": {
  "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
}
```

---

## 7. 故障排查

| 现象 | 检查 |
|------|------|
| 工具未出现 | `command` 路径是否正确；`enabled` 是否为 true |
| 子进程立即退出 | 查看 `workspace/logs/mcp-stderr.log` |
| npx 失败 | `node --version`、`npx --version` |
| 工具被过滤 | 检查 `tools.include` / `tools.exclude` |
| 重载配置 | 重启后端，或调用 `reload_mcp_servers()` |

---

## 8. 后续：Catalog 一键安装（v0.2 规划）

仿 Hermes `optional-mcps/`，麦林计划在 `app/tools/mcp/catalog/` 维护 manifest：

```yaml
name: n8n
install:
  type: git
  url: https://github.com/...
  bootstrap:
    - python3 -m venv .venv
    - .venv/bin/pip install -r requirements.txt
transport:
  command: "${INSTALL_DIR}/.venv/bin/python"
  args: ["${INSTALL_DIR}/server.py"]
```

CLI：`mailin mcp install n8n` → 自动 clone 到 `workspace/mcp-servers/` 并写入 CONFIG。

v0.1 请手动按本文配置。
