"""Conservative, executable checks used as candidates for grounded review.

This is deliberately a stated subset, not a proof of program correctness.
No user code is executed. Ruff reads stdin; YAML is parsed with safe_load.
"""
import asyncio
import ast
import copy
import json
import re
import shlex
import sys

import yaml

PYTHON_CHECKS = "B006,B012,E722,F821,F822,F823,S602,SIM115"


async def analyze(code: str, task: str, language: str | None) -> dict | None:
    if language not in {"python", "ansible"}:
        return None
    if language == "python":
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache",
            "--select", PYTHON_CHECKS, "--output-format", "json", "--stdin-filename", "review.py", "-",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(code.encode()), timeout=5)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode not in {0, 1}:
            raise RuntimeError("Ruff review checks are unavailable")
        diagnostics = [{"id": str(i), "rule": d["code"], "message": d["message"],
                        "line": d["location"]["row"]} for i, d in enumerate(json.loads(stdout))]
        # The selected lint rules are not a Python syntax checker.
        try:
            tree = ast.parse(code)
            for diagnostic in diagnostics:
                if diagnostic["rule"] == "F821":
                    match = re.search(r"`([A-Za-z_]\w*)`", diagnostic["message"])
                    name = match[1] if match else ""
                    if name in {"json", "math", "statistics", "decimal", "collections", "itertools", "re", "pathlib", "datetime"} and any(
                            isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == name
                            for n in ast.walk(tree)):
                        updated = copy.deepcopy(tree)
                        insertion = 1 if (updated.body and isinstance(updated.body[0], ast.Expr)
                                          and isinstance(updated.body[0].value, ast.Constant)
                                          and isinstance(updated.body[0].value.value, str)) else 0
                        while insertion < len(updated.body) and isinstance(updated.body[insertion], ast.ImportFrom) and updated.body[insertion].module == "__future__":
                            insertion += 1
                        updated.body.insert(insertion, ast.Import(names=[ast.alias(name=name)]))
                        diagnostic["suggested_fix"] = ast.unparse(ast.fix_missing_locations(updated)) + "\n"
                if diagnostic["rule"] == "B012":
                    updated = _RemoveFinallyReturn(diagnostic["line"]).visit(copy.deepcopy(tree))
                    if ast.dump(updated) != ast.dump(tree):
                        diagnostic["suggested_fix"] = ast.unparse(ast.fix_missing_locations(updated)) + "\n"
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and node.args and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"):
                    continue
                argument = node.args[0]
                shell = next((kw for kw in node.keywords if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True), None)
                if not (shell and isinstance(argument, ast.BinOp) and isinstance(argument.op, ast.Add)
                        and isinstance(argument.left, ast.Constant) and isinstance(argument.left.value, str)
                        and isinstance(argument.right, ast.Name)):
                    continue
                prefix = argument.left.value
                words = shlex.split(prefix) if re.fullmatch(r"[\w./\s-]+", prefix) else []
                if not words or words[-1] != "--" or not prefix[-1:].isspace():
                    continue
                argv = ast.List(elts=[*(ast.Constant(value=w) for w in words), argument.right], ctx=ast.Load())
                fix = _replace_nodes(code, [(argument, ast.unparse(argv)), (shell.value, "False")])
                for diagnostic in diagnostics:
                    if diagnostic["rule"] == "S602" and diagnostic["line"] == node.lineno:
                        diagnostic["suggested_fix"] = fix
            exception_shadowed = any((isinstance(n, ast.Name) and n.id == "Exception" and isinstance(n.ctx, ast.Store))
                                    or (isinstance(n, ast.arg) and n.arg == "Exception")
                                    or (isinstance(n, ast.alias) and (n.asname or n.name) == "Exception")
                                    or (isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == "Exception") for n in ast.walk(tree))
            if not exception_shadowed:
                for handler in ast.walk(tree):
                    if (isinstance(handler, ast.ExceptHandler) and handler.type is None
                            and len(handler.body) == 1 and isinstance(handler.body[0], ast.Return)
                            and (handler.body[0].value is None or isinstance(handler.body[0].value, ast.Constant))):
                        for diagnostic in diagnostics:
                            if diagnostic["rule"] == "E722" and diagnostic["line"] == handler.lineno:
                                lines = code.splitlines(keepends=True)
                                lines[handler.lineno - 1] = re.sub(r"^(\s*)except\s*:", r"\1except Exception:", lines[handler.lineno - 1])
                                diagnostic["suggested_fix"] = "".join(lines)
            shadowed_open = any((isinstance(n, ast.Name) and n.id == "open" and isinstance(n.ctx, ast.Store))
                                or (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == "open")
                                or (isinstance(n, ast.arg) and n.arg == "open")
                                or (isinstance(n, ast.alias) and (n.asname or n.name) == "open") for n in ast.walk(tree))
            if not shadowed_open:
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr in {"read", "readline", "readlines", "write", "writelines"}
                            and isinstance(node.func.value, ast.Call)
                            and isinstance(node.func.value.func, ast.Name) and node.func.value.func.id == "open"):
                        diagnostics.append({"id": str(len(diagnostics)), "rule": "OPEN_READ",
                                            "message": "Результат open() используется без гарантии закрытия файла.", "line": node.lineno})
        except SyntaxError as exc:
            diagnostics.append({"id": str(len(diagnostics)), "rule": "syntax", "message": exc.msg, "line": exc.lineno or 1})
        from app.services.review_contracts import diagnostics as contract_diagnostics
        for diagnostic in contract_diagnostics(code, task):
            diagnostics.append({"id": str(len(diagnostics)), **diagnostic})
        return {"language": language, "scope": "Python: " + PYTHON_CHECKS + ", OPEN_READ, syntax; поддерживаемые числовые примеры из задачи", "diagnostics": diagnostics}

    try:
        document = yaml.safe_load(code)
    except yaml.YAMLError:
        return {"language": language, "scope": "Ansible: YAML syntax", "diagnostics": [
            {"id": "0", "rule": "syntax", "message": "Некорректный YAML.", "line": 1}]}
    diagnostics = []
    seen = set()
    def visit(value, inherited_no_log=False):
        if isinstance(value, (list, dict)):
            if id(value) in seen:
                return
            seen.add(id(value))
        if isinstance(value, list):
            for item in value:
                visit(item, inherited_no_log)
        elif isinstance(value, dict):
            no_log = value.get("no_log", inherited_no_log)
            shell = value.get("ansible.builtin.shell", value.get("shell"))
            args = value.get("args") or {}
            if not isinstance(args, dict):
                args = {"creates": "unknown"}
            if isinstance(shell, dict):
                args = {**args, **shell}
                shell = shell.get("cmd")
            if isinstance(shell, str):
                try:
                    words = shlex.split(shell)
                except ValueError:
                    words = []
                if (words and words[0] == "mkdir" and "-p" not in words and "--parents" not in words
                        and not args.get("creates") and not any(k in value for k in ("when", "failed_when", "ignore_errors"))):
                    diagnostic = {"rule": "mkdir-repeat", "message": "mkdir без -p или creates завершается ошибкой, если каталог уже существует.",
                                  "search_query": "Ansible идемпотентность shell command creates повторный запуск mkdir"}
                    if len(words) == 2 and re.fullmatch(r"/[\w./-]+", words[1]):
                        previous = value.get("args")
                        value["args"] = {**(previous or {}), "creates": words[1]}
                        diagnostic["suggested_fix"] = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
                        if previous is None:
                            value.pop("args")
                        else:
                            value["args"] = previous
                    diagnostics.append(diagnostic)
            debug = value.get("ansible.builtin.debug", value.get("debug"))
            if isinstance(debug, dict) and no_log is False:
                var = debug.get("var")
                if isinstance(var, str) and re.search(r"\b" + re.escape(var) + r"\s*(?:—|–|-|:)\s*(?:секрет|secret)", task, re.I):
                    previous = value.get("no_log")
                    value["no_log"] = True
                    fix = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
                    if previous is None:
                        value.pop("no_log")
                    else:
                        value["no_log"] = previous
                    diagnostics.append({"rule": "secret-debug", "message": f"Debug выводит {var}, явно обозначенный в задаче как секрет; no_log не включён.", "suggested_fix": fix})
            for key in ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always"):
                if key in value:
                    visit(value[key], no_log)
    visit(document)
    return {"language": language, "scope": "Ansible: YAML syntax, mkdir-repeat, secret-debug",
            "diagnostics": [dict(id=str(i), **d) for i, d in enumerate(diagnostics)]}


