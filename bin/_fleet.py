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

"""One view of the fleet, joining what is intended to what is actually there.

Shared by llmao-fleet and llmao-watch. Three sources, and the gaps between
them are the interesting part:

  config.yaml fleet.hosts   what llmao INTENDS to exist. Includes boxes with
                            no public port and boxes that have never fetched
                            their config -- neither appears in LiteLLM.
  LiteLLM /model/info       what the proxy ROUTES to. Includes stale routes
                            for boxes that are gone, and duplicates.
  the box's own /metrics     what is actually RUNNING. Unauthenticated, so it
                            works regardless of which key the box wants.

A box present in one and absent from another is a finding, not a detail:

  in config, no route        not serving traffic; nothing in the gateway UI
                             explains why
  route, not in config       orphan -- the box is gone and LiteLLM still
                             balances onto it (issue #29)
  model not in models.yaml   validate_fleet would raise on next startup; the
                             p6 [qwen3-8b-a, 8001, qwen3-8b] rows are this
  running, wrong key         the 401 class. /health needs no credential, so
                             llmao calls it healthy while LiteLLM cannot use
                             it, forever.

Reads /opt/llmao/config.yaml when it can (root on the gateway host), which
also supplies the LiteLLM master key, so there is nothing to export there.
Off-box it falls back to LLMAO_DIRECT / LLMAO_WATCH_BOXES and reports the
reduced view honestly rather than pretending the fleet is four boxes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

CONFIG = os.environ.get("LLMAO_CONFIG", "/opt/llmao/config.yaml")
_LABEL = "llmao.vllm.api_key"  # must match llmao/intent.py

NO_ENDPOINTS_HELP = (
    "  on the gateway host: run it with sudo -- config.yaml is 0440 and\n"
    "                       carries the master key, the salt and the hosts\n"
    '  elsewhere:           export LLMAO_DIRECT="gemma4-26b=host:port,'
    'qwen3.8-27b=host:port"'
)


# ----------------------------------------------------------------- key derivation


def derive_vllm_api_key(salt: str, host: str, listen_port: int) -> str:
    """sk-vllm- + urlsafe-b64(HMAC-SHA256(salt, label\\nhost\\nport)), unpadded.

    Reimplemented rather than imported: these scripts run from bin/ on a
    managed host with the system python, outside llmao's venv. Must stay
    byte-identical to llmao/intent.py -- a drift here reports a working
    box as mis-keyed.
    """
    key = (salt or "").strip()
    if not key or key.startswith("CHANGE_ME"):
        raise ValueError("fleet.vllm_api_salt is missing or CHANGE_ME")
    ip = (host or "").strip()
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    msg = f"{_LABEL}\n{ip}\n{int(listen_port)}".encode()
    digest = hmac.new(key.encode(), msg, hashlib.sha256).digest()
    return "sk-vllm-" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# ------------------------------------------------------------------- config.yaml


def _parse_narrow_yaml(text: str) -> dict:
    """Enough YAML for a Puppet-generated config.yaml. No pyyaml on the host.

    Handles 2-space nested mappings, scalar values, and `- [a, b, c]` rows.
    Anything it does not recognise is skipped rather than guessed at, so a
    missing key reads as missing rather than wrong.
    """
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    last_key: list[tuple[int, str, dict]] = []

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()

        if line.startswith("- "):
            # List item. Attach to the most recent key at a smaller indent.
            for ind, key, parent in reversed(last_key):
                if ind < indent:
                    parent.setdefault(key, [])
                    if isinstance(parent[key], list):
                        parent[key].append(_scalar(line[2:].strip()))
                    break
            continue

        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            stack = [(-1, root)]
        parent = stack[-1][1]

        m = re.match(r"^([^:#]+):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if val in ("", "|", ">"):
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
            last_key.append((indent, key, parent))
        else:
            parent[key] = _scalar(val)
            last_key.append((indent, key, parent))
    return root


def _scalar(val: str):
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        return [_scalar(x) for x in val[1:-1].split(",") if x.strip() != ""]
    if val in ("null", "~", ""):
        return None
    if val in ("true", "True"):
        return True
    if val in ("false", "False"):
        return False
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    if re.fullmatch(r"-?\d+\.\d+", val):
        return float(val)
    return val.strip("'\"")


def load_config(path: str = CONFIG) -> dict | None:
    """config.yaml as a dict, or None if unreadable (not root, or off-box)."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    try:
        # Present in llmao's venv, not in the system python this runs under.
        import yaml

        return yaml.safe_load(text) or {}
    except ImportError:
        return _parse_narrow_yaml(text)
    except Exception:
        return _parse_narrow_yaml(text)


