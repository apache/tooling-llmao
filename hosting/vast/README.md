# Vast.ai runbook

Custom **template** (on-start, env), Vast’s **stock vLLM container**. Do not
maintain a derived image for Vast.

`provision.sh` curls `install_set.py`, which fetches host JSON
(`X-LLMAO-Host: $PUBLIC_IPADDR`) and writes one Supervisor program per model.
supervisord runs `vllm serve`. There is no Python process manager.

Vast instances often reach `llm.apache.org` through a transparent HTTP proxy:
the TCP peer is not the instance and there is no `X-Forwarded-For`. Vast sets
`PUBLIC_IPADDR` in the container; the installer sends that as `X-LLMAO-Host`.
That value must match a `fleet.hosts` key. There is no `?host=` query.

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
  # PUBLIC_IPADDR is set by Vast; install_set.py requires it
  ```

  Put the instance public IP under `fleet.hosts` in `config.yaml`. Do not set
  per-instance docker env for the fleet key or a set id.

## On-create

Set template env `PROVISIONING_SCRIPT` to the raw GitHub URL of
`hosting/vast/provision.sh` (not `install_set.py`). Vast fetches that into
`/provisioning.sh` and runs it as a **shell** script; see
[`../README.md`](../README.md) (boot). `provision.sh` then curls
`install_set.py` from `main`; pin that second URL to a commit SHA when this
is no longer a moving target (a broken `main` would brick every new box).

## Smoke

1. `supervisorctl status`
2. `curl -sS -H "Authorization: Bearer <api_key>" http://127.0.0.1:<port>/v1/models`
