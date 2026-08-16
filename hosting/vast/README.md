# Vast.ai runbook

Use Vast’s **stock vLLM template** (`vastai/vllm` or `vllm/vllm-openai`).
Do not maintain a derived image in v1.

Our layer is: fetch YAML from asfquart, run `vllm serve` per entry. That is
a small Python process. Revisit a `Dockerfile` here only if we later need
extra packages, a non-vLLM runtime, or boxes that cannot pull this repo.

## Instance

- Launch mode: SSH (or Jupyter + SSH).
- Map ports **8000–8002**.
- Disk: large enough for the heaviest set that will be assigned (HF cache
  under `hf_home` from YAML, typically `/workspace/hf-cache`).
- Environment:

  ```bash
  FLEET_KEY=<shared-secret>
  VLLM_SET=<set-id>
  ASFQUART_URL=https://<asfquart-host>
  ```

  Template is identical for every box; only these three values change.

## On-start (pin a commit)

Replace `COMMIT` with a SHA or tag from `apache/tooling-llmao`. Do not use
`main`.

```bash
set -eu
COMMIT=<pin>
BASE="https://raw.githubusercontent.com/apache/tooling-llmao/${COMMIT}/hosting"
mkdir -p /workspace/llmao-hosting
curl -fsSL "$BASE/fetch_config.py" -o /workspace/llmao-hosting/fetch_config.py
curl -fsSL "$BASE/launcher.py" -o /workspace/llmao-hosting/launcher.py
curl -fsSL "$BASE/onstart.sh" -o /workspace/llmao-hosting/onstart.sh
chmod +x /workspace/llmao-hosting/onstart.sh
exec /workspace/llmao-hosting/onstart.sh
```

Requires outbound HTTPS to GitHub (once, for scripts) and to `ASFQUART_URL`
(every boot, for YAML).

## Smoke

1. SSH in; confirm `/workspace/servers.yaml` exists and is not empty.
2. `curl -sS -H "Authorization: Bearer <api_key>" http://127.0.0.1:8000/v1/models`
3. Point LiteLLM `api_base` at the box public URL + port with the **same**
   `api_key` as the YAML entry.

Control-plane endpoint and LiteLLM wiring land in later slices.
