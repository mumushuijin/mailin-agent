from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDoc:
    summary: str
    description: str


TOOL_DOCS: dict[str, ToolDoc] = {
    "run_shell": ToolDoc(
        summary="在工作区内执行 shell 命令（构建、git、包管理等）",
        description="""一句话功能：在 workspace 沙箱内执行一条 shell 命令，用于构建、测试、git、包管理等终端操作。

适用场景：
- 运行 npm/pip/cargo 等构建或测试命令
- git status / git diff / git log 等版本管理
- 启动短时脚本或查看 CLI 工具输出

不适用场景（请用结构化工具）：
- 读取文件内容 → read_file（热工具）
- 写入或编辑文件 → write_file / replace_in_file
- 列出目录 → list_directory
- 搜索文件内容 → search_files
- 直接访问 .env、bootstraps/、sessions/ 等敏感路径

参数说明：
- command（必填）：要执行的 shell 命令字符串
- timeout（可选，秒）：前台超时时间，默认见 CONFIG.json tools.shell.default_timeout
- workdir（可选）：相对 workspace 的工作目录，默认 tools.shell.workdir 或 "."
- background（可选，默认 false）：true 时立即返回作业 id，用 process 查询/等待/终止

返回格式：
- 成功：命令 stdout/stderr 合并输出；无输出时返回提示
- 后台：返回作业 id，不阻塞
- 失败：中文错误说明或退出码
- 输出过大时自动截断，完整内容落盘到 tool_results/，可用 read_file 分页读取

风险限制：
- 默认需要用户确认（requires_confirmation）
- CONFIG.json 中 auto_approve_patterns 可匹配只读安全命令自动放行
- hardline 危险命令（如 rm -rf /）会被钩子直接拦截
- 子进程环境已过滤 LLM API Key 等凭证
- 后台作业仅当前会话可见，取消会话会终止仍在运行的作业""",
    ),
    "process": ToolDoc(
        summary="列出、等待或终止当前会话的后台 shell 作业",
        description="""一句话功能：管理本会话由 run_shell(background=true) 启动的后台作业。

适用场景：
- 查看仍在跑的 dev server / 长任务
- 等待作业结束并取输出
- 杀掉失控的后台进程

不适用场景：
- 前台短命令 → 直接 run_shell
- 操作其他会话的进程（会被拒绝）

参数说明：
- action（必填）：list | wait | kill
- job_id（wait/kill 必填）：list 返回的作业 id
- timeout（可选，wait 等待秒数，默认 30，上限 300）

返回格式：
- list：每行 job_id、status、command
- wait：作业输出与退出码；超时则提示仍在运行
- kill：确认已终止

风险限制：
- 只能看见和操作当前会话的作业
- 不会跨会话 kill""",
    ),
}
