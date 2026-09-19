# GPU-box hosting

Artifacts that run **on** a GPU instance (Vast.ai now; RunPod next). They
are not part of the asfquart process.

**Design (why):** [`docs/vllm-fleet-design.md`](../docs/vllm-fleet-design.md).

**Control plane (all providers):** asfquart serves `GET /vllm/config`
as JSON from `config.yaml` `fleet.hosts` (keyed by client IP) plus the catalog.
Bearer fleet key (template).

Provider-specific: how that JSON becomes running vLLM. Both providers
write Supervisor units; there is no long-lived Python launcher.

## Layout

| Path | Role |
|------|------|
| `vast/provision.sh` | Vast on-create (curl installer from GitHub) |
| `vast/install_set.py` | GET JSON, write `/etc/supervisor/conf.d/vllm-*.conf`, `supervisorctl update` |
| `vast/README.md` | Vast.ai runbook |
| `runpod/README.md` | RunPod runbook (image not built yet) |

## Box environment

Required:

- `FLEET_KEY` — shared secret on the **template**; `Authorization: Bearer`
- `ASFQUART_URL` — origin only, `https://llm.apache.org`
- `DATA_DIRECTORY` — volume workspace (typically `/workspace`). HF cache
  and logs are `$DATA_DIRECTORY/hf-cache` and `.../logs`, not in the config JSON.

## Boot

```text
Vast:   provision.sh → curl install_set.py → GET JSON → supervisor units → vllm serve
RunPod: image entrypoint → COPY'd installer → GET JSON → supervisor units → vllm serve
```

Vast still curls `refs/heads/main`. Pin that URL to a **commit SHA** before
treating instance-create as production-safe. RunPod pins by **image digest**.

## Fit check

Catalog `vram_gb` / `disk_gb` vs the card and `statvfs`, **before** the
weights pull. vLLM's own allocator runs later (engine init) and does not
replace this. NVIDIA: `nvidia-smi`. AMD: `rocm-smi` when we have a ROCm
image. Silent if the probe is missing.
