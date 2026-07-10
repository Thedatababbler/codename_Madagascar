"""First-batch BBEH tools for CodeAgent (no network search)."""

from __future__ import annotations

import ast
import operator
from typing import Any

from orchestra.tools.base import ToolBuildContext, ToolRegistry

BBEH_TOOL_IDS = ("python_math", "calculator", "final_answer")

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _eval_ast(node: ast.AST) -> float | int:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_eval_ast(node.left), _eval_ast(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_eval_ast(node.operand))
    raise ValueError("Unsupported expression")


def _safe_eval(expression: str) -> str:
    tree = ast.parse(expression, mode="eval")
    value = _eval_ast(tree)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _build_smolagents_tools() -> dict[str, Any]:
    from smolagents import FinalAnswerTool, tool

    @tool
    def calculator(expression: str) -> str:
        """Evaluate a basic arithmetic expression.

        Args:
            expression: Arithmetic expression using +, -, *, /, //, %, ** and parentheses.
        """
        try:
            return _safe_eval(expression)
        except Exception as exc:  # noqa: BLE001 - surface tool errors to agent
            return f"ToolError: {type(exc).__name__}: {exc}"

    @tool
    def python_math(expression: str) -> str:
        """Evaluate a Python arithmetic expression for math reasoning.

        Args:
            expression: Expression limited to numeric literals and arithmetic operators.
        """
        try:
            return _safe_eval(expression)
        except Exception as exc:  # noqa: BLE001
            return f"ToolError: {type(exc).__name__}: {exc}"

    return {
        "calculator": calculator,
        "python_math": python_math,
        "final_answer": FinalAnswerTool(),
    }


def register_bbeh_tools(registry: ToolRegistry) -> None:
    def _factory(tool_id: str):
        def build(_context: ToolBuildContext) -> Any:
            return _build_smolagents_tools()[tool_id]

        return build

    for tool_id in BBEH_TOOL_IDS:
        registry.register(tool_id, _factory(tool_id))
