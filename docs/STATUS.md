# Build status and backlog

**As of:** 2026-11-25<br>
**Repo:** `apache/tooling-llmao`<br>
**Product design (concepts/policy):** `apache/rai-private` → `services/llmao/README.md`<br>
**How to run/use this software:** repo [`README.md`](../README.md)<br>
**Ops:** Infra `p6/modules/llmao/README.md`

**Doc split**

| Kind | Where |
|------|--------|
| Product concepts (project-centered, budgets, credentials) | **rai-private design** |
| What the code does *today* | [`ARCHITECTURE.md`](ARCHITECTURE.md); detail is the code |
| What is open, what is parked, what is waiting on a decision | **GitHub issues** |
| **Planned UX** (layouts, workflows, phases) | **UX backlog** below |
| How to run locally / PATs against proxy | **README** (how-to) |
| How the pieces connect | [`ARCHITECTURE.md`](ARCHITECTURE.md); the design docs it points at |

---

## Where the work stands

In GitHub issues, since 2026-11-25: what is open, what is parked and why,
and the decisions waiting (#94). One note that is not an issue: the
request and budget scaffolding in `seam.py` is a colleague's in-progress
work and is not to be touched.

---

## Tuning: what was limiting the fleet

Three bottlenecks stacked in front of the GPUs, each hiding the next.
Recorded here because the order matters more than any individual value.

**Apache `ProxyTimeout` killed any generation over 60 seconds.** It looked
exactly like a model truncating. Now 1800s.

**vLLM's `max_num_batched_tokens` defaults to 2048**, which held every rental
to 2–3 concurrent requests while 90% of each card's KV cache sat idle —
requests spent 4.3× longer queued than being processed. Raised, the 27B
went from 2 concurrent to 38.

The value is **not portable between cards**: 16384 suits the 96GB and 80GB
rentals, and on a 24GB card it OOM'd during an FP8 matmul and killed the
engine outright. That rental runs 4096. It scales with headroom, not with the
model.

**KV cache was never the constraint.** It is consumed by context, not by
request count — small prompts cannot fill it however many you send, which
is why an early load test proved nothing.

The order that matters: transport, then correctness, then scheduler, then
memory, then hardware. We measured hardware first, concluded a 96GB card
was over-provisioned, and were wrong.

**Tool-call parsers are per model and the names mislead.** `qwen3-8b` needs
`hermes`, not `qwen3_xml`, despite being a Qwen model — it emits
`<tool_call>` blocks wrapping JSON, the Hermes convention. With the wrong
parser the call arrives as text in `content`, `tool_calls` is absent, and
nothing reports an error. Verify by running each candidate against the
model's real output offline; `vllm:tool_call_parser_invocations`
distinguishes "the parser declined" from "the model never called".

**Qwen's FP8 checkpoints do not load on a GB10.** They are block-quantized
(`weight_block_size [128, 128]`), which vLLM runs through DeepGEMM, and
DeepGEMM handles only SM90 (Hopper) and SM100 (datacenter Blackwell). The
GB10 reports compute capability 12.1, and the engine dies during load with
`Unknown SF transformation` and nothing else — no mention of the card or of
quantization. The unquantized weights avoid that path entirely, which is
what `qwen3-8b-bf16` is. `qwen3.8-27b` is the same kind of checkpoint and
should be expected to fail the same way there.

**A unified-memory machine has no VRAM to query.** On the GB10
`nvidia-smi --query-gpu=memory.total` prints `[N/A]`. The rental reads
`MemTotal` and `MemAvailable` from `/proc/meminfo` instead, and vLLM agrees
with that view: it reported 121.63 GiB as the device total.

**vLLM's startup plan kills a unified-memory machine.** With
`VLLM_ENABLE_STARTUP_PLAN=1`, vLLM 0.32 fingerprints the plan with the
device's total memory from NVML, which a GB10 answers with
`NVMLError_NotSupported`, and nothing catches it. The engine dies after
the weights are loaded and before the KV cache is sized, supervisord
restarts it, and the log grows by a ten-minute load per attempt (2026-11-20,
three servers on one GB10, for hours). The installer now leaves the
variable out on a unified-memory machine (`unified_memory()`: `nvidia-smi`
answers, but not with a number). vLLM fixed the lookup after 0.32.0
([#57378](https://github.com/vllm-project/vllm/pull/57378)); the guard can
go once the fleet runs a release that carries it, though the plan buys
nothing on that machine either way. `grep -c NVMLError_NotSupported` on a
server's log is the tell.

**Servers that load together shrink each other's KV cache.** vLLM charges a
server for the drop in device-wide free memory while it loads. Two
`qwen3-8b-bf16` servers starting at once on the GB10 were each billed 34.72
GiB against 15.27 GiB of weights, most likely for both copies, and ended
with 17.11 GiB of KV cache where their 0.436 share allowed about 36. They
serve; they hold roughly half the context they could. vLLM's own suggestion
is an explicit `--kv-cache-memory` in bytes, which does not depend on what
else is loading. Not done: the installer is left alone while the rentals are
in use.

---

## Testing

Two scripts in `bin/`, with no endpoints committed — they discover from
LiteLLM's routing table at runtime. `llmao-saturate` is stdlib-only.
`llmao-smoke` reads `litellm.master_key` from `config.yaml` via PyYAML.
`LLMAO_KEY` overrides it; load that from a file
(`read -r LLMAO_KEY < ~/.llmao-key && export LLMAO_KEY`). A key typed on
the command line is stored in shell history and is visible to other users
via `ps`.

| | what it answers |
|---|---|
| `llmao-smoke` | does every model still work: completion, reasoning control, tool calling, vision, sustained output, large prompt. Exits non-zero, so it works in CI |
| `llmao-saturate` | where does a rental start queueing, and is the limit the scheduler or memory — which decides whether a bigger card would help |

`llmao-smoke --direct` bypasses LiteLLM; the difference between the two runs
is the gateway's own overhead. Do not run the two scripts together — smoke
queueing behind a saturation ramp produces numbers that look like a
regression and are not.

---

## UX backlog (planned — do these)

Product intent for budgets, roles, and reports: **design §6**. Below is **what to build in the UI**, in order.

### IA (target)

```text
My Keys | Other Keys (PMC+) | Models | Projects | Reports?
Home = role-aware launchpad (not keys-only)
```

**Projects** is the second pillar (envelopes + people). Budgets are not a floating top-level “Budgets” app without project context.

### P0 — Project list + read-only project overview

- **Projects** list: projects I’m in / I administer; mini envelope % for steward projects.
- **Project overview** (`/projects/<name>` or equivalent):
  - Money meters: **People** vs **Automation** spend split (display split OK if one team budget under the hood)
  - Period label (e.g. monthly · reset date)
  - Grantor of the dollar ceiling (v1: Free Tier on first cfg default; later RAI / Security / …)
  - By-person spend this period (**transparent to project members**)
  - Export CSV (steward+)
  - Automation summary + link toward Other Keys for that project
- Empty: “Envelope appears when first key is minted” / trial copy when RAI defines defaults
- Flashes on any POST

### P1 — Home pressure + key ↔ project links

- Home: purpose line; primary CTA keys
- **≥ ~90%** near-limit callouts (keys or projects)
- **Top ~3** personal key (or project) usages this period
- Steward strip: 2–3 administered projects with % used
- My Keys: **project column → project overview**; optional near-limit badge
- Collapsed “For scripts” base URL hint (not hero)

### P2 — Member caps (steward write)

- Members table: cap / used / **Edit cap** dialog (empty = no cap; cannot exceed envelope)
- Optional: lower people/automation sub-caps if policy allows
- **Cannot raise** outer ceiling (no fake Increase button — Request/raise is RAI)
- Quart **flashes** on save/error; later PMC email (design audit)

### P3 — RAI allocations

- Superuser-only: set/raise project envelope(s), period, **trial/free/allocated** type
- Dual hard people vs automation limits when RAI/LiteLLM support exists
- Flashes; PMC notification when email exists

### P4 — Reports (open product)

- Foundation roll-up (RAI)
- Commons fairness / high-usage distributions (careful labels)
- Self-hosted utilization (capacity; Infra signals as needed)
- My usage (committer)
- **Not in open product:** donated **credit burn-down** (RAI-private vendor deals)

### P5 — Capacity + models allow-list

- Capacity meters (TPM/RPM/parallel) separate from $
- Models: wire Available (and key `models` allow-list) when team / envelope policy exists; column already stubbed

### Cross-cutting UX rules (when building)

- ASF vocabulary only (project, person, purpose, envelope)
- Script-first; secret once
- Quart flashes for mutations
- Proxy-down = loud banner, not silent zeros
- Supply-path model fields: site admin only (already in Models v1)

---

## Data handling

Nothing is stored on the GPU rentals. They run vLLM and nothing else — no
database, no application state. Prompt content exists in GPU memory for the
duration of one request and is overwritten. Deleting a pod leaves nothing
behind.

On the gateway host, two things are retained.

**LiteLLM's spend log**, in Postgres: token counts, model, latency, the
key's hash, user and project, the tool's user agent. LiteLLM's `messages`,
`response` and `proxy_server_request` columns can hold full request and
completion text and are switched off; every row contains `{}`.

**The prompt log**, since 2026-11-04, decided openly with the terms
written first (`docs/prompt-logging-design.md`, decisions 1 to 4). One
record per request: the prompt as it arrived, the model asked for and the
one that served it, the key and its project, the tool, token counts and
timing, the finish reason. The response is never recorded. The files are
root-owned under `/x1/llmao/prompt-log`, readable by the site admins over
`sudo` and by nothing else; no page on the site shows a prompt; no request
parameter switches the logging off, and the proxy strips the ones that
would. Retention is 90 days, by the sink that writes the files. The Terms
of Use (`/help/terms`) say all of this to users.

What the log is for is in `docs/analysis-design.md`: advice back to
projects and people about the shape of their use, never its content, and
a service shaped by real use. The analysis runs on the host, by an admin,
with our own models as judges, and its store carries the same retention.
Each piece of advice is a snapshot; nothing tracks a person's behaviour
over time.

Both providers are on secure tiers. A host operator with hardware access
could in principle observe a request while it is processed; that is true of
any rented hardware, is contractually prohibited on both platforms, and is
a live window rather than an archive.

The guidance to users stands: anything sent through is visible to the site
admins; public and project-internal material, yes; credentials, other
people's personal data and embargoed work, no. Unpublished security work
belongs on the models marked Private.

---

## Multi-repo

| Tree | Role |
|------|------|
| rai-private `services/llmao/` | Product design |
| tooling-llmao | Software + STATUS (this file) |
| p6 `modules/llmao` | Production deploy (**in production**) |
