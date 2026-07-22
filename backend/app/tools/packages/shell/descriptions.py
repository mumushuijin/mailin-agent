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
- timeout（可选，秒）：超时时间，默认见 CONFIG.json tools.shell.default_timeout
- workdir（可选）：相对 workspace 的工作目录，默认 tools.shell.workdir 或 "."

返回格式：
- 成功：命令 stdout/stderr 合并输出；无输出时返回提示
- 失败：中文错误说明或退出码
- 输出过大时自动截断，完整内容落盘到 tool_results/，可用 read_file 分页读取

风险限制：
- 默认需要用户确认（requires_confirmation）
- CONFIG.json 中 auto_approve_patterns 可匹配只读安全命令自动放行
- hardline 危险命令（如 rm -rf /）会被钩子直接拦截
- 子进程环境已过滤 LLM API Key 等凭证""",
    ),
}
