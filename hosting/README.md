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

VRAM is summed over every server on the box, because each loads its own
copy of the weights. Disk is counted once per distinct model: two servers
of the same model share one copy under the HF cache. `vram_gb` is weights
only, so a box that passes with nothing to spare still has no KV cache.

The disk check compares the full requirement with free space even when the
weights are already on the volume, so a re-run on a nearly full box can
refuse where a first install would not.

## Sizing servers that share a card

Left alone, each `vllm serve` takes 0.9 of the card and a second server
cannot start. `install_set.py` divides the memory instead (`apportion`):

- The budget is 0.9 of what is **free** at install time.
- Each server gets its `vram_gb`, plus a share of the remainder in
  proportion to its `max_model_len`. An equal split if any server has none.
- The result is passed as `--gpu-memory-utilization`, a fraction of the
  card's **total**, rounded down to three places.
- A `gpu_memory_utilization` set in `models.yaml` is kept and deducted from
  the budget first.
- No flag is set when the memory cannot be read, when a server declares no
  `vram_gb`, or when nothing is left after the weights.

Each server's fraction is printed to the install log.

Example: 128 GB in the machine, 100 GB available, two `qwen3-8b` servers at
9 GB each. The budget is 90 GB, each server gets 9 + 36 = 45 GB, and each
starts with `--gpu-memory-utilization 0.351`.

**Unified memory (GB10).** `nvidia-smi` answers but prints `[N/A]` for
memory. The installer then reads `/proc/meminfo`: `MemTotal` as the total,
`MemAvailable` as what is free. If `nvidia-smi` is absent altogether, memory
is unknown and nothing is sized.

What this does not handle:

- **Sizing happens once, at install.** Adding a server to a box means
  re-running the install so the existing servers restart with smaller shares.
- **Re-running on a live box.** With the servers still up there is little
  free memory, so the fit check may refuse and no fraction is set. Stop the
  servers first.
- **Servers that load at the same time.** vLLM charges each one for the
  drop in free memory while it loads, which includes the other's weights.
  Two 15 GiB servers on a GB10 each ended with 17 GiB of KV cache where
  their share allowed about 36. vLLM suggests an explicit
  `--kv-cache-memory` in bytes, which the installer does not emit yet.

## Cards a checkpoint will not load on

Qwen's FP8 checkpoints are block-quantized, which vLLM runs through
DeepGEMM, and DeepGEMM handles only SM90 and SM100 cards. Elsewhere (the
GB10 is 12.1) the engine dies during load with `Unknown SF transformation`.
Give such a box a model whose weights are not block-quantized FP8;
`qwen3-8b-bf16` exists for this. Nothing checks this before the pull.
