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

"""The empty-completion guard rejects silence and nothing else.

Most of these assert the guard STAYS OUT OF THE WAY. A post-call hook that
raises on a response it should have passed through breaks every caller at
once, and it runs on every non-streaming completion the gateway serves, so
the false-positive cases matter more than the one it exists to catch.
"""

import asyncio
import datetime as dt
import json
import types

import litellm
import pytest

import litellm_hooks
from litellm_hooks import empty_completion_guard, explain


def _response(finish_reason, content=None, reasoning=None, tool_calls=None, model="qwen3.8-27b"):
    """Enough of a LiteLLM ModelResponse for the guard to read."""
    message = types.SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)
    choice = types.SimpleNamespace(finish_reason=finish_reason, message=message)
    return types.SimpleNamespace(choices=[choice], model=model)


def _run(data, response):
    return asyncio.run(
        empty_completion_guard.async_post_call_success_hook(data=data, user_api_key_dict=None, response=response)
    )


# ------------------------------------------------------------------ the catch


def test_budget_spent_on_reasoning_is_rejected():
    """The measured failure: finish=length, reasoning present, no answer."""
    resp = _response("length", content="", reasoning="x" * 9000)
    with pytest.raises(ValueError) as excinfo:
        _run({"model": "qwen3.8-27b", "max_tokens": 500}, resp)
    message = str(excinfo.value)
    # A user has to be able to act on this without reading the source.
    assert "max_tokens=500" in message
    assert "enable_thinking" in message
    assert "9,000 characters of reasoning" in message


def test_content_none_counts_as_empty():
    """vLLM returns content: null rather than "" for this case."""
    with pytest.raises(ValueError):
        _run({"model": "qwen3.8-27b", "max_tokens": 1000}, _response("length", content=None))


def test_whitespace_only_counts_as_empty():
    with pytest.raises(ValueError):
        _run({"model": "qwen3.8-27b"}, _response("length", content="  \n "))


def test_max_completion_tokens_is_read_too():
    """Newer clients send max_completion_tokens rather than max_tokens."""
    with pytest.raises(ValueError) as excinfo:
        _run({"model": "qwen3.8-27b", "max_completion_tokens": 250}, _response("length", content=""))
    assert "max_tokens=250" in str(excinfo.value)


# -------------------------------------------------------------- the pass-overs


def test_truncated_answer_is_left_alone():
    """A cut-off ANSWER is a failure users already understand.

    This is what a non-reasoning model does with too small a budget, and
    turning it into an error would be a regression for every such caller.
    """
    resp = _response("length", content="To explain why Dijkstra fails on graphs")
    assert _run({"model": "gemma4-26b", "max_tokens": 500}, resp) is resp


def test_normal_completion_is_left_alone():
    resp = _response("stop", content="Hello!")
    assert _run({"model": "gemma4-26b"}, resp) is resp


def test_empty_content_with_finish_stop_is_left_alone():
    """Only `length` means the budget ran out. Anything else is not ours."""
    resp = _response("stop", content="")
    assert _run({"model": "gemma4-26b"}, resp) is resp


def test_truncated_tool_call_is_left_alone():
    """A tool call is an answer, even with no prose beside it."""
    resp = _response("length", content=None, tool_calls=[{"id": "call_1"}])
    assert _run({"model": "gemma4-26b"}, resp) is resp


def test_malformed_response_is_left_alone():
    """Never turn an unexpected response shape into an error of our own."""
    for broken in (
        types.SimpleNamespace(choices=[]),
        types.SimpleNamespace(),
        types.SimpleNamespace(choices=[types.SimpleNamespace(finish_reason="length")]),
    ):
        assert _run({"model": "x"}, broken) is broken


def test_embedding_style_response_is_left_alone():
    """The hook runs on every call type, not just chat completions."""
    resp = types.SimpleNamespace(data=[{"embedding": [0.1]}], model="nomic")
    assert _run({"model": "nomic"}, resp) is resp


# ----------------------------------------------------------------- the message


def test_message_without_a_requested_budget():
    """Some callers send no max_tokens at all; the text must still read."""
    message = explain("qwen3.8-27b", None, 0)
    assert "its entire output budget" in message
    assert "characters of reasoning" not in message


# ================================================================ prompt log
#
# The apparatus (settings, build-once, fail-at-start), the pre-call strip,
# and the body: one record per event, written to the day's file.


@pytest.fixture
def unbuilt(monkeypatch):
    """A fresh module: no instance built, no settings on litellm."""
    built = litellm_hooks._prompt_log
    if built is not None:
        built.close()
    monkeypatch.setattr(litellm_hooks, "_prompt_log", None)
    monkeypatch.setattr(litellm, "callback_settings", {}, raising=False)
    yield
    built = litellm_hooks._prompt_log
    if built is not None:
        built.close()


