#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.rag import RAGService  # noqa: E402


async def main() -> None:
    service = RAGService(get_settings())
    try:
        first_question = "Почему в Ansible лучше избегать command и shell?"
        first = await service.answer(first_question, chat_id="multiturn-check")
        history = [
            {"role": "user", "content": first_question},
            {"role": "assistant", "content": first["answer"]},
        ]
        second = await service.answer(
            "А как для них обеспечить идемпотентность?",
            history=history,
            chat_id="multiturn-check",
        )
        print(
            json.dumps(
                {"first": first, "second": second},
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())
