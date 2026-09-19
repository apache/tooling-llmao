# RunPod runbook (knowledge; image not built yet)

RunPod templates pull a **Docker image**. Vast uses a stock vLLM template
and curls `install_set.py` at create. Same control plane, different
artifact.

Nothing in this directory is a running image yet. Registry location is a
placeholder until Infra says where to push. The notes below are what the
image and `fleet.hosts` row must satisfy.

## Same as Vast

- `GET /vllm/config` by **client IP**, Bearer `FLEET_KEY`.
- JSON listen ports only. The box never sees the public mapping.
- Template env: `FLEET_KEY`, `ASFQUART_URL=https://llm.apache.org`,
  `DATA_DIRECTORY` (typically `/workspace`). TLS verify is on; there is
  no skip flag.
- **Supervisord** runs one `vllm serve` per catalog row (several listens
  in one pod). That is the usual shape for a GPU *pod*, not a one-process
  web container, and it matches Vast.
- Weights and logs live on the volume (`$DATA_DIRECTORY/hf-cache`,
  `.../logs`), not in the image.
- Pre-pull fit check: catalog `vram_gb` / `disk_gb` against the card and
  `statvfs`. That is **not** vLLM: vLLM only sizes KV after the weights
  are on disk (`torch` / HIP `mem_get_info`). NVIDIA probe today is
  `nvidia-smi`; missing probe is not a failure. AMD (`rocm-smi`) when we
  have a ROCm image.

## Different from Vast

| | Vast | RunPod |
|--|------|--------|
| Image | stock `vastai/vllm` (or similar) | **our** image (`IMAGE_REGISTRY_PLACEHOLDER/llmao-vllm-box`) |
| Installer | curl `install_set.py` from GitHub **main** | **COPY** in the image (boot must not depend on GitHub) |
| Pin | later: curl a **commit SHA** | image **digest** |
| Public port | Vast API HostPort (`vast_client`) | see Ports below |

Pin the Vast curl before create-instance is treated as production-safe: a
broken `main` would brick every new box. The image digest is the same
kind of pin for RunPod.

## Ports (open; 4th host-row slot is a stopgap)

`fleet.hosts` already accepts `[model, listen, name|null, public]`. That
was added so a RunPod box could get an `api_base` at all: RunPod does not
expose a Vast-style HostPort to us today, and it **reassigns** the TCP
public port on every pod recreate.

We do **not** want operators editing that fourth number forever. Once
self-deploy is formalized (create from template, then read the mapping),
prefer one of:

1. **HTTP proxy (stable URL).** Template exposes `N/http`. Public origin
   is `https://<podId>-<listen>.proxy.runpod.net`. The listen port is in
   the hostname, so recreate does not need a new fourth slot. Only use
   this as `api_base` after we prove LiteLLM and long generations survive
   that proxy (timeouts, TLS, HTTP/1.1).
2. **Direct TCP + API.** Template exposes `N/tcp`. Public port changes.
   RunPod **REST v2** `GET /pods/{id}` → `portMappings` can fill
   `public_port` the way `vast_client` fills HostPort. GraphQL exists but
   is deprecated (~early 2027); new code should be REST.

Until one of those is wired, keep the fourth slot. It may go away.

The template must still **expose every listen port** the set uses (max 10
HTTP ports on RunPod). Control plane JSON stays listen-only.

## Image sketch (next, not this note)

`FROM vllm/vllm-openai:…` (pin a digest later), install supervisord,
COPY the shared installer, `CMD`/`ENTRYPOINT` that fetches config, writes
units, `exec supervisord -n`. No baked weights.

## Smoke (once a pod exists)

1. `supervisorctl status`
2. `curl -sS -H "Authorization: Bearer <api_key>" http://127.0.0.1:<listen>/v1/models`
