# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Box-start: fetch set JSON, write Supervisor units, exit.

Not a process manager. supervisord owns vllm serve. Shared by every
provider; each provider's provision.sh sets the environment and runs this.

Sends X-LLMAO-Host from LLMAO_HOST, the box's public IP as its provider
reports it, so lookup works when the TCP peer is a transparent proxy (no
X-Forwarded-For). The provider's own variable is mapped to LLMAO_HOST by
its provision.sh, so this file knows no provider's names.

Run with --no-reload where supervisord is not yet running: the units are
written, and supervisord reads them when it starts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_PATH = "/vllm/config"
CONF_DIR = Path(os.environ.get("SUPERVISOR_CONF_DIR", "/etc/supervisor/conf.d"))
VLLM_BIN = os.environ.get("VLLM_BIN", "/usr/local/bin/vllm")
# The share of the free GPU memory the servers on a box divide between them.
# vLLM's own default for one server, applied to what is free.
GPU_USABLE_FRACTION = 0.9
# But never less than this left over, per card. vLLM's flag budgets its own
# allocations; the CUDA context, the buffers GPUs use to talk to each other,
# and CUDA graph capture come out of what it leaves. That is roughly fixed in
# size, not a fraction: 10% of an 80GB card is plenty, 10% of a 12GB card was
# not -- two 12GB RTX 3060s splitting qwen3-8b ran out at startup with
# 1.2GB left each. A judgement, not a measurement; see capacity planning.
GPU_RESERVE_GB = 2.0
MEMINFO = Path("/proc/meminfo")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def config_url(asfquart_url: str) -> str:
    return asfquart_url.rstrip("/") + CONFIG_PATH


HOST_HEADER = "X-LLMAO-Host"


