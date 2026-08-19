# Vast.ai runbook

Use Vast’s **stock vLLM template**. Do not maintain a derived image in v1.

`provision.sh` installs `launcher.py` and a supervisor unit. It does **not**
fetch model config. The launcher GETs JSON from asfquart when it starts.

## Instance

- Launch mode: SSH (or Jupyter + SSH).
- Map ports used by the set (example inventory: 8001, 8003).
- Disk: large enough for HF cache under `/workspace/hf-cache`.
- Environment:

  ```bash
  FLEET_KEY=<shared-secret>
  VLLM_SET=<model_set>
  ASFQUART_URL=https://llm.apache.org:8443
  ```

  Template is identical for every box; only these values change.

## On-create

Point Vast on-start at `hosting/vast/provision.sh` (or curl it, then run).
Today the script pulls `launcher.py` from `main`; pin a commit when this
is no longer a moving target.

`SSL_VERIFY=0` is set in the supervisor unit because asfquart is still on
**:8443** with a self-signed cert. **Remove that env when llm.apache.org
serves :443** with a public CA; the launcher defaults to verifying TLS.

## Smoke

1. SSH; `tail -f /var/log/vllm-launcher.log`.
2. `curl -sS -H "Authorization: Bearer <api_key>" http://127.0.0.1:<port>/v1/models`
3. LiteLLM `api_base` is already the public URL in `model_list.yaml`; same `api_key`.
