# Bootstraps

麦林的 Agent 自有空间设定。每轮对话会从这里读取人格与记忆并注入上下文（不受会话压缩影响）。

| 文件 | 用途 |
|------|------|
| `SOUL.md` | 产品身份、人格核心、交流风格、思考与行动原则 |
| `USER.md` | 用户信息与偏好（组装视图，源数据见 `user_sections.json`） |
| `MEMORY.md` | 长期记忆（跨会话沉淀；组装视图，源数据见 `memory_sections.json`） |
| `HEARTBEAT.md` | 周期性自检（可选） |
| `memory_sections.json` | 热层 MEMORY 分节源数据（固定 5 节，勿增删节） |
| `user_sections.json` | 热层 USER 分节源数据（固定 4 节，勿增删节） |

`MEMORY.md` / `USER.md` 由 JSON **自动组装**，请勿手改；意外修改后重启服务会自动对齐 JSON 或恢复快照（`bootstraps/.hot_memory_snapshots/`）。

项目规则请写在**项目工作区根目录**的 `AGENTS.md`，不要放在本目录。

运行时配置 `CONFIG.json` 位于 Agent 自有空间根目录。
