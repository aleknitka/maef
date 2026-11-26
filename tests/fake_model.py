"""Fake OpenAI-compatible chat-completions server for tests and the wheel smoke check.

It answers POST requests with a chat completion and never calls a real model, so it costs nothing:

- the first request (no tool message yet) asks for the `current_time` tool;
- a request that already carries a tool message gets the final text answer.

Every reply carries `usage` so the budget path sees token counts. Each reply can be delayed with the
file named by FAKE_LATENCY_FILE (seconds, read per request; default 0).

Run it as a standalone server:

    python tests/fake_model.py --port 9911

and point an `openai-compatible` model profile at http://127.0.0.1:9911/v1.
"""

import argparse
import itertools
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LATENCY_FILE = os.environ.get("FAKE_LATENCY_FILE", "")
_counter = itertools.count(1)


def _latency() -> float:
    if not LATENCY_FILE:
        return 0.0
    try:
        with open(LATENCY_FILE, encoding="utf-8") as handle:
            return float(handle.read().strip())
    except (OSError, ValueError):
        return 0.0


def _completion(body: dict, number: int) -> dict:
    if any(message.get("role") == "tool" for message in body.get("messages", [])):
        message = {"role": "assistant", "content": f"The time is now (fake) #{number}."}
        finish_reason, usage = "stop", {"prompt_tokens": 160, "completion_tokens": 30}
    else:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "current_time", "arguments": "{}"}}
            ],
        }
        finish_reason, usage = "tool_calls", {"prompt_tokens": 100, "completion_tokens": 20}
    usage = {**usage, "total_tokens": usage["prompt_tokens"] + usage["completion_tokens"]}
    return {
        "id": f"fake-{number}",
        "object": "chat.completion",
        "model": body.get("model", "fake"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # keep test output quiet
        pass

    def do_POST(self):  # http.server naming
        length = int(self.headers.get("content-length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "invalid JSON"}})
            return
        time.sleep(_latency())
        self._send(200, _completion(body, next(_counter)))

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeModelServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


def make_server(port: int = 0, host: str = "127.0.0.1") -> FakeModelServer:
    """A bound, not yet serving, fake model. Port 0 picks a free port (see server_address)."""
    return FakeModelServer((host, port), _Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=9911)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = make_server(args.port, args.host)
    print(f"fake model listening on http://{args.host}:{server.server_address[1]}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