def _settings(monkeypatch, **section):
    section.setdefault("retention_days", 90)
    monkeypatch.setattr(litellm, "callback_settings", {"prompt_log": section}, raising=False)


def test_import_builds_nothing(unbuilt):
    """Importing the file, as every test here does, must not need litellm.yaml."""
    assert litellm_hooks._prompt_log is None
    assert isinstance(litellm_hooks.empty_completion_guard, litellm_hooks.EmptyCompletionGuard)


def test_missing_section_stops_the_proxy(unbuilt):
    with pytest.raises(RuntimeError, match=r"callback_settings\.prompt_log is missing"):
        _ = litellm_hooks.prompt_log


def test_missing_dir_setting_stops_the_proxy(unbuilt, monkeypatch):
    _settings(monkeypatch)
    with pytest.raises(RuntimeError, match=r"callback_settings.prompt_log.dir is missing"):
        _ = litellm_hooks.prompt_log


def test_missing_or_bad_retention_stops_the_proxy(unbuilt, monkeypatch, tmp_path):
    for bad in (None, "soon", 0):
        monkeypatch.setattr(litellm_hooks, "_prompt_log", None)
        monkeypatch.setattr(litellm, "callback_settings", {"prompt_log": {"dir": str(tmp_path), "retention_days": bad}})
        with pytest.raises(RuntimeError, match=r"callback_settings.prompt_log.retention_days must be"):
            _ = litellm_hooks.prompt_log


def test_absent_directory_is_not_created(unbuilt, monkeypatch, tmp_path):
    """A missing directory in production means /x1 is not mounted."""
    missing = tmp_path / "prompt-log"
    _settings(monkeypatch, dir=str(missing))
    with pytest.raises(RuntimeError, match="not a writable directory"):
        _ = litellm_hooks.prompt_log
    assert not missing.exists()


def test_built_once(unbuilt, monkeypatch, tmp_path):
    _settings(monkeypatch, dir=str(tmp_path))
    first = litellm_hooks.prompt_log
    assert isinstance(first, litellm_hooks.PromptLog)
    assert (first.dir, first.retention_days) == (tmp_path, 90)
    assert litellm_hooks.prompt_log is first


def test_other_missing_names_still_raise(unbuilt):
    with pytest.raises(AttributeError):
        _ = litellm_hooks.no_such_hook


def _auth():
    """What LiteLLM's UserAPIKeyAuth carries that the hook reads."""
    return types.SimpleNamespace(user_id="someone", key_alias="someone-laptop", team_alias="tooling")


def _pre_call(data):
    return asyncio.run(litellm_hooks.prompt_log.async_pre_call_hook(_auth(), None, data, "completion"))


def test_pre_call_hook_returns_the_same_dict(unbuilt, monkeypatch, tmp_path):
    _settings(monkeypatch, dir=str(tmp_path))
    data = {"model": "x", "messages": [], "user": "t"}
    assert _pre_call(data) is data


# ----------------------------------------------------------- no opt-out
#
# What a client can send to switch logging off, and that the strip removes
# it. The surface itself is measured against a live proxy by
# bin/check-no-log.py; these pin the strip and LiteLLM's own decision.


def test_strip_removes_no_log():
    data = {"model": "x", "no-log": True, "stream": True}
    assert litellm_hooks._strip_opt_outs(data) == ["no-log"]
    assert data == {"model": "x", "stream": True}


def test_strip_reports_nothing_for_an_ordinary_request():
    data = {"model": "x", "metadata": {"headers": {"user-agent": "curl"}}}
    assert litellm_hooks._strip_opt_outs(data) == []


def test_opt_out_attempt_is_warned_with_the_user(unbuilt, monkeypatch, tmp_path, caplog):
    """Silently ignoring it is the design; silently is for the client, not for us."""
    _settings(monkeypatch, dir=str(tmp_path))
    data = {"model": "x", "no-log": True, "metadata": {"headers": {"x-litellm-enable-message-redaction": "true"}}}
    with caplog.at_level("WARNING", logger="litellm_hooks.prompt_log"):
        _pre_call(data)
    (record,) = caplog.records
    assert "someone" in record.getMessage()
    assert "no-log" in record.getMessage()
    assert "x-litellm-enable-message-redaction" in record.getMessage()


def test_ordinary_request_is_not_warned(unbuilt, monkeypatch, tmp_path, caplog):
    _settings(monkeypatch, dir=str(tmp_path))
    with caplog.at_level("WARNING", logger="litellm_hooks.prompt_log"):
        _pre_call({"model": "x", "messages": []})
    assert not caplog.records


