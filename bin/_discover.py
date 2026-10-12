"""One endpoint per model, for llmao-smoke and llmao-saturate.

A thin shim over _fleet.py, which does the real work. Kept because both
scripts import `direct_endpoints` and `endpoint_for`, and because they want
something _fleet deliberately does not provide: exactly one address per
model, since they send one request at a time and need a single target.

Sources, in _fleet's order: config.yaml fleet.hosts (root on the gateway
host), LiteLLM's routing table, then LLMAO_DIRECT / LLMAO_WATCH_BOXES.

    export LLMAO_DIRECT="gemma4-26b=1.2.3.4:12711,qwen3.8-27b=5.6.7.8:15602"

THE BUG THIS REPLACES

The old version iterated the LiteLLM result to apply LLMAO_DIRECT_<MODEL>
overrides, so with no LiteLLM there was nothing to iterate and every
override was ignored. Both callers invoke it with no arguments, which meant
`--direct` and all of llmao-saturate silently found nothing off the gateway
host. `python3 _discover.py` reported "no endpoints discovered" with the
variables correctly set.

The old LLMAO_DIRECT_<MODEL> form is still read, but it cannot be resolved
on its own: the suffix is the model name upper-cased with `.` and `-` as
underscores, and that does not reverse -- QWEN3_8_27B could be `qwen3.8-27b`
or `qwen3-8-27b`. It is matched against the real names when _fleet knows
them, and reported as unresolved when it cannot be. Prefer LLMAO_DIRECT.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _fleet import NO_ENDPOINTS_HELP, fleet   # noqa: E402,F401


def _envkey(model: str) -> str:
    return re.sub(r"[.-]", "_", model).upper()


def _legacy_env(known: set[str]) -> tuple[dict[str, str], dict[str, str]]:
    """LLMAO_DIRECT_<MODEL> variables, resolved against real model names."""
    by_suffix = {_envkey(m): m for m in known}
    resolved: dict[str, str] = {}
    unresolved: dict[str, str] = {}
    for k, v in sorted(os.environ.items()):
        if not k.startswith("LLMAO_DIRECT_") or not v.strip():
            continue
        suffix = k[len("LLMAO_DIRECT_"):]
        addr = v.strip().replace("http://", "").rstrip("/")
        if model := by_suffix.get(suffix):
            resolved[model] = addr
        else:
            unresolved[suffix] = addr
    return resolved, unresolved


def direct_endpoints(models: list[str] | None = None,
                     unresolved: dict[str, str] | None = None) -> dict[str, str]:
    """{model_name: 'host:port'} -- one reachable box per model.

    When a model has several boxes, the one with a LiteLLM route wins, since
    that is what the gateway would have sent the request to anyway. Use
    llmao-fleet to see all of them.
    """
    boxes, _meta = fleet()
    out: dict[str, str] = {}
    for b in sorted(boxes, key=lambda b: (bool(b.routes), bool(b.public_port))):
        if b.addr and b.model:
            out[b.model] = b.addr          # last wins; routed boxes sort last

    legacy, unmatched = _legacy_env(set(models or ()) | set(out))
    out.update(legacy)                     # an explicit override wins
    if unresolved is not None:
        unresolved.update(unmatched)
    return out


def endpoint_for(model: str) -> str | None:
    return direct_endpoints([model]).get(model)


if __name__ == "__main__":
    unres: dict[str, str] = {}
    eps = direct_endpoints(unresolved=unres)
    if not eps:
        print("no endpoints discovered\n" + NO_ENDPOINTS_HELP)
    for m, hp in sorted(eps.items()):
        print(f"{m:16} {hp}")
    for suffix, hp in sorted(unres.items()):
        print(f"\nLLMAO_DIRECT_{suffix}={hp}\n"
              f"  cannot tell which model that names -- the suffix mangling\n"
              f"  does not reverse. Use the literal name instead:\n"
              f'    export LLMAO_DIRECT="<model>={hp}"')

