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

"""The prompt log's record, file naming and reading (llmao/prompt_log.py)."""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import subprocess
import sys

from easydict import EasyDict as edict  # noqa: N813

from llmao import prompt_log

DAY = dt.date(2026, 11, 1)
T0 = 1793525400.0  # 2026-11-01T09:30:00Z


def _payload(**over):
    """Enough of LiteLLM's StandardLoggingPayload for the record, as 1.105.0 fills it."""
    p = dict(
        litellm_call_id="call-1",
        trace_id="trace-1",
        call_type="acompletion",
        status="success",
        stream=True,
        custom_llm_provider="hosted_vllm",
        total_tokens=610,
        prompt_tokens=580,
        completion_tokens=30,
        startTime=T0,
        completionStartTime=T0 + 0.42,
        endTime=T0 + 2.5,
        model="qwen3-8b",
        model_group="qwen3-8b",
        api_base="http://203.0.113.10:8001/v1",
        metadata=dict(
            user_api_key_hash="hash-1",
            user_api_key_alias="someone-laptop",
            user_api_key_user_id="someone",
            user_api_key_team_alias="tooling",
            user_agent="OpenAI/Python 1.109.1",
            requester_custom_headers={"x-tool": "opencode"},
        ),
        end_user="t",
        user_agent="OpenAI/Python 1.109.1",
        messages=[{"role": "user", "content": "what is 17 x 23"}],
        response={"choices": [{"finish_reason": "stop", "message": {"content": "391"}}]},
        error_information=None,
        model_parameters={"temperature": 0.2, "max_tokens": 200},
    )
    p.update(over)
    return p


def test_the_record_keeps_the_prompt_and_the_use_but_not_the_response():
    rec = prompt_log.record(_payload())
    assert rec["ts"] == "2026-11-01T09:30:02.500Z"
    assert (rec["duration_s"], rec["ttft_s"]) == (2.5, 0.42)
    assert (rec["call_id"], rec["trace_id"], rec["call_type"]) == ("call-1", "trace-1", "acompletion")
    assert (rec["status"], rec["error"]) == ("success", None)
    assert (rec["model"], rec["deployment"], rec["provider"]) == ("qwen3-8b", "qwen3-8b", "hosted_vllm")
    assert rec["api_base"] == "http://203.0.113.10:8001/v1"
    assert rec["stream"] is True
    assert rec["tokens"] == {"prompt": 580, "completion": 30, "total": 610}
    assert rec["finish_reason"] == "stop"
    assert rec["key"] == {"hash": "hash-1", "alias": "someone-laptop", "user": "someone", "project": "tooling"}
    assert (rec["end_user"], rec["user_agent"]) == ("t", "OpenAI/Python 1.109.1")
    assert rec["client_headers"] == {"x-tool": "opencode"}
    assert rec["params"] == {"temperature": 0.2, "max_tokens": 200}
    assert rec["messages"] == [{"role": "user", "content": "what is 17 x 23"}]
    # The response's text is nowhere in it.
    assert "391" not in json.dumps(rec)


def test_a_tool_schema_with_an_array_parameter_is_written_and_read_back(tmp_path):
    # {"items": ...} is JSON Schema's array element type; a dict that mirrors
    # keys as attributes loses its items() method to it and json.dumps fails.
    tools = [{"type": "function", "function": {"parameters": {"type": "array", "items": {"type": "string"}}}}]
    rec = prompt_log.record(_payload(model_parameters={"tools": tools}))
    (tmp_path / prompt_log.day_file_name(DAY)).write_text(json.dumps(rec, default=str) + "\n")
    [back] = prompt_log.read_day(tmp_path, DAY)
    assert back["params"]["tools"] == tools
    assert json.loads(json.dumps(back)) == back


def test_a_plain_request_has_no_time_to_first_token():
    rec = prompt_log.record(_payload(stream=False))
    assert rec["stream"] is False and rec["ttft_s"] is None


def test_a_failure_keeps_the_error_class_and_code_but_never_its_message():
    payload = _payload(
        status="failure",
        response=None,
        error_information=dict(
            error_class="RateLimitError",
            error_code="429",
            llm_provider="hosted_vllm",
            normalized_error="429_RATE_LIMIT_EXCEEDED",
            error_message="Rate limited while handling: what is 17 x 23",
            traceback="...",
        ),
    )
    rec = prompt_log.record(payload)
    assert rec["status"] == "failure"
    assert rec["error"] == {
        "class": "RateLimitError",
        "code": "429",
        "provider": "hosted_vllm",
        "normalized": "429_RATE_LIMIT_EXCEEDED",
    }
    assert rec["finish_reason"] is None
    assert "Rate limited" not in json.dumps(rec)


