from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDoc:
    summary: str
    description: str


TOOL_DOCS: dict[str, ToolDoc] = {
    "todo": ToolDoc(
        summary="维护当前会话的任务列表（pending / in_progress / completed）",
        description="""一句话功能：在本会话内增删改待办，状态只能是 pending、in_progress、completed。

适用场景：
- 长任务开始时列出步骤并在过程中更新
- 避免在长推理里忘掉计划

不适用场景：
- 跨会话长期记忆 → 用 memory 工具
- 向用户提问 → 用 ask_user

参数说明：
- action（必填）：list | add | update | complete
- content（add 必填；update 可选）：任务描述
- item_id（update/complete 必填）：list 返回的 id
- status（update 可选）：pending | in_progress | completed；非法值会被拒绝

返回格式：
- 当前全部待办的 id、status、content
- 非法 status 或找不到 id 时返回错误且不修改列表""",
    ),
    "ask_user": ToolDoc(
        summary="遇到歧义时向用户提问，或交还用户结束当前回合",
        description="""一句话功能：暂停当前回合，向用户提出澄清问题或交还控制权。不要猜测答案。

适用场景：
- 需求有多种合理解释（mode=answer_and_continue）
- 需要用户在几个方案中选择
- 缺关键信息无法安全执行
- 已给出足够信息，应停止并交还用户（mode=handoff_and_stop）

不适用场景：
- 高风险命令审批（系统会单独弹出允许/拒绝）
- 能靠读文件或跑测试自行确认的问题

参数说明：
- question（必填）：要问用户的问题或交还说明
- options（可选）：选项列表；为空则接受短文本（仅 answer_and_continue）
- allow_multiple（可选，默认 false）：是否允许多选
- mode（可选，默认 answer_and_continue）：
  - answer_and_continue：用户回答后继续本回合
  - handoff_and_stop：用户确认已看到信息后结束本回合，不执行后续副作用工具

返回格式：
- answer_and_continue：用户的选择或短回答
- handoff_and_stop：交还确认后的终止说明
- 用户取消/超时则返回错误，不要当作默认选项""",
    ),
}
