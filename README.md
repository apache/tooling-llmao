# llmao

Tooling’s **implementation** of the ASF LLM gateway at `llm.apache.org`.

**What this app is for:** Apache-facing control plane for **shared, attributed,
limited** access to Foundation-sanctioned inference. You sign in with ASF,
manage **PATs** (and later project envelopes), and browse the model catalog.
**Inference** goes to the **LiteLLM proxy** with a PAT — not through a chat UI
here.

| Doc | Role |
|-----|------|
| **`apache/rai-private` → `services/llmao/README.md`** | Product design (concepts, policy) |
| **[`docs/STATUS.md`](docs/STATUS.md)** | Build status + **planned UX** backlog |
| **This README** | How to run and use the software |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Repo structure |
| [`docs/vllm-fleet-design.md`](docs/vllm-fleet-design.md) | GPU fleet: control-plane contract, box provisioning |
| [`docs/fleet-state.md`](docs/fleet-state.md) | Fleet membership: ownership, lifecycle, recovery |
| Infra `p6/modules/llmao` | Production deploy |

```
ASF id ──oauth──►  llmao (identity, PAT UX, project governance UI)
                         │ admin
                         ▼
client tools ──PAT──►  LiteLLM proxy ──► models
```

---

## Quickstart (local, asfquart + TLS)

The app is **always asfquart** (Apache OAuth). Local login needs HTTPS and a
host name OAuth will accept — same pattern as Apache STeVe: **mkcert** certs
for `localhost.apache.org` (see `certs/README.md`).

