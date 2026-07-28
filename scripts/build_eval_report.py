#!/usr/bin/env python3
"""Build docs/rag_evaluation.md directly from timestamped eval artifacts."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "tests/eval/results"
REPORT = ROOT / "docs/rag_evaluation.md"
LABELS = ("baseline", "chunk_512", "generation_qwen35")
METRICS = (
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
    "has_citation",
)
REQUIRED_THRESHOLDS = {
    "faithfulness": 0.70,
    "answer_relevancy": 0.70,
    "has_citation": 0.95,
}
PRIMARY_METRICS = ("faithfulness", "answer_relevancy")


def main() -> None:
    artifacts = {label: load_latest(label) for label in LABELS}
    best_label = max(LABELS, key=lambda label: selection_score(artifacts[label]))
    best = artifacts[best_label]
    hallucination = load_optional("*_hallucination_live.json")
    hallucination_manual_review = load_optional(
        "*_hallucination_manual_review.json"
    )
    failures = load_failures(best, count=5)
    rows = {label: table_row(label, artifacts[label]) for label in LABELS}

    content = f"""# Оценка качества RAG

Отчёт сгенерирован из timestamped-артефактов в `tests/eval/results/`. Числа ниже
не переписывались вручную.

## 1. Конфигурация

- Production LLM: `{best["configuration"]["production_model"]}` через локальный Ollama.
- Judge LLM: `{best["configuration"]["judge_model"]}` через OpenAI-compatible API Ollama;
  judge отделён от production-модели.
- Judge embeddings: `{best["configuration"]["judge_embedding_model"]}`, размерность 2560.
- Vector store: Qdrant; cosine similarity.
- Chunking: SentenceSplitter, baseline 256/32, эксперимент 512/32.
- Retrieval: top-K 10; re-ranker выключен; в generation передаются top-5.
- Score threshold: {fmt(best["configuration"]["score_threshold"])}.
- Evaluation: RAGAS 0.4 collections API, `llm_factory`, пять метрик.
- Selection policy: обязательные gates faithfulness > 0.70, answer relevancy >
  0.70, has_citation > 0.95; среди прошедших вариантов максимизируется среднее
  faithfulness и answer relevancy.

Облачных Anthropic/OpenAI ключей в окружении нет, поэтому этот baseline намеренно
получен локальным judge. Его нельзя напрямую сравнивать с будущим прогоном Claude
или OpenAI: при смене judge весь baseline пересчитывается.

## 2. Golden dataset

`tests/eval/golden_dataset.json` содержит 35 уникальных пар с полями
`user_input`, `reference`, `reference_contexts`. Сырой набор создавался
`TestsetGenerator.generate_with_chunks()` по 10 документам из
`data/retrieval-corpus/` и сохраняется в `tests/eval/golden_dataset_raw.csv`.
После генерации вопросы вычитаны вручную: удалены дубли, общие и нелепые вопросы,
а reference и reference_contexts сверены с корпусом и исправлены.

## 3. Baseline

{metric_table([rows["baseline"]])}

Артефакт: `{Path(artifacts["baseline"]["csv"]).name}`.

## 4. Эксперимент A — chunking

Меняется только chunk size: 256 → 512. Overlap=32, top-K=10, модели, judge и
golden dataset фиксированы.

{metric_table([rows["baseline"], rows["chunk_512"]])}

{decision("chunk_512", "baseline", artifacts)}

## 5. Эксперимент B — generation model

Меняется только production LLM: `qwen3:latest` → `qwen3.5:9b` на коллекции с
chunk size 512. Retrieval, judge и golden dataset фиксированы.
Для более медленной локальной модели верхняя граница request timeout поднята до
180 секунд; это operational guard, а не параметр качества RAG.

{metric_table([rows["chunk_512"], rows["generation_qwen35"]])}

{decision("generation_qwen35", "chunk_512", artifacts)}

## 6. Финальная конфигурация и числа

