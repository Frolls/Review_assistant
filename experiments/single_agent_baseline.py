from __future__ import annotations

import asyncio
import time

from langchain.agents import create_agent

from experiments.common import (
    TEST_QUESTIONS,
    UsageTracker,
    close_rag_service,
    experiment_model,
    final_ai_text,
    judge_results_if_complete,
    make_result,
    save_results,
    search_knowledge_base,
)


SYSTEM_PROMPT = """\
Ты single-agent baseline для корпоративной базы знаний по code review.
Для каждого вопроса ровно один раз вызови search_knowledge_base, передав полный вопрос.
Затем найди в результате факты и сразу оформи окончательный связный ответ.
Используй только факты из tool result; каждое фактическое утверждение подкрепляй ссылкой
[1], [2] на соответствующий фрагмент. Не выдумывай сведения из общих знаний.
Если confident=false, верни дословно значение поля answer и ничего больше.
"""


async def run() -> None:
    agent = create_agent(
        model=experiment_model(),
        tools=[search_knowledge_base],
        system_prompt=SYSTEM_PROMPT,
        name="single_agent",
    )
    records = []
    try:
        for index, test in enumerate(TEST_QUESTIONS, 1):
            print(f"\n=== Single-agent {index}/5: {test.id} ===")
            tracker = UsageTracker()
            started_at = time.perf_counter()
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": test.question}]},
                config={"callbacks": [tracker]},
            )
            latency_ms = (time.perf_counter() - started_at) * 1000
            answer = final_ai_text(result)
            print(answer)
            records.append(
                make_result(
                    implementation="single-agent",
                    test=test,
                    answer=answer,
                    tracker=tracker,
                    latency_ms=latency_ms,
                    handoff_count=0,
                )
            )
        save_results("single-agent", records)
        if await judge_results_if_complete():
            print("\nLLM-судья обновил quality_score для обеих реализаций.")
        else:
            print("\nQuality-score будет рассчитан после прогона второй реализации.")
    finally:
        await close_rag_service()


if __name__ == "__main__":
    asyncio.run(run())
