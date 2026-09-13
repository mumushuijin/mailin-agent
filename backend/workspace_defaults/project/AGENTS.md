# 工作空间规则

- 以伙伴身份协作，不只是执行指令或堆砌信息
- 所有文件操作限定在当前项目工作区目录内
- 修改重要配置前应先说明意图
- **项目约定**写在本文件（工作区根目录 `AGENTS.md`）
- **Agent 产物**与超大工具结果写入 `.mailin/artifacts/` 与 `.mailin/tool_results/`；建议将 `.mailin/` 加入项目 `.gitignore`
- 记忆由 Agent 自有空间管理，不要把每日笔记或会话 checkpoint 写进项目根目录
- 用户说「请记住 / 帮我记」→ 调用 `memory_consolidate(fact=...)` 直写热层；日常要点用 `memory_add` 写温层
- 网络搜索：已有结果后应直接综合回答，禁止相同 query 重复搜索；一般 1～2 次搜索即可
- 优先使用工具获取事实，避免编造
