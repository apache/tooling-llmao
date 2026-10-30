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
"""Reading the boxes' /metrics (llmao/sampler.py)."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.capacity import HOUR_S, HourlyRollup
from llmao.fleet import Fleet, FleetDeployment, VllmServer
from llmao.sampler import MetricsSampler, sample_interval
from llmao.vllm_client import parse_kv_cache_tokens, parse_metrics

# As vLLM 0.27 writes it, trimmed.
METRICS = "\n".join(
    [
        "# HELP vllm:num_requests_running Number of requests in model execution batches.",
        'vllm:num_requests_running{engine="0",model_name="qwen3-8b"} 2.0',
        'vllm:num_requests_waiting{engine="0",model_name="qwen3-8b"} 1.0',
        'vllm:kv_cache_usage_perc{engine="0",model_name="qwen3-8b"} 0.25',
        'vllm:num_preemptions_total{engine="0",model_name="qwen3-8b"} 3.0',
        'vllm:prompt_tokens_total{engine="0",model_name="qwen3-8b"} 212.0',
        'vllm:generation_tokens_total{engine="0",model_name="qwen3-8b"} 131.0',
        'vllm:request_success_total{engine="0",finished_reason="stop",model_name="qwen3-8b"} 7.0',
        'vllm:request_success_total{engine="0",finished_reason="length",model_name="qwen3-8b"} 4.0',
        'vllm:request_queue_time_seconds_bucket{engine="0",le="0.3",model_name="qwen3-8b"} 10.0',
        'vllm:request_queue_time_seconds_bucket{engine="0",le="+Inf",model_name="qwen3-8b"} 11.0',
        'vllm:request_queue_time_seconds_sum{engine="0",model_name="qwen3-8b"} 0.9',
        'vllm:request_queue_time_seconds_count{engine="0",model_name="qwen3-8b"} 11.0',
    ]
)


def test_parse_metrics_reads_what_the_rollup_needs():
    got = parse_metrics(METRICS, now=1.0)
    assert (got.running, got.waiting, got.kv) == (2.0, 1.0, 0.25)
    assert got.counters == {
        "preemptions": 3.0,
        "prompt_tokens": 212.0,
        "generation_tokens": 131.0,
        # one line per finish reason, summed
        "requests": 11.0,
        "queue_s": 0.9,
        "queued": 11.0,
    }
    assert got.buckets == {0.3: 10.0, float("inf"): 11.0}


def test_parse_metrics_survives_an_unexpected_body():
    """A box answering with HTML, or nothing, must not stop the sampler."""
    for body in ("", "<html>503</html>", "vllm:num_requests_running{} notanumber"):
        got = parse_metrics(body, now=1.0)
        assert got.running == 0.0
        assert got.counters == {}


def test_the_interval_is_required_and_never_off():
    assert sample_interval(edict(sample_interval_s=15)) == 15
    with pytest.raises(ValueError, match="sample_interval_s is required"):
        sample_interval(edict())
    for bad in (0, -5):
        with pytest.raises(ValueError, match="never off"):
            sample_interval(edict(sample_interval_s=bad))
    with pytest.raises(ValueError, match="whole number"):
        sample_interval(edict(sample_interval_s="soon"))


def test_the_old_name_still_works_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="llmao.sampler"):
        assert sample_interval(edict(contention_interval_s=30)) == 30
    assert "contention_interval_s is deprecated; rename it to sample_interval_s" in caplog.text
    # The new name wins, silently.
    caplog.clear()
    assert sample_interval(edict(sample_interval_s=15, contention_interval_s=30)) == 15
    assert caplog.text == ""


def _server(host, *, public_port=8001):
    return VllmServer(
        model_name="qwen3-8b",
        name="qwen3-8b",
        host=host,
        listen_port=8001,
        hf_model="Qwen/Qwen3-8B",
        api_key="sk-x",
        args=[],
        public_port=public_port,
    )


def test_each_server_is_named_by_host_and_listen_port():
    reachable = _server("203.0.113.10", public_port=22018)
    fleet = Fleet(None, [reachable, _server("198.51.100.8", public_port=None)])
    fleet.deployments.append(FleetDeployment(model_name="grok-4", name="grok-4", self_hosted=False))
    sampler = MetricsSampler(fleet, HourlyRollup(15), interval_s=15)
    # Read at the public port; named by the listen port. No address, no reading.
    assert sampler.targets() == [(reachable, "203.0.113.10:8001", "qwen3-8b", "http://203.0.113.10:22018")]


def test_a_pass_feeds_the_rollup_and_notes_a_box_that_did_not_answer():
    fleet = Fleet(None, [_server("203.0.113.10"), _server("198.51.100.8")])
    rollup = HourlyRollup(15)
    sampler = MetricsSampler(fleet, rollup, interval_s=15)

    def answer(request):
        if request.url.host == "198.51.100.8":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, text=METRICS)

    async def one_pass():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            await sampler.sample_once(client)

    asyncio.run(one_pass())
    # An hour on, the reading's hour is finished.
    (hour,) = rollup.take_finished(time.time() + HOUR_S)
    assert hour.server == "203.0.113.10:8001"
    assert hour.readings == 1


_CACHE_INFO = """\
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="google/gemma-4-26B-A4B-it"} 0.0
# HELP vllm:cache_config_info Information of the LLMEngine CacheConfig
# TYPE vllm:cache_config_info gauge
vllm:cache_config_info{block_size="16",cache_dtype="auto",enable_prefix_caching="True",gpu_memory_utilization="0.9",kv_cache_max_concurrency="6.52951355508556",kv_cache_memory_bytes="None",kv_cache_size_tokens="855836",num_cpu_blocks="None",num_gpu_blocks="33960"} 1.0
"""


def test_parse_kv_cache_tokens_prefers_the_reported_count():
    """kv_cache_size_tokens is authoritative; the block product is not.

    Real output from the A100 serving Gemma reports 855,836 tokens while
    num_gpu_blocks * block_size gives 543,360 -- blocks do not map uniformly
    to tokens for a model with heterogeneous head dimensions. Taking the
    product would understate the cache by a third and falsely flag a
    correctly-sized server as oversized.
    """
    assert parse_kv_cache_tokens(_CACHE_INFO) == 855836
    assert 33960 * 16 == 543360  # what the wrong answer would have been


def test_parse_kv_cache_tokens_falls_back_to_block_product():
    """Builds that do not emit the token count still get an approximation."""
    text = 'vllm:cache_config_info{block_size="16",num_gpu_blocks="1000"} 1.0'
    assert parse_kv_cache_tokens(text) == 16000

    text = 'vllm:cache_config_info{block_size="16",num_gpu_blocks="1000",kv_cache_size_tokens="None"} 1.0'
    assert parse_kv_cache_tokens(text) == 16000


def test_parse_kv_cache_tokens_absent():
    """vLLM's metric names are not a stable API.

    A missing or renamed metric degrades the display rather than breaking the
    probe loop, so this returns None instead of raising.
    """
    assert parse_kv_cache_tokens("vllm:num_requests_running{} 0.0\n") is None
    assert parse_kv_cache_tokens("") is None


def test_parse_kv_cache_tokens_malformed():
    for text in (
        'vllm:cache_config_info{block_size="16"} 1.0',  # no block count
        'vllm:cache_config_info{block_size="x",num_gpu_blocks="1"} 1.0',
        "vllm:cache_config_info block_size=16 1.0",  # no braces
        'vllm:cache_config_info{block_size="0",num_gpu_blocks="0"} 1.0',
    ):
        assert parse_kv_cache_tokens(text) is None, text


def test_a_reading_carries_the_kv_cache_size_and_the_sampler_hands_it_to_the_fleet():
    assert parse_metrics(_CACHE_INFO, now=1.0).kv_cache_tokens == 855836
    srv = _server("203.0.113.10")
    fleet = Fleet(None, [srv])
    sampler = MetricsSampler(fleet, HourlyRollup(15), interval_s=15)

    async def one_pass():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text=METRICS + _CACHE_INFO))
        async with httpx.AsyncClient(transport=transport) as client:
            await sampler.sample_once(client)

    assert srv.kv_cache_tokens is None
    asyncio.run(one_pass())
    assert srv.kv_cache_tokens == 855836
