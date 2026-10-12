# llmao

Tooling’s **implementation** of the ASF LLM gateway at `llm.apache.org`.

**What this app is for:** Apache-facing control plane for **shared, attributed,
limited** access to Foundation-sanctioned inference. You sign in with ASF,
manage **PATs** (and later project envelopes), and browse **Models**.
**Inference** goes to the **LiteLLM proxy** with a PAT — not through a chat UI
here.

| Doc | Role |
|-----|------|
| **`apache/rai-private` → `services/llmao/README.md`** | Product design (concepts, policy) |
| **[`docs/STATUS.md`](docs/STATUS.md)** | Build status + **planned UX** backlog |
| **This README** | How to run the software |
| **[Help](https://llm.apache.org/help)** (`docs/help/`) | How to use the gateway: keys, connecting tools |
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

Required secret YAML (copy from `*.example`; app and `make proxy` **fail-fast**
if missing). `models.yaml` is committed (no secrets). Do not join
`config.yaml` and `litellm.yaml`.

| File | From | Role |
|------|------|------|
| `config.yaml` | `config.yaml.example` | llmao / asfquart |
| `litellm.yaml` | `litellm.yaml.example` | LiteLLM proxy (`store_model_in_db`; do **not** include `models.yaml`) |
| `models.yaml` | (in git) | Admin definitions: UX + vLLM recipe + `/model/new` template |

```bash
make install
cp config.yaml.example config.yaml
cp litellm.yaml.example litellm.yaml
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

PAT metadata lives in LiteLLM’s Postgres. **models.yaml** is admin
definitions (llmao only; not included by LiteLLM). Routes live in the DB
(`store_model_in_db`). Commercial entries need a static `api_base`;
self-host `api_base` is per instance. Self-host `litellm_params.model` is
`hosted_vllm/<name>`, not `openai/`.

Editing `models.yaml` does not change a live **deployment** until llmao
POSTs `/model/new` again (reload **llmao**, not LiteLLM). After Puppet/VCS
updates `litellm.yaml`, **restart LiteLLM**. Production secrets are on-disk
YAML, not env vars.

ASGI (TLS on the reverse proxy):

```bash
uv run python -m hypercorn main:llmao_app --bind 0.0.0.0:8080
```

```bash
make test          # offline seam + models.yaml tests (no OAuth session automation yet)
```

---

## Using the gateway

How to get a key and connect a tool is in the live Help, which is the user
documentation:

- [Help](https://llm.apache.org/help) — what the gateway is and where to start
- [FAQ](https://llm.apache.org/help/faq) — keys, connecting an
  OpenAI-compatible tool, empty answers, rejected requests, truncation, who
  can see your prompts
- [Claude Code](https://llm.apache.org/help/claude-code)
- [Codex](https://llm.apache.org/help/codex)
- [Grok Build](https://llm.apache.org/help/grok-build)
- [Pi](https://llm.apache.org/help/pi)

Those pages are rendered from [`docs/help/`](docs/help/) on each request, so
edit them there; see [`docs/help-authoring.md`](docs/help-authoring.md). A
local run serves the same pages at `/help`.

**Projects** (envelopes, member caps, by-person usage) and **Reports** are product intent — see design §6 and the UX backlog in [`docs/STATUS.md`](docs/STATUS.md).

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
   `store_model_in_db: true`. Do not include `models.yaml`. llmao POSTs
   `/model/new` when a self-host vLLM is serving (and at startup for commercial).

4. **Serve** llmao (`main.py` or Hypercorn). Point client tools at the
   **LiteLLM** base URL with PATs, not at llmao for chat.

The token handler in `auth.py` is a stub for calling llmao’s own API
non-interactively; inference PATs are LiteLLM virtual keys.

### Self-hosted models via vLLM

Self-host models run as **vLLM** processes on GPU boxes (Vast and
RunPod today). LiteLLM stays in front for PATs and project budgets, and is
also where fleet state lives: a **deployment** `api_base` is the public host
and port. `GET /vllm/config` still comes from `fleet.hosts` (listen ports)
plus `models.yaml`. Boxes fetch it with template `FLEET_KEY`.

See [`hosting/README.md`](hosting/README.md),
[`docs/vllm-fleet-design.md`](docs/vllm-fleet-design.md) and
[`docs/fleet-state.md`](docs/fleet-state.md). Operational detail — what is
running where, and the exact launch commands — is kept out of this repo.

`models.yaml` is **admin definitions** — served id, licence, provenance, and
the vLLM recipe. A LiteLLM **deployment** is one instance of a row (one
`api_base`) in the proxy database (`store_model_in_db`), created when a
server is serving and removed when it goes down. Cache and logs live under
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
export VLLM_API_KEY=$(bin/llmao-vllm-api-key --host <ip> --port <listen>)
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
litellm.yaml.example     → litellm.yaml (do not include models.yaml; store_model_in_db)
models.yaml              admin definitions (committed; no secrets)
certs/                   mkcert PEMs + README
llmao/                   seam, auth, models, fleet, proxy/ (LiteLLM client)
hosting/vast/            provision.sh + install_set.py
hosting/runpod/          RunPod template notes (image not built yet)
tests/                   offline seam, fleet, hosting installer
```
