# llmao

Tooling’s **implementation** of the ASF LLM gateway seam at `llm.apache.org`.

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
| `litellm.yaml` | `litellm.yaml.example` | LiteLLM proxy (`include: model_list.yaml`) |
| `model_list.yaml` | `model_list.yaml.example` | **Model inventory SoT** (routes + UX metadata) |

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
# set api keys in model_list.yaml (eyaml in production)
make proxy                             # litellm --config litellm.yaml
make run
```

Open `https://localhost.apache.org:8443/` (port from `config.yaml`), sign in
with ASF.

PAT metadata lives in LiteLLM’s Postgres. Model inventory is **only**
`model_list.yaml` (not DB `STORE_MODEL_IN_DB`). Provider **API keys** in that
file come from eyaml in production; **`api_base` is cleartext** (not shown in UX).

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

Full status and phased UI plan: **[`docs/STATUS.md`](docs/STATUS.md)**.
Product design: **rai-private** `services/llmao/README.md`.
Ops: Infra **`p6/modules/llmao/README.md`**.

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
   `litellm --config litellm.yaml`. Model routes live in **`model_list.yaml`**
   (included); provider API keys via eyaml in production.

4. **Serve** llmao (`main.py` or Hypercorn). Point client tools at the
   **LiteLLM** base URL with PATs, not at llmao for chat.

The token handler in `auth.py` is a stub for calling llmao’s own API
non-interactively; inference PATs are LiteLLM virtual keys.

### Self-hosted models via vLLM

Self-host catalog models run as **vLLM** processes on GPU boxes (Vast today).
LiteLLM stays in front for PATs and project budgets, and is also where fleet
state lives: a route's `api_base` is the host and port, and `model_info` carries
the recipe. Boxes fetch `GET /vllm/config` with template `FLEET_KEY`; asfquart
keys the host from `X-Forwarded-For` or the peer IP and returns the routes
matching it.
See [`hosting/README.md`](hosting/README.md),
[`docs/vllm-fleet-design.md`](docs/vllm-fleet-design.md), and
[`docs/fleet-state.md`](docs/fleet-state.md).

Example inventory today (`model_list.yaml.example`): `gemma4-26b`, `qwen3-8b`.
`model_list.yaml` is the **catalog** — what each model is, its licence and
provenance, and the vLLM recipe. Routes are *instances* of a catalog entry and
live in LiteLLM's database (`STORE_MODEL_IN_DB=True`), created when a server
goes healthy and removed when it goes down. Cache/logs live under `$DATA_DIRECTORY` on the
box (typically `/workspace`), not in the config JSON.

---

## Layout

```
main.py                  entry: create_app, run_standalone / run_asgi
pages.py                 HTML + /static
api.py                   JSON /healthz, /vllm/config, /v1/*
templates/ static/       EZT + Bootstrap (`fleet.ezt` site-admin)
bin/fetch-thirdparty.sh  vendor Bootstrap/icons
bin/gen-litellm-master-key.sh   print sk-… for admin key
config.yaml.example      → config.yaml (gitignored; secrets, no fleet.hosts)
litellm.yaml.example     → litellm.yaml (include model_list.yaml)
model_list.yaml.example  → model_list.yaml (model catalog; no secrets)
certs/                   mkcert PEMs + README
llmao/                   seam, auth, models, litellm_client, fleet
hosting/vast/            provision.sh + install_set.py
tests/                   offline seam, fleet, hosting installer
```