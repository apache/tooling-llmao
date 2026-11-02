# Architecture (implementation)

This document describes **how this repository is structured**. Product intent,
credential rules, ownership, and non-goals live in the **master design**:

- **Committers:** `apache/rai-private` → `services/llmao/README.md`

This app is the **asfquart / Tooling half** of the gateway: identity, project
vocabulary, PAT UX, and (planned) project envelope UX. **LiteLLM** is the
inference pipe and meter. Completions are **not** re-proxied through this process.
Product concepts: rai-private design; planned UI work: `docs/STATUS.md`.

## Always asfquart

There is no “dev auth mode.” The process is always constructed with
`asfquart.construct("llmao", …)` in root `main.py` (same idea as Apache
STeVe’s `server/main.py`). HTML lives in `pages.py`, JSON API in `api.py`,
both bind to `asfquart.APP` after construct. Standalone: `python main.py`
(`runx` + optional TLS). ASGI: `hypercorn main:llmao_app` (`run_asgi`).

- OAuth at `/auth` (oauth.apache.org)
- Session cookies (`SESSION_COOKIE_SECURE=True` → use HTTPS locally)
- Optional `token_handler` for bearer tokens against *this* app (stub today)
- `@asfquart.auth.require` / `Requirements` on protected routes; project-scoped
  rules stay in `seam.py`

Local TLS: `config.yaml` `server.certfile` / `keyfile` under `certs/`, typically
mkcert for **`localhost.apache.org`**. `config.yaml` is gitignored (secrets).
Loaded as **`APP.cfg`** (EasyDict); use dotted access (`APP.cfg.litellm.base_url`).

## The two halves

**asfquart** handles *who you are and what you're allowed to do* at the
Foundation level. LDAP-backed `ClientSession` carries `uid`, committer
`projects`, and PMC `committees` (`pmcs`).

**LiteLLM proxy** holds *teams, users, virtual keys (PATs), budgets, capacity
limits, and spend*. Clients call its OpenAI-compatible API with a PAT. This
process talks to LiteLLM over the **admin** surface (master key, **async
httpx**) to provision teams and mint or revoke virtual keys—see design
§5–6. Project names are LDAP/session names (asfquart); no rename map.

**Model definitions** are `models.yaml` (llmao UX + vLLM recipe + `/model/new`
template). LiteLLM does **not** include it. `store_model_in_db: true` in
`litellm.yaml` `general_settings` (Puppet may also set the env). Deployments
are pushed by llmao. Self-host prefix is `hosted_vllm/`.
A model carries a vLLM recipe if we can run it ourselves, and says nothing
about where it is served: that is each deployment's. Our boxes come from
`fleet.hosts`; providers we pay (xAI, OpenRouter) from `config.yaml`
`providers`, each listing the models it serves and holding its own key. One
model can be served several ways at once; LiteLLM's `order` puts our boxes
first and a provider only behind them. Governance fields live under
`model_info`. Standalone asfquart watches `config.yaml` (`runx` extra_files);
production Puppet restarts the service after a change.

**LiteLLM virtual keys / teams** need Postgres + Prisma (`litellm[proxy,extra-proxy]`).
Developers: system PostgreSQL + `make db`. Production: Puppet + on-disk
`database_url`.

**LLMAO's own records** live in LiteLLM's memory store (`/v1/memory`), since
LLMAO has no database. By key prefix: `llmao:request:` (access requests),
`llmao:tiers` (tier limits an admin changed), `llmao:skew:` (what the
outside-edits runner found), `llmao:action:` (the admin action log),
`llmao:log:` (notices), `llmao:capacity:` (one row per server per hour),
`llmao:demand:` and `llmao:demand-day:` (one row per model per hour from
the spend log, and the per-day marker). Per-project token caps are still
team metadata.

**PATs:** personal keys bind ASF uid + project team + purpose; automation
keys are team-scoped exceptions (who may create them is an **open RAI
policy** question — see design + `docs/STATUS.md`). Secrets shown once;
metadata in LiteLLM.

**GPU fleet (control plane).** The box side is `docs/box-protocol.md`; the
state machine is `docs/box-lifecycle.md`. `APP.fleet` (`llmao/fleet.py`) is
built at startup.

