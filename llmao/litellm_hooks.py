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

"""Turn a silent truncation into an error that says what to do.

Runs INSIDE the LiteLLM proxy process, which is what separates this from
llmao/proxy/: that package is our client for LiteLLM's admin API, this is
code LiteLLM loads and calls. Wired up in litellm.yaml:

    litellm_settings:
      callbacks: ["llmao.litellm_hooks.empty_completion_guard"]

THE FAILURE

A reasoning model emits its thinking FIRST, into the SAME budget as the
answer. When max_tokens runs out during the thinking phase the request
succeeds -- HTTP 200, finish_reason "length" -- and the content is empty.
Nothing in the response says the budget was the problem, so the client
shows a blank reply or "Response was truncated before completion", and a
user cannot tell this apart from the model being broken.

Measured on qwen3.8-27b through the proxy, one moderately hard question:

    max_tokens  reasoning   result
    500         on          finish=length, 500 tokens, NO content
    1000        on          finish=length, 1000 tokens, NO content
    2000        on          finish=length, 2000 tokens, NO content
    4000        on          finish=stop,  3455 tokens, answered
    500         off         finish=length, 500 tokens, answered then cut

reasoning_effort does not rescue it: low, medium and xhigh all returned
nothing at 2000, because the floor is the thinking phase itself -- roughly
3000 tokens for that question, growing with the difficulty of the task and
the length of the input.

The last row is why this guard is narrow. With reasoning off, too small a
budget truncates the ANSWER, which is a failure every user already
understands and should not be turned into an error. Only the empty case is
incomprehensible, so only the empty case is rejected.

WHY IT IS WORTH THE CODE

In one day this single behaviour was reported as a rate limit, a
context-window overflow, a routing bug and a dead model, by ten different
clients including a bare curl. Because it is indistinguishable from success
at the HTTP layer, every diagnosis started from nothing.

WHAT IT DOES NOT COVER

Streaming. LiteLLM dispatches streamed responses through
async_post_call_streaming_iterator_hook, and by then the client already has
a 200 and an open stream; raising would break the stream rather than
explain it. Streaming callers still get silence, and the Models page
documents the budget floor for them.
"""

from __future__ import annotations

import logging
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_LOGGER = logging.getLogger(__name__)

# Roughly what the 27B needs before it says anything, measured. Quoted so a
# caller has a number to act on rather than "raise max_tokens".
_HEADROOM_TOKENS = 4000


def _first_choice(response: Any) -> Any:
    try:
        return response.choices[0]
    except (AttributeError, IndexError, TypeError):
        return None


def _finish_reason(response: Any) -> str | None:
    choice = _first_choice(response)
    return getattr(choice, "finish_reason", None) if choice is not None else None


def _message(response: Any) -> Any:
    """The assistant message, or None if this is not a chat completion.

    None is the signal to stay out of the way entirely. Embeddings, batch
    results and any response shape a future LiteLLM introduces all land
    here, and a guard that raises on a shape it does not recognise is worse
    than the silence it was written to fix.
    """
    choice = _first_choice(response)
    return getattr(choice, "message", None) if choice is not None else None


def _answer(message: Any) -> str:
    """The answer text, ignoring reasoning. Empty string when there is none."""
    return (getattr(message, "content", None) or "").strip()


def _has_tool_calls(message: Any) -> bool:
    """A tool call IS an answer, even with no content beside it."""
    return bool(getattr(message, "tool_calls", None))


def _reasoning_chars(message: Any) -> int:
    # LiteLLM returns reasoning_content and duplicates it under
    # provider_specific_fields.reasoning; see tests/test_chat_template_kwargs.py,
    # where reading the wrong one made a working parameter look dropped.
    for field in ("reasoning_content", "reasoning"):
        value = getattr(message, field, None)
        if value:
            return len(str(value))
    return 0


def explain(model: str, requested: int | None, reasoning_chars: int) -> str:
    """The message a caller gets. Separate so it can be read in a test."""
    budget = f"max_tokens={requested:,}" if requested else "its entire output budget"
    spent = f" It produced {reasoning_chars:,} characters of reasoning and no answer." if reasoning_chars else ""
    return (
        f"{model} used {budget} before producing any answer.{spent}"
        f" This model reasons before it answers, and the reasoning comes out of"
        f" the same budget as the reply. Either raise max_tokens (around"
        f" {_HEADROOM_TOKENS:,} is enough for most questions), or send"
        f' "chat_template_kwargs": {{"enable_thinking": false}} to skip'
        f" reasoning and get an answer immediately."
    )


class EmptyCompletionGuard(CustomLogger):
    """Reject a 200 that carries no answer, with a message explaining why.

    Returns the response untouched for everything else. A guard that alters
    successful completions would be worse than the problem it solves.
    """

    async def async_post_call_success_hook(
        self,
        data: dict,
        user_api_key_dict: Any,
        response: Any,
    ) -> Any:
        if _finish_reason(response) != "length":
            return response
        message = _message(response)
        if message is None:
            return response
        if _answer(message) or _has_tool_calls(message):
            return response

        model = data.get("model") or getattr(response, "model", None) or "This model"
        requested = data.get("max_tokens") or data.get("max_completion_tokens")
        chars = _reasoning_chars(message)

        _LOGGER.warning(
            "empty completion: model=%s max_tokens=%s reasoning_chars=%d",
            model,
            requested,
            chars,
        )
        # litellm/proxy/utils.py re-raises whatever this hook raises, so the
        # caller gets an error instead of an empty 200.
        raise ValueError(explain(str(model), requested, chars))


# The instance litellm.yaml refers to by dotted path.
empty_completion_guard = EmptyCompletionGuard()
