# 事件运行时升级与回滚

本次升级改为事件账本，不自动迁移旧消息账本。请先保留完整备份，再用配套版本的前端和后端创建新会话。

## 当前格式

| 数据 | 当前版本 | 不兼容时的行为 |
| --- | --- | --- |
| `ledger_meta.json` 与 `ledger.jsonl` 事件信封 | `schema_version=3` | 拒绝读写旧会话，保留原文件 |
| Agent checkpoint | `schema_version=3` | 拒绝旧快照，不把它当成可恢复状态 |
| 独立模型窗口文档 | `schema_version=1` | 拒绝未知文档版本；校验窗口/压缩版本、来源游标和指纹 |
| 历史投影 | `version=4` | 拒绝旧投影，不用新版本覆盖它 |
| 持久时间轴事件 | `schema_version=3` | 客户端拒绝不支持的事件协议 |
| SSE / WebSocket 帧 | `stream_schema_version=3` | 客户端拒绝缺失或未知版本，不套用旧协议 |

窗口文档还携带 `window_schema_version`、`compression_version`、`source_event_cursor`、内容 SHA-256 和 bootstrap/reference 指纹。checkpoint 的事件游标、窗口的来源游标、`state_revision` 和 stream 的 `transport_seq` 各有用途，不可互换。临时 token chunk、心跳和普通进度不进入持久账本。

## 升级前备份

1. 停止发送消息和工具操作，正常退出桌面应用，并停止独立启动的后端进程。确认没有进程仍使用该运行根。
2. 确认实际运行根。默认开发目录为仓库下的 `.runtime`；自定义部署以 `MAILIN_RUNTIME_ROOT` 或启动时指定的运行根为准。项目 workspace 路径与全局运行根不同。
3. 完整复制运行根的 `data/` 到一个新的备份目录，并记录配套应用版本、备份时间和原运行根路径。至少必须备份 `data/agent-home/sessions/`，但同时备份整个 `data/` 才能保留配置、bootstrap 和记忆资源。
4. 验证备份目录可读取，并比较源与备份中的文件数量、大小或 SHA-256。保留原始备份，不让新版本直接对它运行。

会话目录包含共享的 `sessions/index.json`、`sessions/checkpoints.sqlite`，以及每个 `sessions/<session_id>/` 下的账本、引用内容、摘要、窗口和历史投影。不要只复制 `ledger.jsonl` 或某个会话子目录。

SQLite 的 `checkpoints.sqlite-wal` 和 `checkpoints.sqlite-shm` 若仍存在，也必须与 `checkpoints.sqlite` 一起保留；WAL 中可能包含尚未合并到主库的已提交数据。停止应用后复制整个目录可保持同一时点的一致性。不要在应用运行时逐个复制这三个文件，也不要为了备份手工删除 WAL。诊断 ZIP 面向单会话排查，不包含完整共享 checkpoint 数据库，不能替代升级备份。

## 升级后使用

启动新版本后，新建会话会写入当前版本的 `ledger_meta.json`，随后追加事件信封。新会话使用新的 session ID，与保留的旧目录隔离。

打开旧会话时会收到明确的格式不支持错误。缺少元数据的旧账本、旧版本元数据、旧 checkpoint 和旧投影都不会被悄悄升级。拒绝读写时原数据保持不变；不要手动修改版本字段来绕过校验，旧消息行并不因此具备事件协议要求的身份或来源信息。

新格式会话的读取与导出会校验账本引用资源。历史或其他分支的已记录资源缺失或损坏也会报告完整性错误；应从同一会话的完整备份恢复资源，不要删除事件或修改哈希来绕过校验。

检查一次新会话的发送、刷新后历史读取和工具结果展示，再决定是否清理旧会话。新版本保留旧数据不代表它能读取旧历史。

时间轴读取使用 `/api/session/<session_id>/timeline` 和 `/events` 的 `branch_id`、`after_cursor`；cursor 是服务端生成且绑定分支的不透明位置。历史继续通过聊天提交的 `branch_id` 与 `fork_cursor {branch_id,event_seq,event_id}` 创建子分支，每个分支使用独立 checkpoint。fork 只重建已记录消息；包含未配对工具调用的位置会拒绝继续，避免重新执行历史工具。默认聊天与旧 history API 展示 `main`。

## 用户主动清理旧会话

确认备份完整后，可在应用中删除指定旧会话；删除会同时移除会话索引项、该会话资源目录和对应 checkpoint 记录。需要全部重建时使用应用提供的清空会话功能，并确认其范围。

若无法通过应用清理，应先停止所有使用该运行根的进程，再把整个 `data/agent-home/sessions/` 目录移动到另一个归档位置，启动后创建新的会话存储。这会移出全部会话和共享 checkpoint 数据，而不仅是旧格式会话。保留归档用于回滚；不要手动拼接索引、数据库和不同时间点的会话子目录。

本文描述清理流程，不会自动执行删除或移动。

## 回滚边界

旧应用不能读取本次新增的事件账本和 checkpoint。回滚时先停止新版本，另行备份升级后的 `data/`，再恢复升级前的完整 `data/` 备份，并使用创建该备份的配套应用版本。恢复应作为同一组数据进行：会话目录、索引、checkpoint 数据库及其存在的 WAL/SHM、配置和上下文资源。

升级后新建的会话不会出现在升级前的备份中。保留升级后备份可在重新使用新版本时取回这些会话；没有跨版本合并或自动降级承诺。不要让两个版本同时打开同一个运行根，也不要仅降级应用而继续写入新格式数据。

## 验证覆盖

`backend/tests/test_event_runtime_versions.py` 覆盖旧/未知账本元数据、checkpoint、历史投影和窗口文档版本被拒绝且源数据不变，以及新会话使用当前格式。账本事件 schema、损坏资源和旧会话目录的补充覆盖分别位于 `test_event_ledger_contract.py`、`test_model_window.py` 和 `test_conversation_files.py`。
