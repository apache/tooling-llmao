# RunPod runbook

RunPod pods run **our** image: stock vLLM plus supervisord and
[`provision.sh`](provision.sh) as the entrypoint. The installer is the shared
[`../install_set.py`](../install_set.py), fetched on first boot rather than
baked in, so changing it needs no new image.

Not yet run on a real pod. What is untested is listed at the end.

## Image

```bash
docker build -t <registry>/llmao-vllm-box:<tag> hosting/runpod
docker push <registry>/llmao-vllm-box:<tag>
```

The registry is still to be settled with Infra. Rebuild only when
`provision.sh` or the vLLM version in the `Dockerfile` changes. The vLLM
version is pinned (`v0.25.1`), not `:latest`, so every pod created from the
template runs the same engine.

## Template

- **Image:** `<registry>/llmao-vllm-box:<tag>`
- **Start command: empty.** The image's entrypoint is `provision.sh`; anything
  here arrives as its arguments, which it ignores with a warning.
- **Volume** mounted at `/workspace`, large enough for the HF cache.
- **Public IP** required: `provision.sh` refuses to start without
  `RUNPOD_PUBLIC_IP`, because it is how the box says which host it is.
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

Add the pod's `RUNPOD_PUBLIC_IP` to `fleet.hosts` in `config.yaml`. Order does
not matter: a pod created before its row retries until llmao knows it.

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

- The image has not been built. The `Dockerfile` assumes the vLLM image runs
  as root and has `curl` and `python3`, as its upstream Dockerfile shows.
- That the IP the pod reaches llm.apache.org **from** matches
  `RUNPOD_PUBLIC_IP`. llmao looks the box up by `X-LLMAO-Host`, so it need not,
  but a proxy that strips headers would break the lookup.
- RunPod's handling of a container whose start command is empty when the
  image sets its own `ENTRYPOINT`.
