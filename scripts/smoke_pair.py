"""Live smoke: readiness of both services → cited RAG → preview/cancel.

Requires INTERNAL_TOKEN and an OWNER_ID. Never approves an external effect.
"""
import argparse
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="http://localhost:8000")
    parser.add_argument("--bot", default="http://localhost:8081")
    args = parser.parse_args()
    owner = os.environ["OWNER_ID"]
    headers = {"Content-Type": "application/json", "X-Internal-Token": os.environ["INTERNAL_TOKEN"], "X-User-ID": owner}
    def request(path, payload=None, method=None, base=None):
        req = Request((base or args.backend) + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers, method=method)
        with urlopen(req, timeout=200) as response:
            text = response.read().decode()
            return text if "text/event-stream" in response.headers.get("Content-Type", "") else json.loads(text)
    assert request("/ready")["status"] == "ok"
    assert request("/health", base=args.bot)["status"] == "ok"
    result = request("/rag/query", {"question": "Почему изменяемый аргумент по умолчанию Python разделяется между вызовами?"})
    assert result["confident"] and result["sources"]
    assert any(f"[{s['id']}]" in result["answer"] for s in result["sources"])
    thread = "smoke-" + uuid4().hex
    query = urlencode({"thread_id": thread})
    try:
        request("/agent/stream", {"thread_id": thread, "input": {"messages": [{"role": "user", "content": f"Подготовь отправку в Telegram чат {owner} текста: smoke test. Вызови send_telegram_message для подтверждения."}]}})
        pending = request("/agent/pending?" + query)["pending"]
        assert pending and pending["request_id"] and str(pending["chat_id"]) == owner
        cancelled = request("/agent/stream", {"thread_id": thread, "resume": False, "request_id": pending["request_id"]})
        events = [json.loads(line[6:]) for line in cancelled.splitlines() if line.startswith("data: ")]
        assert any(e["event"] == "done" for e in events)
        assert not any(e["event"] == "error" for e in events)
        assert request("/agent/pending?" + query)["pending"] is None
    finally:
        request("/agent/history?" + query, method="DELETE")
    print("PASS: backend ready, bot alive, cited RAG, preview/cancel")


if __name__ == "__main__":
    main()
