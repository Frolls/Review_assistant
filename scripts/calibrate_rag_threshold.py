#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import statistics
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.rag import RAGService  # noqa: E402


GOLDEN_QUERIES = {
    "positive": [
        "Почему в Ansible task лучше избегать command и shell?",
        "Что делать, если секрет уже попал в diff?",
        "Как безопасно добавить NOT NULL колонку в большую таблицу?",
        "Для чего в Python нужен Protocol?",
        "Как работает structural pattern matching в Python?",
    ],
    "negative": [
        "Когда лучше высаживать томаты в открытый грунт?",
        "Как приготовить яблочный пирог?",
        "Кто выиграл последний чемпионат мира по футболу?",
        "Какая завтра погода в Екатеринбурге?",
        "Как выбрать зимние шины для автомобиля?",
    ],
}


async def main() -> None:
    service = RAGService(get_settings())
    rows: list[dict[str, object]] = []
    try:
        for label, questions in GOLDEN_QUERIES.items():
            for question in questions:
                prepared = await service.prepare(question)
                rows.append(
                    {
                        "label": label,
                        "question": question,
                        "top_score": prepared.top_score,
                        "confident": prepared.confident,
                    }
                )
    finally:
        await service.close()

    summary = {}
    for label in GOLDEN_QUERIES:
        scores = [
            float(row["top_score"])
            for row in rows
            if row["label"] == label and row["top_score"] is not None
        ]
        summary[label] = {
            "min": min(scores),
            "median": statistics.median(scores),
            "max": max(scores),
        }
    print(
        json.dumps(
            {
                "embedding_model": get_settings().embedding_model,
                "collection": get_settings().rag_collection,
                "threshold": get_settings().rag_score_threshold,
                "summary": summary,
                "queries": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
