# Build status and backlog

**As of:** 2026-11-12<br>
**Repo:** `apache/tooling-llmao`<br>
**Product design (concepts/policy):** `apache/rai-private` → `services/llmao/README.md`<br>
**How to run/use this software:** repo [`README.md`](../README.md)<br>
**Ops:** Infra `p6/modules/llmao/README.md`

**Doc split**

| Kind | Where |
|------|--------|
| Product concepts (project-centered, budgets, credentials) | **rai-private design** |
| What the code does *today* (short) | **Done** below — detail is the code |
| What is open, what is parked, what is waiting on a decision | **Where the work stands** below |
| **Planned UX** (layouts, workflows, phases) | **UX backlog** below |
| How to run locally / PATs against proxy | **README** (how-to) |
| How the pieces connect | [`ARCHITECTURE.md`](ARCHITECTURE.md); the design docs it points at |

---

## Done (today — thin)

Implemented enough for local production-shaped use: asfquart OAuth; LiteLLMBackend + fail-fast team cache warm; `models.yaml` definitions; PAT UX (**My Keys**, including key requests; automation keys are listed for site admins as **Service keys**); **Models** page (supply-path redaction for non–site-admins); secrets as dual YAML / eyaml intent; system Postgres + prisma setup; offline `tests/mock_backend.py`.

**The front door** is three steps, sign in, make a key, send a request, each marked done when the server can see it is: a key exists for this person, a spend-log row exists for them ever, each one question to LiteLLM with a one-record answer. Picking a model is advice inside the connect step, not a step, since nothing is stored when someone picks one. The welcome page says nothing about who sees prompts; hosting facts live on the Models page, the FAQ and the Terms. A site admin can **operate as a normal user** from the name menu: the standing goes everywhere, the Admin link, the work pointer and `/admin` itself, and the flag is per browser session and names the uid that set it.

**Models page:** definitions-first table (name, id, Available Yes/No, Request a key → `/keys/new?model=`). Context, license, and hosting live in Details; supply-path fields stay site-admin in the modal. Available is policy (always true until P5) **and** in service **and** in LiteLLM. Self-hosted with nothing Healthy or no deployment is No. Sort puts No last. Per-process state is in the `/models` admin drill-down.

**models.yaml vs LiteLLM:** admin definitions; the proxy does **not** include this file.
`litellm.yaml` `general_settings.store_model_in_db: true` (Puppet may also set the env). A model carries a vLLM recipe or a static `api_base`, one or the other (fail-fast); self-hosted is a property of a deployment, not written in `models.yaml`. Self-host `litellm_params.model` is `hosted_vllm/<name>` (not `openai/`). Standalone watches `config.yaml`; Puppet restarts on change. YAML edit ≠ live deployment until llmao POSTs `/model/new` again.

**LiteLLM price list:** LiteLLM prices each request to a paid provider from its model cost map, fetched from GitHub at its startup. It reloads every 24 hours because of a schedule set once with `POST /schedule/model_cost_map_reload?hours=24` (master key). The schedule lives in LiteLLM's database, so a rebuilt database loses it; llmao logs an ERROR at startup when it is missing. Spend is priced when a request is made and is never re-priced, so it is LiteLLM's estimate, not the provider's bill.

**GPU fleet:** `fleet.hosts` (public IP → rows of model name and listen port); rental JSON listen only; `VllmServer` + `FleetDeployment`. `/models` is signed-in (host:port admin). A deployment's identity is its listen URL plus model name (`DeploymentKey`), stamped into the LiteLLM row as `model_info.llmao_key`; pages and log lines name it by `FleetDeployment.label`, the model then where it runs ("qwen3-8b on Vast 53865410:8001", the listen port, since one host can run several servers; "grok-4 at xAI"), with the rental's own label, status and console link beside it, and the public port only where it differs. Deployments are one list grouped by model, each row with a primary action and a menu for the rest. "Stop tracking" drops a rental from the list; it does not stop the rental. The green badge is Healthy **and** LiteLLM has that `api_base`. Self-host: `/model/new` when Healthy, `/model/delete` on Unhealthy, Stalled, Rejected or Foreign when a deployment exists, and on an unlisted host that is not Healthy (`models.yaml` template; the states are in `docs/rental-lifecycle.md`). `config.yaml` `providers` holds every account we pay: the GPU hosts `vast` and `runpod` (only `api_key`; `fleet.vast.api_key` still works, with a deprecation warning) and model providers. Provider: `/model/new` at llmao startup, skipped when already registered; `order` puts it behind our rentals. LiteLLM `/health` cached on the deployment by the skew runner.

