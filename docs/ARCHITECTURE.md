# Architecture (implementation)

This document describes **how this repository is structured**. Product intent,
credential rules, ownership, and non-goals live in the **master design**:

- **Committers:** `apache/rai-private` → `services/llmao/README.md`

This app is the **asfquart / Tooling half** of the gateway: identity, ASF
project ↔ LiteLLM team mapping, authorization, and (next) PAT lifecycle UX.
**LiteLLM** is the OpenAI-compatible inference path, budgets, virtual keys, and
metering. Completions are **not** re-proxied through this process.

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
Loaded as **`APP.cfg`** (EasyDict); use dotted access (`APP.cfg.litellm.mode`).

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
Developers: system PostgreSQL + `make db` (`bin/setup_litellm_db.py`). Production:
Puppet creates the DB and deploys on-disk `database_url`.

`litellm.mode: mock` in config is only an offline stand-in for team/usage
storage in tests and laptop work without a proxy—not a second auth system.

## The seam

`seam.py`:

1. **authorizes** project membership / PMC admin after asfquart has authenticated;
2. **resolves** the ASF project to a LiteLLM team (budget on first use).

## Request path (this process)

```
browser → HTTPS (local mkcert or prod proxy)
       → asfquart OAuth / session
       → @require + seam.authorize
       → LiteLLM admin API (team/budget; soon PATs)
```

## Inference path (LiteLLM)

```
client tool → LiteLLM + PAT → budget / capacity → model
```

See `../README.md` for quickstart and API surface.
