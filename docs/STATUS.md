# Build status and backlog

**As of:** 2026-09-13<br>
**Repo:** `apache/tooling-llmao`<br>
**Product design (concepts/policy):** `apache/rai-private` → `services/llmao/README.md`<br>
**How to run/use this software:** repo [`README.md`](../README.md)<br>
**Ops:** Infra `p6/modules/llmao/README.md`

**Doc split**

| Kind | Where |
|------|--------|
| Product concepts (project-centered, budgets, credentials) | **rai-private design** |
| What the code does *today* (short) | **Done** below — detail is the code |
| **Planned UX** (layouts, workflows, phases) | **UX backlog** below |
| How to run locally / PATs against proxy | **README** (how-to) |

---

## Done (today — thin)

Implemented enough for local production-shaped use: asfquart OAuth; LiteLLMBackend + fail-fast team cache warm; `model_list.yaml` inventory; PAT UX (**My Keys** / **Other Keys**); **Models** catalog (supply-path redaction for non–site-admins); secrets as dual YAML / eyaml intent; system Postgres + prisma setup; offline `tests/mock_backend.py`.

**Models page:** catalog-first table (name, id, Available Yes/No, Request a key → `/keys/new?model=`). Context, license, and hosting live in Details; supply-path fields stay site-admin in the modal. Available is policy (always true until P5) **and** in service **and** in LiteLLM. Self-hosted with nothing serving or no deployment is No. Sort puts No last. Per-process state is on `/fleet`.

**Catalog vs LiteLLM:** `model_list.yaml` is not included by the proxy.
`litellm.yaml` `general_settings.store_model_in_db: true` (Puppet may also set the env). `self_hosted` is a required boolean. Commercial rows need a static `api_base` (fail-fast). Standalone watches `config.yaml`; Puppet restarts on change.

**GPU fleet:** `fleet.hosts` (IP → listen port); box JSON listen only; `VllmServer` + `FleetDeployment`. `/fleet` is signed-in (host:port admin). Green serving = vLLM up **and** LiteLLM has that `api_base`. Self-host: `/model/new` after serving, `/model/delete` on down (catalog YAML). Commercial: `/model/new` at llmao startup. LiteLLM `/health` cached on the deployment by the skew runner.

**Public port resolution differs by provider.** Vast exposes its container→public mapping through an API and resolves automatically. RunPod does not, so a host row takes an optional fourth element pinning the public port. RunPod also reassigns the port on every pod recreate, even when the pod lands on the same machine — so a pinned value is expected to change, not to be stable.

**The proxy serves end to end.** A PAT reaches three self-hosted models through `llm.apache.org` with TLS on 443, the LiteLLM port bound to loopback, tool calling and vision on the models that support them, and per-key attribution in the spend log.

| model | ctx | modality | tools | reasoning default | sustained |
|---|---|---|---|---|---|
| `gemma4-26b` | 131,072 | text+vision | yes | **off** | ~128 tok/s |
| `qwen3.8-27b` | 131,072 | text+vision | yes | on | ~46 tok/s |
| `qwen3-8b` | 40,960 | text | yes | on | ~54 tok/s |

Sustained figures are long generations. Short prompts measure round-trip
overhead and run roughly half these. Single-stream decode is
memory-bandwidth bound, so the models sit closer together than their sizes
suggest — the difference between them is concurrency, not per-request speed.

**Fit validation:** the catalog declares `vram_gb` and `disk_gb`, and a box checks them against `nvidia-smi` and `statvfs` before pulling weights. Requirements sum across co-resident servers. Silent when `nvidia-smi` is absent — an unknown is not a failure.

**Observed state:** `/fleet` shows measured KV cache against the served context window, scraped from vLLM's `/metrics` on the transition into SERVING. A `--max-model-len` above what the cache holds makes vLLM hang rather than error, which was previously only visible by reading a startup log.

Remaining: long vLLM boot, config revision on the box, pending-assignment table, box smoke, and nothing restarting a GPU box automatically.

Open policy still: **who creates automation PATs** (A RAI / B Chair-VP / C any PMC — code provisional C). See design §5.1.1.

Edge cases that bite operators (LiteLLM down, pagination, master key drift, prisma path, etc.) are tracked as work items, not product design.

---

## Tuning: what was limiting the fleet

Three bottlenecks stacked in front of the GPUs, each hiding the next.
Recorded here because the order matters more than any individual value.

**Apache `ProxyTimeout` killed any generation over 60 seconds.** It looked
exactly like a model truncating. Now 1800s.

**vLLM's `max_num_batched_tokens` defaults to 2048**, which held every box
to 2–3 concurrent requests while 90% of each card's KV cache sat idle —
requests spent 4.3× longer queued than being processed. Raised, the 27B
went from 2 concurrent to 38.

