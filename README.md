# llmao

Tooling’s **implementation** of the ASF LLM gateway seam at `llm.apache.org`:
asfquart for Apache identity and (soon) PAT lifecycle; admin access to LiteLLM
for teams, budgets, and virtual keys.

### Product design (by reference)

The **authoritative product design** is not duplicated here. Committers with
access: **`apache/rai-private`** → `services/llmao/README.md` (goals, credential
model, teams/limits, ownership, non-goals, rejected alternatives).

| Tree | Role |
|------|------|
| `apache/rai-private` → `services/llmao/` | Master design (RAI) |
| **this repo** (`apache/tooling-llmao`) | Software Tooling builds |
| Infra `p6/modules/llmao` | Puppet / production deploy |

asfquart owns identity and per-PMC authorization. **LiteLLM** owns teams,
**virtual keys** (the PATs), budgets, metering, and the OpenAI-compatible API
that **clients** use. This process is the **seam** (project → team mapping,
authz, key management UX next)—not a second completion proxy.

Scripted workloads are primary (design §2 / §5). Point Cursor, CLIs, and SDKs
at the LiteLLM OpenAI endpoint with a project PAT (`sk-…`), not at a chat form
in this app.

```
ASF id ──oauth/JWT──►  asfquart / llmao     ──admin──►  litellm proxy
                       (who you are,                    (teams, budgets,
                        what PMCs, PATs)                 virtual keys)

client tools ────────────────────────────────PAT────►  litellm ──► models
```

---

## Quickstart (local, asfquart + TLS)

The app is **always asfquart** (Apache OAuth). Local login needs HTTPS and a
host name OAuth will accept — same pattern as Apache STeVe: **mkcert** certs
for `localhost.apache.org` (see `certs/README.md`).

