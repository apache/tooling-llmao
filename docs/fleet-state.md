# Design: Fleet State — Ownership, Lifecycle, and Recovery

Companion to [`vllm-fleet-design.md`](vllm-fleet-design.md), which defines the
control-plane contract: what a host is, how a box fetches its assignment, and
what the JSON looks like. This document covers **where fleet state lives, how
it changes, and what happens when it is lost**.

---

## 1. LiteLLM is the source of truth

An earlier draft of this document proposed a runtime-owned YAML file for
membership, on the grounds that a GPU box rebooting during a database outage
could still fetch its assignment.

**That argument does not hold.** During a Postgres outage LiteLLM cannot
authenticate any request — virtual keys live there — and with
`STORE_MODEL_IN_DB` it cannot route either. The gateway is down regardless, so
a box that fetches its config during that window comes up serving a model
nothing can reach. The resilience buys nothing.

State lives in LiteLLM's database. There is no second store.

### 1.1 What a route already carries

| field | fleet meaning |
|---|---|
| `model_name` | the catalog model, and the routing key |
| `litellm_params.api_base` | **which host, which port** |
| `litellm_params.api_key` | the bearer token for that vLLM |
| `model_info` | arbitrary dict — carries the recipe and provenance |

`GET /vllm/config` becomes: select routes whose `api_base` host matches the
caller's IP, return their `model_info.vllm` blocks and ports.

### 1.2 Enabling it

`STORE_MODEL_IN_DB` is an **environment variable**, not a config key:

```
STORE_MODEL_IN_DB=True
```

Without it, `/model/new` returns HTTP 500 with
`Set 'STORE_MODEL_IN_DB='True'' in your env to enable this feature`. The YAML
`model_list` then becomes a bootstrap seed rather than the source of truth.

---

## 2. Registration is health-gated

A provisioned instance can take fifteen minutes to load weights. A route whose
backend is not yet serving will fail every request routed to it.

**Rule: a route exists in LiteLLM if and only if its vLLM is serving.**

```
add host    -> record assignment, generate api_key, no route yet
box boots   -> GET /vllm/config
health OK   -> POST /model/new
health DOWN -> POST /model/delete
retire      -> delete route, drop assignment
```

Uniform, with no special cases. An earlier draft proposed registering at
add-time when the `model_name` already had healthy peers and deferring
otherwise — that makes the same operation behave differently depending on the
state of unrelated servers, which is fine when written and baffling later.

### 2.1 Why not register early and let cooldown absorb it

Verified against litellm 1.99.0 source. Two findings:

**There is no per-deployment enable/disable.** No `enabled` or `is_disabled`
field, and nothing in the model-management endpoints. A route is registered or
deleted; there is no third state.

**Cooldown does not protect a booting backend.** `DEFAULT_COOLDOWN_TIME_SECONDS`
is **5**, so a cooled deployment re-enters rotation almost immediately. And
`router_utils/cooldown_handlers.py` deliberately exempts single-deployment
model groups — `SINGLE_DEPLOYMENT_TRAFFIC_FAILURE_THRESHOLD` is **1000**, with
the comment *"by default we should avoid cooldowns on single deployment model
groups."*

So the first server for a new model would fail every request for the full boot
window, uncooled. Health-gating is the only mechanism available.

`cooldown_time` and `allowed_fails` are settable per-deployment via
`model_info` if different behaviour is wanted later.

### 2.2 The pending assignment

Health-gating means an assignment must exist before its route does. That state
is small — IP, port, model name, generated key, per pending host — but it is
state.

Options, preferred first:

- **A table in the same Postgres.** Not LiteLLM's schema, but the same
  database, so no new backup story and no new failure mode. "State-free" meant
  no *second* datastore; this respects that.
- **In memory, accepting loss on restart.** A box that already fetched keeps
  running; one that has not gets 404 and retries. Zero persistence, but a
  restart mid-provisioning strands a box being paid for.
- **Register immediately and tolerate the failures.** Simplest, and a bad
  default given §2.1.

---

## 3. Three config lifetimes

Config here has three distinct change costs, and conflating them causes most of
the confusion:

| lifetime | what | to change it |
|---|---|---|
| **baked** | `ASFQUART_URL`, `FLEET_KEY` in the instance template | re-provision |
| **boot** | the vLLM assignment — model, port, launch args | box must re-fetch and restart vLLM |
| **live** | routes, keys, budgets in LiteLLM | immediate |

Changing `max_model_len` does nothing until that box restarts vLLM. The catalog
says one thing and the server does another, silently.

**This is a second kind of skew.** `check_config_skew` compares llmao against
LiteLLM. Nothing compares llmao's *intended* launch args against what a box is
actually running.

### 3.1 Config revision

`/vllm/config` responses carry a revision — a hash of the assignment payload.
The box records what it applied and reports it back, so the UX can show
`applied rev 3, current rev 5` rather than letting a stale server pretend to be
current.

If boxes re-fetch periodically rather than only at boot, a changed revision
also becomes the trigger for a restart, making config changes eventually
consistent.

Whether that restart should be automatic is open: an unattended restart drops
in-flight requests. Probably detect automatically, apply on a button.

---

## 4. Modelling variants

### 4.1 Contract versus recipe

**`model_name` is a contract with callers.** Everything behind it must be
interchangeable from their point of view.

**`model_info.vllm` is a recipe for the box.** Recipes may differ freely as
long as the contract holds.

Same `model_name`, different recipes — fine:

