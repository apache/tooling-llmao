# llmao

A thin **litellm-proxy gateway fronted by asfquart**, served at `llm.apache.org`.
This is **Phase 1**: ASF identity, per-PMC budgets, a model catalog with
governance metadata, and manual model selection — text or file in, metered
response out.

<p align="center">
  <img src="docs/screenshots/portal.png" width="820"
       alt="llmao portal — a metered call billed to a PMC, with model governance metadata, budget, and per-project activity">
</p>

`asfquart` owns identity and per-PMC authorization. litellm owns the catalog,
budgets, metering, and the OpenAI-compatible API. The code in this repo is the
**seam** between them, plus a thin portal.

```
ASF id ──oauth/JWT──►  asfquart front  ──team key──►  litellm proxy ──►  models
                       (who you are,                  (what it cost,      (external +
                        what PMCs)                      per-team budget)    self-host)
```

---

## Quickstart (no external services)

Runs out of the box in **dev mode** (stub login) with a **mock LLM backend**,
so you can click through the whole flow on a laptop.

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
identity — e.g. uid `jdoe`, projects `airflow, lineage`, PMC `airflow`. Then:

- pick a model (note the license / openness / provenance shown inline),
- type a prompt or attach a text/code file,
- **Send** — the call is metered and billed to the selected project,
- watch the **Budget & activity** panel update (activity is visible to PMC
  members of the project).

Run the tests:

```bash
make test          # 9 tests: seam, budgets, authz, catalog, HTTP API
```

---

## What Phase 1 includes

| Capability | Where it lives | Notes |
|---|---|---|
| ASF login + PMC authz | `auth.py` + asfquart (prod) | dev-stub mirrors asfquart's `ClientSession` shape |
| Per-PMC budgets & spend | litellm teams (prod) / `MockBackend` (dev) | one litellm *team* per ASF project |
| Project ↔ team mapping | `seam.py` | the one real piece of Phase 1 code |
| Model catalog + governance metadata | `catalog.py` | license, openness, weights, provenance (explicit) |
| OpenAI-compatible chat API | `app.py` `/v1/chat/completions` | text or uploaded file |
| Per-project activity view | `app.py` `/v1/projects/<p>/usage` | PMC admins / site admins only |
| Thin portal | `portal.py` | single self-contained page, no build step |

**Deferred to later phases:** input scanning (Phase 2), automatic routing
(Phase 3), benchmarking (Phase 4). Models are chosen by hand in Phase 1.

---

## API

All endpoints require an authenticated session (cookie) or, in asf mode, a
bearer PAT. The chat endpoint is OpenAI-shaped, so existing clients work by
changing the base URL.

```bash
# List approved models (with governance metadata under `.llmao`)
GET /v1/models

# Chat. The billed project comes from the X-LLMAO-Project header,
# the body's "project", or (if you're on exactly one) your sole project.
POST /v1/chat/completions
  { "model": "selfhost/gemma4-26b", "messages": [{"role":"user","content":"hi"}] }

# Per-project budget (members) and activity (PMC admins)
GET /v1/projects/<project>/budget
GET /v1/projects/<project>/usage
```

Errors use standard codes: `401` unauthenticated, `403` not a member / not a
PMC admin, `404` unknown model, `429` project budget exceeded, `504` the model
timed out or the proxy was unreachable. Error bodies are always JSON.

---

## Production

Two environment flips move from the laptop demo to the real thing:

```bash
export LLMAO_AUTH_MODE=asf          # oauth.apache.org + LDAP via asfquart
export LLMAO_LITELLM_MODE=proxy     # talk to a real litellm proxy
```

1. **Install asfquart** (provides the OAuth gateway at `/auth`, JWT/PAT, and
   LDAP-backed sessions): see
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
   the seam uses it to provision teams and mint per-team keys.

3. **Serve** behind hypercorn and point DNS/TLS for `llm.apache.org` at it.

The PAT handler in `auth.py` (`make_token_handler`) is a stub: wire it to your
token store to let non-interactive CLI/SDK callers authenticate.

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

Slow models: the gateway waits `LLMAO_REQUEST_TIMEOUT_S` (default 600s) for a
response; on timeout the portal returns a clean `504` rather than hanging.

**Adding or changing a model touches two files that must agree:**
`llmao/catalog.py` (what the portal lists, plus governance metadata) and
`litellm/config.yaml` (the generated route). Each catalog `backend` string must
equal a config `model_name`. For a self-host model, set its `served_name` (the
vLLM `--served-model-name`) and `api_base_env` (the env var holding that
model's vLLM URL) in the catalog, then `make config` to regenerate.

---

## The catalog is the source of truth

`llmao/catalog.py` defines the models and their governance metadata. The
litellm proxy config is generated from it (`scripts/render_litellm_config.py`),
so the portal's list and the proxy's routes never drift. Add a model by adding
a `CatalogModel`, then `make config`.

---

## Layout

```
llmao/
  app.py            app factory + routes (dev: plain Quart; prod: asfquart)
  seam.py           ASF project -> litellm team; authz; metered chat
  auth.py           identity resolution (asfquart session/PAT or dev stub)
  litellm_client.py ProxyBackend (real) + MockBackend (dev), one interface
  catalog.py        models + license/openness/weights/provenance
  portal.py         the single-page portal
  store.py          tiny JSON state store (swap for a DB later)
  config.py         env-driven settings
litellm/config.yaml litellm proxy config (generated)
scripts/            config renderer
tests/              Phase 1 test suite
```