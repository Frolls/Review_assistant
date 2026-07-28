#!/usr/bin/env python3
"""Post-factum Phoenix HallucinationEvaluator over traced live RAG requests."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from phoenix.client import Client
from phoenix.evals import LLM, evaluate_dataframe
from phoenix.evals.metrics import HallucinationEvaluator


def main() -> None:
    args = parse_args()
    client = Client(base_url=args.phoenix_url)
    spans = client.spans.get_spans_dataframe(project_identifier=args.project)
    live = traced_rag_rows(spans).tail(args.limit)
    if len(live) < 20:
        raise RuntimeError(
            f"only {len(live)} traced RAG requests found; run trace_rag_smoke.py first"
        )

    llm = LLM(
        provider="openai",
        model=args.model,
        api_key=os.getenv("OPENAI_API_KEY", "ollama"),
        base_url=os.getenv(
            "RAG_EVAL_OPENAI_BASE_URL",
            os.getenv("OPENAI_BASE_URL", "http://host.docker.internal:11434/v1"),
        ),
    )
    evaluator = HallucinationEvaluator(
        llm=llm,
        temperature=0,
        max_tokens=512,
        extra_body={"think": False},
    )
    evaluated = evaluate_dataframe(
        dataframe=live,
        evaluators=[evaluator],
        exit_on_error=False,
    )

    label_column = None
    score_column = "hallucination_score"
    if score_column in evaluated.columns:
        payloads = evaluated[score_column].map(score_payload)
        evaluated["hallucination_label"] = payloads.map(
            lambda value: value.get("label")
        )
        evaluated["hallucination_value"] = payloads.map(
            lambda value: value.get("score")
        )
        evaluated["hallucination_explanation"] = payloads.map(
            lambda value: value.get("explanation")
        )
        label_column = "hallucination_label"
    hallucination_rate = None
    if label_column is not None:
        labels = evaluated[label_column].astype(str).str.casefold()
        hallucination_rate = float(labels.eq("hallucinated").mean())

    annotations_logged = 0
    if label_column is not None and not args.skip_phoenix_log:
        annotations = evaluated[
            [
                "span_id",
                "hallucination_label",
                "hallucination_value",
                "hallucination_explanation",
            ]
        ].rename(
            columns={
                "hallucination_label": "label",
                "hallucination_value": "score",
                "hallucination_explanation": "explanation",
            }
        )
        annotations["metadata"] = [
            {
                "model": args.model,
                "source": "phoenix.evals.HallucinationEvaluator",
            }
            for _ in range(len(annotations))
        ]
        client.spans.log_span_annotations_dataframe(
            dataframe=annotations,
            annotation_name="hallucination",
            annotator_kind="LLM",
            sync=True,
        )
        annotations_logged = len(annotations)

    timestamp = datetime.now(UTC).astimezone().strftime("%Y-%m-%d_%H%M%S")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / f"{timestamp}_hallucination_live.csv"
    json_path = args.output_dir / f"{timestamp}_hallucination_live.json"
    evaluated.to_csv(csv_path, index=False)
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "project": args.project,
        "model": args.model,
        "rows": len(evaluated),
        "hallucination_rate": hallucination_rate,
        "label_column": str(label_column) if label_column is not None else None,
        "annotations_logged": annotations_logged,
        "csv": str(csv_path),
    }
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def traced_rag_rows(spans: pd.DataFrame) -> pd.DataFrame:
    roots = spans[spans["name"].eq("rag.evaluate")].copy()
    rows: list[dict[str, Any]] = []
    for span_id, root in roots.sort_values("start_time").iterrows():
        trace_id = root["context.trace_id"]
        retrievers = spans[
            spans["context.trace_id"].eq(trace_id)
            & spans["span_kind"].eq("RETRIEVER")
        ]
        documents: list[dict[str, Any]] = []
        for value in retrievers["attributes.retrieval.documents"].dropna():
            if isinstance(value, list) and len(value) > len(documents):
                documents = value
        context = "\n\n".join(
            str(document.get("document.content", "")).strip()
            for document in documents
            if str(document.get("document.content", "")).strip()
        )
        input_value = attribute_text(root.get("attributes.input.value"))
        output_value = attribute_text(root.get("attributes.output.value"))
        if input_value and output_value and context:
            rows.append(
                {
                    "span_id": span_id,
                    "trace_id": trace_id,
                    "input": input_value,
                    "output": output_value,
                    "context": context,
                }
            )
    return pd.DataFrame(rows)


def attribute_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("value") or value.get("text") or "").strip()
    return str(value or "").strip()


def score_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if dump is not None:
        return dump()
    return {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phoenix-url", default="http://phoenix:6006")
    parser.add_argument("--project", default="ai-pr-review-assistant")
    parser.add_argument(
        "--model",
        default=os.getenv("RAG_EVAL_JUDGE_MODEL", "qwen2.5:14b"),
    )
    parser.add_argument("--limit", type=int, default=23)
    parser.add_argument("--skip-phoenix-log", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("tests/eval/results"),
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