The value is **not portable between cards**: 16384 suits the 96GB and 80GB
boxes, and on a 24GB card it OOM'd during an FP8 matmul and killed the
engine outright. That box runs 4096. It scales with headroom, not with the
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

---

## Testing

Two scripts in `bin/`, stdlib-only, with no endpoints committed — they
discover from LiteLLM's routing table at runtime.

| | what it answers |
|---|---|
| `llmao-smoke` | does every model still work: completion, reasoning control, tool calling, vision, sustained output, large prompt. Exits non-zero, so it works in CI |
| `llmao-saturate` | where does a box start queueing, and is the limit the scheduler or memory — which decides whether a bigger card would help |

`llmao-smoke --direct` bypasses LiteLLM; the difference between the two runs
is the gateway's own overhead. Do not run the two scripts together — smoke
queueing behind a saturation ramp produces numbers that look like a
regression and are not.

---

## Connecting an agent

Claude Code works against the gateway with environment variables alone;
LiteLLM exposes `/v1/messages`, so no shim is needed.

```
ANTHROPIC_BASE_URL=https://llm.apache.org
ANTHROPIC_AUTH_TOKEN=<PAT>
ANTHROPIC_MODEL=gemma4-26b
CLAUDE_CODE_MAX_CONTEXT_TOKENS=120000
```

Three things that are not obvious:

- **Set the context limit below the real window.** vLLM rejects a request
  where prompt + `max_tokens` exceeds it, and a client that assumes 200k
  will auto-compact too late.
- **Pick a model whose reasoning is off by default.** A reasoning model
  emits nothing for a minute while it thinks; the client abandons the
  stream and retries, and the retries stack.
- **`reasoning_effort` vocabularies differ.** Qwen3.8-27B accepts only
  `xhigh`, `medium`, `low` and 400s on `high`, which several clients send
  by default. Gemma accepts all five.

Tool use through the Anthropic path is currently broken — see open issues.

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

### Open RAI (block precise numbers, not UX scaffolding)

1. Trial/free default amounts and duration
2. Hard dual budgets vs display-only people/automation split
3. Narrow stewards later (Chair vs any PMC)?
4. Capacity fair-share defaults from Infra

---

## Engineering backlog (non-UX or infra)

1. **`hosted_vllm/` provider prefix.** Routes are created as
   `openai/<name>`; LiteLLM's docs specify `hosted_vllm/` for an
   OpenAI-compatible vLLM server. Three symptoms point at it:
   `reasoning_effort` validated against OpenAI's vocabulary, tool use
   routed to the Responses API with a `tool_choice` shape vLLM rejects,
   and backend errors translating badly — a vLLM 422 arriving as HTTP 200
   with a null body. Worth verifying with a parallel test route before
   changing the push code.
2. **Route registration is not idempotent.** A host that changes address
   leaves the old route behind, and nothing removes it — one model had
   accumulated twelve identical routes, invisible in both UIs. The
   `bases - fleet_bases` branch of the skew check is exactly this case and
   should delete rather than only report. Pair with reconciliation at
   startup.
3. **Catalog does not record `reasoning_effort` levels** per model.
4. Harden PAT against LiteLLM pagination / delete ids
5. Automation creator policy after RAI decides §5.1.1
6. Site admin via `rai` PMC (optional keep cfg list)
7. PMC notification email on key/budget lifecycle
8. Advisor / richer routing
9. **Nothing restarts a GPU box.** All three were launched by hand; a
    host restart leaves the box up and the model down. RunPod pods carry
    their launch command in `--docker-args` and recover; the hand-launched
    box does not.

---

## Data handling

Nothing is stored on the GPU boxes. They run vLLM and nothing else — no
database, no application state. Prompt content exists in GPU memory for the
duration of one request and is overwritten. Deleting a pod leaves nothing
behind.

What is retained is on the gateway host: token counts, model, latency, and
a hashed key for attribution. LiteLLM's `messages`, `response` and
`proxy_server_request` columns can hold full request and completion text
and are switched off; every row contains `{}`.

That is a current state rather than a principle. Prompt history is useful —
for debugging, evaluation, possibly retrieval — and we will likely want
some form of it. When we do it should be a deliberate decision with
retention limits and an access model, decided openly rather than arrived at
by default.

Both providers are on secure tiers. A host operator with hardware access
could in principle observe a request while it is processed; that is true of
any rented hardware, is contractually prohibited on both platforms, and is
a live window rather than an archive.

For the pilot the guidance is that anything sent through should be treated
as visible to llmao admins: public and project-internal material, yes;
credentials or embargoed work, no. That belongs on the Models page.

---

## Multi-repo

| Tree | Role |
|------|------|
| rai-private `services/llmao/` | Product design |
| tooling-llmao | Software + STATUS (this file) |
| p6 `modules/llmao` | Production deploy (**in production**) |
