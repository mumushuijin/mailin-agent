"""上下文压缩层级常量与说明。

## 正常管线（assemble_context → compress_working_messages）

| 层 | 常量 | 触发条件 | 说明 |
|----|------|----------|------|
| A | TAIL_TOOL_SUMMARY | 尾部窗口内存在超大工具结果时几乎总是触发 | 模型摘要（首选）+ 路径落盘引用 |
| B | OLD_TOOL_ONELINE | 存在 20k tokens 尾部窗口以外的历史时触发 | 旧工具结果一行可读摘要 + 去重 |
| C | MIDDLE_SUMMARY | 混合信号或工作上下文总量达到上限时触发 | 中间历史分块结构化 LLM 摘要并合并 |
| D | REJECT | 达到最大压缩次数或各层仍无法满足预算 | 提示用户换新会话 |

## 独立兜底

| 入口 | 触发条件 | 说明 |
|------|----------|------|
| emergency_compress | LLM API 返回上下文超长（413 等） | 强摘要 + 仅保留最末关键消息 |
"""

from __future__ import annotations

from enum import IntEnum


class CompressionLayer(IntEnum):
    TAIL_TOOL_SUMMARY = 1
    OLD_TOOL_ONELINE = 2
    MIDDLE_SUMMARY = 3
    REJECT = 4
    EMERGENCY = 5


LAYER4_REJECT_MESSAGE = (
    "上下文已满，请新建会话或清理历史。可查看 memory/ 目录中已沉淀的每日记忆。"
)
