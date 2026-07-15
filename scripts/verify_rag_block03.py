from __future__ import annotations

import asyncio
import json
from typing import Any

from app.core.config import get_settings
from app.services.rag import RAGService


QUESTIONS = [
    {
        "type": "хороший",
        "question": "Почему в Ansible task лучше избегать command и shell?",
    },
    {
        "type": "хороший",
        "question": "Что делать, если секрет уже попал в diff?",
    },
    {
        "type": "хороший",
        "question": "Как безопасно добавить NOT NULL колонку в большую таблицу?",
    },
    {
        "type": "средний",
        "question": (
            "Как отревьюить PR, где endpoint пишет в базу, вызывает внешний HTTP API "
            "и форматирует ответ в одной функции?"
        ),
    },
    {
        "type": "вне базы",
        "question": "Когда лучше высаживать томаты в открытый грунт?",
    },
]


async def main() -> None:
    service = RAGService(get_settings())
    try:
        await service.build()
        results: list[dict[str, Any]] = []
        for item in QUESTIONS:
            result = await service.answer(item["question"])
            sources = result["sources"]
            top_source = sources[0] if sources else {}
            results.append(
                {
                    "type": item["type"],
                    "question": item["question"],
                    "answer": result["answer"],
                    "top_score": result["top_score"],
                    "top_source": top_source.get("source"),
                    "top_source_score": top_source.get("score"),
                    "sources": sources,
                }
            )
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())
