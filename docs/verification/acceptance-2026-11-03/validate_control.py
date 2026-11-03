"""Validate inspected synthetic outputs and reference behavior offline.

Run only in an isolated, read-only container with writable /tmp and no network.
Python output must match the inspected reference AST before execution. Ansible
runs only on container-local localhost; the directory is under its /tmp.
"""
import ast
import io
import json
from pathlib import Path
import re
import subprocess
from contextlib import redirect_stdout

import yaml

ROOT = Path("/results")
rows = json.loads((ROOT / "review-raw.json").read_text())
report = {"python": [], "ansible": [], "missing_fixes": [], "ground_truth": []}
for row in rows:
    if not row["expected_defects"]:
        continue
    blocks = re.findall(r"```(?:python|yaml)\s*\n(.*?)```", row["answer"], re.S)
    if not blocks:
        report["missing_fixes"].append(row["id"])
        continue
    assert len(blocks) == 1
    code = blocks[0]
    if row["id"] == "event_queue-defect":
        assert ast.dump(ast.parse(code)) == ast.dump(ast.parse(row["reference_fix"]))
        namespace = {}
        exec(compile(code, "inspected-control-fix", "exec"), namespace)
        enqueue = namespace["enqueue"]
        first, second = enqueue("one"), enqueue("two")
        assert first == ["one"] and second == ["two"] and first is not second
        queue = []
        assert enqueue("three", queue=queue) is queue and queue == ["three"]
        report["python"].append({"case": row["id"], "passed": ["independent_keyword_defaults", "explicit_list_identity"]})
    elif row["id"] == "archive_directory-defect":
        tasks = yaml.safe_load(code)
        assert tasks == [{"name": "Prepare archive directory", "ansible.builtin.shell": {
            "cmd": "mkdir /tmp/review-acceptance-archive"}, "args": {
            "cmd": "mkdir /tmp/review-acceptance-archive", "creates": "/tmp/review-acceptance-archive"}}]
        playbook = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                     "vars": {"ansible_python_interpreter": "/usr/bin/python3"}, "tasks": tasks}]
        path = Path("/tmp/generated-review-fix.json")
        path.write_text(json.dumps(playbook))
        runs = []
        for repeat in range(2):
            result = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)],
                                    capture_output=True, text=True, timeout=60)
            runs.append({"repeat": repeat, "returncode": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
            assert result.returncode == 0, runs
            assert re.search(r"changed=" + ("1" if repeat == 0 else "0") + r"\b", result.stdout), runs
        report["ansible"].append({"case": row["id"], "passed": ["executes_on_localhost", "repeat_has_no_changes"], "runs": runs})
    else:
        raise AssertionError("Uninspected generated code: " + row["id"])

# Establish the Python oracle from authored fixtures without using model output.
for row in rows:
    if row["language"] != "python":
        continue
    namespace = {}
    with redirect_stdout(io.StringIO()):
        exec(compile(row["code"], "authored-fixture", "exec"), namespace)
    family = row["id"].rsplit("-", 1)[0]
    defective = bool(row["expected_defects"])
    if family == "event_queue":
        first, second = namespace["enqueue"]("a"), namespace["enqueue"]("b")
        observed_defect = first is second
    elif family == "json_import":
        try:
            value = namespace["decode_config"]('{"enabled": true}')
        except NameError:
            observed_defect = True
        else:
            assert value == {"enabled": True}
            observed_defect = False
    elif family == "finally_override":
        value = namespace["reciprocal"](4)
        try:
            namespace["reciprocal"](0)
        except ZeroDivisionError:
            propagates = True
        else:
            propagates = False
        observed_defect = value != .25 or not propagates
    elif family == "mean_contract":
        observed_defect = namespace["average"]([2, 8]) != 5
    else:
        raise AssertionError(family)
    assert observed_defect == defective, row["id"]
    report["ground_truth"].append({"case": row["id"], "observed_defect": observed_defect, "matches_label": True})
print(json.dumps(report, ensure_ascii=False, indent=2))
