"""Validate the reviewed synthetic fixes inside a network-disabled container.

Python is executed only after AST equality with the predefined reference.
YAML is checked structurally, not executed by Ansible.
"""
import ast
import io
import json
from pathlib import Path
import re
from unittest.mock import patch

import yaml


results = []
for run in ("baseline", "repeat"):
    for row in json.loads(Path(f"/results/{run}-raw.json").read_text()):
        if not row["expected_defects"] or not row["answer"]:
            continue
        blocks = re.findall(r"```(?:python|yaml)\s*\n(.*?)```", row["answer"], re.S)
        if not blocks:
            continue
        assert len(blocks) == 1
        code = blocks[0]
        if row["language"] == "python":
            assert ast.dump(ast.parse(code)) == ast.dump(ast.parse(row["reference_fix"]))
            namespace = {}
            exec(compile(code, "reviewed-fixture", "exec"), namespace)
            if row["id"] == "shared_cache-defect":
                function = namespace["remember"]
                first, second = function("a"), function("b")
                assert first == {"a": True} and second == {"b": True} and first is not second
                explicit = {}
                assert function("c", explicit) is explicit and explicit == {"c": True}
                checks = ["independent_defaults", "explicit_cache_preserved"]
            else:
                function = namespace["first_line"]
                stream = io.StringIO("first\nsecond\n")
                with patch("builtins.open", return_value=stream):
                    assert function("synthetic-path") == "first\n"
                assert stream.closed

                class FailedRead(io.StringIO):
                    def readline(self, *args):
                        raise OSError("synthetic read failure")

                stream = FailedRead()
                with patch("builtins.open", return_value=stream):
                    try:
                        function("synthetic-path")
                    except OSError:
                        pass
                    else:
                        raise AssertionError("Read failure was swallowed")
                assert stream.closed
                checks = ["first_line_result", "closed_on_success", "closed_on_read_error"]
        else:
            actual, expected = yaml.safe_load(code), yaml.safe_load(row["reference_fix"])
            # Display names do not change module parameters or execution.
            for tasks in (actual, expected):
                for task in tasks:
                    task.pop("name", None)
            assert actual == expected
            checks = ["yaml_matches_reference_file_module_state_directory_mode"]
        results.append({"run": run, "case": row["id"], "passed": checks})
print(json.dumps({"results": results, "ansible_execution": False}, indent=2))
