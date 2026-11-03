"""Disposable document parser; the parent kills it on timeout/cancellation."""
import json
import sys


def main():
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    from app.chat.media import extract_docx_text, extract_pdf_text, MAX_EXTRACTED_CHARS
    try:
        data = sys.stdin.buffer.read(10 * 1024**2 + 1)
        if len(data) > 10 * 1024**2:
            raise ValueError("Размер файла превышает 10 МБ.")
        parser = extract_pdf_text if sys.argv[1] == "pdf" else extract_docx_text
        text = parser(data)
        if len(text) > MAX_EXTRACTED_CHARS:
            raise ValueError("Документ слишком большой для разбора целиком.")
        result = {"text": text}
    except Exception:
        result = {"error": "Не удалось разобрать документ или превышены лимиты документа."}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
