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

"""The hooks LiteLLM loads: the empty-completion guard and the prompt log.

Runs INSIDE the LiteLLM proxy process, which is what separates this from
llmao/proxy/: that package is our client for LiteLLM's admin API, this is
code LiteLLM loads and calls. Wired up in litellm.yaml:

    litellm_settings:
      callbacks: ["litellm_hooks.empty_completion_guard", "litellm_hooks.prompt_log"]

LiteLLM looks for this file in the directory that holds litellm.yaml, and
imports it by name if it is not there. It sits beside main.py because, like
main.py, it is what a process loads; llmao/ is the library.

The file-path load (litellm/proxy/types_utils/utils.py, get_instance_fn)
executes this file once PER NAME in the callbacks list and registers none
of them in sys.modules, so two entries mean two module objects. Nothing
here may rely on module state being shared between the hooks.

The prompt log is the second half of this file; the guard is first.

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

import json
import logging
import os
from pathlib import Path
from typing import Any

import litellm
from easydict import EasyDict as edict  # noqa: N813
from litellm.integrations.custom_logger import CustomLogger

# One logger per hook, named for the callback path litellm.yaml uses, so a
# log line says which hook spoke. Not __name__: the file-path load (see the
# module docstring) gives every copy the same __name__, "litellm_hooks".
_GUARD_LOGGER = logging.getLogger("litellm_hooks.empty_completion_guard")
_PROMPT_LOGGER = logging.getLogger("litellm_hooks.prompt_log")

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

        _GUARD_LOGGER.warning(
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


# ============================================================ the prompt log
#
# Design: docs/prompt-logging-design.md. This is the apparatus of step H1:
# the settings, the build-once, the fail-at-start, and the three methods
# LiteLLM calls. Its body is the step H1 PROBE, not the writer: it records
# what the hook sees for requests that try to opt out of logging, so the
# "No opt-out" section of the design can be written from fact. The probe
# body goes in step N3; the apparatus stays.
#
# Settings come from litellm.yaml, which LiteLLM stores on its module
# before it loads any callback (proxy_server.py, "callback_settings"):
#
#     callback_settings:
#       prompt_log:
#         dir: /x1/llmao/prompt-log
#
# The instance is built the first time LiteLLM asks this module for
# "prompt_log" (the module-level __getattr__ at the bottom), so a plain
# import of this file, as the tests do, builds nothing, and a proxy whose
# config cannot do what the terms promise does not start.

_PROMPT_LOG_SETTING = "callback_settings.prompt_log"


def _prompt_log_settings() -> edict:
    """The prompt_log section of litellm.yaml, or the error that names it."""
    section = (getattr(litellm, "callback_settings", None) or {}).get("prompt_log")
    if not isinstance(section, dict):
        raise RuntimeError(f"litellm.yaml: {_PROMPT_LOG_SETTING} is missing; the prompt log cannot start without it")
    return edict(section)


def _prompt_log_dir(settings: edict) -> Path:
    """The log directory, which must already exist and be writable.

    The hook does not create it. In production a missing directory means
    the data volume is not mounted, and a hook that created one would log
    to the root disk without anyone noticing. Puppet creates it.
    """
    raw = settings.get("dir")
    if not raw:
        raise RuntimeError(f"litellm.yaml: {_PROMPT_LOG_SETTING}.dir is missing")
    path = Path(raw)
    if not path.is_dir() or not os.access(path, os.W_OK):
        raise RuntimeError(
            f"litellm.yaml: {_PROMPT_LOG_SETTING}.dir is {path}, which is not a writable directory."
            " The hook does not create it; see docs/prompt-logging-design.md, Files."
        )
    return path


# What a client can send to switch logging off, measured on 1.105.0 with
# bin/check-no-log.py (docs/prompt-logging-design.md, "No opt-out"):
#
#   "no-log": true in the body
#       Skips every logging callback, ours included. The event never fires.
#   The header litellm-enable-message-redaction: true, or its newer
#   spelling x-litellm-enable-message-redaction: true
#       The event fires, but every message in the payload has been
#       replaced by "redacted-by-litellm".
#   turn_off_message_logging, in the body or in metadata
#       Rejected by the proxy with a 401 before any hook runs. Nothing to do.
#   The header x-litellm-disable-callbacks
#       Ignored on a non-premium proxy. Nothing to do.
#
# The pre-call hook removes the "no-log" field and both redaction headers
# before LiteLLM reads them, so the request is logged like any other.
# Nothing is rejected and the client is not told; the proxy's log gets a
# WARNING naming the user, the key and what they sent. litellm.yaml also
# sets global_disable_no_log_param, LiteLLM's own switch for "no-log"; the
# strip here is the one we control.
_REDACTION_HEADERS = ("litellm-enable-message-redaction", "x-litellm-enable-message-redaction")


def _strip_opt_outs(data: dict) -> list[str]:
    """Remove the opt-out switches from a request; the names of what was removed."""
    removed = []
    if data.pop("no-log", None) is not None:
        removed.append("no-log")
    for holder in (data, data.get("metadata"), data.get("litellm_metadata")):
        headers = isinstance(holder, dict) and holder.get("headers")
        if isinstance(headers, dict):
            removed.extend(f"header {name}" for name in _REDACTION_HEADERS if headers.pop(name, None) is not None)
    return removed


def _probe_view(data: dict) -> dict:
    """The opt-out switches as they appear in a request, for the probe."""
    meta = data.get("metadata") or data.get("litellm_metadata") or {}
    headers = meta.get("headers") or {}
    return dict(
        no_log=data.get("no-log"),
        turn_off=data.get("turn_off_message_logging"),
        meta_no_log=meta.get("no-log"),
        meta_turn_off=meta.get("turn_off_message_logging"),
        headers={k: v for k, v in headers.items() if "redaction" in k or "callbacks" in k or "log" in k},
    )


class PromptLog(CustomLogger):
    """Records every request that flows through the proxy. PROBE BODY, see above."""

    def __init__(self, directory: Path):
        super().__init__()
        self.dir = directory

    @classmethod
    def from_settings(cls) -> PromptLog:
        return cls(_prompt_log_dir(_prompt_log_settings()))

    # --- the probe. Each method appends one line saying what it saw; the
    # request's `user` field is the tag that pairs the lines of one request.

    def _probe(self, event: str, **fields: Any) -> None:
        with (self.dir / "probe.jsonl").open("a") as f:
            f.write(json.dumps(dict(event=event, **fields), default=str) + "\n")

    def _probe_event(self, event: str, kwargs: dict) -> None:
        payload = kwargs.get("standard_logging_object") or {}
        params = kwargs.get("litellm_params") or {}
        dynamic = kwargs.get("standard_callback_dynamic_params") or {}
        self._probe(
            event,
            tag=payload.get("end_user"),
            status=payload.get("status"),
            messages=repr(payload.get("messages"))[:160],
            no_log=params.get("no-log"),
            dynamic_turn_off=dynamic.get("turn_off_message_logging"),
        )

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: str) -> dict:
        self._probe("pre_call", tag=data.get("user"), call_type=call_type, **_probe_view(data))
        removed = _strip_opt_outs(data)
        if removed:
            _PROMPT_LOGGER.warning(
                "logging opt-out ignored: user=%s key=%s team=%s call=%s sent %s",
                user_api_key_dict.user_id,
                user_api_key_dict.key_alias,
                user_api_key_dict.team_alias,
                call_type,
                ", ".join(removed),
            )
        return data

    async def async_log_success_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        self._probe_event("success", kwargs)

    async def async_log_failure_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        self._probe_event("failure", kwargs)


_prompt_log: PromptLog | None = None


def __getattr__(name: str) -> Any:
    """Build `prompt_log` once, when LiteLLM first asks for it by name."""
    if name != "prompt_log":
        raise AttributeError(name)
    global _prompt_log
    if _prompt_log is None:
        _prompt_log = PromptLog.from_settings()
        _PROMPT_LOGGER.info("prompt log: writing to %s", _prompt_log.dir)
    return _prompt_log
