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
"""What llmao reads from a vLLM process, and how each answer is read.

The one module that knows vLLM's HTTP surface: ``/health`` and its 200
against a 503 while the model loads, ``/v1/models`` and the
``max_model_len`` field that is the served context window, and
``/metrics`` with the ``cache_config_info`` line (the KV cache size) and
the gauges and counters capacity planning rolls up. It dials whatever URL
it is handed and knows no port plan; the fleet decides what to ask and
when (docs/fleet-reconcile-design.md). Another engine gets its own client
module, not an ``if`` in this one.

``probe`` produces ``VllmProbe``, the snapshot of one probe of one address:
what ``/health`` said, and what ``/v1/models`` said when it was asked.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from easydict import EasyDict as edict  # noqa: N813

from llmao.capacity import Sample

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class VllmProbe:
    """What one probe of one address found, as of ``at``. Nothing of ours on it.

    Three levels of contact. ``reached``: vLLM's HTTP server answered at all
    (a 503 while the model loads counts). ``ok``: ``/health`` gave a 200,
    which is vLLM's own word that it is serving and not "all is well", since
    ``/health`` takes no key. ``served_status``: what ``/v1/models`` answered
    with the key we believe the box runs with, when it was asked: a 401 is
    the box refusing that key, a 200 carries the served context window and
    the weights it loaded. None when it was not asked, or nothing usable
    came back.
    """

    url: str
    at: float
    reached: bool
    ok: bool
    # Why not ok: "timeout", "no connection", or "HTTP <status>". None when ok.
    error: str | None
    served_status: int | None = None
    max_model_len: int | None = None
    hf_id: str = ""

    @classmethod
    def no_address(cls, at: float) -> VllmProbe:
        """The probe of a server with no address to dial: an attempt that reached nothing.

        Nothing to dial is the strongest form of not reached, and it still
        counts as an attempt, so the grace clock runs on a host no listing
        names and it reads Stalled once the grace is out, as a dead rental
        should.
        """
        return cls("", at, reached=False, ok=False, error="no address to dial")

    @property
    def key_rejected(self) -> bool | None:
        """``/v1/models``' verdict on the key: True on a 401, False on a 200, None when it said neither."""
        if self.served_status == 401:
            return True
        if self.served_status == 200:
            return False
        return None


async def probe(
    client: httpx.AsyncClient,
    root: str,
    api_key: str,
    *,
    ask_models: bool,
    now: float | None = None,
) -> VllmProbe:
    """One probe of one address: ``/health``, then ``/v1/models`` with the key if it passed and the caller asked.

    ``root`` is the server root, ``http://host:port``, never a /v1-suffixed
    base. Whether to ask ``/v1/models`` is the caller's: it is fixed at
    engine start, so asking on every pass is waste, and the fleet knows
    which servers still need it. Never raises.
    """
    at = now or time.time()
    ok, reached, error = await fetch_health(client, f"{root}/health")
    if not (ok and ask_models):
        return VllmProbe(root, at, reached, ok, error)
    served = await fetch_served_models(client, root, api_key)
    return VllmProbe(
        root,
        at,
        reached,
        ok,
        error,
        served_status=served.status,
        max_model_len=served.max_model_len,
        hf_id=served.hf_id,
    )


async def fetch_health(client: httpx.AsyncClient, url: str) -> tuple[bool, bool, str | None]:
    """GET /health as (ok, reached, err).

    ``reached`` is True when vLLM's HTTP server answered at all (e.g. a 503
    while the model is still loading); False for no connection. ``ok`` is
    only a 200: vLLM says it is up, which is not "all is well", since
    /health takes no key.
    """
    try:
        resp = await client.get(url)
    except httpx.TimeoutException as e:
        _LOGGER.warning("health %s: %s", url, e)
        return False, False, "timeout"
    except httpx.HTTPError as e:
        _LOGGER.warning("health %s: %s", url, e)
        return False, False, "no connection"
    if resp.status_code == 200:
        return True, True, None
    return False, True, f"HTTP {resp.status_code}"


def parse_max_model_len(models_json: Any) -> int | None:
    """Served context window from /v1/models, or None."""
    try:
        rows = models_json["data"]
    except (TypeError, KeyError):
        return None
    for row in rows or []:
        value = row.get("max_model_len") if isinstance(row, dict) else None
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