Беру вариант **{best_label}**, потому что он проходит обязательные gates, а
среднее двух ключевых метрик faithfulness и answer relevancy максимально среди
трёх прогонов: {selection_score(best):.3f}. Финальные параметры:
chunk size {best["configuration"]["chunk_size"]}, overlap
{best["configuration"]["chunk_overlap"]}, top-K
{best["configuration"]["top_k"]}, re-ranker
{"включён" if best["configuration"]["reranker_enabled"] else "выключен"}.

{metric_table([rows[best_label]])}

Порог для справочного/внутреннего сценария зафиксирован как engineering minimum:
faithfulness > 0.70, answer relevancy > 0.70, has_citation > 0.95.

## 7. Failure analysis

Худшие ответы отсортированы по faithfulness по финальному CSV. Диагноз:
низкие faithfulness и context recall — retrieval; низкий faithfulness при
высоком recall — generation.

{render_failures(failures)}

## 8. Известные проблемы и план улучшений

{known_issues(best)}

- RAGAS — LLM-as-judge, поэтому ожидается шум порядка 5–10%; выводы делаются
  только на фиксированных 35 вопросах и одном judge.
- Следующий шаг: проверить low-recall случаи с semantic chunking и отдельным
  re-ranker экспериментом, затем полностью пересчитать baseline.
- Phoenix используется для диагностики: retriever spans показывают чанки и
  similarity scores, LLM spans — prompt, response и token usage.
{hallucination_summary(hallucination, hallucination_manual_review)}

![Phoenix annotations: false positive HallucinationEvaluator](screenshots/annotations_example_1.png)

![Phoenix: раскрытый RAG trace](screenshots/phoenix_rag_trace.png)

## Воспроизведение

```bash
docker compose up -d qdrant phoenix
docker compose --profile eval run --rm eval python scripts/verify_eval.py
docker compose --profile eval run --rm eval python scripts/prepare_eval_collections.py
docker compose --profile eval run --rm eval python scripts/run_ab_evals.py
docker compose --profile eval run --rm eval python scripts/build_eval_report.py
```