- `GET /vllm/config` (Bearer `fleet.key`) maps `X-LLMAO-Host` to a
  `fleet.hosts` row, else the leftmost `X-Forwarded-For`, else the peer.
  The header is whatever the provider's `provision.sh` sets: Vast's
  instance id (`CONTAINER_ID`), RunPod's pod id. That name is the
  server's key, what the bearer is minted from, and what an approval is
  by; where the server is dialed (`VllmServer.address`) is unknown until
  the provider's listing places it, since the listing carries both the
  address and the name a box on it sends (`Rental.name`). A name that is
  an IP literal (a legacy or local box) is its own address.
- The JSON is `{host, servers[]}`. `servers[].port` is the **container
  listen** port, never the public port. Both providers' listings supply the
  public port; it is never sent to the box.
- The runners (`app.add_runner` in `main.py`; each is one `while True`
  loop with its own interval):
  - `fleet-lifecycle` (`Fleet.run_lifecycle`, every `health_interval_s`):
    reads the Vast listing, places servers on their boxes, notes rentals
    nobody intends, probes every server's `/health` (and `/v1/models`
    when the key or window needs asking), then calls `Fleet.after_probe`,
    which is `DeploymentManager.sync_selfhost`: `/model/new` for a
    Healthy server, `/model/delete` for a down one.
  - `runpod-rentals` (`Fleet.run_runpod_rentals`): reads what RunPod
    reports about our pods every 10 minutes.
  - `litellm-skew` (`LiteLLMBackend.run_skew`, every `skew_interval_s`,
    after the fleet's first pass): reads the LiteLLM deployments and, on a
    long interval, LiteLLM's own `/health`; reports orphans (deployments
    nothing intends) for the /models strip. Nothing deletes an orphan
    unattended.
  - `litellm-outside-edits` (`LiteLLMBackend.watch_outside_edits`, same
    interval): finds teams and keys changed in LiteLLM's own admin UI
    rather than through llmao, and records them for "Work needed".
  - `metrics-sampler`, `capacity-writer`, `capacity-prune`
    (`llmao/sampler.py`, `llmao/capacity.py`): read each serving box's
    `/metrics` every `sample_interval_s`, fold the readings into one row
    per server per hour, store finished hours, prune past
    `capacity_retention_days`. Design: `docs/capacity-design.md`.
  - `demand-rollup` (`DemandRollup`, `llmao/capacity.py`): once a day at
    01:00 UTC and at start, reads each closed day of LiteLLM's spend log
    that the store lacks and stores it as demand rows, one per model per
    hour, with a day marker carrying `FOLD_VERSION`. Design:
    `docs/prompt-logging-design.md`, "Demand rows".
  - `memory-prune` (`llmao/pruner.py`): daily, the other records in the
    memory store (access requests, the action log).
- `Fleet.models` is the YAML recipe. `FleetDeployment` is one intended
  LiteLLM deployment. `VllmServer` carries the lifecycle state: Awaiting,
  Loading, Healthy, Unhealthy, Stalled, Rejected or Foreign.
- The `/models` admin drill-down shows the green badge only when the server
  is Healthy **and** LiteLLM has its deployment. host:port is site-admin.
- JSON handlers use `@api` in `api.py`. Do not wrap Quart `asgi_app` with
  Werkzeug ProxyFix.

**GPU boxes are not this process.** Vast: custom template,
`PROVISIONING_SCRIPT` → `provision.sh` → curl `install_set.py` (stock
container). RunPod: our own image, built by CI from `hosting/runpod/`, with
`provision.sh` as the entrypoint; the installer is still fetched at boot.
Detail: [`hosting/README.md`](../hosting/README.md). Runbooks:
[`hosting/vast/README.md`](../hosting/vast/README.md);
[`hosting/runpod/README.md`](../hosting/runpod/README.md).

Build status and backlog: **`docs/STATUS.md`**.

The app **always** uses **LiteLLMBackend** against a real LiteLLM admin API.
Offline **MockBackend** lives under `tests/` and is injected only by unit
tests—not a second runtime mode.

**Team ids:** in-process cache `project (team_alias) → team_id` only (immutable
under our rules). Warmed at startup (`before_serving` + `warm()`, fail-fast if
LiteLLM is down). Spend/budget always from live `team/info` when the id is
known; after a cache-miss `team/list`, use list-row fields (no redundant
`team/info`). No on-disk state store.

## The seam

`seam.py`:

