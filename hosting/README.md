# GPU-box hosting

Artifacts that run **on** a GPU instance (Vast.ai now, RunPod later). They
are not part of the asfquart process.

**Design (why):** [`docs/vllm-fleet-design.md`](../docs/vllm-fleet-design.md).

**Control plane:** asfquart serves `GET /vllm/config/{set_id}` as JSON, built
from `model_list.yaml` for that `model_set`. Fleet key Bearer auth.

There is **no `servers.yaml`**. vLLM processes are long-lived; fetching a
file at provision time does not save anything. The launcher fetches JSON
when **it** starts.

## Layout

| Path | Role |
|------|------|
| `launcher.py` | fetch JSON, spawn/monitor `vllm serve`, SIGTERM |
| `vast/provision.sh` | Vast on-create: install launcher + supervisor (no config fetch) |
| `vast/README.md` | Vast.ai runbook |

## Box environment

Required:

- `FLEET_KEY` — shared secret; `Authorization: Bearer`
- `VLLM_SET` — `model_set` id from `model_list.yaml`
- `ASFQUART_URL` — origin only, e.g. `https://llm.apache.org:8443`

Optional:

- `SSL_VERIFY` — `0` skips TLS verify. **Stopgap** while llm.apache.org is
  on **:8443** with a self-signed cert. **Drop `SSL_VERIFY=0` when that
  host moves to :443** with a public CA.
- `LAUNCHER_MAX_RESTARTS` — default `5` (per server); then stay up for SSH

## Boot

```text
provision.sh → supervisor → launcher.py → GET JSON → vllm serve …
```

v1 uses the stock Vast vLLM image plus `provision.sh`. Pin a git tag or
commit when curling `launcher.py` in production; `main` is a convenience
for now.
