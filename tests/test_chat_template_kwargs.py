# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Regression test for Issue #31: chat_template_kwargs.enable_thinking survival through LiteLLM Proxy.

Verifies that when a client sends a chat completion HTTP request containing
chat_template_kwargs (enable_thinking=True / False) to LiteLLM Proxy's HTTP API
(/v1/chat/completions) with drop_params: true, the parameter survives proxy HTTP
transformation and arrives at the downstream backend unchanged.

#31 was filed as a proxy bug and turned out not to be one: the parameter was
arriving correctly all along, and the check was reading the wrong response
field. LiteLLM returns reasoning as `reasoning_content`, and duplicates it
under `provider_specific_fields.reasoning`; reading `message.reasoning`
reports zero every time and looks exactly like a dropped parameter.

The test is still worth having. It pins the behaviour so a future LiteLLM
version that DOES start dropping the kwarg is caught here rather than in a
pipeline run, where the symptom is a reasoning model quietly ignoring its
off switch.
"""

import asyncio
import json
import pathlib
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import httpx
import litellm
import pytest
import yaml


class _MockBackendHandler(BaseHTTPRequestHandler):
    # Shared across instances by design: BaseHTTPRequestHandler is constructed
    # per request, so per-instance storage would discard every request the
    # test needs to assert on. ClassVar makes that explicit rather than
    # incidental.
    received_requests: ClassVar[list[dict]] = []

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
def test_chat_template_kwargs_enable_thinking_survives_proxy_http(mock_backend_url, enable_thinking):
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
                        # hosted_vllm/, matching what llmao registers. The
                        # prefix decides which transformation the request goes
                        # through, so testing under openai/ would not exercise
                        # the path the fleet actually uses.
                        "model": "hosted_vllm/google/gemma-4-26B-A4B-it",
                        "api_base": mock_backend_url,
                        "api_key": "sk-vllm",
                    },
                }
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
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
                assert received["chat_template_kwargs"] == {"enable_thinking": enable_thinking}
        finally:
            litellm.drop_params = orig_drop
            # Blocking, and deliberately so: unlinking one tempfile in a
            # finally block does not warrant a thread hop, and the loop is
            # about to exit anyway. missing_ok rather than exists()-then-
            # remove -- one syscall, and no window between the two.
            pathlib.Path(config_path).unlink(missing_ok=True)

    asyncio.run(run())
