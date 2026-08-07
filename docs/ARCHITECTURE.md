# Architecture (implementation)

This document describes **how this repository is structured**. Product intent,
credential rules, ownership, and non-goals live in the **master design**:

- **Committers:** `apache/rai-private` → `services/llmao/README.md`

This app is the **asfquart / Tooling half** of the gateway: identity, ASF
project ↔ LiteLLM team mapping, authorization, and (next) PAT lifecycle UX.
**LiteLLM** is the OpenAI-compatible inference path, budgets, virtual keys, and
metering. Completions are **not** re-proxied through this process.

## The two halves

**asfquart** (front) handles *who you are and what you're allowed to do*. In
production the app is built with `asfquart.construct("llmao")`, which mounts
the ASF OAuth gateway at `/auth`, supports bearer tokens via a `token_handler`,
and populates a `ClientSession` from LDAP with the user's `uid`, committer
`projects`, and PMC `committees`. Per-PMC gating is the `@require(R.pmc_member)`
decorator. None of this is reimplemented here.

**LiteLLM proxy** holds *teams, users, virtual keys (PATs), budgets, capacity
limits, and spend*. Clients call its OpenAI-compatible API with a PAT. This
process talks to LiteLLM over the **admin** surface (master key) to provision
teams and (soon) mint, regenerate, or revoke virtual keys—see design §5–6
(user + team + purpose on normal keys).

## The seam

`seam.py` is the join code that matters today. It:

1. **authorizes** — the calling identity must be a member (or site admin) of
   the project it wants to act on; activity views require PMC admin (design:
   PMC as team admin);
2. **resolves** the ASF project to a LiteLLM team, provisioning the team with a
   budget on first use.

Keeping the ASF-project ↔ LiteLLM-team mapping correct as membership changes
is “the part that isn't free” (design §6.1). Today the mapping is created
lazily and persisted in `store.py`; production should reconcile against LDAP /
asfquart membership on a schedule.

## Why it runs with no infrastructure

So the seam is reviewable and demoable anywhere, both halves have a local
stand-in selected by environment variables:

- `LLMAO_AUTH_MODE=dev` → a stub login that produces the same `Identity` an
  asfquart session would.
- `LLMAO_LITELLM_MODE=mock` → an in-process backend that fakes teams and usage
  storage, so authz and budget GETs are exercised end to end.

Flipping both to `asf` / `proxy` swaps in the real systems without changing any
code above the backend interface. Production LiteLLM uses **Postgres** (design
§9); that is an Infra concern (`p6/modules/llmao`), not this package’s store.

## Request path (this process — control / PAT plane)

```
operator/browser → [asfquart front] → seam.authorize
                 → [LiteLLM admin API] team/budget (and soon PAT keys)
```

## Inference path (LiteLLM — not reimplemented here)

```
client tool → [LiteLLM] PAT (virtual key) → budget / capacity check → model
```

PATs bind **person + project (+ purpose)** for normal committer work (design
§5). Service-account keys may be team-only for automation.

See `../README.md` for the API surface currently exposed and quickstart.
