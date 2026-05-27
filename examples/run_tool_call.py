import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.llm.client import ReviewAssistantClient


REPORTS_DIR = ROOT_DIR / "logs"
README_PATH = ROOT_DIR / "README.md"
JSON_REPORT_PATH = REPORTS_DIR / "tool_call_cases.json"
MARKDOWN_REPORT_PATH = REPORTS_DIR / "tool_call_cases.md"
README_RESULTS_START = "<!-- TOOL_CALL_RESULTS_START -->"
README_RESULTS_END = "<!-- TOOL_CALL_RESULTS_END -->"
TEST_CASES = [
    (
        "a",
        "Запрос, который точно требует tool",
        "Покажи правила ревью по idempotence в Ansible и как их учитывать в PR.",
    ),
    (
        "b",
        "Запрос, который точно не требует tool",
        "Привет! Кто ты и чем можешь помочь на ревью?",
    ),
    (
        "c",
        "Пограничный случай",
        "Стоит ли придираться к именам переменных в этом PR?",
    ),
]


def build_markdown_report(case_results: list[dict[str, str]]) -> str:
    lines = [
        "# Наблюдения по 3 контрольным кейсам",
        "",
        "Этот блок можно перенести в README.md как фактические результаты живого прогона.",
        "",
    ]

    for item in case_results:
        lines.append(f"## {item['case_id']}. {item['title']}")
        lines.append("")
        lines.append(f"Запрос: `{item['query']}`")
        lines.append("")
        lines.append(f"- Tool вызван: {item['tool_used']}")
        lines.append(f"- Аргументы: {item['arguments']}")
        lines.append(f"- Финальный ответ: {item['final_text']}")
        lines.append(f"- Всего токенов: {item['usage_total_tokens']}")
        lines.append("")

    return "\n".join(lines)


def update_readme(markdown_report: str) -> None:
    readme_text = README_PATH.read_text(encoding="utf-8")
    start_index = readme_text.find(README_RESULTS_START)
    end_index = readme_text.find(README_RESULTS_END)
    if start_index == -1 or end_index == -1 or end_index < start_index:
        return

    new_text = (
        readme_text[: start_index + len(README_RESULTS_START)]
        + "\n\n"
        + markdown_report
        + "\n\n"
        + readme_text[end_index:]
    )
    README_PATH.write_text(new_text, encoding="utf-8")


def main() -> None:
    try:
        client = ReviewAssistantClient()
    except RuntimeError as exc:
        print(f"Не удалось создать клиента: {exc}")
        return

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    case_results: list[dict[str, str]] = []

    print("Контрольные кейсы для README и лога:\n")
    for case_id, title, query in TEST_CASES:
        print(f"[{case_id}] {title}")
        print(f"USER: {query}")
        try:
            run_result = client.run_with_details(query)
        except RuntimeError as exc:
            print(f"Не удалось выполнить запрос: {exc}")
            return
        print(f"ASSISTANT: {run_result.final_text}")
        print(f"TOOL_USED: {'да' if run_result.tool_used else 'нет'}")
        print(f"TOKENS: {run_result.usage_total_tokens}\n")

        arguments = "не вызывался"
        if run_result.tool_calls:
            arguments = json.dumps(run_result.tool_calls[0].arguments, ensure_ascii=False)

        case_results.append(
            {
                "case_id": case_id,
                "title": title,
                "query": query,
                "tool_used": "да" if run_result.tool_used else "нет",
                "arguments": arguments,
                "final_text": run_result.final_text,
                "usage_total_tokens": str(run_result.usage_total_tokens),
            }
        )

    JSON_REPORT_PATH.write_text(
        json.dumps(case_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_report = build_markdown_report(case_results)
    MARKDOWN_REPORT_PATH.write_text(markdown_report, encoding="utf-8")
    update_readme(markdown_report)

    print(f"JSON-отчёт сохранён: {JSON_REPORT_PATH}")
    print(f"Markdown-отчёт сохранён: {MARKDOWN_REPORT_PATH}")
    print(f"README обновлён: {README_PATH}")


if __name__ == "__main__":
    main()
