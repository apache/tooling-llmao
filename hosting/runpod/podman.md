# Checking the RunPod image locally with Podman

The real image is built by GitHub (see [README.md](README.md#image)). This
note is for checking a change to `provision.sh`, `bin/`, `requirements.txt`
or the `Dockerfile` **before** pushing it, on a workstation that has Podman
but no Docker daemon.

It does not build the real image. It builds the same Dockerfile on a small
stand-in base, which is enough to prove everything we add: the vendored
supervisor, the commands in `bin/`, and the boot script. What only the real
vLLM base can show is listed at the end.

## Problems you will hit, and why

**1. Rootless Podman cannot unpack images without subuid ranges.** Image
files are owned by many users (root, `shadow`, ...). Rootless Podman maps
those to a range of IDs listed for you in `/etc/subuid` and `/etc/subgid`. With
no range, the first pull fails:

```text
potentially insufficient UIDs or GIDs available in user namespace
(requested 0:42 for /etc/gshadow) ... lchown /etc/gshadow: invalid argument
```

The proper fix needs root, once:

```bash
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"
podman system migrate
```

Without root, use a separate store with the `vfs` driver and
`ignore_chown_errors`, so files keep your ID instead of failing. That is
what the commands below do. It leaves your normal Podman storage alone.

**2. `--runroot` must be under 50 characters.** Podman refuses a longer
one (`the specified runroot is longer than 50 characters`). Put it under
`$XDG_RUNTIME_DIR` (`/run/user/<uid>`), not in a deep scratch directory.
`--root`, the image store, may be anywhere.

**3. Podman 4.x does not know `COPY --link`.**

```text
COPY only supports the --chmod=<permissions> --chown=<uid:gid> and the --from=<image|stage> flags
```

`--link` is what lets BuildKit (the GitHub build) skip unpacking the vLLM
base. Here it changes nothing about the result, so the stand-in copy drops it.
Newer Buildah releases accept it; if `podman build` on yours does, skip that
step.

**4. Short names do not resolve.** `podman run llmao-box-check` fails with
`short-name ... did not resolve to an alias`. Use the full local name,
`localhost/llmao-box-check`.

**5. Do not pull the vLLM base with `vfs`.** It is many GB compressed and
larger unpacked, and `vfs` stores a full copy of every layer rather than the
differences. Check `df -h` first; with subuid ranges set up (problem 1),
Podman uses `overlay` instead, which shares layers and is the only sensible
way to build against the real base.

## The check

From the repository root. `$CHECK` is any scratch directory; delete it when
done.

```bash
CHECK=$(mktemp -d)
RUNROOT="$XDG_RUNTIME_DIR/llmao-pm"        # short: problem 2
mkdir -p "$RUNROOT"
PM="podman --root $CHECK/store --runroot $RUNROOT --storage-driver vfs --storage-opt vfs.ignore_chown_errors=true"

# A copy of the build context, on a small base instead of vLLM (problem 5),
# without --link (problem 3). Only the final FROM changes; the vendor stage
# keeps its own.
cp -r hosting/runpod "$CHECK/ctx"
SLIM=$(grep -m1 '^FROM python:' hosting/runpod/Dockerfile | awk '{print $2}')
sed -i "s|^FROM vllm/vllm-openai:.*|FROM $SLIM|; s/COPY --link /COPY /" "$CHECK/ctx/Dockerfile"

$PM build -t localhost/llmao-box-check "$CHECK/ctx"
```

**What we add is in place and runs:**

```bash
$PM run --rm --entrypoint sh localhost/llmao-box-check -c '
  ls -l /usr/local/bin/supervisord /usr/local/bin/supervisorctl /opt/llmao/provision.sh
  supervisord --version'
```

Expect all three files mode `-rwxr-xr-x` and the supervisor version from
`requirements.txt`.

**The boot script refuses a pod with no public IP:**

```bash
$PM run --rm -e FLEET_KEY=x -e ASFQUART_URL=https://x.example localhost/llmao-box-check
```

Expect `RUNPOD_PUBLIC_IP is unset` and a non-zero exit.

**A provisioned boot runs supervisord as PID 1, fetching nothing.** A fake
volume that is already provisioned, with one stand-in server:

```bash
W="$CHECK/workspace"
mkdir -p "$W/.llmao/conf.d"
date > "$W/.llmao/provisioned"
printf '[program:vllm-sleeper]\ncommand=sleep 600\nautorestart=true\n' > "$W/.llmao/conf.d/vllm-sleeper.conf"

$PM run -d --name llmao-boot -v "$W:/workspace" \
  -e FLEET_KEY=x -e ASFQUART_URL=https://x.example -e RUNPOD_PUBLIC_IP=198.51.100.8 \
  localhost/llmao-box-check
sleep 5
$PM logs llmao-boot | head -3              # "already provisioned; fetching nothing"
$PM exec llmao-boot supervisorctl status   # vllm-sleeper  RUNNING
$PM exec llmao-boot sh -c 'tr "\0" " " < /proc/1/cmdline; echo; ls /run/llmao'
$PM rm -f llmao-boot
ls "$W/logs"                               # supervisord.log, on the volume
```

PID 1 should be `python3 -m supervisor.supervisord -n -c /etc/supervisor/supervisord.conf`,
and `/run/llmao` should hold `supervisor.sock` and `supervisord.pid`.
supervisord logs `CRIT Server 'unix_http_server' running without any HTTP
authentication checking`; that is expected, since the socket is mode 0700
inside the container.

A first boot, the one that fetches the installer and retries, is covered by
`tests/test_runpod_provision.py` without any container.

**Clean up.** The store holds files owned by mapped IDs, so remove it from
inside Podman's user namespace:

```bash
podman unshare rm -rf "$CHECK"
rm -rf "$RUNROOT"
```

## What this does not show

Only the real base, in the GitHub build or on a pod, can show:

- that BuildKit accepts the `COPY --link` lines as written;
- that `vllm/vllm-openai` runs as root and has `python3` and `curl` where
  `provision.sh` expects them;
- anything about vLLM itself: weights, the GPU, the servers starting.
