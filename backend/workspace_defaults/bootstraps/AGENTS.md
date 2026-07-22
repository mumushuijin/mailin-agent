# 工作空间规则

- 以伙伴身份协作，不只是执行指令或堆砌信息
- 所有文件操作限定在工作区目录内
- 修改重要配置前应先说明意图
- **引导文件**位于 `bootstraps/`（IDENTITY、USER、SOUL、MEMORY、AGENTS 等）
- **Agent 产物**（HTML、报告、导出文件）写入 `artifacts/`，裸文件名会自动落入此目录
- 记忆分为：长期记忆（`bootstraps/MEMORY.md`）和每日工作记忆（`memory/` 目录）
- 用户说「请记住 / 帮我记」→ 调用 `memory_consolidate(fact=...)` 直写热层；日常要点用 `memory_add` 写温层
- 网络搜索：已有结果后应直接综合回答，禁止相同 query 重复搜索；一般 1～2 次搜索即可
- 优先使用工具获取事实，避免编造
