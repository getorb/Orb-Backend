"""Calculator tool — safe math expression eval via Python AST."""

import ast
import logging
import math
import operator

from tool_registry import tool

log = logging.getLogger("orb.tools.calc")

# Only these AST node types are allowed. Anything else (Call, Attribute, Subscript, etc.)
# raises — so no arbitrary code execution.
_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

# Constants the model may reference by name
_NAMES = {
    "pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf,
}

# Single-arg math functions the model may use
_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "log": math.log, "log2": math.log2,
    "log10": math.log10, "exp": math.exp, "sin": math.sin, "cos": math.cos,
    "tan": math.tan, "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "floor": math.floor, "ceil": math.ceil, "round": round,
    "degrees": math.degrees, "radians": math.radians,
}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Num):  # py<3.8 fallback
        return node.n
    if isinstance(node, ast.Name):
        if node.id in _NAMES:
            return _NAMES[node.id]
        raise ValueError(f"unknown name '{node.id}'")
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"operator {type(node.op).__name__} not allowed")
        return op(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _UNARYOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unary {type(node.op).__name__} not allowed")
        return op(_eval_node(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ValueError(f"function {ast.dump(node.func)} not allowed")
        args = [_eval_node(a) for a in node.args]
        return _FUNCS[node.func.id](*args)
    raise ValueError(f"node type {type(node).__name__} not allowed")


@tool(
    name="calculate",
    description=(
        "Evaluate a math expression. Use whenever the user asks an arithmetic "
        "question — 'what's 18% of 240', 'square root of 1764', '2^32', 'sin(pi/4)'. "
        "Supports +, -, *, /, **, sqrt, sin/cos/tan, log, exp, abs, floor, ceil, "
        "round, and constants pi, e, tau. Convert percent questions to "
        "multiplication: '18% of 240' -> '0.18 * 240'."
    ),
    parameters={
        "expression": {
            "type": "string",
            "description": "Python-style math expression. No variables, no function calls outside the allowed list.",
        },
    },
    required=["expression"],
)
async def calculate(expression: str) -> str:
    expr = expression.strip()
    if not expr:
        return "What should I compute?"
    # Normalize common verbal forms the LLM might pass through
    expr = expr.replace("^", "**").replace("×", "*").replace("÷", "/").replace(",", "")
    try:
        tree = ast.parse(expr, mode="eval")
        result = _eval_node(tree)
    except ZeroDivisionError:
        return "Division by zero — that's undefined."
    except Exception as e:
        return f"I couldn't compute that — {e}."
    # Pretty-print: integers stay int, floats round to reasonable precision
    if isinstance(result, float):
        if result.is_integer():
            return f"{int(result)}."
        return f"{result:.6g}."
    return f"{result}."