def model_names(cfg: dict) -> set[str]:
    """model_name values from models.yaml, for catching unknown-model rows."""
    path = cfg.get("models_path")
    if not path:
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            return {
                m.group(1).strip().strip("'\"") for m in re.finditer(r"^\s*-?\s*model_name:\s*(\S+)", fh.read(), re.M)
            }
    except OSError:
        return set()


# ------------------------------------------------------------------------- boxes


@dataclass
class Box:
    model: str
    label: str
    host: str
    listen_port: int | None = None
    public_port: int | None = None
    in_config: bool = False
    routes: list[str] = field(default_factory=list)  # LiteLLM model_id values
    api_base: str | None = None  # as LiteLLM has it
    auth: str | None = None  # derived|shared|open|401|?

    @property
    def addr(self) -> str | None:
        """Where to scrape /metrics. None when there is no way in."""
        if self.public_port:
            return f"{self.host}:{self.public_port}"
        if self.api_base:
            return self.api_base
        return None

    @property
    def src(self) -> str:
        return ("C" if self.in_config else "-") + ("L" if self.routes else "-")

    def key(self) -> tuple:
        return (self.host, self.listen_port, self.public_port)


def _clean(addr: str) -> str:
    return (addr or "").strip().replace("http://", "").replace("https://", "").rstrip("/")


def _parse_host_row(raw) -> tuple[str, int, str, int | None] | None:
    """[model, port] | [model, port, name] | [model, port, name, public_port].

    Field 0 is the MODEL NAME and field 2 is the server label. Reversing them
    is the live p6 bug: [qwen3-8b-a, 8001, qwen3-8b] declares a model called
    qwen3-8b-a, which models.yaml does not define, and validate_fleet raises
    at startup.
    """
    if isinstance(raw, str):
        raw = _scalar(raw)
    if not isinstance(raw, (list, tuple)) or len(raw) not in (2, 3, 4):
        return None
    try:
        port = int(raw[1])
    except (TypeError, ValueError):
        return None
    model = str(raw[0]).strip()
    name = str(raw[2]).strip() if len(raw) >= 3 and raw[2] not in (None, "null") else model
    public = None
    if len(raw) == 4 and raw[3] not in (None, "null"):
        try:
            public = int(raw[3])
        except (TypeError, ValueError):
            public = None
    return model, port, name, public


def boxes_from_config(cfg: dict) -> list[Box]:
    hosts = (cfg.get("fleet") or {}).get("hosts") or {}
    out = []
    for host, rows in hosts.items():
        for raw in rows or []:
            parsed = _parse_host_row(raw)
            if not parsed:
                continue
            model, listen, label, public = parsed
            out.append(
                Box(
                    model=model,
                    label=label,
                    host=str(host).strip(),
                    listen_port=listen,
                    public_port=public,
                    in_config=True,
                )
            )
    return out