async def fetch_served_models(client: httpx.AsyncClient, base: str, api_key: str) -> edict:
    """GET /v1/models with the key we believe this vLLM has: (status, max_model_len, hf_id).

    ``status`` is the HTTP status, or None when nothing usable came back. A
    401 is the box refusing the key (Rejected); a 200 carries the served
    context window, ``max_model_len``, which is None when the body has none,
    and ``hf_id``, the first served model's id ("" when the body names none).

    base is the server root, not api_base: a /v1-suffixed base would ask for
    /v1/v1/models and 404. The KV cache size, the other half of the oversized
    check, is read by the sampler (llmao/sampler.py), the one reader of
    /metrics.

    Never raises: an unreachable or differently-shaped endpoint leaves both
    unknown and the UX shows them as such.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    served = edict(status=None, max_model_len=None, hf_id="")
    try:
        resp = await client.get(f"{base}/v1/models", headers=headers)
        served.status = resp.status_code
        if resp.status_code == 200:
            body = resp.json()
            served.max_model_len = parse_max_model_len(body)
            rows = body.get("data") if isinstance(body, dict) else None
            served.hf_id = next((str(r["id"]) for r in rows or () if isinstance(r, dict) and r.get("id")), "")
    except (httpx.HTTPError, ValueError):
        pass
    return served


# Instant values. Metric names with the vllm: prefix stripped.
_GAUGES = {
    "num_requests_running": "running",
    "num_requests_waiting": "waiting",
    "kv_cache_usage_perc": "kv",
}
# vLLM's totals since it started. request_success_total has a line per
# finish reason, so lines of one name are summed.
_COUNTERS = {
    "prompt_tokens_total": "prompt_tokens",
    "generation_tokens_total": "generation_tokens",
    "request_success_total": "requests",
    "num_preemptions_total": "preemptions",
    "request_queue_time_seconds_sum": "queue_s",
    "request_queue_time_seconds_count": "queued",
}
_QUEUE_BUCKET = "request_queue_time_seconds_bucket"


def _labels(inner: str) -> dict[str, str]:
    labels = {}
    for part in inner.split(","):
        key, _, value = part.partition("=")
        if key:
            labels[key.strip()] = value.strip().strip('"')
    return labels


def parse_kv_cache_tokens(metrics_text: str) -> int | None:
    """KV cache size in tokens from vLLM's Prometheus output, or None.

    vLLM reports cache geometry as labels on an Info-style metric:

        vllm:cache_config_info{block_size="16",num_gpu_blocks="33960",
                               kv_cache_size_tokens="855836",...} 1.0

    Prefer kv_cache_size_tokens, which is the same number vLLM prints at
    startup as "GPU KV cache size: N tokens".

    Do NOT compute it as num_gpu_blocks * block_size. On the A100 serving
    Gemma those give 543,360 against a reported 855,836 -- blocks do not map
    uniformly to tokens for a model with heterogeneous head dimensions, and
    the product understates by a third. Understating the cache would falsely
    flag a correctly-sized server as oversized.

    The product is kept only as a fallback for builds that do not emit the
    token count, where an approximate figure beats none.

    Returns None rather than raising when the metric is absent or shaped
    differently -- vLLM's metric names are not a stable API, and a missing
    figure should degrade the display rather than break the probe loop.
    """
    for line in metrics_text.splitlines():
        if not line.startswith("vllm:cache_config_info"):
            continue
        start, end = line.find("{"), line.rfind("}")
        if start < 0 or end < start:
            continue
        labels: dict[str, str] = {}
        for part in line[start + 1 : end].split(","):
            k, _, v = part.partition("=")
            labels[k.strip()] = v.strip().strip('"')

        direct = labels.get("kv_cache_size_tokens")
        if direct and direct != "None":
            try:
                tokens = int(direct)
            except ValueError:
                tokens = 0
            if tokens > 0:
                return tokens

        try:
            blocks = int(labels["num_gpu_blocks"])
            size = int(labels["block_size"])
        except (KeyError, ValueError):
            continue
        if blocks > 0 and size > 0:
            return blocks * size
    return None


def parse_metrics(text: str, *, now: float | None = None) -> Sample:
    """What the rollup needs, from vLLM's Prometheus exposition text.

    Hand-parsed rather than with a client library: a dozen metrics in one
    format. Anything unreadable is skipped, so a box answering with HTML or
    nothing gives an empty reading rather than an error.
    """
    out = Sample(ts=now if now is not None else time.time(), kv_cache_tokens=parse_kv_cache_tokens(text))
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, brace, rest = line.partition("{")
        if brace:
            inner, _, value = rest.partition("} ")
        else:
            inner = ""
            name, _, value = line.partition(" ")
        name = name.strip().removeprefix("vllm:")
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            continue
        if name in _GAUGES:
            setattr(out, _GAUGES[name], getattr(out, _GAUGES[name]) + number)
        elif name in _COUNTERS:
            key = _COUNTERS[name]
            out.counters[key] = out.counters.get(key, 0.0) + number
        elif name == _QUEUE_BUCKET:
            try:
                bound = float(_labels(inner).get("le", ""))
            except ValueError:
                continue
            out.buckets[bound] = out.buckets.get(bound, 0.0) + number
    return out
