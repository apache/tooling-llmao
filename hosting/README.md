# GPU-box hosting

Artifacts that run **on** a GPU instance (Vast.ai now, RunPod later). They
are not part of the asfquart process.

**Design (why):** [`docs/vllm-fleet-design.md`](../docs/vllm-fleet-design.md).
`servers.yaml` schema is defined there (§5); this tree does not duplicate it.

**Control plane:** asfquart serves `GET /vllm/config/{set_id}` with the fleet
key. That endpoint is application code (`api.py` / `llmao/`), not here.

## Layout

| Path | Role |
|------|------|
| `fetch_config.py` | `FLEET_KEY` + `VLLM_SET` + `ASFQUART_URL` → write `servers.yaml` |
| `launcher.py` | parse YAML, spawn/monitor `vllm serve`, SIGTERM |
| `onstart.sh` | provider on-start: fetch then exec launcher |
| `vast/README.md` | Vast.ai runbook (stock vLLM template + env) |

Same scripts for every provider. Only the runbook (how to set env / on-start)
differs.

## Box environment

Required:

- `FLEET_KEY` — shared secret; `Authorization: Bearer`
- `VLLM_SET` — set id (semantic or `box-a`; control plane owns names)
- `ASFQUART_URL` — origin only, e.g. `https://llmao.apache.org` (no trailing
  path). **Required.** Not hard-coded; dev and prod hosts differ.

Optional:

- `SERVERS_YAML` — default `/workspace/servers.yaml`
- `LAUNCHER_MAX_RESTARTS` — default `5` (per server); then stay up for SSH

## Boot

```text
onstart.sh → fetch_config.py → launcher.py
```

v1 uses the **stock Vast vLLM image** plus this on-start. A derived image is
not justified until the layer is more than these three files (see Vast
runbook).

Pin the git **tag or commit** in the provider on-start snippet. Do not
`curl` floating `main`.
