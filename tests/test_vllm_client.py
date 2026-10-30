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

"""One probe of one address, against a stand-in vLLM: what each answer becomes on the snapshot."""

import asyncio

import httpx

from llmao.vllm_client import VllmProbe, probe

ROOT = "http://10.0.0.1:8001"
MODELS = {"data": [{"id": "google/gemma", "max_model_len": 131072}]}


def _probe(answer, *, ask_models=True, now=10.0) -> tuple[VllmProbe, list[str]]:
    """One probe against a vLLM that answers ``answer(request)`` (a Response, or an exception to raise),
    and the paths it asked for."""
    asked = []

    def handle(request):
        asked.append(request.url.path)
        out = answer(request)
        if isinstance(out, Exception):
            raise out
        return out

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await probe(client, ROOT, "sk-x", ask_models=ask_models, now=now)

    return asyncio.run(run()), asked


def _vllm(health: int, models: int = 200, body=None):
    """A stand-in that answers /health and /v1/models with these statuses."""

    def answer(request):
        if request.url.path == "/health":
            return httpx.Response(health)
        assert str(request.url) == f"{ROOT}/v1/models", "the root is dialed, never a /v1-suffixed base"
        assert request.headers["Authorization"] == "Bearer sk-x"
        return httpx.Response(models, json=body if body is not None else MODELS)

    return answer


def test_serving_with_our_key():
    got, asked = _probe(_vllm(200))
    assert (got.reached, got.ok, got.error) == (True, True, None)
    assert got.served_status == 200
    assert got.key_rejected is False
    assert (got.max_model_len, got.hf_id) == (131072, "google/gemma")
    assert (got.url, got.at) == (ROOT, 10.0)
    assert asked == ["/health", "/v1/models"]


def test_up_but_refusing_our_key():
    got, _ = _probe(_vllm(200, models=401))
    assert got.ok is True
    assert got.served_status == 401
    assert got.key_rejected is True
    assert (got.max_model_len, got.hf_id) == (None, "")


def test_loading_answers_503_and_is_not_asked_for_its_models():
    got, asked = _probe(_vllm(503))
    assert (got.reached, got.ok, got.error) == (True, False, "HTTP 503")
    assert got.served_status is None
    assert got.key_rejected is None
    assert asked == ["/health"]


def test_not_asked_means_not_asked():
    """The fleet decides when /v1/models is worth a request; a passing probe alone does not ask."""
    got, asked = _probe(_vllm(200), ask_models=False)
    assert got.ok is True
    assert got.served_status is None
    assert got.key_rejected is None
    assert asked == ["/health"]


def test_no_connection_and_timeout_are_told_apart():
    def refused(request):
        return httpx.ConnectError("refused", request=request)

    def slow(request):
        return httpx.ConnectTimeout("slow", request=request)

    down, _ = _probe(refused)
    assert (down.reached, down.ok, down.error) == (False, False, "no connection")
    late, _ = _probe(slow)
    assert (late.reached, late.ok, late.error) == (False, False, "timeout")


def test_a_models_answer_with_no_window_leaves_it_unknown():
    got, _ = _probe(_vllm(200, body={"data": [{"id": "google/gemma"}]}))
    assert got.served_status == 200
    assert got.key_rejected is False
    assert (got.max_model_len, got.hf_id) == (None, "google/gemma")