def test_what_the_payload_lacks_is_null_not_an_error():
    rec = prompt_log.record({})
    assert rec["ts"] is None and rec["duration_s"] is None and rec["model"] is None
    assert rec["tokens"] == {"prompt": 0, "completion": 0, "total": 0}
    assert rec["key"] == {"hash": None, "alias": None, "user": None, "project": None}
    assert rec["client_headers"] is None and rec["params"] == {}


def test_a_day_file_is_named_for_its_utc_day():
    assert prompt_log.day_file_name(DAY) == "prompts-2026-11-01.jsonl"
    assert prompt_log.SINK_PATTERN == "prompts-{time:YYYY-MM-DD!UTC}.jsonl"


def test_a_day_is_read_plain_or_gzipped_and_a_broken_line_is_skipped(tmp_path):
    plain = tmp_path / "prompts-2026-11-01.jsonl"
    plain.write_text('{"call_id": "a"}\n{"call_id": "b"\n{"call_id": "c"}\n')
    assert [r["call_id"] for r in prompt_log.read_day(tmp_path, DAY)] == ["a", "c"]
    # After rotation the day is gzipped; the plain file is gone.
    plain.unlink()
    with gzip.open(tmp_path / "prompts-2026-11-01.jsonl.gz", "wt") as f:
        f.write('{"call_id": "z"}\n')
    assert [r["call_id"] for r in prompt_log.read_day(tmp_path, DAY)] == ["z"]
    # A day with no file at all is simply empty.
    assert list(prompt_log.read_day(tmp_path, dt.date(2026, 10, 31))) == []


# ---- the rollup of a day


def _rec(hour, model="qwen3-8b", **over):
    r = dict(
        ts=f"2026-11-01T{hour:02d}:30:00.000Z",
        model=model,
        stream=False,
        ttft_s=None,
        finish_reason="stop",
        params={},
        key={"project": "tooling"},
    )
    r.update(over)
    return r


def test_what_counts_as_demand_is_decided_once_for_both_sides():
    assert prompt_log.counts_as_demand("qwen3-8b", "tooling")
    assert not prompt_log.counts_as_demand(None, "tooling")
    assert not prompt_log.counts_as_demand("", "tooling")
    assert not prompt_log.counts_as_demand("qwen3-8b", prompt_log.HEALTH_CHECK_TEAM)
    assert not prompt_log.counts_as_demand("0a1b2c3d-0a1b-0a1b-0a1b-0a1b2c3d4e5f", "tooling")


def test_a_day_rolls_up_per_model_per_hour_with_what_only_the_log_can_say():
    out = prompt_log.rollup(
        [
            _rec(9, stream=True, ttft_s=0.5, params={"tools": [{}]}),
            _rec(9, stream=True, ttft_s=2.0, finish_reason="length"),
            _rec(9, stream=False),
            _rec(10, model="gemma4-26b"),
            # Not demand: a health check, a deployment id, a record with no time.
            _rec(10, key={"project": prompt_log.HEALTH_CHECK_TEAM}),
            _rec(10, model="0a1b2c3d-0a1b-0a1b-0a1b-0a1b2c3d4e5f"),
            _rec(10, ts=None),
        ]
    )
    assert out.records == 7
    assert [(h.hour, h.model, h.requests) for h in out.hours] == [
        ("2026-11-01T09", "qwen3-8b", 3),
        ("2026-11-01T10", "gemma4-26b", 1),
    ]
    nine = out.hours[0]
    assert (nine.streamed, nine.with_tools, nine.finish_length) == (2, 1, 1)
    assert (nine.ttft_p50_s, nine.ttft_p95_s) == (0.5, 2.0)
    assert out.hours[1].ttft_p50_s is None