- FP8 on one box, BF16 on another
- weights from HF on one, an internal mirror on another
- different cards, `gpu_memory_utilization`, `--kv-cache-memory`

Different `model_name` required:

- different served context window — a 32k route cannot take a 100k prompt
- different reasoning parser, or thinking on by default versus off
- anything that changes the shape of a response

**If two routes share a `model_name`, their caller-visible parameters must
match.** Otherwise identical requests behave differently depending on which
backend they land on. Where a pool is uneven, advertise the **minimum** — a
pool with a 40k and a 128k server advertises 40k, or it is not a pool.

### 4.2 Recipes carry provenance

Weights come from HF, from internal mirrors, from image registries, and in
several quantizations. The recipe should say so explicitly rather than
overloading one string:

```yaml
model_info:
  vllm:
    model: Qwen/Qwen3-8B-FP8      # what vLLM is told to load
    source:
      kind: hf                    # hf | url | registry | local
    vram_gb: 8                    # checkable before pulling 50GB
    disk_gb: 18
    args: ["--reasoning-parser", "qwen3"]
```

Three reasons to separate `source` from `model`: the box can check fit before
downloading; credentials differ by source kind; and a box that already holds
the weights should not re-download because the catalog names an HF repo.

### 4.3 Host capacity is discovered, not declared

VRAM and disk are discoverable on the box. A hand-typed `vram_gb: 80` is wrong
the first time a provider supplies a different card than was ordered — which,
with rented instances, is a matter of when.

The model's requirement goes in the recipe; the box decides fit against
hardware it can see. `--kv-cache-memory` makes this exact: vLLM prints the byte
count it wants, so the install step can start conservative, read the figure,
and relaunch.

---

## 5. Lifecycle

### 5.1 Retirement

Deleting a route deletes the record, so "did this host ever exist" loses its
answer. Either mark `model_info.retired` and remove the route from routing, or
accept LiteLLM's own audit trail as the record.

Worth deciding rather than losing by default.

### 5.2 Staleness

`last_config_fetch` is the liveness signal — surface it as a coloured age,
green under an hour, red over a week.

**Never auto-retire.** A box down for maintenance is indistinguishable from one
that is dead, and silently dropping it during an outage is the wrong default.

Retiring promptly is also hygiene: providers recycle IPs, so a stale entry may
eventually match an instance rented by someone else. `GET /vllm/config`
requires the fleet key with a constant-time compare, so the blast radius is
bounded — but that key is a single shared secret baked into the box template,
which makes "nobody else could hold it" an assumption.

### 5.3 At a dozen hosts

- **Group by provider in the UI.** The useful question is "how many rented
  boxes are still alive", not an alphabetical list of IPs.
- **`model_name` pools need a minimum-advertising rule** (§4.1), which cannot
  be a static catalog value once a pool is uneven.
- **A `notes` field is worth considering.** "Rented for the superset scan, kill
  after" is obvious for a week and unrecoverable after a month.

---

## 6. Loss and recovery

| event | LiteLLM DB | in-memory health |
|---|---|---|
| Puppet run | untouched | untouched |
| app restart | untouched | rebuilt in ~45s from probes |
| Postgres outage | unavailable; auth **and routing** fail | untouched |
| host rebuild | **lost** | lost |

Health state is correctly ephemeral — a live measurement, not a record.

With routes in Postgres, a lost database means the proxy no longer knows what
to proxy, not merely who may call it. Virtual keys are stored hashed and shown
once at mint, so recovery also means re-minting every key and reconfiguring
every consumer.

**Partial mitigation for free:** the GPU boxes keep running. They hold their
assignment locally and continue serving; they simply cannot re-fetch.

### 6.1 Backup

```bash
sudo -u postgres pg_dump litellm | gzip > "$dest/litellm-$stamp.sql.gz"
```

One artifact now, rather than a database dump plus a state file.

**`$dest` is unresolved and belongs to the p6 work.** Three constraints:

- it must be captured **off-host**. `bpc_client_asf` pulls via rsync from a
  central BackupPC server, so the client has no local daemon and coverage
  cannot be confirmed from the box. The share list lives on the server — ask
  Infra which paths are pulled. A default share of `/etc` and `/home` would
  miss `/var/backups`.
- confirm the host is not in `bpc_client_asf::excludelist`.
- a file-level copy of a live Postgres data directory is **not** a valid
  backup. The `pg_dump` is what makes it restorable.

---

## 7. Implementation

1. `STORE_MODEL_IN_DB=True` in the service environment
2. Derive `GET /vllm/config` from routes matching the caller's IP
3. Push `/model/new` on the healthy transition, `/model/delete` on down
4. Somewhere for pending assignments (§2.2)
5. Config revision on `/vllm/config`, reported back by `install_sets.py`
6. UI: add, retire, edit

---

## 8. Open

- Pending-assignment storage (§2.2).
- Retirement record after route deletion (§5.1).
- Automatic restart on revision change, or detect-and-prompt (§3.1).
- Advertising the minimum across an uneven pool (§4.1).
- Backup destination (§6.1).
- **Does `model_info` survive a `/model/new` round-trip intact?** If LiteLLM
  normalises or drops unknown nested keys, recipes cannot live there. Testable
  against a local proxy with `STORE_MODEL_IN_DB=True`.

**Resolved:** LiteLLM is the source of truth; no second datastore; registration
is health-gated; there is no per-deployment enable/disable and cooldown does
not cover the boot window; host capacity is discovered, not declared;
`model_name` is a caller contract and `model_info.vllm` a box recipe.

---

*End of design document.*