def fetch_config(
    url: str,
    fleet_key: str,
    *,
    host: str | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise SystemExit(f"config url must be http(s), got {scheme!r}")
    headers = {"Authorization": f"Bearer {fleet_key}", "Accept": "application/json"}
    if host:
        headers[HOST_HEADER] = host
    req = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise SystemExit(f"config fetch HTTP {status} from {url}")
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"config fetch HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"config fetch failed: {exc.reason}") from exc
    if not body.strip():
        raise SystemExit("config fetch returned empty body")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"config fetch is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("config JSON must be an object")
    if "error" in data and "servers" not in data:
        raise SystemExit(f"config fetch error: {data['error']}")
    return data


def normalize_args(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, list):
        return [str(item) for item in raw]
    raise SystemExit(f"args must be a list or string, got {type(raw).__name__}")


def parse_server(raw: dict[str, Any]) -> dict[str, Any]:
    missing = [k for k in ("name", "model", "port", "api_key") if not raw.get(k)]
    if missing:
        raise SystemExit(f"server entry missing required fields: {', '.join(missing)}")
    gmu = raw.get("gpu_memory_utilization")
    mml = raw.get("max_model_len")
    vram = raw.get("vram_gb")
    disk = raw.get("disk_gb")
    kv = raw.get("kv_bytes_per_token")
    served = raw.get("served_model_name")
    return {
        "name": str(raw["name"]),
        "model": str(raw["model"]),
        "served_model_name": str(served) if served else None,
        "port": int(raw["port"]),
        "api_key": str(raw["api_key"]),
        "gpu_memory_utilization": float(gmu) if gmu is not None else None,
        "max_model_len": int(mml) if mml is not None else None,
        "vram_gb": float(vram) if vram is not None else None,
        "disk_gb": float(disk) if disk is not None else None,
        "kv_bytes_per_token": int(kv) if kv is not None else None,
        "args": normalize_args(raw.get("args")),
    }


def servers_from_config(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("servers")
    if not rows:
        raise SystemExit("config JSON has no servers")
    return [parse_server(s) for s in rows]


def _meminfo_gb(key: str) -> float | None:
    """One /proc/meminfo line (reported in kB) in GB, or None if it is not there."""
    try:
        lines = MEMINFO.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        name, _, rest = line.partition(":")
        if name.strip() == key:
            try:
                return int(rest.split()[0]) / (1024.0**2)
            except (IndexError, ValueError):
                return None
    return None


def _gpus_gb(field: str, meminfo_key: str) -> list[float] | None:
    """One nvidia-smi memory field for every GPU in GB, or None if it is unavailable.

    Capacity is read from the card rather than declared in models.yaml: a
    hand-typed figure is wrong the first time a provider supplies a different
    GPU than was ordered, and with rented instances that is a matter of when.

    A unified-memory machine (GB10) has no VRAM of its own: nvidia-smi is
    there and answers "[N/A]". The GPU draws on system memory, so that is
    what ``meminfo_key`` reads, as one GPU. No nvidia-smi at all is still an
    unknown.
    """
    try:
        out = (
            subprocess.run(
                ["nvidia-smi", f"--query-gpu={field}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            .stdout.strip()
            .splitlines()
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    try:
        return [int(line.strip()) / 1024.0 for line in out]
    except ValueError:
        unified = _meminfo_gb(meminfo_key)
        return [unified] if unified is not None else None


def gpu_count() -> int:
    """GPUs on this box; 1 when they cannot be counted."""
    return len(_gpus_gb("memory.total", "MemTotal") or [None])


def free_vram_gb() -> float | None:
    """Free VRAM on the card with least of it, or None if nvidia-smi is unavailable.

    Every server spans every GPU, an equal share on each, so the card with
    least room is the one that decides.
    """
    cards = _gpus_gb("memory.free", "MemAvailable")
    return min(cards) if cards else None


def total_vram_gb() -> float | None:
    """VRAM of the smallest card, or None if nvidia-smi is unavailable."""
    cards = _gpus_gb("memory.total", "MemTotal")
    return min(cards) if cards else None


def usable_gb(free_gb: float) -> float:
    """What the servers on a card may claim between them: what is free, less the reserve."""
    return free_gb - max(free_gb * (1 - GPU_USABLE_FRACTION), GPU_RESERVE_GB)


def kv_gb(spec: dict[str, Any]) -> float:
    """KV cache for one request at max_model_len, in GB; 0 when the model does not say.

    vLLM refuses to start rather than take a context it cannot hold, so this
    is memory a server needs before it serves anything. Two servers seen to
    fail without it: 9GB of qwen3-8b weights fit a 12GB card, the 2.8GB of
    KV cache for its 40960-token context did not.
    """
    if not spec.get("kv_bytes_per_token") or not spec.get("max_model_len"):
        return 0.0
    return spec["kv_bytes_per_token"] * spec["max_model_len"] / 1024.0**3


def free_disk_gb(path: str) -> float | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return (st.f_bavail * st.f_frsize) / (1024.0**3)


def hf_home(data_dir: str | Path) -> Path:
    """Where the servers on this box keep their weights: HF_HOME in every unit."""
    return Path(data_dir) / "hf-cache"


def cached_weights_gb(model: str, data_dir: str | Path) -> float:
    """What this model already has on disk under HF_HOME, in GB; 0 when nothing.

    Counted through the snapshots, hub/models--<org>--<name>/snapshots/*,
    whose entries are symlinks to the bytes: the older hub cache kept them
    in the model's own blobs directory, the current one keeps the shards
    content-addressed under hub/blobs/<xx>/ shared by every model, with
    only the small files left per model. Walking blobs found 0.2GB of an
    18GB model. Each target counts once, however many snapshots name it.

    A re-provision of a box that already holds its weights must not be
    refused for lacking the room to pull them again: that box asked for
    48GB free with 37GB of the same weights already there, and kept its
    old units.
    """
    snapshots = hf_home(data_dir) / "hub" / f"models--{model.replace('/', '--')}" / "snapshots"
    seen: set[tuple[int, int]] = set()
    total = 0
    try:
        files = [p for p in snapshots.rglob("*") if p.is_file()]
    except OSError:
        return 0.0
    for path in files:
        try:
            st = path.stat()  # follows the symlink
        except OSError:
            continue
        if (st.st_dev, st.st_ino) in seen:
            continue
        seen.add((st.st_dev, st.st_ino))
        total += st.st_size
    return total / (1024.0**3)


def check_fit(specs: list[dict[str, Any]], *, data_dir: str) -> list[str]:
    """Reasons these servers will not fit, or an empty list.

    Checked before writing units because the alternative is a fifteen-minute
    weights pull followed by an engine-init failure whose message does not
    mention memory. Requirements come from models.yaml; capacity is read from
    the box.

    Per card: every server spans every GPU, so each card holds 1/cards of
    each server's weights and KV cache. Against the share the servers will
    actually get, usable_gb of what is free, as apportion divides it.

    Silent when nvidia-smi is unavailable or a model declares no vram_gb --
    an unknown is not a failure, and refusing to start on a missing optional
    field would be worse than the problem.
    """
    problems: list[str] = []

    cards = gpu_count()
    sized = [s for s in specs if s.get("vram_gb")]
    weights = sum(s["vram_gb"] for s in sized) / cards
    kv = sum(kv_gb(s) for s in sized) / cards
    free = free_vram_gb()
    if sized and free is not None and weights + kv > usable_gb(free):
        names = ", ".join(s["name"] for s in sized)
        each = " each" if cards > 1 else ""
        across = f" across {cards} GPUs" if cards > 1 else ""
        problems.append(
            f"VRAM: {names} need {weights + kv:.1f}GB per card{across} "
            f"({weights:.1f}GB weights, {kv:.1f}GB KV cache at max_model_len), "
            f"{usable_gb(free):.1f}GB usable of {free:.1f}GB free{each}."
        )

    # Once per model, not per server: two servers of the same model share
    # one copy of the weights under HF_HOME. Less what is already there.
    per_model: dict[str, float] = {}
    for s in specs:
        if s.get("disk_gb"):
            per_model[s["model"]] = max(per_model.get(s["model"], 0.0), s["disk_gb"])
    cached = sum(cached_weights_gb(model, data_dir) for model in per_model)
    want_disk = max(sum(per_model.values()) - cached, 0.0)
    have_disk = free_disk_gb(data_dir)
    if want_disk and have_disk is not None and want_disk > have_disk:
        already = f" after {cached:.1f}GB already cached" if cached else ""
        problems.append(f"disk: need {want_disk:.1f}GB under {data_dir}{already}, {have_disk:.1f}GB free")

    return problems


def apportion(
    specs: list[dict[str, Any]], total_gb: float | None, free_gb: float | None = None, *, cards: int = 1
) -> list[dict[str, Any]]:
    """Divide the card between the servers on this box.

    vLLM takes its --gpu-memory-utilization fraction of the whole card
    whatever the model's size, because KV cache expands to fill it. Left at
    the default, the first server claims the card and a second cannot start.

    Each server gets its weights (``vram_gb``) plus a share of what is left,
    proportional to ``max_model_len`` so servers end up with a similar number
    of full-context slots. Without a ``max_model_len`` on every one of them
    the split is equal. A fraction set in models.yaml is kept and deducted.

    The servers share usable_gb of ``free_gb``, what is free right
    now, not of the card's size. On a unified-memory machine the operating
    system lives in the same memory, and MemAvailable already leaves it out.
    vLLM's flag is a fraction of the total, so the result is still expressed
    against ``total_gb``. Without ``free_gb`` the whole card counts as free.

    With several GPUs every server spans all of them, so the sums are per
    card: 1/cards of each server's weights, against one card's total and
    free. vLLM's flag is a fraction of each card.

    Fills in ``gpu_memory_utilization`` in place and returns the specs.
    Changes nothing when the card or a server's ``vram_gb`` is unknown, or
    when the weights alone do not fit -- vLLM's default then applies, and
    check_fit is what reports a shortfall.
    """
    if not total_gb:
        return specs
    todo = [s for s in specs if s.get("gpu_memory_utilization") is None]
    if not todo or any(not s.get("vram_gb") for s in todo):
        return specs
    pinned = sum(s["gpu_memory_utilization"] for s in specs if s.get("gpu_memory_utilization") is not None)
    usable = usable_gb(free_gb or total_gb) - pinned * total_gb
    leftover = usable - sum(s["vram_gb"] / cards for s in todo)
    if leftover <= 0:
        return specs
    if all(s.get("max_model_len") for s in todo):
        weights = [float(s["max_model_len"]) for s in todo]
    else:
        weights = [1.0] * len(todo)
    for spec, weight in zip(todo, weights, strict=True):
        gb = spec["vram_gb"] / cards + leftover * weight / sum(weights)
        # Rounded down so the fractions never add up to more than the budget.
        spec["gpu_memory_utilization"] = int(gb / total_gb * 1000 + 1e-6) / 1000
    return specs


def build_argv(spec: dict[str, Any]) -> list[str]:
    cmd = [
        VLLM_BIN,
        "serve",
        spec["model"],
        "--host",
        "0.0.0.0",
        "--port",
        str(spec["port"]),
    ]
    # The name LiteLLM asks for. Without it vLLM answers only to the Hugging
    # Face id, and every request through LiteLLM is a 404 while /health still
    # passes. Absent only from a control plane older than the field.
    if spec.get("served_model_name"):
        cmd.extend(["--served-model-name", spec["served_model_name"]])
    # Split across every GPU on the box; vLLM otherwise uses only the first.
    if (spec.get("tensor_parallel_size") or 1) > 1:
        cmd.extend(["--tensor-parallel-size", str(spec["tensor_parallel_size"])])
    if spec.get("gpu_memory_utilization") is not None:
        cmd.extend(["--gpu-memory-utilization", str(spec["gpu_memory_utilization"])])
    if spec.get("max_model_len") is not None:
        cmd.extend(["--max-model-len", str(spec["max_model_len"])])
    cmd.extend(spec["args"])
    return cmd


def program_name(spec_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", spec_name).strip("-") or "model"
    return f"vllm-{slug}"


def _ini_escape(value: str) -> str:
    return value.replace("%", "%%")


def program_ini(
    spec: dict[str, Any],
    *,
    hf_home: str,
    vllm_cache: str,
    log_dir: str,
    data_dir: str,
    hf_token: str | None = None,
) -> str:
    name = program_name(spec["name"])
    command = shlex.join(build_argv(spec))
    log = str(Path(log_dir) / f"{spec['name']}.log")
    # The startup plan: vLLM records the KV cache size that fit once its CUDA
    # graphs were captured, and later boots use it rather than estimating.
    # The estimate can miss the graphs entirely (it did on 2x12GB cards), so
    # a server may fail its first boot; supervisord's restart then reads the
    # plan. It lives in VLLM_CACHE_ROOT, on the volume so it survives restarts.
    # expandable_segments is what PyTorch's own out-of-memory error advises.
    pairs = [
        f'HF_HOME="{_ini_escape(hf_home)}"',
        f'VLLM_CACHE_ROOT="{_ini_escape(vllm_cache)}"',
        'VLLM_ENABLE_STARTUP_PLAN="1"',
        'PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True"',
        f'VLLM_API_KEY="{_ini_escape(spec["api_key"])}"',
    ]
    # Hugging Face's rate limit for unauthenticated downloads, and gated
    # repositories. Not a speed-up by itself. Optional: a control plane
    # without one hands out no token, and the pull proceeds as before.
    if hf_token:
        pairs.append(f'HF_TOKEN="{_ini_escape(hf_token)}"')
    env = ",".join(pairs)
    return (
        f"[program:{name}]\n"
        f"command={_ini_escape(command)}\n"
        f"directory={_ini_escape(data_dir)}\n"
        f"autostart=true\n"
        f"autorestart=true\n"
        f"startretries=5\n"
        f"environment={env}\n"
        f"stdout_logfile={_ini_escape(log)}\n"
        f"stderr_logfile={_ini_escape(log)}\n"
    )


def write_units(specs: list[dict[str, Any]], conf_dir: Path, *, hf_token: str | None = None) -> list[Path]:
    data_dir = Path(require_env("DATA_DIRECTORY"))
    weights = hf_home(data_dir)
    # Where the Vast vLLM template already keeps it.
    vllm_cache = data_dir / ".vllm_cache"
    log_dir = data_dir / "logs"
    weights.mkdir(parents=True, exist_ok=True)
    vllm_cache.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    conf_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for spec in specs:
        path = conf_dir / f"{program_name(spec['name'])}.conf"
        path.write_text(
            program_ini(
                spec,
                hf_home=str(weights),
                vllm_cache=str(vllm_cache),
                log_dir=str(log_dir),
                data_dir=str(data_dir),
                hf_token=hf_token,
            )
        )
        path.chmod(0o600)
        written.append(path)
        print(f"wrote {path}", file=sys.stderr)
    return written


def existing_programs(conf_dir: Path) -> list[str]:
    """The vLLM programs an earlier install wrote here. Ours, by name; nothing else supervisord runs."""
    return sorted(path.stem for path in conf_dir.glob("vllm-*.conf"))


def supervisorctl(*args: str) -> None:
    """One supervisorctl call, its failure reported and not raised.

    Stopping a program that is not running, or starting one already up, is
    an error to supervisorctl and not to us. Nor is a missing supervisorctl.
    """
    try:
        failed = subprocess.run(["supervisorctl", *args], check=False).returncode != 0
    except OSError as exc:
        failed = True
        print(f"install_set: supervisorctl could not run: {exc}", file=sys.stderr)
    if failed:
        print(f"install_set: supervisorctl {' '.join(args)} reported an error", file=sys.stderr)


def supervisorctl_update() -> None:
    subprocess.check_call(["supervisorctl", "reread"])
    subprocess.check_call(["supervisorctl", "update"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch this box's set and write a Supervisor unit per server.")
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="write the units only; supervisord is not running yet and will read them when it starts",
    )
    args = parser.parse_args(argv)

    fleet_key = require_env("FLEET_KEY")
    asfquart_url = require_env("ASFQUART_URL")
    host = require_env("LLMAO_HOST")
    url = config_url(asfquart_url)
    data = fetch_config(url, fleet_key, host=host)

    # Refuse before writing units rather than after a long weights pull. The
    # provisioning log is where this is read, so the message has to stand on
    # its own -- there is no reporting channel back to llmao yet.
    data_dir = os.environ.get("DATA_DIRECTORY", "/workspace")
    specs = servers_from_config(data)

    # A re-provision finds the last install's servers running: on a reboot
    # supervisord starts them alongside this. They hold the GPU memory the fit
    # check is about to read, so a box of two servers refused its own set and
    # kept serving the old units. Stop them first -- ours only, by name.
    # `supervisorctl stop all` would stop the provider's own services too,
    # the terminal included. With --no-reload supervisord is not running.
    old = existing_programs(CONF_DIR)
    live = bool(old) and not args.no_reload
    if live:
        supervisorctl("stop", *old)

    problems = check_fit(specs, data_dir=data_dir)
    if problems:
        for line in problems:
            print(f"install_set: will not fit -- {line}", file=sys.stderr)
        # Leave the box serving what it served, not serving nothing.
        if live:
            supervisorctl("start", *old)
        return 1

    # A server dropped from the set must not be left for supervisord to run.
    for name in old:
        (CONF_DIR / f"{name}.conf").unlink(missing_ok=True)

    cards = gpu_count()
    for spec in specs:
        spec["tensor_parallel_size"] = cards
    total = total_vram_gb()
    for spec in apportion(specs, total, free_vram_gb(), cards=cards):
        gmu = spec["gpu_memory_utilization"]
        if total and gmu is not None:
            card = f"each of {cards} cards" if cards > 1 else "the card"
            print(f"install_set: {spec['name']} gets {gmu} of {card} ({gmu * total:.1f}GB)", file=sys.stderr)
    write_units(specs, CONF_DIR, hf_token=data.get("hf_token"))
    if args.no_reload:
        return 0
    supervisorctl_update()
    # `update` restarts only the programs whose unit changed. A server whose
    # recipe did not change was stopped above all the same, and stayed
    # stopped: a box re-provisioned for one model's new flag came back with
    # the other model down. Start each one; one already up is an error to
    # supervisorctl and not to us.
    for spec in specs:
        supervisorctl("start", program_name(spec["name"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