1. **authorizes** project membership / PMC admin after asfquart has authenticated;
2. **delegates** project-scoped team/key ops to the backend (product API speaks project).

## Request path (this process)

```
browser → HTTPS (local mkcert or prod proxy)
       → asfquart OAuth / session
       → @require + seam.authorize
       → LiteLLM admin API (team/budget/PATs; deployments)
```

HTML mutations are `POST /do-*` only, then **303** to a GET display (flash for status; created-key secret is a `raw` HTML flash). JSON API is separate.

## Inference path (LiteLLM)

```
client tool → LiteLLM + PAT → budget / capacity → model
```

See `../README.md` for quickstart and API surface.

## Models and deployments

The vocabulary the `/models` page and the fleet code share. (Folded in from
the retired models-dashboard design, whose two passes shipped as
`llmao/model_status.py` and the combined Models page.)

- A **model** is what a user calls by the `model` parameter (`qwen3-8b`).
  A **deployment** is one place it is served: a vLLM server on a box we
  rent, or a commercial provider's endpoint. One model may have several
  deployments at once; LiteLLM's `order` puts our boxes first.
- **Self-hosted** means we control the virtual machine, not that we own the
  hardware (the ASF owns none). A rented Vast or RunPod VM running our vLLM
  is self-hosted.
- **Privacy** is a first-class, user-facing field. It answers "will my
  prompt leave the LLMs we control?"
  - **Private**: served only by vLLM on VMs we control.
  - **External**: sent to a third-party provider.
  - **Mixed**: the model has both kinds of deployment, or a LiteLLM
    deployment that config does not list. Mixed is the least private tier,
    so a Private badge is never shown for a model that could route
    externally.
  - The wording must not overclaim: a GPU host operator can in principle
    observe a rented VM's memory.
  - The LiteLLM-to-box hop is plain HTTP on the provider's public port,
    authenticated by the per-box bearer. It is not TLS.
- **What users see** per model:
  - Available, Degraded or Unavailable (the roll-up in
    `docs/box-lifecycle.md`).
  - Labeled capability chips.
  - The context window as the **minimum** across healthy deployments.
  - Concurrency as "up to N full-context requests": KV cache tokens divided
    by the window, summed over healthy self-hosted deployments. An upper
    bound.
  - Never an Awaiting, Loading or Stalled deployment. A box's pre-states
    must not make a working model look broken.
- **What admins see** in addition: the attention strip (machines nobody
  intends, whether they asked for a config or a provider reports them;
  Stalled, Unhealthy, Rejected, Foreign, unlisted, skew), a replica summary
  per model, and the deployments drill-down with one primary action and a
  menu. An approved host's rows say so; the approval is never counted.
- **Single source of intent:** `fleet.hosts` plus in-memory approvals say
  what should run; servers and LiteLLM deployments are derived from it. A
  deployment config intends but LiteLLM lacks is a visible skew badge, not
  a mystery.

## The system

Everything llmao talks to, and who talks to whom. Two processes are ours
on the one box: this one (the portal) and the LiteLLM proxy, which runs
our two callbacks from `litellm_hooks.py` inside it. The boxes and the
providers are outside.

```mermaid
flowchart TB
  subgraph people [people and their tools]
    BROWSER[browser]
    TOOL[client tool<br/>with a PAT]
  end
  subgraph ourbox [our box, Puppet-managed]
    direction TB
    LLMAO[llmao, the portal<br/>asfquart + runners]
    subgraph proxy [LiteLLM proxy]
      ROUTER["router: PAT, team budget,<br/>rate limits, spend log"]
      HOOKS["litellm_hooks.py:<br/>empty_completion_guard, prompt_log"]
      MEMORY[("memory store<br/>llmao:* rows")]
    end
    PG[(Postgres)]
  end
  subgraph fleet [GPU boxes we rent]
    VLLM[vLLM servers<br/>/health /v1/models /metrics]
  end
  subgraph outside [outside services]
    OAUTH[oauth.apache.org + LDAP]
    PAID[model providers<br/>xAI, OpenRouter]
    RENT[rental providers' APIs<br/>Vast, RunPod]
  end

  BROWSER -->|HTTPS| LLMAO
  LLMAO <-->|sign-in| OAUTH
  TOOL -->|OpenAI API + PAT| ROUTER
  ROUTER --> HOOKS
  ROUTER <--> PG
  ROUTER -->|hosted_vllm, per-box key| VLLM
  ROUTER -->|provider key| PAID
  LLMAO -->|admin API, master key:<br/>teams, keys, model/*, spend| ROUTER
  LLMAO <-->|/v1/memory| MEMORY
  VLLM -->|GET /vllm/config, fleet key| LLMAO
  LLMAO -->|probe + sample| VLLM
  LLMAO -->|listings, prices| RENT
```