def test_the_tool_prints_the_rollup_and_says_whether_the_day_had_a_file(tmp_path):
    (tmp_path / "prompts-2026-11-01.jsonl").write_text(json.dumps(_rec(9)) + "\n")
    tool = prompt_log.TOOL
    run = subprocess.run(
        [sys.executable, str(tool), "--log-dir", str(tmp_path), "rollup", "--json", "2026-11-01"],
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(run.stdout)
    assert (out["day"], out["found"], out["records"]) == ("2026-11-01", True, 1)
    assert out["hours"][0]["model"] == "qwen3-8b"
    absent = json.loads(
        subprocess.run(
            [sys.executable, str(tool), "--log-dir", str(tmp_path), "rollup", "--json", "2026-10-31"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert absent == {"day": "2026-10-31", "found": False, "records": 0, "hours": []}


def test_rollup_day_spawns_the_tool_and_a_tool_that_fails_is_an_error_with_its_last_words(tmp_path):
    (tmp_path / "prompts-2026-11-01.jsonl").write_text(json.dumps(_rec(9)) + "\n")
    result = asyncio.run(prompt_log.rollup_day(DAY, tmp_path))
    assert result.found and result.hours[0].requests == 1
    broken = tmp_path / "broken"
    broken.write_text("import sys; print('the disk is on fire', file=sys.stderr); sys.exit(3)\n")
    try:
        asyncio.run(prompt_log.rollup_day(DAY, tmp_path, tool=broken))
    except prompt_log.RollupError as e:
        assert "exited 3" in str(e) and "the disk is on fire" in str(e)
    else:
        raise AssertionError("a failed tool must be an error")
    slow = tmp_path / "slow"
    slow.write_text("import time; time.sleep(5)\n")
    try:
        asyncio.run(prompt_log.rollup_day(DAY, tmp_path, tool=slow, timeout_s=0.5))
    except prompt_log.RollupError as e:
        assert "longer than" in str(e)
    else:
        raise AssertionError("a tool past its timeout must be an error")


def test_the_tool_requires_its_log_directory(tmp_path):
    run = subprocess.run(
        [sys.executable, str(prompt_log.TOOL), "rollup", "--json", "2026-11-01"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode != 0
    assert "--log-dir is required" in run.stderr


def test_the_log_directory_is_required_in_config():
    assert prompt_log.log_dir(edict(prompt_log_dir="/x1/llmao/prompt-log")).as_posix() == "/x1/llmao/prompt-log"
    try:
        prompt_log.log_dir(edict())
    except ValueError as e:
        assert "prompt_log_dir is required" in str(e)
    else:
        raise AssertionError("missing prompt_log_dir must stop startup")


# ---- the helpers both sides share, and the reports


def test_tools_are_the_product_before_the_slash_and_browsers_are_one():
    assert prompt_log.tool_family("opencode/latest/2.2.1/acp") == "opencode"
    assert prompt_log.tool_family("codex-tui/0.166.0 (Linux Unknown; x86_64)") == "codex-tui"
    assert prompt_log.tool_family("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36") == "browser"
    assert prompt_log.tool_family(None) == "unknown"
    assert prompt_log.tool_family("curl") == "curl"


def test_prompt_length_buckets_cover_every_length():
    lengths = (0, 999, 1_000, 7_999, 8_000, 31_999, 32_000, 127_999, 128_000, 10**7)
    assert [prompt_log.prompt_bucket(n) for n in lengths] == [
        "<1k", "<1k", "1k-8k", "1k-8k", "8k-32k", "8k-32k", "32k-128k", "32k-128k", ">128k", ">128k",
    ]  # fmt: skip


def _full(hour, model="qwen3-8b", **over):
    r = dict(
        ts=f"2026-11-01T{hour:02d}:30:00.000Z",
        model=model,
        status="success",
        error=None,
        stream=False,
        finish_reason="stop",
        params={},
        tokens={"prompt": 500, "completion": 20, "total": 520},
        key={"project": "tooling", "user": "someone"},
        user_agent="OpenAI/Python 1.109.1",
        messages=[{"role": "user", "content": "x"}],
    )
    r.update(over)
    return r


def test_the_reports_count_requests_by_who_what_and_how_without_reading_a_prompt():
    rep = prompt_log.report(
        [
            _full(9, stream=True, params={"tools": [{}]}, messages=[{}, {}, {}]),
            _full(9, key={"project": "spark", "user": None}, user_agent="opencode/1", tokens={"prompt": 9000}),
            _full(10, model="gemma4-26b", finish_reason="length"),
            _full(10, status="failure", error={"class": "RateLimitError"}, tokens={"prompt": 0}),
            _full(11, key={"project": prompt_log.HEALTH_CHECK_TEAM}),  # not demand
        ]
    )
    assert rep.records == 4 and rep.days == ["2026-11-01"]
    assert [(r.name, r.requests, r.prompt_tokens) for r in rep.by_project] == [("tooling", 3, 1000), ("spark", 1, 9000)]
    assert [(r.name, r.requests) for r in rep.by_user] == [("someone", 3), ("(automation)", 1)]
    assert [(r.name, r.requests) for r in rep.by_model] == [("qwen3-8b", 3), ("gemma4-26b", 1)]
    assert [(r.name, r.requests) for r in rep.by_agent] == [("OpenAI", 3), ("opencode", 1)]
    (gemma, qwen) = rep.prompt_len
    assert (qwen.model, qwen["<1k"], qwen["8k-32k"]) == ("qwen3-8b", 1, 1)  # the failure is not a length
    assert (gemma.model, gemma["<1k"]) == ("gemma4-26b", 1)
    assert [(s.model, s.requests, s.streamed, s.with_tools, s.finish_length) for s in rep.shares] == [
        ("gemma4-26b", 1, 0, 0, 1),
        ("qwen3-8b", 3, 1, 1, 0),
    ]
    assert [(f.model, f.error, f.count) for f in rep.failures] == [("qwen3-8b", "RateLimitError", 1)]
    assert [(c.model, c.p50, c.p95) for c in rep.conversation] == [("gemma4-26b", 1, 1), ("qwen3-8b", 1, 3)]
