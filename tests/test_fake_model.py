import threading

import httpx

from fake_model import make_server


def test_fake_model_serves_tool_call_then_answer_with_usage():
    server = make_server(0)  # port 0: the OS picks a free port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/v1/chat/completions"
        question = {"model": "fake", "messages": [{"role": "user", "content": "What time is it?"}]}
        with httpx.Client(timeout=10) as client:
            first = client.post(url, json=question)
            assert first.status_code == 200
            first_body = first.json()
            assert first_body["choices"][0]["finish_reason"] == "tool_calls"
            assert first_body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "current_time"

            answer = client.post(
                url,
                json={
                    "model": "fake",
                    "messages": [*question["messages"], {"role": "tool", "tool_call_id": "call_1", "content": "12:00"}],
                },
            )
        assert answer.status_code == 200
        body = answer.json()
        assert body["object"] == "chat.completion"
        choice = body["choices"][0]
        assert choice["finish_reason"] == "stop"
        assert choice["message"]["role"] == "assistant" and choice["message"]["content"]
        usage = body["usage"]
        assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