The inference path, tool to model, never enters the portal. The portal
sees it afterwards, in LiteLLM's spend log, and the hooks see it as it
happens, inside the proxy.

## Inside the portal

What runs in this process and what each part reads or writes. The HTML
and JSON handlers answer requests; the runners are the control plane and
the meters; everything they learn sits on `Fleet` or in the memory
store, where the pages read it.

```mermaid
flowchart LR
  subgraph handlers [request handlers]
    AUTH[OAuth + session<br/>require + seam.authorize]
    PAGES["pages.py: / projects keys<br/>models admin admin/capacity help"]
    JSON["api.py: /vllm/config<br/>/v1/projects/…/usage, budget, /healthz"]
  end
  subgraph state [in-process state]
    FLEET["Fleet: servers, deployments,<br/>approvals, rentals, orphans"]
    ROLLUP[HourlyRollup]
  end
  subgraph runners [runners]
    LIFE[fleet-lifecycle]
    RUNPOD[runpod-rentals]
    SKEW[litellm-skew]
    EDITS[litellm-outside-edits]
    SAMPLER[metrics-sampler]
    WRITER[capacity-writer]
    DEMAND[demand-rollup]
    PRUNE[capacity-prune<br/>memory-prune]
  end
  subgraph backend [LiteLLMBackend, one transport]
    MGRS[teams keys metering tiers<br/>requests skew log deployments]
    MEM[MemoryStore]
  end
  BOXES[vLLM servers]
  LITELLM[LiteLLM admin API]
  VAST[Vast API]
  RP[RunPod API]

  AUTH --> PAGES
  AUTH --> JSON
  PAGES --> MGRS
  PAGES --> FLEET
  JSON -->|config for a box| FLEET
  LIFE -->|listing| VAST
  LIFE -->|probe /health, /v1/models| BOXES
  LIFE --> FLEET
  LIFE -->|after_probe: sync_selfhost| MGRS
  RUNPOD --> RP
  RUNPOD --> FLEET
  SKEW -->|/model/info, /health| MGRS
  SKEW --> FLEET
  EDITS --> MGRS
  SAMPLER -->|/metrics| BOXES
  SAMPLER --> ROLLUP
  WRITER --> ROLLUP
  WRITER --> MEM
  DEMAND -->|/spend/logs/v2, one closed day| MGRS
  DEMAND --> MEM
  PRUNE --> MEM
  MGRS --> LITELLM
  MEM -->|/v1/memory| LITELLM
```

## Class relationships

`Fleet` is built at startup from `config.yaml` (`intent.intended_deployments`
turns `fleet.hosts` and `providers` into `IntendedDeployment` rows) and
holds the fleet state the runners and pages read. `LiteLLMBackend` takes
the same `Fleet`; it and its `DeploymentManager`
(`llmao/proxy/deployments.py`, which adds and deletes deployments and
confirms every write by re-reading `/model/info`) are the only things
that write deployments to LiteLLM. The lifecycle/skew logic in
`model_status.py` is a set of **pure functions**: it takes a
`DeploymentSnapshot` (frozen out of a deployment + its server by
`fleet.snapshot_for_status`) and returns a status object, with no shared
mutable state.

