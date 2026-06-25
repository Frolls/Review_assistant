from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import tiktoken
except ModuleNotFoundError:  # pragma: no cover - mock stays usable in tiny envs.
    tiktoken = None


def count_prompt_tokens(messages: list[dict]) -> int:
    if tiktoken is None:
        return max(1, len(json.dumps(messages, ensure_ascii=False)) // 4)
    encoding = tiktoken.get_encoding("o200k_base")
    total = 2
    for message in messages:
        total += 4
        total += len(encoding.encode(str(message.get("role", ""))))
        total += len(encoding.encode(str(message.get("content", ""))))
    return total


class MockOpenAIHandler(BaseHTTPRequestHandler):
    server_version = "MockOpenAI/0.1"

    def _write_json(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _write_sse(self, chunks: list[str]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        now = int(time.time())
        for chunk in chunks:
            payload = {
                "id": f"chatcmpl-mock-{now}",
                "object": "chat.completion.chunk",
                "created": now,
                "model": "demo-fallback",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": chunk},
                        "finish_reason": None,
                    }
                ],
            }
            self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.02)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/v1/models", "/models"):
            self._write_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "demo-fallback", "object": "model", "owned_by": "local-mock"},
                    ],
                },
            )
            return

        self._write_json(404, {"error": {"message": f"Unsupported path: {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._write_json(404, {"error": {"message": f"Unsupported path: {self.path}"}})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        body = json.loads(raw_body.decode("utf-8"))
        messages = body.get("messages", [])
        user_content = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                user_content = message.get("content", "")
                break
        transcript = "\n".join(str(message.get("content", "")) for message in messages)
        if "Как меня зовут" in user_content and "Аня" in transcript:
            answer = "Тебя зовут Аня."
        elif "Привет, меня зовут Аня" in user_content:
            answer = "Привет, Аня. Запомнила."
        else:
            answer = (
                "fallback-ok: primary provider failed, local mock answered. "
                f"user_prompt={user_content}"
            )

        if body.get("stream"):
            midpoint = max(1, len(answer) // 2)
            self._write_sse([answer[:midpoint], answer[midpoint:]])
            return

        now = int(time.time())
        payload = {
            "id": f"chatcmpl-mock-{now}",
            "object": "chat.completion",
            "created": now,
            "model": "demo-fallback",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": answer,
                    },
                }
            ],
            "usage": {
                "prompt_tokens": count_prompt_tokens(messages),
                "completion_tokens": 24,
                "total_tokens": count_prompt_tokens(messages) + 24,
            },
        }
        self._write_json(200, payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return


def main() -> None:
    host = "127.0.0.1"
    port = 8001
    server = HTTPServer((host, port), MockOpenAIHandler)
    print(f"Mock OpenAI-compatible fallback server listening on http://{host}:{port}/v1")
    server.serve_forever()


if __name__ == "__main__":
    main()