**Public ports come from the provider's listing.** Vast's instance listing and RunPod's pod listing both carry the container→public mapping; the lifecycle stamps each server's public port from the rental its listing places it on. RunPod reassigns the port on every pod recreate, even when the pod lands on the same machine; the next listing read follows it. The optional fourth host-row element that pinned a RunPod port is deprecated: it loads with a warning and only seeds the port until the listing names the rental.

**The proxy serves end to end.** A PAT reaches the self-hosted models through `llm.apache.org` with TLS on 443, the LiteLLM port bound to loopback, tool calling and vision on the models that support them, and per-key attribution in the spend log. The fleet is self-hosted only as of November 2026: nothing is served from a paid provider.

| model | ctx | modality | tools | reasoning default | sustained |
|---|---|---|---|---|---|
| `gemma4-26b` | 131,072 | text+vision | yes | **off** | ~128 tok/s |
| `qwen3.8-27b` | 131,072 | text+vision | yes | on | ~46 tok/s |
| `qwen3.8-27b-int4` | 131,072 | text+vision | yes | on | not measured |
| `qwen3.5-9b` | 131,072 | text+vision | yes | on | not measured |
| `qwen3-8b` | 40,960 | text | yes | on | ~54 tok/s |

Sustained figures are long generations. Short prompts measure round-trip
overhead and run roughly half these. Single-stream decode is
memory-bandwidth bound, so the models sit closer together than their sizes
suggest — the difference between them is concurrency, not per-request speed.

`qwen3-8b-bf16`, `qwen3-8b` from the unquantized weights for hardware the
FP8 build cannot load on (the GB10, below), is still defined but retired:
no deployment, none planned. It stays in `models.yaml` as the record of how
that card was served.

**Capacity planning.** `/admin/capacity` is a planning page: day charts,
what was asked of each model beside what its rentals served, cost per
million tokens, and a Recommendations box whose first rule (a rental whose
KV cache is too small for its traffic) has already found one. The figures
are the hourly capacity values the sampler folds from each rental's `/metrics`
and the demand values the daily rollup folds from LiteLLM's spend log and
the prompt log. Design: `docs/capacity-design.md`.

**The prompt log.** Since 2026-11-04 the proxy records every request it
serves, one JSON line per request in one file per UTC day on the gateway
host, written by a LiteLLM hook through loguru, which owns rotation,
compression and the 90-day retention. The prompt is kept, the response is
not. A record is a plain dict, since a client's tool schema may carry a
key named `items`; the key block is hash, purpose, user, project, the
purpose being the text typed when the key was made. A project or person
that is none is labelled `llmao:none`, a master-key request
`llmao:master-key`: one word each, and the prefix says the gateway made
the label up. Design: `docs/prompt-logging-design.md`; what is kept and
who reads it is under "Data handling" below and in the Terms of Use.