```mermaid
classDiagram
  class Fleet {
    +servers: list~VllmServer~
    +deployments: list~FleetDeployment~
    +models: models.yaml rows
    +provisioning: ProvisioningRecord
    +approvals: dict~host, Approval~
    +litellm: LiteLLMDeployments
    +orphans: list~Orphan~
    +vast_rentals, runpod_rentals: ProviderRentals
    +discovered: dict~name, Discovered~
    +ready: Event
    +run_lifecycle()
    +probe_all()
    +place_servers()
    +approve_models()
    +model_rollup()
  }
  class FleetDeployment {
    +model_name, name
    +self_hosted
    +vllm: VllmServer
    +provider, litellm_model
    +litellm: tuple~LiteLLMDeployment~
    +litellm_health: LiteLLMHealth
    +skew: list
    +api_base
    +in_litellm
  }
  class VllmServer {
    +model_name, name, served_name
    +host, address, listen_port, public_port
    +api_key
    +rental: Rental
    +unlisted, unlisted_passes
    +probe: VllmProbe
    +state
    +config_served_at, first_probed_at, first_reached_at
    +last_ok, fails
    +key_rejected, rejected_passes
    +kv_cache_tokens, observed_max_model_len
    +record_probe()
  }
  class IntendedDeployment {
    +key: DeploymentKey
    +model_name, name, origin
    +host, listen_port, recipe, api_key
    +provider, litellm_model, api_base
  }
  class Approval {
    +host
    +models
    +at
  }
  class Rental {
    +provider, id, name
    +host, ports, status
    +usd_per_hour
  }
  class VllmProbe {
    +url, at
    +reached, ok, error
    +served_status, max_model_len
    +key_rejected
  }
  class LiteLLMBackend {
    +deployments: DeploymentManager
    +memory: MemoryStore
    +teams, keys, metering, tiers
    +requests, skew, log
    +warm()
    +run_skew()
    +watch_outside_edits()
  }
  class DeploymentManager {
    +fleet
    +fetch_deployments()
    +add_deployment()
    +delete_deployment()
    +ensure_providers()
    +sync_selfhost()
    +check_config_skew()
    +check_health_skew()
  }
  class LiteLLMDeployments {
    +rows
    +read_at
    +under(key)
  }
  class DeploymentSnapshot
  class DeploymentStatus
  class ModelStatus

  Fleet "1" *-- "0..n" VllmServer : servers
  Fleet "1" *-- "0..n" FleetDeployment : deployments
  Fleet "1" *-- "0..n" Approval : approvals
  FleetDeployment "1" o-- "0..1" VllmServer : vllm
  VllmServer "1" o-- "0..1" Rental : placed on
  VllmServer "1" o-- "0..1" VllmProbe : last probe
  VllmServer ..> IntendedDeployment : from_intended()
  FleetDeployment ..> IntendedDeployment : from_intended()
  Fleet "1" -- "1" LiteLLMBackend : held by
  LiteLLMBackend "1" *-- "1" DeploymentManager : deployments
  DeploymentManager ..> LiteLLMDeployments : fetch_deployments()
  Fleet ..> LiteLLMDeployments : update_litellm_deployments()
  DeploymentSnapshot ..> FleetDeployment : snapshot_for_status()
  DeploymentSnapshot ..> VllmServer : snapshot_for_status()
  DeploymentSnapshot ..> DeploymentStatus : deployment_status()
  DeploymentStatus ..> ModelStatus : model_status()
```

The capacity side has its own small set, all in `llmao/capacity.py` and
`llmao/sampler.py`, sharing nothing with the fleet classes but the
`Fleet` the sampler reads its targets from:

```mermaid
classDiagram
  class MetricsSampler {
    +targets()
    +sample_once()
    +run()
  }
  class HourlyRollup {
    +record()
    +missed()
    +take_finished()
    +put_back()
  }
  class Hour {
    +hour, server, model_name
    +readings, busy, queueing
    +kv_sum, kv_peak, counters, buckets
    +usd_per_hour
    +row()
  }
  class CapacityStore {
    +write() / read()
    +write_demand() / read_demand()
    +demand_days_done()
    +mark_demand_day() / clear_demand_day()
    +prune()
  }
  class CapacityWriter {
    +write_finished()
    +run()
  }
  class CapacityPruner {
    +run()
  }
  class DemandRollup {
    +missing_days()
    +roll_up(day)
    +roll_up_missing()
    +run()
  }
  class UsageManager {
    +spend_log(day)
  }
  MetricsSampler --> HourlyRollup : record()
  HourlyRollup "1" *-- "0..n" Hour
  CapacityWriter --> HourlyRollup : take_finished()
  CapacityWriter --> CapacityStore : write()
  CapacityPruner --> CapacityStore : prune()
  DemandRollup --> UsageManager : spend_log(day)
  DemandRollup --> CapacityStore : write_demand(), mark_demand_day()
  CapacityStore --> MemoryStore : /v1/memory
```