Requires [uv](https://docs.astral.sh/uv/) on your `PATH`.

```bash
make install
cp config.yaml.example config.yaml     # gitignored; edit secrets
# generate certs under certs/ (mkcert) — certs/README.md
make run                               # uv run python main.py
```

Open `https://localhost.apache.org:8443/` (port from `config.yaml`), sign in
with ASF. Default `litellm.mode: mock` needs no LiteLLM process.

### Real LiteLLM (proxy mode)

PAT metadata (alias, user, team, spend) lives in **LiteLLM’s Postgres**, not
in llmao. The full `sk-…` is returned only at key create; list/info APIs expose
metadata for the gateway UI later.

```bash
cp litellm.yaml.example litellm.yaml   # gitignored
./bin/gen-litellm-master-key.sh        # print sk-…; paste into BOTH:
#   litellm.yaml  → general_settings.master_key
#   config.yaml   → litellm.master_key
# set database_url in litellm.yaml to a real Postgres URL
# config.yaml → litellm.mode: proxy
make proxy                             # litellm --config litellm.yaml
make run
```

Production secrets come from **hiera/eyaml** into both on-disk YAML files
(Puppet). Do not rely on environment variables for production secrets.

ASGI (TLS on the reverse proxy):

```bash
uv run python -m hypercorn main:llmao_app --bind 0.0.0.0:8080
```

```bash
make test          # offline seam/catalog tests (no OAuth session automation yet)
```

---

## What this includes today

| Capability | Where it lives | Notes |
|---|---|---|
| ASF login + PMC authz | asfquart always + `auth.py` | `@asfquart.auth.require`; project scope in seam |
| Per-PMC budgets & spend (read) | litellm teams / `MockBackend` | one litellm *team* per ASF project |
| Project ↔ team mapping | `seam.py` | provision team on first use |
| Model inventory | open design | LiteLLM config + future UX/advisor; not dual-owned |
| Per-project activity view | `api.py` | PMC admins / site admins only |
| HTML shell | `pages.py` + EZT + `static/` | Bootstrap; PAT UI next |
| Local TLS + configs | `main.py`, `config.yaml`, `litellm.yaml` | examples committed; secrets gitignored |

**Next:** mint/list/revoke LiteLLM virtual keys (metadata from `/key/list`);
budget updates via the gateway.

---

## API

Authenticated asfquart session (cookie after OAuth). Not the same thing as a
LiteLLM virtual key (PAT for inference).

```bash
# Per-project budget (members) and activity (PMC admins)
GET /v1/projects/<project>/budget
GET /v1/projects/<project>/usage

GET /healthz
```

Unauthenticated access to protected routes redirects to OAuth (browser) or
fails auth via asfquart. Project membership failures return JSON `403` where
the handler still runs.

---

## Production

1. **asfquart** (dependency via `pyproject.toml`) always provides OAuth at
   `/auth` and LDAP-backed sessions — see
   <https://github.com/apache/infrastructure-asfquart>.

2. **Secrets on disk:** Puppet/hiera/eyaml renders `config.yaml` and
   `litellm.yaml` with the **same** `master_key` (`sk-…`) and other secrets.
   No production env-var secret channel.

3. **LiteLLM** with Postgres (`database_url` in `litellm.yaml`) and
   `litellm --config litellm.yaml`. Model routes and provider keys live in
   that file (inventory design still open).

4. **Serve** llmao (`main.py` or Hypercorn). Point client tools at the
   **LiteLLM** base URL with PATs, not at llmao for chat.

The token handler in `auth.py` is a stub for calling llmao’s own API
non-interactively; inference PATs are LiteLLM virtual keys.

### Self-hosted models via vLLM

The self-host catalog entries are served by **vLLM** — one vLLM process per
model, each exposing an OpenAI-compatible endpoint — with the litellm proxy in
front for per-PMC budgets (budgets live in litellm; vLLM has none). Each model
runs on its own port; litellm routes to the right one by `model_name`
(Option A), so there is no model-swap latency.

The PoC self-host tier (sized for a single 48GB GPU, e.g. an L40S) is three
Apache-2.0 open-weight models:

| Catalog model | vLLM served name | HF weights | Role |
|---|---|---|---|
| Gemma 4 26B-A4B | `gemma4-26b` | `google/gemma-4-26b-a4b` | general / multimodal / agentic |
| Qwen 3.6-27B | `qwen3.6-27b` | `Qwen/Qwen3.6-27B` | coding |
| Qwen3-8B | `qwen3-8b` | `Qwen/Qwen3-8B` | fast / routine calls |

Each model is served by a vLLM container, for example:

```bash
docker run --rm --gpus all --ipc=host -p 8003:8003 \
  -v ~/.cache/huggingface:/root/.cache/huggingface -e HF_TOKEN=$HF_TOKEN \
  vllm/vllm-openai:v0.6.6 \
  --model Qwen/Qwen3-8B --served-model-name qwen3-8b --port 8003
```

litellm reaches each model through a per-model base-URL env var
(`LLMAO_SELFHOST_GEMMA_URL`, `LLMAO_SELFHOST_QWEN_CODER_URL`,
`LLMAO_SELFHOST_QWEN8B_URL`), each pointing at that model's vLLM port. The full
five-container stack (llmao + litellm + three vLLM servers) is defined in
`infra/docker/docker-compose.yml`.

**Model inventory** is an open design (UX / advisor / LiteLLM). For now, model
routes live in **`litellm.yaml`** (from `litellm.yaml.example`). Legacy
`llmao/catalog.py` + `scripts/render_litellm_config.py` are not the active path.

---

## Layout

```
main.py                  entry: create_app, run_standalone / run_asgi
pages.py                 HTML + /static
api.py                   JSON /healthz and /v1/*
templates/ static/       EZT + Bootstrap
bin/fetch-thirdparty.sh  vendor Bootstrap/icons
bin/gen-litellm-master-key.sh   print sk-… for both YAML files
config.yaml.example      → config.yaml (gitignored)
litellm.yaml.example     → litellm.yaml (gitignored)
certs/                   mkcert PEMs + README
llmao/                   seam, auth, litellm_client, store, config
tests/                   offline seam/catalog tests
```
