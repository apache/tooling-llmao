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
httpx**) to provision teams and (soon) mint or revoke virtual keys—see design
§5–6. Project names are LDAP/session names (asfquart); no rename map.

**Model inventory** is `model_list.yaml` only (LiteLLM `include`; no
`STORE_MODEL_IN_DB`). llmao loads the same file for UX (`llmao/models.py`).
Governance fields live flat under each entry’s `model_info`. API keys in that
file are secrets (eyaml); `api_base` is cleartext. Restart LiteLLM after
inventory changes (Puppet/systemd later).

**LiteLLM virtual keys / teams** need Postgres + Prisma (`litellm[proxy,extra-proxy]`).
Developers: system PostgreSQL + `make db`. Production: Puppet + on-disk
`database_url`.

**PATs:** personal keys bind ASF uid + project team + purpose; automation
keys are team-scoped exceptions (who may create them is an **open RAI
policy** question — see design + `docs/STATUS.md`). Secrets shown once;
metadata in LiteLLM.

**GPU fleet (vLLM on Vast, later RunPod):** design in
`docs/vllm-fleet-design.md`. Box-side fetch/launcher/runbooks live in
`hosting/` (stock provider vLLM image + on-start; no derived image in v1).
The asfquart config endpoint is not in that tree.

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
       → LiteLLM admin API (team/budget; soon PATs)
```

HTML mutations are `POST /do-*` only, then **303** to a GET display (flash for status; created-key secret is a `raw` HTML flash). JSON API is separate.

## Inference path (LiteLLM)

```
client tool → LiteLLM + PAT → budget / capacity → model
```

See `../README.md` for quickstart and API surface.
