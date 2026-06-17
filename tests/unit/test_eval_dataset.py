import importlib.util
from pathlib import Path

import pytest


_RUN_EVALUATION_PATH = Path(__file__).resolve().parents[2] / "eval" / "run_evaluation.py"
_SPEC = importlib.util.spec_from_file_location("run_evaluation", _RUN_EVALUATION_PATH)
run_evaluation = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(run_evaluation)
active_golden_items = run_evaluation.active_golden_items
mask_proxy_url = run_evaluation.mask_proxy_url
normalize_judgement = run_evaluation.normalize_judgement
validate_proxy_url = run_evaluation.validate_proxy_url


def test_active_golden_items_skips_deprecated_cases():
    golden = {
        "version": 1,
        "items": [
            {"id": "review_active", "deprecated": False},
            {"id": "review_deprecated", "deprecated": True},
            {"id": "review_legacy_without_flag"},
        ],
    }

    items = active_golden_items(golden)

    assert [item["id"] for item in items] == ["review_active", "review_legacy_without_flag"]


def test_active_golden_items_rejects_dataset_without_active_cases():
    golden = {"version": 1, "items": [{"id": "review_old", "deprecated": True}]}

    with pytest.raises(ValueError, match="no active items"):
        active_golden_items(golden)


def test_validate_proxy_url_accepts_http_proxy():
    validate_proxy_url("http://user:pass@example.com:8888")


def test_validate_proxy_url_rejects_http_scheme_on_common_socks_port():
    with pytest.raises(RuntimeError, match="common SOCKS port"):
        validate_proxy_url("http://user:pass@example.com:1081")


def test_validate_proxy_url_rejects_socks_proxy_without_extra(mocker):
    mocker.patch.object(run_evaluation.importlib.util, "find_spec", return_value=None)

    with pytest.raises(RuntimeError, match="SOCKS proxy support"):
        validate_proxy_url("socks5://user:pass@example.com:1081")


def test_mask_proxy_url_hides_password():
    masked = mask_proxy_url("http://local_user:secret@example.com:8888")

    assert masked == "http://local_user:***@example.com:8888"
    assert "secret" not in masked


def test_normalize_judgement_accepts_flat_scores_from_local_judge():
    judgement = normalize_judgement(
        {
            "reasoning": "Ответ совпадает с эталоном.",
            "relevance": 5,
            "correctness": 4,
            "completeness": 3,
            "explanation": "Достаточно хорошо.",
        }
    )

    assert judgement["scores"] == {
        "relevance": 5,
        "correctness": 4,
        "completeness": 3,
    }
