# Bootstraps

麦林的引导与设定文件目录。每轮对话会从磁盘读取并注入上下文（不受会话压缩影响）。

| 文件 | 用途 |
|------|------|
| `IDENTITY.md` | 助手身份 |
| `USER.md` | 用户信息与偏好（组装视图，源数据见 `user_sections.json`） |
| `SOUL.md` | 人格核心、交流风格、思考与行动原则 |
| `MEMORY.md` | 长期记忆（跨会话沉淀；组装视图，源数据见 `memory_sections.json`） |
| `memory_sections.json` | 热层 MEMORY 分节源数据（固定 5 节，勿增删节） |
| `user_sections.json` | 热层 USER 分节源数据（固定 4 节，勿增删节） |

`MEMORY.md` / `USER.md` 由 JSON **自动组装**，请勿手改；意外修改后重启服务会自动对齐 JSON 或恢复快照（`bootstraps/.hot_memory_snapshots/`）。
| `AGENTS.md` | 工作区规则与工具约定 |
| `HEARTBEAT.md` | 周期性自检（可选） |
| `BOOTSTRAP.md` | 初始化引导说明 |

运行时配置 `CONFIG.json` 仍位于工作区根目录。
