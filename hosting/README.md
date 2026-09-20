# GPU-box hosting

Artifacts that run **on** a GPU instance (Vast.ai now; RunPod next). They
are not part of the asfquart process.

**Design (why):** [`docs/vllm-fleet-design.md`](../docs/vllm-fleet-design.md).

**Control plane (all providers):** asfquart serves `GET /vllm/config`
as JSON from `config.yaml` `fleet.hosts` plus `models.yaml`. Bearer fleet
key (template). Vast boxes send `X-LLMAO-Host: $PUBLIC_IPADDR` (the TCP
peer is often a transparent proxy).

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
- `PUBLIC_IPADDR` — Vast sets this; `install_set.py` requires it (`X-LLMAO-Host`)
- `PROVISIONING_SCRIPT` — Vast only: raw URL of `provision.sh`

## Boot

```text
Vast:   PROVISIONING_SCRIPT → provision.sh → curl install_set.py → GET JSON → supervisor units → vllm serve
RunPod: image entrypoint → COPY'd installer → GET JSON → supervisor units → vllm serve
```

Vast still curls `install_set.py` from `refs/heads/main`. Pin that URL to a
**commit SHA** before treating instance-create as production-safe. RunPod
pins by **image digest**.

### Vast `PROVISIONING_SCRIPT` vs `install_set.py`

The template sets

`PROVISIONING_SCRIPT=https://raw.githubusercontent.com/apache/tooling-llmao/refs/heads/main/hosting/vast/provision.sh`

The stock vLLM **container has none of our files**. Vast’s base-image
provisioner downloads that URL to **`/provisioning.sh`**, `dos2unix`,
`chmod +x`, and executes it. Their docs call it a **shell script**.

`provision.sh` mkdirs `$DATA_DIRECTORY`, curls `install_set.py`, and runs
`python3`. The `.sh` exists so Vast has something it will run from a URL.
`DATA_DIRECTORY` mkdir could live in Python; that is not why the wrapper
exists.

Do **not** point `PROVISIONING_SCRIPT` at `install_set.py` until a
throwaway instance proves execute is shebang (`./provisioning.sh`) rather
than `bash /provisioning.sh`. Docs and the saved `.sh` name assume bash.
`install_set.py` has no shebang today (ASF license first).

RunPod’s image COPY’s the installer; it does not use `provision.sh`.

## Fit check

`models.yaml` `vram_gb` / `disk_gb` vs the card and `statvfs`, **before** the
weights pull. vLLM's own allocator runs later (engine init) and does not
replace this. NVIDIA: `nvidia-smi`. AMD: `rocm-smi` when we have a ROCm
image. Silent if the probe is missing.
