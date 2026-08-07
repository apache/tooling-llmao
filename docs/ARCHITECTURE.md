# Architecture

llmao is a **control plane** for ASF access to a LiteLLM proxy. It does not
implement a chat client, a completion proxy, a budget engine, or a full
identity system — it composes asfquart and LiteLLM admin APIs and writes only
the join.

## The two halves

**asfquart** (front) handles *who you are and what you're allowed to do*. In
production the app is built with `asfquart.construct("llmao")`, which mounts
the ASF OAuth gateway at `/auth`, supports bearer tokens via a `token_handler`,
and populates a `ClientSession` from LDAP with the user's `uid`, committer
`projects`, and PMC `committees`. Per-PMC gating is the `@require(R.pmc_member)`
decorator. None of this is reimplemented here.

**litellm proxy** (backend) holds *teams, virtual keys, budgets, and spend*.
It exposes an OpenAI-compatible API for **clients** (CLI tools, IDEs, SDKs).
llmao talks to LiteLLM only over the **admin** surface (master key) to
provision teams and (soon) mint or revoke virtual keys. Completions never pass
through this process.

## The seam

`seam.py` is the code that matters. It:

1. **authorizes** — the calling identity must be a member (or site admin) of
   the project it wants to act on; activity views require PMC admin;
2. **resolves** the ASF project to a litellm team, provisioning the team with a
   budget on first use.

Keeping the ASF-project ↔ litellm-team mapping correct as PMC membership
changes is the substance flagged in the plan as "the part that isn't free."
Today the mapping is created lazily and persisted in `store.py`; a production
deployment should reconcile it against LDAP on a schedule.

## Why it runs with no infrastructure

So the control plane is reviewable and demoable anywhere, both halves have a
local stand-in selected by environment variables:

- `LLMAO_AUTH_MODE=dev` → a stub login that produces the same `Identity` an
  asfquart session would.
- `LLMAO_LITELLM_MODE=mock` → an in-process backend that fakes teams and usage
  storage, so authz and budget GETs are exercised end to end.

Flipping both to `asf` / `proxy` swaps in the real systems without changing any
code above the backend interface.

## Request path (control plane)

```
operator/browser → [asfquart front] → seam.authorize
                 → [litellm admin API] team/budget (and soon keys)
```

## Inference path (not llmao)

```
client tool → [litellm proxy] virtual key → budget check → model
```

See `../README.md` for the API and quickstart.
