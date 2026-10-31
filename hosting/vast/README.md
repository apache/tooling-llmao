# Vast.ai runbook

Custom **template** (on-start, env), Vast’s **stock vLLM container**. Do not
maintain a derived image for Vast.

`provision.sh` sets `LLMAO_HOST` from `CONTAINER_ID`, the instance id, and
curls the shared [`../install_set.py`](../install_set.py), which fetches host
JSON (`X-LLMAO-Host: $LLMAO_HOST`) and writes one Supervisor program per
model. supervisord runs `vllm serve`. There is no Python process manager.

Vast instances often reach `llm.apache.org` through a transparent HTTP proxy:
the TCP peer is not the instance and there is no `X-Forwarded-For`. Vast sets
`CONTAINER_ID` in the container (the bare instance id; `VAST_CONTAINERLABEL`
is the same with a `C.` prefix); the installer sends that as `X-LLMAO-Host`.
That value must match a `fleet.hosts` key. There is no `?host=` query. llmao
dials the instance at the address Vast's listing reports, never at its name,
so a row keyed by the instance id needs no address anywhere. A row keyed by
the public IP, as boxes provisioned before this were, still works: the
listing places it by address.

## Instance

- Launch mode: SSH (or Jupyter + SSH).
- Map **container** ports used by the set (example inventory: 8001, 8003).
  Those are `fleet.hosts` listen ports and what `GET /vllm/config` returns.
  Vast remaps them to public HostPorts; llmao does not send the public
  port to the box. With `providers.vast.api_key` in config.yaml,
  `fleet-lifecycle` fills `public_port` from one show-instances GET each
  tick.
- Disk: large enough for HF cache under `$DATA_DIRECTORY/hf-cache`
  (`DATA_DIRECTORY` is typically `/workspace`).
- Environment:

  ```bash
  FLEET_KEY=<shared-secret>   # bake into the template
  ASFQUART_URL=https://llm.apache.org
  # CONTAINER_ID is set by Vast; provision.sh requires it and passes it on as LLMAO_HOST
  ```

  Put the instance id under `fleet.hosts` in `config.yaml`. Do not set
  per-instance docker env for the fleet key or a set id.

## On-create

Set template env `PROVISIONING_SCRIPT` to the raw GitHub URL of
`hosting/vast/provision.sh` (not `install_set.py`). Vast fetches that into
`/provisioning.sh` and runs it as a **shell** script; see
[`../README.md`](../README.md) (boot). `provision.sh` then curls
`install_set.py` from `main`; pin that second URL to a commit SHA when this
is no longer a moving target (a broken `main` would brick every new box).

## Several servers on one box

List each as a row under the host in `fleet.hosts`. The installer divides
the card's memory between them and prints each server's fraction; see
[`../README.md`](../README.md) (sizing) for the rule and its limits. Two
servers of the same model need the disk for one copy of the weights.

To change the set on a running box, re-provision it; see below.

## Re-provisioning a box

Needed whenever what the box was told has changed: its rows in
`fleet.hosts`, a model's recipe in `models.yaml`, or what llmao puts in
`/vllm/config` (for example `served_model_name`).

Vast runs `PROVISIONING_SCRIPT` once, and records that it has in two places
at the root of the container: the file `/.provisioning_complete` and the
directory `/.provisioner_state/`. While either is there, a reboot provisions
nothing. To provision again, remove both:

```bash
rm -r /.provision*
```

then reboot the instance from the Vast console. On boot it finds no marker
and provisions again: it fetches the installer and this box's config, and
writes new Supervisor units.

**When the set changes to a different model**, also remove the old model's
weights before the reboot, under `$DATA_DIRECTORY/hf-cache/hub/`
(one `models--<org>--<name>` directory per model):

```bash
rm -r "$DATA_DIRECTORY"/hf-cache/hub/models--<org>--<old-model>
```

The installer credits the weights a model already has when it checks the
disk, but it does not remove the weights of a model the box no longer
serves, and on an 80GB disk two models' worth of leftovers is a refused
install. A refused install restarts the old units and says why in the
provisioning log: `install_set: will not fit -- disk: ...`.

**Do not `supervisorctl stop all`.** Vast's image runs its own services under
the same supervisord, including the Jupyter terminal you are typing in; it
stops that too, and leaves you no shell to finish with.

## Smoke

1. `supervisorctl status`
2. `curl -sS -H "Authorization: Bearer <api_key>" http://127.0.0.1:<port>/v1/models`
3. In `$DATA_DIRECTORY/logs/<name>.log`: `Model loading took … GiB` (compare
   with `vram_gb`), and the `Free memory on device` line, which gives the
   requested fraction in GiB and the KV cache actually obtained.
