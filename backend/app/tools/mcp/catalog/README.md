# MCP Catalog（v0.2）

仿 Hermes `optional-mcps/`：仅存放 **manifest 清单**，不打包 Server 代码。

每个条目 `catalog/<name>/manifest.yaml` 描述：

- `transport`：stdio command/args 或 http url
- `install`：git clone + bootstrap（可选）
- `auth`：api_key / oauth / none
- `tools.default_enabled`：安装时默认勾选的工具

CLI 规划：

```bash
mailin mcp catalog
mailin mcp install <name>
mailin mcp configure <name>
```

v0.1 用户请直接在 `workspace/CONFIG.json` 的 `mcp_servers` 段手写配置。
