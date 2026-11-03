"""Exact retrieval from a small versioned rule catalogue.

Paraphrases verified against primary documentation on 2026-11-03. This is
separate from vector retrieval: a known rule ID is an exact lookup, not a
fabricated embedding score or a lowered similarity threshold.
"""
RULES = {
    "mkdir-repeat": (
        "https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/shell_module.html",
        "Ansible shell выполняет команду через оболочку. Параметр creates задаёт путь: если он уже существует, задача не запускает команду. Это позволяет пропустить повторное создание каталога; chdir задаёт рабочий каталог команды.",
    ),
    "F821": (
        "https://docs.astral.sh/ruff/rules/undefined-name/",
        "F821: использование неопределённого имени может привести к NameError. Имя должно быть определено или импортировано до использования.",
    ),
    "B012": (
        "https://docs.astral.sh/ruff/rules/jump-statement-in-finally/",
        "B012: return, break и continue внутри finally могут подавить исключение из try/except. Управление не должно покидать finally таким способом; сохраните очистку ресурса и распространение исключения.",
    ),
    "secret-debug": (
        "https://docs.ansible.com/projects/ansible/latest/reference_appendices/logging.html#protecting-sensitive-data-with-no-log",
        "Вывод задачи может раскрывать секретные значения в логах. Для задачи, раскрывающей секрет, установите no_log: true. Это не заменяет хранение секретов и не защищает от отладочного режима Ansible.",
    ),
}


def retrieve(checks):
    if checks is None:
        return []
    result = []
    for diagnostic in checks["diagnostics"]:
        rule = diagnostic["rule"]
        if rule in RULES:
            name, text = RULES[rule]
        elif rule == "task-example":
            name = "Требование текущей задачи: проверенные входные и выходные значения"
            text = "\n".join(case["quote"] for case in diagnostic["examples"])
        else:
            continue
        result.append({"diagnostic_id": diagnostic["id"], "file_name": name,
                       "page": None, "score": None, "snippet": text})
    return result
