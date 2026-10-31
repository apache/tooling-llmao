# RunPod runbook

RunPod pods run **our** image: stock vLLM plus a vendored supervisor and
[`provision.sh`](provision.sh) as the entrypoint. The installer is the shared
[`../install_set.py`](../install_set.py), fetched on first boot rather than
baked in, so changing it needs no new image.

Not yet run on a real pod. What is untested is listed at the end.

## Image

[`.github/workflows/runpod-image.yml`](../../.github/workflows/runpod-image.yml)
builds it on every push to `main` that touches `hosting/runpod/`, and pushes
it to `ghcr.io/apache/tooling-llmao/llmao-vllm-box`, tagged with the commit.
The run's summary gives the digest-pinned reference, `...@sha256:<digest>`.

**A new image changes nothing on its own.** An admin pastes that reference
into the RunPod template; pods created after that run it. Use the digest
reference, not a tag, so the template names exactly one image.

Rebuilt only when `provision.sh`, `bin/`, `requirements.txt` or the
`Dockerfile` changes. The installer is fetched at boot and needs no rebuild.

The vLLM base is pinned by digest (`v0.25.1`), never `:latest`, so every pod
runs the same engine. Changing it is a deliberate edit to the `Dockerfile`.

Why builds are cheap: the final stage is `COPY --link` only, with supervisor
installed in a small Python stage and copied across. BuildKit adds our few
hundred KB without unpacking the vLLM image, and the registry stores the base
layers once, so each rebuild pushes only our layer. The first push copies the
base into ghcr, once. A `RUN` in the final stage would undo this; a test
fails if one appears.

`bin/supervisord` and `bin/supervisorctl` run the vendored copy, so both
commands work as they would from a package.

To check a change before pushing it, on a machine with Podman and no Docker
daemon: [podman.md](podman.md). It covers the rootless-Podman problems you
will hit and a check that builds the image on a small stand-in base.

## Template

- **Image:** `ghcr.io/apache/tooling-llmao/llmao-vllm-box@sha256:<digest>`,
  from the latest workflow run's summary
- **Start command: empty.** The image's entrypoint is `provision.sh`; anything
  here arrives as its arguments, which it ignores with a warning.
- **Volume** mounted at `/workspace`, large enough for the HF cache.
- **Expose** every listen port the box's `fleet.hosts` rows use (RunPod
  allows at most 10 HTTP ports); see Ports below.
- **Environment:**

  ```bash
  FLEET_KEY=<shared-secret>
  ASFQUART_URL=https://llm.apache.org
  # Optional:
  # DATA_DIRECTORY=/workspace            (default)
  # LLMAO_INSTALLER_URL=<raw URL>        pin install_set.py to a commit SHA
  ```

Add the pod to `fleet.hosts` in `config.yaml`, keyed by its **pod id**
(`RUNPOD_POD_ID`, shown in the console and as `id` in the API):

```yaml
fleet:
  hosts:
    "<pod id>": [[qwen3.8-27b, 8001]]
```

Not by IP: a pod whose ports are HTTP-only has no public IP, and pods can
share one. Order does not matter: a pod created before its row retries until
llmao knows it.

**llmao cannot reach a pod-id host yet.** The pod provisions and serves, but
the fleet still treats the key as an address, so its health probes fail and
it is never registered with LiteLLM. Building its address from the pod id
(`https://<podId>-<listen>.proxy.runpod.net`) is the next step.

## Boot

On every start, [`provision.sh`](provision.sh):

1. If `$DATA_DIRECTORY/.llmao/provisioned` is missing: clears
   `.llmao/conf.d`, fetches `install_set.py`, and runs it with `--no-reload`
   to write one Supervisor unit per server into `.llmao/conf.d`. On any
   failure it retries, waiting 15s and doubling to at most 5 min. It writes
   `provisioned` only after the units are in place.
2. Writes `/etc/supervisor/supervisord.conf` (socket and pidfile under
   `/run/llmao`), including `.llmao/conf.d/*.conf`.
3. `exec supervisord -n`: supervisord is PID 1 and restarts any `vllm serve`
   that dies, without touching the others.

A restart keeps only the volume, so everything that must survive is there:
the units, the marker, the installer that ran, weights (`hf-cache`) and logs.
Once provisioned, a boot fetches nothing: not GitHub, not `/vllm/config`.

Retrying covers a "will not fit" refusal too. It will not succeed until
`models.yaml` or `fleet.hosts` changes, and the pod log says why on each try.

## Changing a pod's set

The set is frozen at provisioning. To change it:

```bash
rm /workspace/.llmao/provisioned
```

then restart the pod. The next boot clears the old units and provisions
afresh. Stopping the servers first is not needed: a restart stops them.

When the new set names a different model, also remove the old model's
weights from the volume first, `/workspace/hf-cache/hub/models--<org>--<old-model>`.
The installer credits the weights a model already has when it checks the
disk, but nothing removes a model the pod no longer serves, and the
leftovers can cost the new model the room it needs.

## Ports (open)

`fleet.hosts` accepts `[model, listen, name|null, public]`. The fourth slot
exists because RunPod does not give us a Vast-style HostPort lookup, and it
**reassigns** the public TCP port on every pod recreate. Operators should not
have to keep editing that number. Once creating pods is automated, prefer one
of:

1. **HTTP proxy (stable URL).** Expose `N/http`; the public origin is
   `https://<podId>-<listen>.proxy.runpod.net`. Recreate does not change it.
   Use it as `api_base` only after proving LiteLLM and long generations
   survive that proxy (timeouts, TLS, HTTP/1.1).
2. **Direct TCP + API.** Expose `N/tcp`; RunPod REST v2 `GET /pods/{id}` →
   `portMappings` can fill `public_port` the way `vast_client` fills HostPort.
   GraphQL is deprecated; new code should use REST.

Until one of those is wired, keep the fourth slot.

## Smoke

1. `supervisorctl status`
2. `curl -sS -H "Authorization: Bearer <api_key>" http://127.0.0.1:<listen>/v1/models`
3. `$DATA_DIRECTORY/logs/<name>.log`: `Model loading took … GiB` (compare with
   `vram_gb`). The boot itself logs to the pod's container log, prefixed
   `llmao-provision:`.

## Untested until the first pod

- That the vLLM image runs as root and has `curl` and `python3`, as its
  upstream Dockerfile shows. The image builds and RunPod pulls it (the ghcr
  package is public), but no pod has provisioned from it yet.
- That `X-LLMAO-Host` reaches `/vllm/config` intact from a pod. llmao looks
  the box up by that header alone; a proxy that stripped it would break the
  lookup.
- RunPod's handling of a container whose start command is empty when the
  image sets its own `ENTRYPOINT`.
