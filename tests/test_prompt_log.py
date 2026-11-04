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

import datetime as dt
import gzip
import json

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
