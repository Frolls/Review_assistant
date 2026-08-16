# Сравнение single-agent и multi-agent

## Постановка задачи и тестовый набор

На вход обеим реализациям подаётся один и тот же вопрос пользователя к корпоративной базе знаний по code review. `researcher` извлекает проверяемые факты через `search_knowledge_base`, а `writer` превращает их в ответ; в baseline эти две роли выполняет один агент. Ожидаемый результат — краткий связный ответ на русском языке с цитатами источников `[1]`, `[2]`, либо дословный отказ `по базе не нашёл, могу эскалировать`.

Пять вопросов (и их идентификаторы) объявлены один раз в `experiments/common.py` и используются обоими скриптами: `corpus-1`, `corpus-2`, `corpus-3`, `multi-step-1`, `out-of-base-1`. Первые три проверяют отдельные фрагменты корпуса, четвёртый требует объединить три независимых риска, последний проверяет корректный отказ вне базы.

## Реализация

Multi-agent — ручной supervisor на LangGraph 1.0: `supervisor → researcher → supervisor → writer → supervisor`. Это эквивалентно явному `Command(goto=..., update=...)`, поэтому число handoff прозрачно измеряется в состоянии графа. Каждый вопрос запускается с новым `InMemorySaver`, но с требуемым `thread_id=exp-langgraph`; поток `updates` печатается в консоль, а Mermaid сохраняется в [architecture-multi-agent.md](architecture-multi-agent.md).

Single-agent использует тот же `create_agent`, ту же модель и тот же `search_knowledge_base`; его `handoff_count` всегда равен нулю. Токены собираются callback-ом из `usage_metadata` (с fallback на `token_usage`), latency измеряется вокруг полного `ainvoke/astream`.

## Результаты

Сырые записи находятся в `experiments/results.json`: по одной записи на пару (реализация, вопрос). Скрипты обновляют только свой набор записей и после появления обоих наборов запускают слепого LLM-судью (оценка 1–5) для `quality_score`.

### Оценка качества

В эксперименте использован LLM-судья `qwen2.5:14b` через OpenAI-compatible API Ollama. Судья получает вопрос, reference и обезличенные ответы и оценивает фактическое соответствие, полноту, отсутствие неподтверждённых утверждений и цитирование. RAGAS не использовался: он предназначен для специализированной оценки RAG-метрик (`faithfulness`, `answer_relevancy`, `context_precision`, `context_recall`), тогда как здесь сравнивались две agent-реализации на одном наборе вопросов. Поэтому `quality_score` нельзя трактовать как RAGAS-метрику.

| Метрика | Single-agent (среднее по 5) | Multi-agent (среднее по 5) | Δ |
|---|---:|---:|---:|
| Токены на запрос | 2 731 | 4 129 | +1 398 (+51,2%) |
| LLM-вызовов на запрос | 2,0 | 3,0 | +1,0 (+50%) |
| Latency p50 (мс) | 16 011 | 22 352 | +6 341 (+39,6%) |
| Передач управления на запрос | 0 | 2 | +2 |
| Качество ответа (LLM-судья) | 2,6/5 | 4,6/5 | +2,0 |

Команда воспроизводимого прогона (при запущенном Ollama, Qdrant и установленном lock-файле):

```bash
uv run python experiments/single_agent_baseline.py
uv run python experiments/multi_agent_langgraph.py
```

Эти значения получены прогоном 16 августа 2026 года на Ollama `qwen3:latest`; исходные записи сохранены в `experiments/results.json`.

В этом прогоне multi-agent повысил среднее качество с 2,6 до 4,6 (+2,0 балла), но увеличил токены на 51,2% и p50 latency на 39,6%; LLM-вызовов стало 3 вместо 2, handoff — 2 на запрос. Прирост особенно заметен на `multi-step-1` (1/5 против 5/5). Для этого набора качество оправдывает дополнительные затраты, но при масштабировании нужен бюджет.

## Сопоставление с Anthropic

Anthropic в *How we built our multi-agent research system* сообщает примерно 15× токенов относительно обычного chat и +90,2% качества на breadth-first research. Наш множитель равен `4 129 / 2 731 = 1,51×`. Тестовый набор лишь частично похож на breadth-first research: три вопроса имеют общий корпус и общий контекст, а один многошаговый вопрос допускает разбиение; поэтому переносить результат на широкий параллельный поиск нельзя.

## Решение

**Использую мультиагентность в дипломе для составных review-запросов.** Multi-agent дал +2,0 балла качества при 1,51× токенов, 1,40× p50 latency и 2 handoff. В диплом переходит supervisor LangGraph с агентами `researcher` и `writer`, message passing через состояние графа и бюджетом не более 2 handoff; single-agent остаётся fallback для коротких вопросов. Для tight-coupling и real-time путей baseline сохраняется.

Production-заготовка этого слоя находится в `app/agents/graph.py`: модель и tool передаются в `build_supervisor_graph()`, а адаптер `app/agents/tools.py` использует тот же `RAGService`, что и HTTP-контур. Это позволяет подключить граф к application lifecycle без копирования retrieval-логики.

Граф подключён к lifecycle приложения и доступен через `POST /agent/review`. Persistent ReAct endpoint `POST /agent/stream` сохранён отдельно: он отвечает за checkpointing и HIL для write-действий, а supervisor используется для grounded review-ответов.