Requires [uv](https://docs.astral.sh/uv/) on your `PATH`.

Required on-disk YAML (copy from `*.example`; app and `make proxy` **fail-fast**
if missing — same presumption as STeVe-style config):

| File | From | Role |
|------|------|------|
| `config.yaml` | `config.yaml.example` | llmao / asfquart |
| `litellm.yaml` | `litellm.yaml.example` | LiteLLM proxy (`store_model_in_db`; do **not** include the catalog) |
| `model_list.yaml` | `model_list.yaml.example` | Catalog (UX + vLLM recipe; not the live route table) |

```bash
make install
cp config.yaml.example config.yaml
cp litellm.yaml.example litellm.yaml
cp model_list.yaml.example model_list.yaml
# generate certs under certs/ (mkcert) — certs/README.md
make run                               # uv run python main.py
```

Local run is **production-shaped**: LiteLLM + system Postgres (not an in-app
mock mode). Needs **system PostgreSQL** and **prisma** from
`litellm[proxy,extra-proxy]`.

```bash
# Postgres running (e.g. apt install postgresql; service started)
make db                                # bin/setup_litellm_db.py
# paste printed database_url into litellm.yaml general_settings
./bin/gen-litellm-master-key.sh        # print sk-…; paste into BOTH:
#   litellm.yaml  → general_settings.master_key
#   config.yaml   → litellm.master_key
make proxy                             # litellm --config litellm.yaml
make run
```

Open `https://localhost.apache.org:8443/` (port from `config.yaml`), sign in
with ASF.

PAT metadata lives in LiteLLM’s Postgres. The **catalog** is `model_list.yaml`
(llmao only; not included by LiteLLM). Routes live in the DB
(`store_model_in_db`). Commercial entries need a static `api_base`;
self-host `api_base` is per instance.

After Puppet/VCS updates model list or litellm config, **restart LiteLLM**
(systemd notify in p6 later). Production secrets are on-disk YAML, not env vars.

ASGI (TLS on the reverse proxy):

```bash
uv run python -m hypercorn main:llmao_app --bind 0.0.0.0:8080
```

```bash
make test          # offline seam + model_list tests (no OAuth session automation yet)
```

---

## Using the gateway (after sign-in)

1. **My Keys** — create a personal PAT for a project you belong to (purpose optional).
   Copy the secret **once**.
2. Point your client at the LiteLLM OpenAI-compatible base URL with that `sk-…` key.
   Use a **model id** from **Models** as the `model` parameter.
3. **Other Keys** (PMC / site admin) — automation keys; who minted them is recorded as `created_by`.
4. **Models** — sanctioned inventory (supply-path details for site admins only).

**Projects** (envelopes, member caps, by-person usage) and **Reports** are product intent — see design §6 and the UX backlog in [`docs/STATUS.md`](docs/STATUS.md).

### Things worth knowing before you call a model

**Reasoning defaults differ by model, and the failure is silent.** Some models
reason unless told not to; others do the reverse. A request with a small
`max_tokens` to a reasoning model can spend the whole budget thinking and
return **nothing** — HTTP 200, empty `content`, `finish_reason: length`, no
error. To turn it off:

```json
"chat_template_kwargs": {"enable_thinking": false}
```

**Prompt and output share the context window.** vLLM rejects a request where
prompt tokens + `max_tokens` exceeds the model's window, so the two are not
independent. A client configured with a max-tokens larger than the window gets
a 400 before any prompt is counted — and raising the client's context setting
makes it worse, not better.

**Truncation at a round number is a budget, not the model.** Raise
`max_tokens`. Truncation at a round wall-clock time is a proxy timeout.

The **Models** page carries context window, licence and provenance per model.

---

## Connecting your agent

The gateway speaks both the OpenAI and Anthropic APIs, so most agents work
with environment variables alone.

### Claude Code

```bash
export ANTHROPIC_BASE_URL=https://llm.apache.org
export ANTHROPIC_AUTH_TOKEN=<your PAT from My Keys>
export ANTHROPIC_MODEL=gemma4-26b
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=120000
claude
```

LiteLLM exposes `/v1/messages`, so Claude Code talks to it without a shim.

**`CLAUDE_CODE_MAX_CONTEXT_TOKENS` matters.** Claude Code assumes a 200k
window for a model it does not recognise and auto-compacts too late; requests
then fail once prompt + output exceeds the real window. Set it below the
model's window — 120000 against 131072 leaves room for the response.

**Pick a model whose reasoning is off by default.** A model that reasons
before answering emits nothing for a minute or more, and Claude Code abandons
the stream and retries. The retries stack: we watched a box run the same
expensive generation twice for a response nobody was reading. `gemma4-26b`
starts emitting immediately and works well.

**`--effort` may be needed.** Claude Code sends `reasoning_effort: high` by
default, and not every model accepts that value — Qwen3.8-27B takes only
`xhigh`, `medium` and `low`, and 400s on `high`. `claude --effort medium`
sets it. Gemma accepts all five levels, so no flag is needed there.

**Tool use is currently broken through the Anthropic path.** LiteLLM routes
it to vLLM's `/v1/responses` endpoint with a `tool_choice` shape vLLM does
not accept, so web search and other tools fail with a validation error.
Tracked — the likely fix is the `hosted_vllm/` provider prefix on routes.
Plain conversation is unaffected.

### Pi

_Placeholder._

Pi connects over the OpenAI-compatible API. Known so far: its `contextWindow`
and `maxTokens` settings must sum to less than the model's window, or vLLM
rejects the request — `maxTokens` larger than the window on its own is an
immediate 400.

To be filled in with a working configuration.

### Anything OpenAI-compatible

```bash
export OPENAI_BASE_URL=https://llm.apache.org/v1
export OPENAI_API_KEY=<your PAT>
```

`GET /v1/models` lists what your key can reach.

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
   `store_model_in_db: true`. Do not include `model_list.yaml`. llmao POSTs
   `/model/new` when a self-host vLLM is serving (and at startup for commercial).

4. **Serve** llmao (`main.py` or Hypercorn). Point client tools at the
   **LiteLLM** base URL with PATs, not at llmao for chat.

The token handler in `auth.py` is a stub for calling llmao’s own API
non-interactively; inference PATs are LiteLLM virtual keys.

### Self-hosted models via vLLM

Self-host catalog models run as **vLLM** processes on GPU boxes (Vast and
RunPod today). LiteLLM stays in front for PATs and project budgets, and is
also where fleet state lives: a **deployment** `api_base` is the public host
and port. `GET /vllm/config` still comes from `fleet.hosts` (listen ports)
plus the catalog. Boxes fetch it with template `FLEET_KEY`.

See [`hosting/README.md`](hosting/README.md),
[`docs/vllm-fleet-design.md`](docs/vllm-fleet-design.md) and
[`docs/fleet-state.md`](docs/fleet-state.md). Operational detail — what is
running where, and the exact launch commands — is kept out of this repo.

`model_list.yaml` is the **catalog** — what each model is, its licence and
provenance, and the vLLM recipe. Routes are *instances* of a catalog entry and
live in LiteLLM's database (`store_model_in_db`), created when a server is
serving and removed when it goes down. Cache and logs live under
`$DATA_DIRECTORY` on the box (typically `/workspace`), not in the config JSON.

**Port resolution differs by provider.** Vast exposes its container-to-public
mapping through an API, so those hosts resolve automatically. RunPod does not,
so a RunPod host must state its public port in the `fleet.hosts` row — an
optional fourth element. RunPod also reassigns the port on every pod recreate,
even when the pod lands on the same machine.

---

## Testing and load

Both scripts discover endpoints at runtime rather than carrying them in the
repo. See [`bin/README.md`](bin/README.md).

```bash
export LLMAO_KEY=<a PAT>
./bin/llmao-smoke                    # every model: completion, reasoning
                                     # control, tool calling, vision, long
                                     # output, large prompt
./bin/llmao-smoke --direct           # bypass LiteLLM; the difference between
                                     # the two runs is the gateway's overhead
```

```bash
export VLLM_API_KEY=<llmao::selfhost_api_key>
./bin/llmao-saturate --model <model> # ramp concurrency until something queues,
                                     # and say whether the limit is the
                                     # scheduler or memory
```

`llmao-smoke` exits non-zero on failure, so it works in CI. Run it after any
change to a model, a box, or the gateway.

`llmao-saturate` needs to reach the boxes directly, so it runs on the gateway
host. **Do not run the two together** — smoke queueing behind a saturation
ramp produces numbers that look like a regression and are not.

---

## Layout

```
main.py                  entry: create_app, run_standalone / run_asgi
pages.py                 HTML + /static
api.py                   JSON /healthz, /vllm/config, /v1/*
templates/ static/       EZT + Bootstrap
bin/fetch-thirdparty.sh  vendor Bootstrap/icons
bin/gen-litellm-master-key.sh   print sk-… for admin key
bin/llmao-smoke          end-to-end checks across every model
bin/llmao-saturate       concurrency ramp; finds the queueing point
bin/_discover.py         endpoint discovery shared by both (no committed IPs)
config.yaml.example      → config.yaml (gitignored; secrets)
litellm.yaml.example     → litellm.yaml (no catalog include; store_model_in_db)
model_list.yaml.example  → model_list.yaml (catalog for llmao; no secrets)
certs/                   mkcert PEMs + README
llmao/                   seam, auth, models, litellm_client, fleet
hosting/vast/            provision.sh + install_set.py
hosting/runpod/          RunPod template notes (image not built yet)
tests/                   offline seam, fleet, hosting installer
```
