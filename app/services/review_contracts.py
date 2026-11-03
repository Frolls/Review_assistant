"""Check explicit numeric input/output examples with a bounded AST interpreter.

Never compile/exec user code, import its modules, follow attributes or perform
I/O. Unsupported programs/examples are left unassessed, not called correct.
"""
import ast
import json
import math
import operator
import re

_NUMBER = r"-?\d+(?:\.\d+)?"
_EXAMPLE = re.compile(
    rf"(?P<input>\[\s*{_NUMBER}(?:\s*,\s*{_NUMBER})*\s*\]|{_NUMBER})\s*"
    rf"(?:должн[оаы]?\s+(?:давать|дать|возвращать|вернуть)|ожидается|->|=>|→)\s*(?P<expected>{_NUMBER})",
    re.I,
)
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}


def _bounded(value):
    if type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e100:
        return value
    if isinstance(value, list) and len(value) <= 100:
        return [_bounded(v) for v in value]
    raise ValueError("Unsupported value")


def _expression(node, bindings):
    if isinstance(node, ast.Name) and node.id in bindings:
        return bindings[node.id]
    if isinstance(node, ast.Constant):
        return _bounded(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _expression(node.operand, bindings)
        return _bounded(-value if isinstance(node.op, ast.USub) else +value)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _expression(node.left, bindings), _expression(node.right, bindings)
        if type(left) not in (int, float) or type(right) not in (int, float):
            raise ValueError("Only numeric operators")
        return _bounded(_OPS[type(node.op)](left, right))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords and len(node.args) == 1:
        name = node.func.id
        if name in bindings:
            raise ValueError("Shadowed callable")
        argument = _expression(node.args[0], bindings)
        functions = {"sum": sum, "len": len, "min": min, "max": max, "abs": abs}
        if name in functions:
            return _bounded(functions[name](argument))
    raise ValueError("Unsupported expression")


def evaluate(code, value):
    if len(code) > 64000:
        raise ValueError("Program too large")
    tree = ast.parse(code)
    if len(list(ast.walk(tree))) > 200:
        raise ValueError("Program too large")
    # Module assignments/imports/decorators/defaults could change the meaning of
    # builtins. Accept only one plain, single-argument function in isolation.
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError("Unsupported module")
    function = tree.body[0]
    args = function.args
    positional = args.posonlyargs + args.args
    if (len(positional) != 1 or args.defaults or args.kwonlyargs or args.vararg or args.kwarg
            or function.decorator_list):
        raise ValueError("Unsupported signature")
    body = function.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        raise ValueError("Unsupported body")
    return _expression(body[0].value, {positional[0].arg: _bounded(value)})


def examples(task):
    if len(task) > 20000:
        return []
    result = []
    for match in list(_EXAMPLE.finditer(task))[:10]:
        try:
            value = _bounded(json.loads(match["input"]))
            expected = _bounded(json.loads(match["expected"]))
        except (ValueError, TypeError, OverflowError):
            continue
        result.append({"input": value, "expected": expected, "quote": match[0]})
    return result


def diagnostics(code, task):
    failures = []
    for example in examples(task):
        try:
            actual = evaluate(code, example["input"])
        except (ValueError, TypeError, SyntaxError, ArithmeticError):
            continue
        if type(actual) in (int, float) and not math.isclose(actual, example["expected"], rel_tol=1e-9, abs_tol=1e-9):
            failures.append({**example, "actual": actual})
    if not failures:
        return []
    first = failures[0]
    return [{"rule": "task-example", "line": 1, "examples": examples(task),
             "message": f"Нарушен пример из задачи: для `{first['input']}` ожидается {first['expected']}, выражение возвращает {first['actual']}."}]


def fix_passes(original, fixed, task):
    cases = examples(task)
    if not cases:
        return False
    try:
        # A literal answer to a single example must not replace a function of
        # its input. Reject solutions that stop referring to the parameter.
        before, after = ast.parse(original).body[0], ast.parse(fixed).body[0]
        parameter = (before.args.posonlyargs + before.args.args)[0].arg
        if ast.dump(before.args) != ast.dump(after.args) or before.name != after.name:
            return False
        if not any(isinstance(n, ast.Name) and n.id == parameter for n in ast.walk(after.body[-1])):
            return False
        return all(math.isclose(evaluate(fixed, c["input"]), c["expected"], rel_tol=1e-9, abs_tol=1e-9) for c in cases)
    except (ValueError, TypeError, SyntaxError, ArithmeticError, AttributeError, IndexError):
        return False


def replace_return_expression(code, expression):
    """Only change the expression of an already supported single-return function."""
    if len(expression) > 4000:
        raise ValueError("Expression too large")
    tree = ast.parse(code)
    body = tree.body[0].body
    if len(tree.body) != 1 or not isinstance(body[-1], ast.Return):
        raise ValueError("Unsupported function")
    body[-1].value = ast.parse(expression, mode="eval").body
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"
