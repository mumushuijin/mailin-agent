from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDoc:
    summary: str
    description: str


TOOL_DOCS: dict[str, ToolDoc] = {
    "python_calculator": ToolDoc(
        summary="安全计算数学表达式",
        description="""一句话功能：对工作区内的数学表达式做安全求值，返回数值结果字符串。

适用场景：
- 用户要求计算具体算式、单位换算、复合利息等纯算术问题
- 需要验证多步计算中间结果，避免心算错误
- 表达式仅含数字、运算符和括号

不适用场景：
- 需要符号推导、方程求解、微积分等代数运算 → 直接推理作答
- 需要调用外部 API 或查表（汇率、税率等实时数据）→ 用 web_search 或说明无法获取
- 表达式含变量、函数调用、字符串操作 → 本工具不支持

参数说明：
- expression（必填）：数学表达式，支持 + - * / // % ** 与括号；示例：`(1 + 2) * 3 ** 2`

返回格式：
- 成功：结果的字符串形式（整数或浮点）
- 失败：返回「计算错误: {原因}」

风险限制：
- 沙箱求值，不支持变量与函数，无副作用
- 除零等非法运算会返回错误说明""",
    ),
}
