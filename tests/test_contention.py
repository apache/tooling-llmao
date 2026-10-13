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
"""Contention sampling (llmao/contention.py).

The pilot's one unanswerable-after-the-fact question: did anyone wait?
"""

from llmao.contention import parse_metrics


def test_parse_metrics_reads_the_four_figures():
    text = "\n".join(
        [
            "# HELP vllm:num_requests_running whatever",
            'vllm:num_requests_running{engine="0",model_name="x"} 4.0',
            'vllm:num_requests_waiting{engine="0",model_name="x"} 2.0',
            'vllm:gpu_cache_usage_perc{engine="0",model_name="x"} 0.31',
            'vllm:num_preemptions_total{engine="0",model_name="x"} 3.0',
        ]
    )
    got = parse_metrics("gemma4-26b", text, now=1.0).as_dict()
    assert got["running"] == 4.0
    assert got["waiting"] == 2.0
    assert got["preempted"] == 3.0
    # exposed as a fraction, reported as a percentage
    assert got["kv_pct"] == 31.0


def test_parse_metrics_survives_an_unexpected_body():
    """A box returning HTML, or nothing, must not stop the sampler."""
    for body in ("", "<html>503</html>", "vllm:num_requests_running{} notanumber"):
        got = parse_metrics("x", body, now=1.0)
        assert got.running == 0.0
