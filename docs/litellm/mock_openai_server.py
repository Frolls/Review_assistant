from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockOpenAIHandler(BaseHTTPRequestHandler):
    server_version = "MockOpenAI/0.1"

    def _write_json(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

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
                        "content": (
                            "fallback-ok: primary provider failed, local mock answered. "
                            f"user_prompt={user_content}"
                        ),
                    },
                }
            ],
            "usage": {
                "prompt_tokens": max(1, len(raw_body) // 4),
                "completion_tokens": 24,
                "total_tokens": max(1, len(raw_body) // 4) + 24,
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
