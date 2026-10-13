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
import types

import pytest

from llmao.litellm_hooks import empty_completion_guard, explain


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
