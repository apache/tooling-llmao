# bin/

Five scripts. Different questions, different times to reach for them.

| | when | how long |
|---|---|---|
| `llmao-fleet` | **start here.** What exists, what routes, what works | ~5s |
| `llmao-watch` | alongside anything else, to see what the fleet is doing | until Ctrl-C |
| `llmao-smoke` | after any change to a model, box or the gateway | ~1 min/model |
| `llmao-saturate` | when deciding whether a card is the right size | 5–30 min |
| `llmao-usage` | has this box ever served anything at all | instant |

All stdlib-only Python 3 — nothing to install. All five share one discovery
path in `_fleet.py`, so they see the same fleet.

On the gateway host, run them with `sudo` and there is nothing to export:
`config.yaml` is 0440 and carries the LiteLLM master key, the fleet salt and
`fleet.hosts`. Nothing lands in shell history either.

## llmao-fleet

```bash
sudo ./llmao-fleet              # the whole picture, no traffic sent
sudo ./llmao-fleet --test       # also send one real completion per box and per model
sudo ./llmao-fleet --no-auth    # skip the per-box key probe
sudo ./llmao-fleet --json
```

`--test` is the "is everything actually working" run. It sends one 8-token
completion to **every box in `fleet.hosts`**, not just the healthy ones, then
one per model through the gateway. Boxes that cannot be sent a request are
listed with the reason rather than dropped, because "stalled" is a finding and
an omission looks like a pass:

```
DIRECT -- one 8-token completion per box, bypassing LiteLLM
  PASS  qwen3.8-27b/qwen3.8-27b 103.196.86.105:15602  1.2s  ok
  PASS  gemma4-26b/gemma4-26b 80.188.223.202:12711  0.9s  empty content, finish=length
  FAIL  qwen3-8b/qwen3-8b-b 142.189.180.247:51310  120.0s  TimeoutError: timed out
  skip  qwen3-8b/qwen3-8b-c 99.29.94.155:8002
          no public port: nothing has mapped its listen port, so it is
          unreachable from outside the box
```

This is the only check that proves a box can serve. `/metrics` answers on a box
with no model loaded and `/v1/models` answers on one whose scheduler is wedged
— the `FAIL` above passes both. `empty content, finish=length` is a reasoning
model spending the whole budget thinking: a pass for reachability, a finding
for anything reading the content.

The gateway half needs `LLMAO_KEY` (a PAT from My Keys). It is not in
`config.yaml`, so it cannot be discovered; without it that half says so and
the direct half still runs.

Joins three views of the fleet and reports where they disagree:

| | what it knows |
|---|---|
| `config.yaml` `fleet.hosts` | what llmao **intends** — including boxes with no public port and boxes that have never fetched their config |
| LiteLLM `/model/info` | what the proxy **routes to** — including stale routes and duplicates |
| the box's `/metrics` | what is actually **running**, with no credential needed |

Each disagreement is a failure mode none of the three shows alone:

```
model          label          box                       src  rt  vllm      ctx    kv        auth  run  wt
gemma4-26b     gemma4-26b     80.188.223.202:12711       CL   1    up   131072   40%      shared    2   0
qwen3-8b       (orphan)       175.155.64.248:19231       -L   1  down        ·     · unreachable    ·   ·
qwen3-8b       qwen3-8b-a     213.173.110.78:18490       CL   2    up        ·   60%         401    4   1
qwen3-8b       qwen3-8b-b     142.189.180.247:51310      C-   0    up    40960   46%     derived    2   0
qwen3-8b-c     qwen3-8b       99.29.94.155:8001*         C-   0  down        ·     ·     no port    ·   ·
```

`src` is `C` for in `fleet.hosts`, `L` for LiteLLM has a route. `rt` is how
many routes point at this box; more than one is route duplication.

**The `auth` column is the point of the script.** `/healthz` needs no
credential, so a box with the wrong `--api-key` answers healthy to llmao and
401 to LiteLLM, forever: LiteLLM cools the deployment down, llmao reads that
as the backend being down and deletes the route, the next probe finds
`/healthz` answering and re-adds it with the same wrong key. That is the loop
that produced twenty-two routes for three models. This column shows the
mismatch before the churn starts.

