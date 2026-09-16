"""Regression test for Issue #31: chat_template_kwargs.enable_thinking survival through LiteLLM Proxy.

Verifies that when a client sends a chat completion HTTP request containing
chat_template_kwargs (enable_thinking=True / False) to LiteLLM Proxy's HTTP API
(/v1/chat/completions) with drop_params: true, the parameter survives proxy HTTP
transformation and arrives at the downstream backend unchanged.
"""

import asyncio
import json
import os
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import litellm
import yaml


class _MockBackendHandler(BaseHTTPRequestHandler):
    received_requests = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _MockBackendHandler.received_requests.append(json.loads(body))

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1677858288,
            "model": "mock-vllm-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="module")
def mock_backend_url():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        _, port = s.getsockname()
    server = ThreadingHTTPServer(("127.0.0.1", port), _MockBackendHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.mark.parametrize("enable_thinking", [True, False])
def test_chat_template_kwargs_enable_thinking_survives_proxy_http(
    mock_backend_url, enable_thinking
):
    """End-to-end verification: chat_template_kwargs.enable_thinking survives proxy HTTP forwarding."""

    async def run():
        _MockBackendHandler.received_requests.clear()

        config = {
            "litellm_settings": {"drop_params": True},
            "general_settings": {"master_key": "sk-test"},
            "model_list": [
                {
                    "model_name": "test-gemma",
                    "litellm_params": {
                        "model": "hosted_vllm/google/gemma-4-26B-A4B-it",
                        "api_base": mock_backend_url,
                        "api_key": "sk-vllm",
                    },
                }
            ],
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(config, f)
            config_path = f.name

        from litellm.proxy.proxy_server import app, initialize

        orig_drop = getattr(litellm, "drop_params", False)

        try:
            await initialize(config=config_path)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
            ) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer sk-test"},
                    json={
                        "model": "test-gemma",
                        "messages": [{"role": "user", "content": "hi"}],
                        "chat_template_kwargs": {"enable_thinking": enable_thinking},
                    },
                )
                assert resp.status_code == 200, resp.text
                assert len(_MockBackendHandler.received_requests) == 1
                received = _MockBackendHandler.received_requests[0]
                assert "chat_template_kwargs" in received
                assert received["chat_template_kwargs"] == {
                    "enable_thinking": enable_thinking
                }
        finally:
            litellm.drop_params = orig_drop
            if os.path.exists(config_path):
                os.remove(config_path)

    asyncio.run(run())
