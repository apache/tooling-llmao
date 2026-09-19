# Vast.ai runbook

Use Vast’s **stock vLLM template**. Do not maintain a derived image in v1.

`provision.sh` curls `install_set.py`, which fetches host JSON (by client
IP) and writes one Supervisor program per model. supervisord runs `vllm serve`.
There is no Python process manager.

## Instance

- Launch mode: SSH (or Jupyter + SSH).
- Map **container** ports used by the set (example inventory: 8001, 8003).
  Those are `fleet.hosts` listen ports and what `GET /vllm/config` returns.
  Vast remaps them to public HostPorts; llmao does not send the public
  port to the box. With `fleet.vast.api_key`, `fleet-lifecycle` fills
  `public_port` from one show-instances GET each tick.
- Disk: large enough for HF cache under `$DATA_DIRECTORY/hf-cache`
  (`DATA_DIRECTORY` is typically `/workspace`).
- Environment:

  ```bash
  FLEET_KEY=<shared-secret>   # bake into the template
  ASFQUART_URL=https://llm.apache.org
  ```

  Put the instance public IP under `fleet.hosts` in `config.yaml`. Do not set
  per-instance docker env for the fleet key or a set id.

## On-create

Point Vast on-start at `hosting/vast/provision.sh`. Today it pulls
`install_set.py` from `main`; pin a commit SHA when this is no longer a
moving target (safety: a broken main would brick every box on next create).

## Smoke

1. `supervisorctl status`
2. `curl -sS -H "Authorization: Bearer <api_key>" http://127.0.0.1:<port>/v1/models`