def litellm_routes(cfg: dict | None = None, master_key: str | None = None) -> list[dict]:
    """Every row LiteLLM routes to -- duplicates kept, since they are the bug."""
    base = "http://127.0.0.1:4000"
    key = master_key or os.environ.get("LITELLM_MASTER_KEY") or os.environ.get("MK")
    if cfg:
        lite = cfg.get("litellm") or {}
        base = lite.get("base_url") or base
        key = key or lite.get("master_key")
    if not key:
        return []
    req = urllib.request.Request(base.rstrip("/") + "/model/info", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.load(r)["data"]
    except Exception:
        return []
    out = []
    for row in rows:
        info = row.get("model_info") or {}
        out.append(
            {
                "model": row.get("model_name", "?"),
                "id": info.get("id") or info.get("model_id") or "?",
                "api_base": _clean(info.get("asf_api_base") or ""),
            }
        )
    return out


def _boxes_from_env() -> list[Box]:
    """LLMAO_DIRECT="model=host:public[:listen],..." (or LLMAO_WATCH_BOXES).

    The optional third field is the box's CONTAINER listen port. It is only
    needed to derive that box's api_key, because the HMAC covers host and
    listen port -- and a public address alone cannot tell you the listen port,
    which is why the auth column reads "no keys" off-box without it.
    """
    out: list[Box] = []
    seen: set[tuple[str, int]] = set()
    used: list[str] = []
    # BOTH variables, merged. Taking only the first one that parsed meant a
    # stale LLMAO_DIRECT silently shadowed a freshly exported
    # LLMAO_WATCH_BOXES, and the only symptom was a short box list.
    for var in ("LLMAO_WATCH_BOXES", "LLMAO_DIRECT"):
        raw = os.environ.get(var) or ""
        if not raw.strip():
            continue
        contributed = False
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            name, _, addr = item.partition("=")
            parts = _clean(addr or name).split(":")
            if len(parts) < 2 or not parts[1].isdigit():
                continue
            listen = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
            key = (parts[0], int(parts[1]))
            if key in seen:  # the same box named in both variables
                continue
            seen.add(key)
            contributed = True
            out.append(
                Box(
                    model=name.strip(),
                    label=name.strip(),
                    host=parts[0],
                    public_port=int(parts[1]),
                    listen_port=listen,
                )
            )
        if contributed:
            used.append(var)
    _ENV_SOURCE[:] = used
    return out


# Which environment variables the last _boxes_from_env() call actually used,
# so the caller can name them rather than leaving the user guessing which of
# the two is in play.
_ENV_SOURCE: list[str] = []


def _match_by_derived_key(api_base: str, candidates: list[Box], salt: str | None) -> Box | None:
    """Which candidate row is serving at `api_base`, proven by its own key.

    Returns None when there is no salt to derive with, or when no candidate's
    key is accepted -- both of which leave the route genuinely unexplained
    rather than attached to a guess.
    """
    if not salt:
        return None
    addr = _clean(api_base)
    for b in candidates:
        if not b.listen_port:
            continue
        try:
            key = derive_vllm_api_key(salt, b.host, b.listen_port)
        except ValueError:
            continue
        if _get(f"http://{addr}/v1/models", key, 5.0)[0] == 200:
            return b
    return None


def fleet() -> tuple[list[Box], dict]:
    """All known boxes, joined, plus what could not be determined.

    meta: {"config": bool, "litellm": int routes, "models": set, "source": str}
    """
    cfg = load_config() or {}
    boxes = boxes_from_config(cfg)
    routes = litellm_routes(cfg)

    for r in routes:
        host, _, port = r["api_base"].rpartition(":")
        port_i = int(port) if port.isdigit() else None
        hit = None
        for b in boxes:
            if b.host != host:
                continue
            # Config carries the container listen port and, when pinned, the
            # public one. A route's api_base is always public.
            if port_i is not None and port_i in (b.public_port, b.listen_port):
                hit = b
                break
        if hit is None:
            # Match on model alone before calling it an orphan: a Vast box's
            # public port is discovered at runtime and not pinned in config,
            # so the port will not line up even when the box is legitimate.
            candidates = [b for b in boxes if b.host == host and b.model == r["model"] and not b.public_port]
            if len(candidates) == 1:
                hit = candidates[0]
            elif len(candidates) > 1:
                # Several unpinned rows on one host -- two vLLM processes on
                # one machine, which this fleet has. The derived key settles
                # it: it is an HMAC over host AND LISTEN PORT, so the row
                # whose key this address accepts is the row it belongs to.
                # Guessing instead produced two phantom "orphan" rows and a
                # 401 that was really a key we never tried.
                hit = _match_by_derived_key(r["api_base"], candidates, (cfg.get("fleet") or {}).get("vllm_api_salt"))
        if hit is not None:
            hit.routes.append(r["id"])
            hit.api_base = r["api_base"]
            if hit.public_port is None and port_i:
                hit.public_port = port_i
        else:
            boxes.append(
                Box(
                    model=r["model"],
                    label="(orphan)",
                    host=host,
                    public_port=port_i,
                    routes=[r["id"]],
                    api_base=r["api_base"],
                )
            )

    source = "config.yaml + LiteLLM"
    if not boxes:
        boxes = _boxes_from_env()
        source = " + ".join(_ENV_SOURCE) if _ENV_SOURCE else "LLMAO_WATCH_BOXES / LLMAO_DIRECT (neither set)"
    elif not cfg:
        source = "LiteLLM only"
    elif not routes:
        source = "config.yaml only"

    boxes.sort(key=lambda b: (b.model, b.host, b.listen_port or 0))
    meta = {
        "config": bool(cfg),
        "routes": len(routes),
        "models": model_names(cfg),
        # Off-box there is no config.yaml, so the two credentials the auth
        # probe needs can come from the environment instead. Without either,
        # the probe has nothing to test with and must say so rather than
        # reporting every box as rejecting a key that was never sent.
        "salt": (cfg.get("fleet") or {}).get("vllm_api_salt") or os.environ.get("LLMAO_VLLM_SALT"),
        "shared": (cfg.get("fleet") or {}).get("selfhost_api_key") or os.environ.get("VLLM_API_KEY"),
        "source": source,
    }
    return boxes, meta


# --------------------------------------------------------------------- probing


def _get(url: str, key: str | None, timeout: float) -> tuple[int, bytes | None]:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return 0, None


def probe_auth(box: Box, salt: str | None, shared: str | None, timeout: float = 5.0) -> tuple[str, str | None]:
    """(verdict, working key): derived, shared, OPEN, no keys, a code, unreachable.

    "no keys" means neither credential was available, so nothing was tested.
    Off-box that is the normal answer: export LLMAO_VLLM_SALT (for the derived
    keys) or VLLM_API_KEY (for the shared one) to get a real verdict.

    This is the one question /health cannot answer, and the reason llmao's
    view of health and LiteLLM's view of usability can disagree forever.

    The key comes back so the caller can then ask the box something that does
    need a bearer -- its served model and context window. Callers must not
    print it.
    """
    if not box.addr:
        return "no port", None
    url = f"http://{box.addr}/v1/models"

    if salt and box.listen_port:
        try:
            derived = derive_vllm_api_key(salt, box.host, box.listen_port)
        except ValueError:
            derived = None
        if derived and _get(url, derived, timeout)[0] == 200:
            return "derived", derived
    if shared and _get(url, shared, timeout)[0] == 200:
        return "shared", shared
    code, _ = _get(url, None, timeout)
    if code == 200:
        return "OPEN", None
    if code == 0:
        return "unreachable", None

    # Nothing to derive from and no shared key, so the only request sent was
    # unauthenticated and its 401 is the expected answer, not a finding. A box
    # with a perfectly correct key looks identical from here.
    tried_derived = bool(salt) and bool(box.listen_port)
    if not tried_derived and not shared:
        return "no keys", None
    return str(code), None


# vLLM metric name -> our key. Names moved between engine generations, so
# several can map to one key; whichever the box exports wins.
GAUGES = {
    "num_requests_running": "run",
    "num_requests_waiting": "wait",
    "gpu_cache_usage_perc": "kv",
    "kv_cache_usage_perc": "kv",
}
COUNTERS = {
    "prompt_tokens_total": "in",
    "generation_tokens_total": "out",
    "request_success_total": "fin",
    "num_preemptions_total": "pre",
    "num_preemptions": "pre",
    "prefix_cache_queries_total": "pcq",
    "prefix_cache_hits_total": "pch",
    "gpu_prefix_cache_queries": "pcq",
    "gpu_prefix_cache_hits": "pch",
}
HISTOS = {
    "time_to_first_token_seconds": "ttft",
    "time_per_output_token_seconds": "tpot",
    "e2e_request_latency_seconds": "e2e",
}

_LINE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(\{[^}]*\})?\s+(\S+)$")


