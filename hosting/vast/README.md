# Vast.ai runbook

Use Vast’s **stock vLLM template**. Do not maintain a derived image in v1.

`provision.sh` curls `install_set.py`, which fetches set JSON and writes
one Supervisor program per model. supervisord runs `vllm serve`. There is
no Python process manager.

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

## On-create

Point Vast on-start at `hosting/vast/provision.sh`. Today it pulls
`install_set.py` from `main`; pin a commit when this is no longer a moving
target.

`SSL_VERIFY=0` is the default in `provision.sh` because asfquart is still
on **:8443** with a self-signed cert. **Remove that when llm.apache.org
serves :443** with a public CA.

## Operator env helper

Laptop tool (not on the GPU box). Needs the Vast Python SDK:

```bash
pip install vastai
```

API key: `~/.config/vastai/vast_api_key` (SDK default). Do not put `FLEET_KEY` on the command line.

```bash
python3 hosting/vast/env.py                 # list (default)
python3 hosting/vast/env.py list
python3 hosting/vast/env.py show INSTANCE_ID
python3 hosting/vast/env.py set INSTANCE_ID --vllm-set primary
```

`show` prints account secrets, template docker env, and instance `extra_env` with real values (the CLI masks them).

`set` reads `fleet.key` from repo `config.yaml`, checks `--vllm-set` exists in `model_list.yaml`, prints the models that box will fetch from llm.apache.org, writes `FLEET_KEY` + `VLLM_SET` on the instance, then asks `Ready to reboot? [Y/n]` so on-start re-runs `install_set.py`.

## Smoke

1. `supervisorctl status`
2. `curl -sS -H "Authorization: Bearer <api_key>" http://127.0.0.1:<port>/v1/models`
