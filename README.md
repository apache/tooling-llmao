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

## Quickstart (no external services)

Runs out of the box in **dev mode** (stub login) with a **mock** LiteLLM
backend, so you can exercise auth and budget GETs on a laptop.

```bash
make install        # uv sync — creates .venv and installs deps
make run            # serves http://127.0.0.1:8080
```

Requires [uv](https://docs.astral.sh/uv/) on your `PATH`. `make` uses it to
build an isolated `.venv` so it works on Debian/Ubuntu's "externally managed"
Python (PEP 668) without touching your system packages. If you'd rather drive
uv yourself:

```bash
uv sync
uv run python -m llmao.app
```

Open <http://127.0.0.1:8080>, click **Sign in (dev)**, and stand in as an
identity — e.g. uid `jdoe`, projects `airflow, lineage`, PMC `airflow`. Use the
JSON API for budget and (as PMC) usage.

Run the tests:

```bash
make test
```

---

## What this includes today

| Capability | Where it lives | Notes |
|---|---|---|
| ASF login + PMC authz | `auth.py` + asfquart (prod) | dev-stub mirrors asfquart's `ClientSession` shape |
| Per-PMC budgets & spend (read) | litellm teams (prod) / `MockBackend` (dev) | one litellm *team* per ASF project |
| Project ↔ team mapping | `seam.py` | provision team on first use |
| Model catalog + governance metadata | `catalog.py` | still drives `make config` for the proxy |
| Per-project activity view | `app.py` `/v1/projects/<p>/usage` | PMC admins / site admins only |
| Minimal status page | `portal.py` | no chat UI |

**Next:** mint/list/revoke LiteLLM virtual keys for teams, users, and needs;
budget updates via the control plane.

---

## API

Authenticated session (cookie) or, in asf mode, a bearer token for *this*
service (not the same thing as a LiteLLM virtual key).

```bash
# Per-project budget (members) and activity (PMC admins)
GET /v1/projects/<project>/budget
GET /v1/projects/<project>/usage

GET /healthz
```

Errors use standard codes: `401` unauthenticated, `403` not a member / not a
PMC admin. Error bodies on `/v1/*` are JSON.

---

## Production

Two environment flips move from the laptop demo to the real thing:

```bash
export LLMAO_AUTH_MODE=asf          # oauth.apache.org + LDAP via asfquart
export LLMAO_LITELLM_MODE=proxy     # talk to a real litellm proxy
```

1. **asfquart** (dependency via `pyproject.toml`) provides the OAuth gateway at
   `/auth`, JWT support, and LDAP-backed sessions — see
   <https://github.com/apache/infrastructure-asfquart>. In asf mode the app is
   built with `asfquart.construct("llmao")`, so login and PMC membership come
   from real ASF identity.

2. **Run the litellm proxy** with the generated config:

   ```bash
   make config        # regenerate litellm/config.yaml from the catalog
   make proxy         # litellm --config litellm/config.yaml
   ```

   Set the self-host endpoints (one per vLLM model, e.g.
   `LLMAO_SELFHOST_GEMMA_URL`, `LLMAO_SELFHOST_QWEN_CODER_URL`,
   `LLMAO_SELFHOST_QWEN8B_URL`) and any external provider keys
   (`ANTHROPIC_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK`, etc.). Set
   `LLMAO_LITELLM_MASTER_KEY` to the same value as the proxy's `master_key`;
   the seam uses it to provision teams (and will mint virtual keys for
   operators).

3. **Serve** llmao behind hypercorn for the control plane; point client tools
   at the **LiteLLM** base URL with virtual keys, not at llmao for chat.

The token handler in `auth.py` (`make_token_handler`) is a stub for calling
llmao's own API non-interactively; LiteLLM virtual keys are separate and will
be managed by this control plane.

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

**Adding or changing a model touches two files that must agree:**
`llmao/catalog.py` (governance metadata + route inputs) and
`litellm/config.yaml` (the generated route). Each catalog `backend` string must
equal a config `model_name`. For a self-host model, set its `served_name` (the
vLLM `--served-model-name`) and `api_base_env` (the env var holding that
model's vLLM URL) in the catalog, then `make config` to regenerate.

---

## The catalog is the source of truth (for proxy model routes)

`llmao/catalog.py` defines the models and their governance metadata. The
litellm proxy config is generated from it (`scripts/render_litellm_config.py`),
so catalog and proxy routes stay aligned. Add a model by adding a
`CatalogModel`, then `make config`.

---

## Layout

```
llmao/
  app.py            app factory + routes (dev: plain Quart; prod: asfquart)
  seam.py           ASF project -> litellm team; authz
  auth.py           identity resolution (asfquart session/token or dev stub)
  litellm_client.py ProxyBackend (real) + MockBackend (dev), one interface
  catalog.py        models + license/openness/weights/provenance
  portal.py         minimal status + dev login HTML
  store.py          tiny JSON state store (swap for a DB later)
  config.py         env-driven settings
litellm/config.yaml litellm proxy config (generated)
scripts/            config renderer
tests/              control-plane test suite
```
