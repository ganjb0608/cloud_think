"""受限表达式求值器，用于 workflow.yaml 里的 ``if:`` 条件。

只走 AST 白名单，不用 ``eval`` 的完整语义：禁止属性调用、导入、推导式、
lambda、赋值。支持点号路径（``review.score`` -> ``state["review"]["score"]``）。
"""
from __future__ import annotations

import ast
from typing import Any, Mapping

from .errors import ExpressionError

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare,
    ast.Name, ast.Load, ast.Constant, ast.Attribute, ast.Subscript,
    ast.Call, ast.List, ast.Tuple, ast.Dict, ast.Set, ast.IfExp,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Is, ast.IsNot, ast.Slice,
)

_SAFE_FUNCS: dict[str, Any] = {
    "len": len, "int": int, "float": float, "str": str, "bool": bool,
    "min": min, "max": max, "abs": abs, "any": any, "all": all,
    "sum": sum, "sorted": sorted, "round": round,
}

_MISSING = object()


def _getpath(obj: Any, key: str) -> Any:
    """在 Mapping / 对象 / 序列上统一取值，取不到返回 None。"""
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


class _Evaluator(ast.NodeVisitor):
    def __init__(self, state: Mapping[str, Any]) -> None:
        self.state = state

    def visit(self, node: ast.AST) -> Any:
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(f"表达式中不允许的语法: {type(node).__name__}")
        return super().visit(node)

    def generic_visit(self, node: ast.AST) -> Any:
        raise ExpressionError(f"表达式中不允许的语法: {type(node).__name__}")

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in _SAFE_FUNCS:
            return _SAFE_FUNCS[node.id]
        if node.id == "state":
            return self.state
        val = self.state.get(node.id, _MISSING)
        if val is _MISSING:
            raise ExpressionError(f"表达式引用了未知 channel: {node.id!r}")
        return val

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        return _getpath(self.visit(node.value), node.attr)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        base = self.visit(node.value)
        idx = self.visit(node.slice)
        try:
            return base[idx]
        except (KeyError, IndexError, TypeError):
            return None

    def visit_Slice(self, node: ast.Slice) -> Any:
        lo = self.visit(node.lower) if node.lower else None
        hi = self.visit(node.upper) if node.upper else None
        st = self.visit(node.step) if node.step else None
        return slice(lo, hi, st)

    def visit_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_FUNCS:
            raise ExpressionError("表达式中只允许调用白名单函数: " + ", ".join(sorted(_SAFE_FUNCS)))
        if node.keywords:
            raise ExpressionError("表达式中的函数调用不支持关键字参数")
        return _SAFE_FUNCS[node.func.id](*[self.visit(a) for a in node.args])

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = self.visit(v)
                if not result:
                    return result
            return result
        result = False
        for v in node.values:
            result = self.visit(v)
            if result:
                return result
        return result

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        val = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return not val
        if isinstance(node.op, ast.USub):
            return -val
        return +val

    _BINOPS = {
        ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
        ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
        ast.Pow: lambda a, b: a ** b,
    }

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        fn = self._BINOPS.get(type(node.op))
        if fn is None:
            raise ExpressionError(f"不支持的运算符: {type(node.op).__name__}")
        return fn(self.visit(node.left), self.visit(node.right))

    _CMPOPS = {
        ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
        ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
        ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
        ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
        ast.Is: lambda a, b: a is b, ast.IsNot: lambda a, b: a is not b,
    }

    def visit_Compare(self, node: ast.Compare) -> Any:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            fn = self._CMPOPS.get(type(op))
            if fn is None:
                raise ExpressionError(f"不支持的比较运算: {type(op).__name__}")
            # None 参与大小比较在 Python 里会抛 TypeError，这里统一判为 False，
            # 避免 state 还没填充时条件边直接炸掉整个 run。
            if right is None or left is None:
                if type(op) not in (ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn):
                    return False
            if not fn(left, right):
                return False
            left = right
        return True

    def visit_List(self, node: ast.List) -> Any:
        return [self.visit(e) for e in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> Any:
        return tuple(self.visit(e) for e in node.elts)

    def visit_Set(self, node: ast.Set) -> Any:
        return {self.visit(e) for e in node.elts}

    def visit_Dict(self, node: ast.Dict) -> Any:
        return {self.visit(k): self.visit(v) for k, v in zip(node.keys, node.values)}

    def visit_IfExp(self, node: ast.IfExp) -> Any:
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)


def safe_eval(expr: str, state: Mapping[str, Any]) -> Any:
    """在 ``state`` 上求值受限表达式。"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"表达式语法错误 {expr!r}: {e}") from e
    return _Evaluator(state).visit(tree)


def validate_expr(expr: str, channels: set[str]) -> None:
    """编译期校验：语法合法，且引用的顶层名字都是已声明的 channel。"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"表达式语法错误 {expr!r}: {e}") from e
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in channels \
                and node.id not in _SAFE_FUNCS and node.id != "state":
            raise ExpressionError(f"表达式 {expr!r} 引用了未声明的 channel: {node.id!r}")
