# bin/

Scripts for llmao; needs more for others but this documents model perf testing.

| | when | how long |
|---|---|---|
| `llmao-vllm-api-key` | hand-launched box: print derived `--api-key` | instant |
| `llmao-smoke` | after any change to a model, box or the gateway | ~1 min/model |
| `llmao-saturate` | when deciding whether a card is the right size | 5–30 min |

Smoke and saturate are stdlib-only Python 3. `llmao-vllm-api-key` uses
`llmao.vllm_api_key` and `config.yaml` unless `--fleet-key` is passed.

## llmao-vllm-api-key

HMAC of `fleet.key` + host + listen port. Same string auto-provisioned boxes
will get from `GET /vllm/config` after that is wired. See
[`docs/fleet-state.md`](../docs/fleet-state.md) §2.3.

```bash
bin/llmao-vllm-api-key --host 203.0.113.10 --port 8001
bin/llmao-vllm-api-key --host 203.0.113.10 --port 8001 --fleet-key sk-...
```

Paste into `vllm serve --api-key` / Supervisor on a hand-launched box.

## llmao-smoke

```bash
export LLMAO_KEY=sk-...        # a PAT from My Keys
llmao-smoke                    # every model the gateway advertises
llmao-smoke --model qwen3-8b
llmao-smoke --direct           # bypass LiteLLM; needs VLLM_API_KEY
```

Six checks per model: completion, thinking-off, tool calling, vision,
sustained output, and a 25k-token prompt. Exits non-zero if anything fails,
so it works in CI.

`--direct` is the useful half of a pair: run both and the difference is the
gateway's contribution. That is how you tell a slow model from a slow proxy.

Each of these checks exists because it broke at least once:

- **thinking off** — the kwarg is the only control callers have over
  reasoning, and a low `max_tokens` with it on returns nothing at all
- **tool calling** — parser names are per model and misleading; `qwen3-8b`
  needs `hermes`, not `qwen3_xml`
- **vision** — uses a generated data URL, because vLLM fetches remote image
  URLs server-side and some hosts 403 it
- **long output** — flags a failure at ~60s as a likely proxy timeout

## llmao-saturate

```bash
export VLLM_API_KEY=...
llmao-saturate --model qwen3.8-27b
llmao-saturate --all --prompt-tokens 32000
```

Ramps concurrency at a fixed large prompt size until the scheduler reports
`Waiting > 0`, then says whether the limit is the scheduler or memory.

**The prompt size is the whole point.** KV cache is consumed by context, not
request count — small prompts cannot fill it, so a load test built from them
proves nothing regardless of how many you send.

## Endpoints are discovered, not committed

Neither script contains a host or port. `_discover.py` finds them at runtime:

**On the gateway host**, from LiteLLM's routing table via `model_info`, which
is what the proxy actually routes to and is therefore always current. Needs
the master key — run as root so it can read `/opt/llmao/litellm.env`, or
`export MK=<master key>`.

**Elsewhere**, from environment variables:

```bash
export LLMAO_DIRECT_QWEN3_8B=1.2.3.4:15601      # . and - become _
```

**Neither?** `--direct` is unavailable and the gateway checks still run,
which is most of the value.

Check what it finds:

```bash
python3 _discover.py
```

This matters because **RunPod reassigns the port on every pod recreate**,
even when the pod lands on the same machine — one host went
17140 → 14296 → 10755 → 15602 across four rebuilds. Any committed value
would be stale within days, and these addresses are not something to publish
in a public repo regardless.

`llmao-saturate` also reads each model's context window from the box at
runtime rather than a table, for the same reason: it changes whenever a box
is relaunched with a different `--max-model-len`, and a stale value would
silently send prompts that cannot fit.
