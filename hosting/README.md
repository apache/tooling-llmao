# GPU-box hosting

Artifacts that run **on** a GPU instance (Vast.ai now, RunPod later). They
are not part of the asfquart process.

**Design (why):** [`docs/vllm-fleet-design.md`](../docs/vllm-fleet-design.md).

**Control plane (all providers):** asfquart serves `GET /vllm/config`
as JSON from `config.yaml` `fleet.hosts` (keyed by client IP) plus the catalog.
Bearer fleet key (template).

Provider-specific: how that JSON becomes running vLLM. Vast writes
Supervisor units at box-start. There is no long-lived Python launcher.

## Layout

| Path | Role |
|------|------|
| `vast/provision.sh` | Vast on-create |
| `vast/install_set.py` | GET JSON, write `/etc/supervisor/conf.d/vllm-*.conf`, `supervisorctl update` |
| `vast/README.md` | Vast.ai runbook |

## Box environment

Required:

- `FLEET_KEY` — shared secret on the **template**; `Authorization: Bearer`
- `ASFQUART_URL` — origin only, e.g. `https://llm.apache.org`
- `DATA_DIRECTORY` — Vast template workspace (typically `/workspace`). HF cache
  and logs are `$DATA_DIRECTORY/hf-cache` and `.../logs`, not in the config JSON.

## Boot (Vast)

```text
provision.sh → install_set.py → GET JSON → supervisor units → vllm serve …
```
