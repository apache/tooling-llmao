# Design: Fleet State — Ownership, Lifecycle, and Recovery

**Where live fleet state lives, how it changes, and what happens when it is
lost.** Not the box JSON contract — that is
[`vllm-fleet-design.md`](vllm-fleet-design.md) (host = IP, fleet key,
`GET /vllm/config` shape, listen vs public). Box boot:
[`../hosting/README.md`](../hosting/README.md). Do not merge the two fleet
docs.

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

### 1.1 What a deployment already carries

| field | fleet meaning |
|---|---|
| `model_name` | the `models.yaml` id (LiteLLM model group) |
| `litellm_params.api_base` | **which host, which port** |
| `litellm_params.api_key` | the bearer token for that vLLM |
| `model_info` | arbitrary dict — carries the recipe and provenance |

`GET /vllm/config` is still built from `fleet.hosts` + `models.yaml` (claimed
IP / `X-LLMAO-Host`), not a SELECT on LiteLLM deployments. Traffic SoT is
the deployment row; assignment SoT for the box is still YAML.

### 1.2 Enabling it

Set `general_settings.store_model_in_db: true` in `litellm.yaml`. Puppet may
also set `STORE_MODEL_IN_DB=True` in the process env. Without the flag,
`/model/new` returns HTTP 500. `models.yaml` is the recipe, not the live
deployment table.

---

## 2. Registration is health-gated

A provisioned instance can take fifteen minutes to load weights. A deployment whose
backend is not yet serving will fail every request routed to it.

**Rule: a deployment exists in LiteLLM if and only if its vLLM is serving.**
The Router is the whole proxy; a deployment is one backend (`/model/new` row).

```
add host    -> fleet.hosts row; api_key is HMAC (below), no deployment yet
box boots   -> GET /vllm/config
health OK   -> POST /model/new (same api_key)
health DOWN -> POST /model/delete
retire      -> delete deployment, drop fleet.hosts row
```

Every host follows that sequence. Adding a host does not create a LiteLLM
deployment. `/model/new` runs only when that box's vLLM is serving;
`/model/delete` when it is not.

Do not special-case "this model already has a healthy peer, so register the
new box now." That makes the same operation depend on other servers.

### 2.3 Per-vLLM `api_key` (HMAC, not a table)

The box needs `--api-key` **before** `vllm serve`. LiteLLM needs the **same**
bearer at `/model/new` **after** SERVING. `/health` is unauthenticated;
`GET /v1/models` on the box (KV scrape) is not. LiteLLM encrypts
`litellm_params` on readback, so the portal cannot recover plaintext from
`/model/info`.

We do **not** store the key in Postgres. `fleet.vllm_api_salt` is not
rotated in place (a change means a new bearer on every box, a vLLM restart,
and a new `/model/new`). `fleet.key` is not an input. Derive:

```
HMAC-SHA256(fleet.vllm_api_salt, "llmao.vllm.api_key\n" + host + "\n" + listen_port)
→ sk-vllm- + urlsafe-base64 (no padding)
```

`host` is the `fleet.hosts` IP (normalized). `listen_port` is the container
port, not Vast's public HostPort. Prefix `sk-vllm-` marks a vLLM server
bearer, distinct from the fleet key, LiteLLM keys, and PATs. HMAC-SHA256 as
a PRF, one domain label (other secrets from this salt must use a different
label). Not HKDF (one output). Not SHA256(secret||msg). Not random+table.

The salt lives only in llmao `config.yaml` (Puppet eyaml). It is not in the
Vast template. A missing attribute raises when the fleet is built. There is
no fallback to `selfhost_api_key` or a `models.yaml` `api_key`.

`GET /vllm/config` returns this bearer in `servers[].api_key`. Instance env
injection was tried and does not work on Vast; the JSON is how the box
learns `--api-key`. `install_set.py` writes it into Supervisor as
`VLLM_API_KEY`. Hand-launched boxes use the same function:
`bin/llmao-vllm-api-key --host <ip> --port <listen>` (reads
`fleet.vllm_api_salt`, or `--salt`).

**Who can mint.** The salt never leaves llmao, so a third party cannot
compute a key. That includes a box that copied `FLEET_KEY`. Host and listen
port are still required inputs.

**Leaked host/port map.** A list of fleet IPs and listen ports can leak
(config, logs, the public Vast mapping). Accepted for today: the map alone
does not mint bearers, and we are not hiding membership beyond the fleet
key on `GET /vllm/config`. Revisit if a leaked map should be insufficient
even for someone who also holds `FLEET_KEY` — that caller can still claim
`X-LLMAO-Host` for each listed IP and receive the derived key. Those fetches
are logged (claimed host, TCP peer, `X-Forwarded-For`). Per-instance
bootstrap tokens stay out of scope until that revisit.

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

Health-gating means an assignment exists (`fleet.hosts` YAML) before a
LiteLLM deployment does. IP, port, and model live in that YAML. The vLLM
bearer is HMAC of `fleet.vllm_api_salt` (§2.3), not a generated value to persist.

A second store for the key is not required. Registering immediately to
avoid pending state is still a bad default (§2.1).

---

## 3. Three config lifetimes

Config here has three distinct change costs, and conflating them causes most of
the confusion:

| lifetime | what | to change it |
|---|---|---|
| **baked** | `ASFQUART_URL`, `FLEET_KEY` in the instance template | re-provision |
| **boot** | the vLLM assignment — model, port, launch args | box must re-fetch and restart vLLM |
| **live** | deployments, keys, budgets in LiteLLM | immediate |

Changing `max_model_len` does nothing until that box restarts vLLM. The
definitions file says one thing and the server does another, silently.

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

- different served context window — a 32k deployment cannot take a 100k prompt
- different reasoning parser, or thinking on by default versus off
- anything that changes the shape of a response

**If two deployments share a `model_name`, their caller-visible parameters must
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
the weights should not re-download because `models.yaml` names an HF repo.

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
  be a static `models.yaml` value once a pool is uneven.
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

With deployments in Postgres, a lost database means the proxy no longer knows what
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

1. `store_model_in_db` in YAML (and/or env) — **done** in examples; Puppet env too
2. Derive `GET /vllm/config` from LiteLLM deployments matching the caller IP —
   **not done**; still `fleet.hosts`
3. `/model/new` on serving, `/model/delete` on down — **done** (`asf_api_base`
   on `model_info` for delete). Public port in `api_base`; box JSON listen.
   Commercial `/model/new` at llmao startup.
4. Somewhere for pending assignments (§2.2) — still `fleet.hosts` YAML
5. Config revision on `/vllm/config`, reported back by `install_set.py`
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