Judge cache хранится в `tests/eval/.ragas_cache/`; для полностью независимого
повторного judge-прогона передайте `--no-cache`.
"""
    REPORT.write_text(content, encoding="utf-8")
    print(f"Wrote {REPORT.relative_to(ROOT)} from {best['csv']}")


def load_latest(label: str) -> dict[str, Any]:
    matches = sorted(RESULTS.glob(f"*_{label}.json"))
    if not matches:
        raise FileNotFoundError(f"no aggregate JSON found for label {label!r}")
    return json.loads(matches[-1].read_text(encoding="utf-8"))


def load_optional(pattern: str) -> dict[str, Any] | None:
    matches = sorted(RESULTS.glob(pattern))
    if not matches:
        return None
    return json.loads(matches[-1].read_text(encoding="utf-8"))


def hallucination_summary(
    artifact: dict[str, Any] | None,
    manual_review: dict[str, Any] | None,
) -> str:
    if artifact is None or not finite(artifact.get("hallucination_rate")):
        return ""
    summary = (
        f"- Post-factum Phoenix HallucinationEvaluator: "
        f"{float(artifact['hallucination_rate']) * 100:.1f}% hallucinated "
        f"на {artifact['rows']} live-трейсах (`{Path(artifact['csv']).name}`)."
    )
    if manual_review is None:
        return summary
    return (
        summary
        + f"\n- Ручная проверка положительных verdict: "
        f"{manual_review['confirmed_hallucinated']}/{manual_review['rows']} "
        f"подтверждённых галлюцинаций, "
        f"{manual_review['false_positives']} false positive evaluator."
    )


def table_row(label: str, artifact: dict[str, Any]) -> list[str]:
    aggregates = artifact["aggregates"]
    return [
        label,
        *(fmt(aggregates[name]) for name in METRICS),
        fmt(aggregates["average_latency_ms"], digits=1),
    ]


def metric_table(rows: list[list[str]]) -> str:
    header = (
        "| Вариант | faithfulness | answer_relevancy | context_precision | "
        "context_recall | has_citation | avg latency, ms |\n"
        "|---|---:|---:|---:|---:|---:|---:|"
    )
    return "\n".join([header, *("| " + " | ".join(row) + " |" for row in rows)])


def selection_score(artifact: dict[str, Any]) -> float:
    if not passes_required_thresholds(artifact):
        return -1.0
    values = [artifact["aggregates"].get(name) for name in PRIMARY_METRICS]
    valid = [float(value) for value in values if finite(value)]
    return sum(valid) / len(valid) if valid else -1.0


def decision(candidate: str, control: str, artifacts: dict[str, dict[str, Any]]) -> str:
    candidate_score = selection_score(artifacts[candidate])
    control_score = selection_score(artifacts[control])
    winner = candidate if candidate_score > control_score else control
    loser = control if winner == candidate else candidate
    if not passes_required_thresholds(artifacts[loser]):
        failed = ", ".join(
            name
            for name, threshold in REQUIRED_THRESHOLDS.items()
            if not finite(artifacts[loser]["aggregates"].get(name))
            or float(artifacts[loser]["aggregates"][name]) <= threshold
        )
        return (
            f"Беру вариант **{winner}**: он проходит обязательные gates, а "
            f"**{loser}** не проходит ({failed})."
        )
    return (
        f"Беру вариант **{winner}** в рамках этого сравнения: среднее "
        f"faithfulness и answer relevancy "
        f"{max(candidate_score, control_score):.3f} против "
        f"{min(candidate_score, control_score):.3f}; оба варианта проходят gates."
    )


def passes_required_thresholds(artifact: dict[str, Any]) -> bool:
    aggregates = artifact["aggregates"]
    return all(
        finite(aggregates.get(name)) and float(aggregates[name]) > threshold
        for name, threshold in REQUIRED_THRESHOLDS.items()
    )


def load_failures(artifact: dict[str, Any], count: int) -> list[dict[str, str]]:
    csv_path = ROOT / artifact["csv"]
    with csv_path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    scored = [row for row in rows if finite(row.get("faithfulness"))]
    scored.sort(key=lambda row: float(row["faithfulness"]))
    return scored[:count]


def render_failures(rows: list[dict[str, str]]) -> str:
    sections = []
    for index, row in enumerate(rows, start=1):
        recall = number(row.get("context_recall"))
        faithfulness = number(row.get("faithfulness"))
        if faithfulness < 0.7 and recall < 0.7:
            diagnosis = "retrieval-проблема: нужный материал не покрыт найденными чанками"
        elif faithfulness < 0.7:
            diagnosis = "generation-проблема: контекст достаточен, но ответ ему не следует"
        else:
            diagnosis = "пограничный случай: проверить шум/precision и калибровку judge"
        contexts = json.loads(row.get("retrieved_contexts") or "[]")
        context_summary = " / ".join(
            " ".join(str(item).split())[:240] for item in contexts[:3]
        )
        sections.append(
            f"""### {index}. {row["user_input"]}

- Retrieved contexts: {context_summary}
- Response: {row["response"]}
- Метрики: faithfulness={fmt(row.get("faithfulness"))},
  answer_relevancy={fmt(row.get("answer_relevancy"))},
  context_precision={fmt(row.get("context_precision"))},
  context_recall={fmt(row.get("context_recall"))},
  has_citation={fmt(row.get("has_citation"))}.
- Диагноз: {diagnosis}.
"""
        )
    return "\n".join(sections) if sections else "Нет строк с рассчитанным faithfulness."


def known_issues(artifact: dict[str, Any]) -> str:
    aggregates = artifact["aggregates"]
    issues = []
    for name, threshold in REQUIRED_THRESHOLDS.items():
        value = aggregates.get(name)
        if not finite(value) or float(value) <= threshold:
            issues.append(
                f"- `{name}`={fmt(value)} не превышает целевой порог {threshold:.2f}; "
                "нужен разбор failure cases и новый изолированный эксперимент."
            )
    if not issues:
        issues.append("- Три обязательных целевых порога достигнуты.")
    return "\n".join(issues)


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def number(value: Any) -> float:
    return float(value) if finite(value) else math.nan


def fmt(value: Any, *, digits: int = 3) -> str:
    return f"{float(value):.{digits}f}" if finite(value) else "n/a"


if __name__ == "__main__":
    main()