def test_strip_removes_the_redaction_headers_everywhere_and_nothing_else():
    headers = {
        "litellm-enable-message-redaction": "true",
        "x-litellm-enable-message-redaction": "true",
        "user-agent": "curl",
    }
    data = {
        "headers": dict(headers),
        "metadata": {"headers": dict(headers), "user_api_key_alias": "k"},
        "litellm_metadata": {"headers": dict(headers)},
    }
    litellm_hooks._strip_opt_outs(data)
    for holder in (data, data["metadata"], data["litellm_metadata"]):
        assert holder["headers"] == {"user-agent": "curl"}
    assert data["metadata"]["user_api_key_alias"] == "k"


def test_strip_tolerates_requests_without_metadata():
    data = {"model": "x", "metadata": None}
    litellm_hooks._strip_opt_outs(data)
    assert data == {"model": "x", "metadata": None}


def test_litellm_would_redact_before_the_strip_and_not_after():
    """Pins the LiteLLM contract the strip relies on, against the pinned version."""
    from litellm.litellm_core_utils.redact_messages import should_redact_message_logging

    for header in litellm_hooks._REDACTION_HEADERS:
        data = {"metadata": {"headers": {header: "true"}}}
        assert should_redact_message_logging({"litellm_params": data})
        litellm_hooks._strip_opt_outs(data)
        assert not should_redact_message_logging({"litellm_params": data})


# ------------------------------------------------------------- the body


def _event(**payload):
    """kwargs as LiteLLM hands them to a success or failure event: the payload is what matters."""
    base = dict(
        litellm_call_id="call-1",
        call_type="acompletion",
        status="success",
        stream=False,
        startTime=1793525400.0,
        endTime=1793525401.0,
        model_group="qwen3-8b",
        model="qwen3-8b",
        prompt_tokens=5,
        completion_tokens=1,
        total_tokens=6,
        metadata=dict(user_api_key_user_id="someone", user_api_key_team_alias="tooling"),
        messages=[{"role": "user", "content": "hello from the test"}],
        response={"choices": [{"finish_reason": "stop", "message": {"content": "hi"}}]},
        model_parameters={},
    )
    base.update(payload)
    # The key's metadata rides in the request's params, where the proxy put it.
    params = {"metadata": {"user_api_key_metadata": {"purpose": "someone-laptop", "project": "tooling"}}}
    return {"litellm_call_id": "call-1", "standard_logging_object": base, "litellm_params": params}


def _today_file(directory):
    return directory / f"prompts-{dt.datetime.now(dt.UTC).date().isoformat()}.jsonl"


def test_each_event_is_one_record_line_in_the_days_file_owned_by_root_alone(unbuilt, monkeypatch, tmp_path):
    _settings(monkeypatch, dir=str(tmp_path))
    hook = litellm_hooks.prompt_log
    asyncio.run(hook.async_log_success_event(_event(), None, None, None))
    asyncio.run(
        hook.async_log_failure_event(
            _event(status="failure", response=None, litellm_call_id="call-2"), None, None, None
        )
    )
    hook.complete()
    path = _today_file(tmp_path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(r["call_id"], r["status"]) for r in lines] == [("call-1", "success"), ("call-2", "failure")]
    assert lines[0]["messages"] == [{"role": "user", "content": "hello from the test"}]
    assert lines[0]["key"] == {"hash": None, "purpose": "someone-laptop", "user": "someone", "project": "tooling"}
    # The response's text is not in the file.
    assert "hi" not in [v for r in lines for v in (r["finish_reason"],)]
    assert '"hi"' not in path.read_text()


def test_an_event_without_a_payload_is_warned_and_not_written(unbuilt, monkeypatch, tmp_path, caplog):
    _settings(monkeypatch, dir=str(tmp_path))
    hook = litellm_hooks.prompt_log
    with caplog.at_level("WARNING", logger="litellm_hooks.prompt_log"):
        asyncio.run(hook.async_log_success_event({"litellm_call_id": "call-9"}, None, None, None))
    hook.complete()
    assert "standard_logging_object" in caplog.text and "call-9" in caplog.text
    # loguru opens the day's file when the sink is added; nothing went into it.
    assert _today_file(tmp_path).read_text() == ""


def test_records_never_reach_stderr(unbuilt, monkeypatch, tmp_path, capsys):
    """loguru's default handler prints to stderr, which is the journal; it is removed."""
    _settings(monkeypatch, dir=str(tmp_path))
    hook = litellm_hooks.prompt_log
    asyncio.run(hook.async_log_success_event(_event(), None, None, None))
    hook.complete()
    assert "hello from the test" not in capsys.readouterr().err
