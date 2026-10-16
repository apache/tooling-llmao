# GPU-box hosting

Artifacts that run **on** a GPU instance (Vast.ai and RunPod). They are
not part of the asfquart process.

**Design (why):** [`docs/vllm-fleet-design.md`](../docs/vllm-fleet-design.md).

**Control plane (all providers):** asfquart serves `GET /vllm/config`
as JSON from `config.yaml` `fleet.hosts` plus `models.yaml`. Bearer fleet
key (template). Boxes send `X-LLMAO-Host: $LLMAO_HOST` (the TCP
peer is often a transparent proxy).

Provider-specific: how that JSON becomes running vLLM. Both providers
write Supervisor units; there is no long-lived Python launcher.

## Layout

| Path | Role |
|------|------|
| `install_set.py` | Shared by every provider: GET JSON, write a `vllm-*.conf` per server, `supervisorctl update` (or not, with `--no-reload`) |
| `vast/provision.sh` | Vast on-create: set `LLMAO_HOST`, curl the installer from GitHub, run it |
| `vast/README.md` | Vast.ai runbook |
| `runpod/Dockerfile` | RunPod image: stock vLLM plus vendored supervisor (`runpod/bin/`, `runpod/requirements.txt`) and `runpod/provision.sh`; built and pushed to ghcr by `.github/workflows/runpod-image.yml` |
| `runpod/provision.sh` | RunPod entrypoint: provision once (retrying), then `exec supervisord` |
| `runpod/README.md` | RunPod runbook |
| `runpod/podman.md` | Checking the RunPod image locally with Podman, before pushing |

## Box environment

Required:

- `FLEET_KEY` — shared secret on the **template**; `Authorization: Bearer`
- `ASFQUART_URL` — origin only, `https://llm.apache.org`
- `DATA_DIRECTORY` — volume workspace (typically `/workspace`). HF cache
  and logs are `$DATA_DIRECTORY/hf-cache` and `.../logs`, not in the config JSON.
- `LLMAO_HOST` — the box's name in `fleet.hosts`, sent as `X-LLMAO-Host`.
  Each provider's `provision.sh` sets it from that provider's own variable,
  so `install_set.py` knows no provider's names. Vast: the public IP,
  `PUBLIC_IPADDR`. RunPod: the pod id, `RUNPOD_POD_ID`, since a pod need
  not have a public IP of its own
- `PROVISIONING_SCRIPT` — Vast only: raw URL of `provision.sh`

## Boot

```text
Vast:   PROVISIONING_SCRIPT → provision.sh → curl install_set.py → GET JSON → supervisor units → vllm serve
RunPod: image entrypoint provision.sh → curl install_set.py → GET JSON → units on the volume → exec supervisord → vllm serve
        (once provisioned, a boot skips straight to exec supervisord)
```

Both curl `install_set.py` from `refs/heads/main`. Pin that URL to a
**commit SHA** before treating instance-create as production-safe: a broken
`main` would stop every new box from provisioning. The RunPod image itself
is pinned by its digest in the template.

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

RunPod has its own `runpod/provision.sh`, baked into the image as the
entrypoint; see [`runpod/README.md`](runpod/README.md).

## Fit check

`models.yaml` `vram_gb` / `disk_gb` vs the card and `statvfs`, **before** the
weights pull. vLLM's own allocator runs later (engine init) and does not
replace this. NVIDIA: `nvidia-smi`. AMD: `rocm-smi` when we have a ROCm
image. Silent if the probe is missing.

VRAM is summed over every server on the box, because each loads its own
copy of the weights. Disk is counted once per distinct model: two servers
of the same model share one copy under the HF cache.

Each server needs its weights (`vram_gb`) **and** the KV cache for one
request at its `max_model_len`: vLLM refuses to start rather than take a
context it cannot hold. A model that declares `kv_bytes_per_token` has that
counted; one that does not is checked on its weights alone. (qwen3-8b's 9GB
of weights fit a 12GB card; the 2.8GB of KV cache for its 40960-token
context did not, and vLLM would not start.) The total is checked against
the share the servers will get (see the budget below), as the sizing below
divides it.

## Several GPUs

vLLM uses only the first GPU unless told otherwise. The installer counts the
box's GPUs and runs every server across all of them
(`--tensor-parallel-size`), so each card holds an equal share of each
server's weights and KV cache. The fit check and the sizing below are then
per card, against the card with least free memory. A unified-memory machine
(GB10) is one GPU.

The model must divide evenly across the cards (its attention heads by the GPU
count); vLLM says so at startup if it does not. Cards talk over PCIe unless
the box has NVLink, which costs some speed.

The disk check compares the full requirement with free space even when the
weights are already on the volume, so a re-run on a nearly full box can
refuse where a first install would not.

## Sizing servers that share a card

Left alone, each `vllm serve` takes 0.9 of the card (of each card, with
several GPUs) and a second server cannot start. `install_set.py` divides the memory instead (`apportion`):

- The budget is what is **free** at install time, less a reserve: 10% of
  it, or 2 GB, whichever is more, per card. vLLM's flag covers its own
  allocations; the CUDA context, the buffers GPUs use to talk to each
  other, and CUDA graph capture come out of what it leaves, and that is
  roughly fixed in size. 10% of an 80 GB card is plenty; 10% of a 12 GB
  card was not (two RTX 3060s splitting qwen3-8b ran out at startup with
  1.2 GB left each). The 2 GB is a judgement, not a measurement.
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
  free memory, so the fit check may refuse and no fraction is set.
  Re-provision instead, which starts from a restart: Vast in
  [`vast/README.md`](vast/README.md#re-provisioning-a-box), RunPod in
  [`runpod/README.md`](runpod/README.md#changing-a-pods-set).
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
