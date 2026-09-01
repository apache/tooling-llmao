# Vast.ai runbook

Use Vast’s **stock vLLM template**. Do not maintain a derived image in v1.

`provision.sh` curls `install_set.py`, which fetches host JSON (by client
IP) and writes one Supervisor program per model. supervisord runs `vllm serve`.
There is no Python process manager.

## Instance

- Launch mode: SSH (or Jupyter + SSH).
- Map ports used by the set (example inventory: 8001, 8003).
- Disk: large enough for HF cache under `$DATA_DIRECTORY/hf-cache`
  (`DATA_DIRECTORY` is typically `/workspace`).
- Environment:

  ```bash
  FLEET_KEY=<shared-secret>   # bake into the template
  ASFQUART_URL=https://llm.apache.org:8443
  ```

  Put the instance public IP under `fleet.hosts` in `config.yaml`. Do not set
  per-instance docker env for the fleet key or a set id.

## On-create

Point Vast on-start at `hosting/vast/provision.sh`. Today it pulls
`install_set.py` from `main`; pin a commit when this is no longer a moving
target.

`SSL_VERIFY=0` is the default in `provision.sh` because asfquart is still
on **:8443** with a self-signed cert. **Remove that when llm.apache.org
serves :443** with a public CA.

## Smoke

1. `supervisorctl status`
2. `curl -sS -H "Authorization: Bearer <api_key>" http://127.0.0.1:<port>/v1/models`