- `derived` — the per-box HMAC key, what llmao registers. Correct.
- `shared` — launched with the fleet-wide `selfhost_api_key` as a container
  argument. Works now, breaks on the next recreate.
- `401` — running and rejecting both. The churn loop, in progress.
- `OPEN` — no bearer required at all. A finding in itself.

Exits non-zero when there is anything in `PROBLEMS`, so it works as a check.

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

## llmao-watch

```bash
sudo ./llmao-watch                       # 1s, every box llmao-fleet knows
sudo ./llmao-watch -n 5
sudo ./llmao-watch --include-down        # also boxes with no public port
./llmao-watch --boxes "27B=1.2.3.4:15602,GEM=5.6.7.8:12711"
sudo ./llmao-watch --csv run.csv         # also log every sample
sudo ./llmao-watch --once                # two samples, one row set, exit
```

Rates and interval timings, not cumulative counters:

```
          run  wait     in/s   out/s   ttft   tpot     e2e    kv   hit  fin  pre
   27B      8     1   39,958      46   0.80     21   109.0   62%   56%    1    0
```

`in/s` and `out/s` are deltas of `prompt_tokens_total` and
`generation_tokens_total` over the interval. `ttft`, `tpot` (ms/token) and
`e2e` are `_sum`/`_count` deltas of the histograms, so they are the mean over
requests that **finished in that window** — a long call contributes nothing
until it lands, then moves the mean a lot. Read the gauges per second and the
timings over several rows.

Two things it shows that nothing else does:

- **`out/s` near zero with `run > 0`** is a reasoning model generating
  thinking tokens. Cumulative counters can't distinguish that from a stall.
- **boxes the gateway is not using.** Rows come from `fleet.hosts`, not from
  LiteLLM, so a box that is serving happily with no route — or carrying a
  duplicate route — gets its own row either way. A model split across several
  rows while only one of them moves is the issue-#29 failure mode, live.

## llmao-usage

```bash
llmao-usage            # one pass, cumulative since each engine started
llmao-usage --watch    # every 15s with deltas
```

Zero means nothing *since that vLLM process started*, not nothing ever —
the counters reset on restart.

## Endpoints are discovered, not committed

No script contains a host or port. `_fleet.py` finds them at runtime, in this
order:

1. **`config.yaml` `fleet.hosts`** — the intended fleet, and the only source
   that includes boxes LiteLLM has never heard of. Needs `sudo` on the
   gateway host; it also supplies the master key, so nothing else to export.
2. **LiteLLM `/model/info`** — what the proxy actually routes to.
3. **`LLMAO_DIRECT`**, off-box, with literal model names:

```bash
export LLMAO_DIRECT="gemma4-26b=1.2.3.4:12711,qwen3.8-27b=5.6.7.8:15602"
```

Check what it resolved:

```bash
sudo python3 _fleet.py          # every box, joined
sudo python3 _discover.py       # one address per model, what smoke/saturate use
```

`llmao-smoke` and `llmao-saturate` want exactly one box per model, since they
send one request at a time; when a model has several, the routed one wins.
`llmao-fleet` and `llmao-watch` show them all.

### The old per-model variables

`LLMAO_DIRECT_<MODEL>` is still read, but prefer `LLMAO_DIRECT`. The suffix is
the model name upper-cased with `.` and `-` as underscores, and that does not
reverse — `QWEN3_8_27B` could be `qwen3.8-27b` or `qwen3-8-27b`. It is matched
against real model names when they are known and reported as unresolved when
they are not.

Until recently these were **silently ignored**: `direct_endpoints()` iterated
the LiteLLM result to apply the overrides, so with no LiteLLM there was
nothing to iterate. Both callers invoke it with no arguments, which meant
`--direct` and all of `llmao-saturate` found nothing off the gateway host,
with the variables correctly set.

This matters because **RunPod reassigns the port on every pod recreate**,
even when the pod lands on the same machine — one host went
17140 → 14296 → 10755 → 15602 across four rebuilds. Any committed value
would be stale within days, and these addresses are not something to publish
in a public repo regardless.

`llmao-saturate` also reads each model's context window from the box at
runtime rather than a table, for the same reason: it changes whenever a box
is relaunched with a different `--max-model-len`, and a stale value would
silently send prompts that cannot fit.