def clean_text(analysis, sources):
    return ("Подтверждённых дефектов в пределах выполненных автопроверок не обнаружено. "
            f"Проверено: {analysis['scope']}. Это не полное доказательство корректности кода.")


def source_matches_diagnostic(diagnostic, source):
    """Necessary topic gate, in addition to model validation, not semantic proof."""
    if diagnostic["rule"] == "mkdir-repeat":
        text = source.lower()
        return (any(word in text for word in ("shell", "command", "mkdir"))
                and any(word in text for word in ("creates", "идемпотент", "idempoten", "повтор")))
    return True


def _replace_nodes(code, replacements):
    # AST column offsets are UTF-8 bytes, not Python character offsets.
    lines = code.encode().splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits = [(offsets[n.lineno - 1] + n.col_offset,
              offsets[n.end_lineno - 1] + n.end_col_offset, text.encode()) for n, text in replacements]
    result = code.encode()
    for start, end, text in sorted(edits, reverse=True):
        result = result[:start] + text + result[end:]
    return result.decode()


class _RemoveFinallyReturn(ast.NodeTransformer):
    """Remove a diagnosed unconditional constant return, retaining cleanup."""
    def __init__(self, line):
        self.line = line

    def visit_Try(self, node):
        self.generic_visit(node)
        body = [statement for statement in node.finalbody if not (
            isinstance(statement, ast.Return) and statement.lineno == self.line
            and (statement.value is None or isinstance(statement.value, ast.Constant)))]
        if len(body) == len(node.finalbody):
            return node
        node.finalbody = body
        return node if node.finalbody or node.handlers else node.body