**The analysis of the log** is R&D in `analysis/`, outside the package:
one entry point, `prompts`, that clusters a span of days by project,
person, tool and the opening of the instructions, asks several of our own
models three questions about each cluster (Foundation work or not, the
lowest tier that would do, what people are doing), and reports the
consensus and the clusters running on a heavier model than the work needs.
Four judges on the first day's 23 clusters agreed on Foundation work every
time and on the activity nearly so; the tier needed rules. Since
2026-11-11 the four judges run on production (p6 #2933) with the
`analysis_*` settings rendered by Puppet and required by the script; the
store was rebuilt there under the new labels and nothing waits for a
judge. A cluster's exemplar is re-picked when the day it came from is
pruned, so a steady cluster's verdict is renewed once per retention.
Design: `docs/analysis-design.md`.

**Fleet reconciliation** is most of the way through: each source the
portal watches (rentals, config fetches, approvals, LiteLLM deployments,
vLLM probes) lives in its own module, joined by one key; an approval is
intent; a machine nobody intends is shown, never adopted; an orphan
deployment is skew for a human, never pruned unattended. The Deployment
step, one class with a transition table absorbing `model_status.py`, is
what remains; see "Where the work stands". Design:
`docs/fleet-reconcile-design.md`, a tracking document that is deleted when
the work is done.

**Fit validation:** `models.yaml` declares `vram_gb` and `disk_gb`, and optionally `kv_bytes_per_token` (KV cache per token of context, so a server's need at `max_model_len` is counted, not just its weights), and a rental checks them against `nvidia-smi` and `statvfs` before pulling weights. A rental with several GPUs runs every server across all of them (`--tensor-parallel-size`), and the check and the sizing are per card. VRAM sums across co-resident servers, since each loads its own copy; disk counts once per distinct model, since servers of the same model share one copy of the weights. Silent when `nvidia-smi` is absent — an unknown is not a failure. The rental then divides the card between its servers: the budget is the memory that is free (`MemAvailable` on a unified-memory machine, where `nvidia-smi` reports no VRAM) less a reserve of 10% or 2 GB per card, whichever is more, and each gets its `vram_gb` plus a share of the remainder in proportion to its context window, passed as `--gpu-memory-utilization`, so two servers can share one card. Each server runs with vLLM's startup plan, so the KV cache size measured after CUDA graph capture is kept on the volume and reused, rather than re-estimated on every boot (the estimate can miss the graphs, and a tight card may fail its first boot before the plan exists). A fraction set in `models.yaml` is kept and deducted from the budget first.

Limits of that sizing, all in `hosting/README.md`: it happens once, at install; re-running the install on a rental whose servers are already up sees little free memory and sets no fraction; and servers that load at the same moment get less KV cache than their share.

**Observed state:** the `/models` drill-down shows measured KV cache against the served context window, the cache size taken from each reading of vLLM's `/metrics` by the sampler, and the window from `/v1/models` with the working key, asked on the transition into Healthy and again until it has answered, while the key is refused, or while no listing names the host. A `--max-model-len` above what the cache holds makes vLLM hang rather than error, which was previously only visible by reading a startup log.

**A hardening round from a security audit** landed on 2026-11-12, thirteen commits, each saying which audit finding it answers and where the audit's rating or proposal was wrong. The portal trusts the one Apache proxy in front of it for the client address (hypercorn's `ProxyFixMiddleware`, one hop) and names a rental by that corrected peer, not a header it sent. A rental's `/v1/models` and `/metrics` replies are read up to a 2 MB cap, streamed, since a timeout bounds the wait and never the size. The key list is walked page by page, so a key past the hundredth can be revoked. `@api` replies carry `no-store`, and the prompt log drops credential-shaped custom headers by a pattern over the name, not a list. The tier row's read-change-write is serialised within the one process. Deciding an access request takes a site admin; a cap raised before the request's save failed is said in the admin notice. Ports are range-checked, stored request rows are checked on the way in, two refusals say the same thing whether the key is invisible or someone else's. `log_level` in `config.yaml` sets the level (default DEBUG, so nothing changes until the node yaml says INFO; the p6 side is a class parameter and a template line) and the timestamps carry year, seconds and offset. The fleet key goes over https only, loopback excepted.

Remaining: long vLLM boot, config revision on the rental, pending-assignment table, rental smoke, and nothing restarting a GPU rental automatically.

Open policy still: **who creates automation PATs** (A RAI / B Chair-VP / C any PMC — code provisional C). See design §5.1.1.

Edge cases that bite operators (LiteLLM down, pagination, master key drift, prisma path, etc.) are tracked as work items, not product design.

---

## Where the work stands

As of 2026-11-12. Three lists: what is small enough to pick up in an
hour, what is an afternoon, and the larger threads with the steps each
still has. Then what is waiting on a decision, and what is parked on
purpose. Issues on GitHub hold the ideas that want colleagues' eyes
before any code.

### Small, an hour or less

- **Disk free on `/admin`.** The host has one 100 GB root disk and the
  prompt log grows on it. A free-space figure beside the LiteLLM version
  check. The DataDog monitor half is Infra's.
- **`hosted_vllm/` check in `validate_models`:** a model with a vLLM
  recipe must name `hosted_vllm/...` in `litellm_params.model`.
- **`analysis/prompts` under a closed pipe** (`prompts clusters | head`)
  ends in a BrokenPipe traceback.

### Medium, an afternoon

- **Check LiteLLM now, per deployment, on `/models`.** LiteLLM's own
  health check runs every four hours because it sends real completions;
  after a fix, "LiteLLM down" lingers for hours. A button that runs
  `GET /health?model_id=<id>` for that one deployment and feeds the
  result into the same fields the skew check writes. A paid deployment
  would get a note that it spends tokens.
- **"At or near their limit" on `/admin`.** Free-tier people at 80% of
  the rolling allowance and projects at 80% of their weekly cap, from the
  activity rows the usage pages already read. Quiet when nobody is near.
- **Issue #68, the scanner.** A source probes `/v1/models` daily with
  model names as tokens. A refused-at-the-door admin signal, kept apart
  from refusals of model calls.

### The larger threads

**Fleet reconciliation, the Deployment step.** Waiting on a decision
(below) since 2026-10-30.

- Correct the design doc's transition matrix: a walk of its cells
  against the code found four undeclared mismatches, agreed to be real.
- `deployment.py` commit 1: the Deployment class and its transition
  table, absorbing `model_status.py`; `deployment_status(...)` becomes a
  method, retiring the five-call pair in `fleet.py` and `pages.py`.
- `docs/rental-lifecycle.md` rewritten in the new names.
- The tracking doc folded into `ARCHITECTURE.md` and deleted.
- Then re-assess the parked backlog against the new shape.

**The UX rethink** (started 2026-11-02; the capacity display round is
done).

- The tiles-and-words commit on `/admin/capacity`: the card's $/day is
  the period average (verified, not a bug) beside present-tense $/hr
  rental lines, so label it so and add the current rate; whether an
  idle-rental dollar tile belongs is a question to ask first.
- Details on `/models` as a tabbed dialog: general; provenance from
  Apache Sourcelume; an admin tab with provisioning facts and GPU
  metrics.
- A rental's detail as a modal, live facts only.
- Capacity advising on the shape of the next rental for a model ("a 96 GB
  card with 42 GB of disk") from what its deployments measured.
- Parked within it: performance numbers, disk and GPU metrics, verdicts.

**Capacity recommendations.** The first rule is live.

- Hours-over-threshold counts in the summaries, for burst awareness
  (qwen3-8b averaged 1% busy and hit KV 100% in burst hours).
- The other rules the data supports: Add (busy or queueing in a share of
  hours), Retire one of several idle rentals, Deploy (asked for, nothing
  serving), Retire an unasked deployment.
- Snooze on a recommendation; the minimum rentals per model in config,
  since redundancy is policy.

**The analysis, the portal's part** (`docs/analysis-design.md` A6 to
A8; parked 2026-11-05 until the judges have run for a while).

- The project page's panels from each project's share of the demand
  values: tools by model, refusals, latency, prompt lengths, a daily
  trend. The data has been in the demand values since fold version 3.
- A note to a project, written by an admin, dated, shown until a PMC
  member acknowledges it: the way a finding reaches the people it
  concerns.
- A person's own shape on the keys page, from LiteLLM's daily rows.
- The taxonomy refined with colleagues against real verdicts; tests for
  `analysis/_core.py` once its functions stop moving.

**Access control** (`docs/access-control-design.md`; paused 2026-11-08
with a three-step plan agreed and nothing built).

- The design, revised 2026-11-08: LiteLLM's groups cover a model name;
  its tag routing chooses among a name's deployments, and a `!` or `&`
  tag from the key or team cannot be taken off by the caller. So the
  second name per paid route is withdrawn: provider deployments carry
  `paid`, teams that may never spend carry `!paid`. The hole it closes:
  once a provider deployment shares a name with ours, the free tier
  reaches it as fallback.
- Step 1: llmao tags `paid` and `self-hosted` on the deployments it adds,
  `router_settings` in the example config; the live rows backfilled by
  hand.
- Step 2: the p6 change that switches `enable_tag_filtering` on, a
  reviewed PR.
- Step 3: `!paid` on the pretend projects' teams in the existing
  out-of-line check; the analysis judge sends `!paid` (#73).
- Still open: who grants a restricted group (site admins only, or a PMC
  for its own project); paid models granted per project with a budget;
  a model going from Private to Mixed means prompts may leave our
  infrastructure, and keys minted under the Private promise may want
  revoking or re-consenting; the doc's questions 2, 3, 4, 6 and 7.

### Waiting on a decision

- **The reconcile matrix correction**, two ways: (a) take LiteLLM's
  "has the deployment" fact out of the Deployment step and make the
  delete a plan rule, which is the recommendation; or (b) keep it in and
  rewrite three cells to what the code does, carrying the crossed concern
  into `deployment.py`.
- **Who creates automation PATs** (design §5.1.1): RAI, a Chair or VP, or
  any PMC; the code is provisionally the last.
- **The Open RAI numbers** under the UX backlog: trial amounts and
  duration, hard dual budgets or a display split, narrower stewards,
  capacity fair-share defaults.

### Held on GitHub for colleagues

- **#72, request priority on our rentals.** vLLM's priority scheduling
  would let the analysis judge queue behind users, but any client could
  then raise its own priority; it needs the proxy to strip the field from
  everyone not allowed to set it. Not done.
- **#73, "self-hosted only" on a request.** Now step 3 of the access
  control plan above: tags on the deployments, the filter switched on
  in `litellm.yaml`, and a request sends `!paid`. Not in our hook, which
  runs before routing and cannot choose a deployment. Not a development
  item while nothing is served from a paid provider.
- **#68, the scanner**, above.

### Parked on purpose

- Withdraw an approval from the UI; a Reboot button once a provider API
  is verified to reboot a rental; a RunPod console link per pod once a
  stable per-pod URL is confirmed; explicit `--kv-cache-memory` on a
  tight card; FLEET_KEY hardening and rotation; the budget and request
  scaffolding in `seam.py`, which belongs to a colleague's in-progress
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

### P6 — Models page follow-ups

Left open when the models-dashboard design was retired (its two passes
shipped; the durable parts are in `docs/ARCHITECTURE.md` and
`docs/rental-lifecycle.md`):

- **Event history** per deployment: record every lifecycle transition with
  a timestamp and show the latest ("Loading since 12:04"). Debugging a
  stalled rental otherwise means guessing.
- **Check LiteLLM now** per deployment: force LiteLLM's health check from
  the page. It costs tokens, so a per-deployment cooldown (proposed five
  minutes); paid providers flagged. The shape is settled under "Where the
  work stands", medium.
- **Preview as user** toggle for admins, so the user view can be checked
  without a second account.
- **Drained** state for planned maintenance, so maintenance is not read as
  degradation.
- **Privacy wording** for users, with the rented-VM caveat (a GPU host
  operator can in principle observe a VM's memory).

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

1. **`hosted_vllm/` — done in `models.yaml`.** New `/model/new` bodies use
   that prefix. LiteLLM DB rows created as `openai/` stay until deleted and
   re-pushed.
2. **Deployment registration — done.** A host that changed address used to
   leave the old deployment behind for good (one model had accumulated
   twelve identical rows). Now every deployment is stamped with its
   identity (`model_info.llmao_key`), every write is confirmed by
   re-reading `/model/info`, and a deployment no server claims is an
   orphan on the /models strip after `ORPHAN_PASSES` skew passes, with
   Stop tracking; it is never deleted unattended (2026-10-28). The rest
   of the clean-up is the reconcile migration
   (`docs/fleet-reconcile-design.md`).
3. **`models.yaml` does not record `reasoning_effort` levels** per model
   (Qwen 3.8 lists them; not a uniform field).
4. Harden PAT against LiteLLM pagination / delete ids
5. Automation creator policy after RAI decides §5.1.1
6. Site admin via `rai` PMC (optional keep cfg list)
7. PMC notification email on key/budget lifecycle
8. Advisor / richer routing
9. **Nothing restarts a GPU rental.** All three were launched by hand; a
    host restart leaves the rental up and the model down. RunPod pods carry
    their launch command in `--docker-args` and recover; the hand-launched
    rental does not.
10. **Active notification when a model goes unserved.** A server was lost
    unseen for days in late October; Infra has been asked for a monitor.
    Until then the redundancy rule on `/admin/capacity` is the only
    signal, and only to someone looking.
11. **asfquart `Response` pass-through.** `pages.py` carries a
    `use_template` shim that should go upstream into asfquart and then be
    dropped here.

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
