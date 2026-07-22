import ast
import operator
from pathlib import Path

from app.tools.card import ToolCard, make_card
from app.tools.packages.base import ToolPackage
from app.tools.packages.calculator.descriptions import TOOL_DOCS

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
}


class _SafeEval(ast.NodeVisitor):
    def visit(self, node):
        if isinstance(node, ast.Expression):
            return self.visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](self.visit(node.left), self.visit(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](self.visit(node.operand))
        raise ValueError("不支持的表达式")


def _python_calculator(expression: str) -> str:
    """安全计算数学表达式，支持 + - * / // % ** 和括号。

    不支持变量、函数调用或其他副作用。计算失败时返回错误说明。
    """
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        result = _SafeEval().visit(tree)
        return str(result)
    except Exception as e:
        return f"计算错误: {e}"


class CalculatorPackage(ToolPackage):
    def build_cards(self, config: dict | None = None) -> list[ToolCard]:
        doc = TOOL_DOCS["python_calculator"]
        return [
            make_card(
                package="calculator",
                name="python_calculator",
                handler=_python_calculator,
                summary=doc.summary,
                description=doc.description,
                display_name="计算器",
                display_icon="🔢",
            ),
        ]


PACKAGE = CalculatorPackage(Path(__file__).parent)
