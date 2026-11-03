import json
from pathlib import Path

import pytest

from app.services.review_eval import score


CASES = json.loads((Path(__file__).parents[1] / "eval/code_review/cases.json").read_text())


def test_eval_reports_false_positives_separately_from_recall_and_fix_usability():
    rows = [
        {"expected_defects": ["bug"], "review": {"true_positives": 1, "false_positives": 0, "false_negatives": 0, "fix_usable": False}},
        {"expected_defects": [], "review": {"true_positives": 0, "false_positives": 1, "false_negatives": 0, "fix_usable": False}},
    ]
    result = score(rows)
    assert result["precision"] == .5 and result["recall"] == 1
    assert result["clean_false_positive_rate"] == 1 and result["usable_fix_rate"] == 0
    rows[0]["review"]["fix_usable"] = None
    with pytest.raises(ValueError, match="независимую"):
        score(rows)


def test_review_set_contains_balanced_defects_fixes_and_clean_examples():
    assert len({c["id"] for c in CASES}) == 9
    assert {kind: sum(c["kind"] == kind for c in CASES) for kind in ("defect", "fixed", "clean")} == {"defect": 3, "fixed": 3, "clean": 3}
    for case in CASES:
        assert bool(case["expected_defects"]) == (case["kind"] == "defect")
        if case["language"] == "python":
            compile(case["code"], case["id"], "exec")
            if case["reference_fix"]:
                compile(case["reference_fix"], case["id"], "exec")


def test_authored_mutable_default_example_reproduces_defect_and_reference_fix():
    case = next(c for c in CASES if c["id"] == "shared_cache-defect")
    # Only the checked-in fixtures are executed; model outputs never reach exec.
    for code, expected in [(case["code"], {"first": True, "second": True}), (case["reference_fix"], {"second": True})]:
        namespace = {}
        exec(compile(code, "authored-eval-fixture", "exec"), namespace)
        namespace["remember"]("first")
        assert namespace["remember"]("second") == expected