def scrape(addr: str, timeout: float = 4.0) -> dict | None:
    """One box's /metrics, summed across label sets. None if unreachable.

    No credentials: /metrics is unauthenticated on these boxes.
    """
    try:
        with urllib.request.urlopen(f"http://{addr}/metrics", timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None

    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name, raw = m.group(1), m.group(3)
        if name.startswith("vllm:"):
            name = name[5:]
        try:
            val = float(raw)
        except ValueError:
            continue
        if val != val:  # NaN
            continue

        if key := GAUGES.get(name):
            out[key] = val
        elif key := COUNTERS.get(name):
            out[key] = out.get(key, 0.0) + val
        else:
            for base, k in HISTOS.items():
                if name == base + "_sum":
                    out[k + "_s"] = out.get(k + "_s", 0.0) + val
                elif name == base + "_count":
                    out[k + "_n"] = out.get(k + "_n", 0.0) + val
    return out


def completion(target: str, model: str, key: str | None, timeout: float = 60.0) -> tuple[bool, str, float]:
    """Send one tiny completion. (ok, detail, seconds).

    `target` is a box as host:port, or a full base URL for the gateway --
    https://llm.apache.org/v1 -- so the same function covers both paths and
    the two results are comparable.

    The only check that proves a box can actually serve: /metrics answers on a
    box with no model loaded, and /v1/models answers on one whose scheduler is
    wedged. Deliberately 8 tokens, so a stalled box fails on time rather than
    on budget.
    """
    if target.startswith(("http://", "https://")):
        url = target.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
    else:
        url = f"http://{target}/v1/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "max_tokens": 8,
            "temperature": 0.0,
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode("utf-8", "replace")[:160].replace("\n", " ")
        return False, f"HTTP {e.code} {detail}".strip(), time.monotonic() - t0
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"[:160], time.monotonic() - t0
    dt = time.monotonic() - t0
    try:
        choice = data["choices"][0]
        text = (choice["message"].get("content") or "").strip()
        reason = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return False, "no choices in response", dt
    if not text:
        # A reasoning model can spend the whole budget thinking and return
        # HTTP 200 with empty content. That is a pass for reachability and a
        # finding for anything that reads the content.
        return True, f"empty content, finish={reason}", dt
    return True, text[:40], dt


def served_model(addr: str, key: str | None = None, timeout: float = 4.0) -> tuple[str | None, int | None]:
    """(model id, max_model_len) from /v1/models.

    Needs the bearer the box accepts -- unlike /metrics, this endpoint is
    authenticated, so without the key it comes back empty on a perfectly
    healthy box.
    """
    code, body = _get(f"http://{addr}/v1/models", key, timeout)
    if code != 200 or not body:
        return None, None
    try:
        row = (json.loads(body).get("data") or [{}])[0]
    except Exception:
        return None, None
    return row.get("id"), row.get("max_model_len")


if __name__ == "__main__":
    bx, meta = fleet()
    print(f"source: {meta['source']}   boxes: {len(bx)}   routes: {meta['routes']}")
    for b in bx:
        print(
            f"  {b.src} {b.model:14} {b.label:14} {b.host}:"
            f"{b.listen_port or '?'} -> {b.public_port or '?'}  "
            f"routes={len(b.routes)}"
        )
