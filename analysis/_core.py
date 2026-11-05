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

"""What the analysis scripts share: where the records are, which days to read.

The analyses of the prompt log are R&D and live in this directory, outside
the llmao package (docs/prompt-logging-design.md, "The post-process"):
nothing in llmao/ or bin/ imports from here. The scripts import
llmao.prompt_log for the records themselves and this module for what they
need around them.

config.yaml is read here, from the current directory, because a script runs
at a shell on the gateway host with no portal around it. The portal reads
its config once in main.py and nothing in llmao/ opens the file.
"""

from __future__ import annotations

import datetime as dt
import pathlib

CONFIG = pathlib.Path("config.yaml")


def configured(key: str, config: pathlib.Path = CONFIG) -> pathlib.Path:
    """A required path from config.yaml in the current directory.

    A missing file or key stops the script with a message. A script can
    fail harder than the portal: a startup error there takes a service
    down, here it makes the admin fix their invocation.
    """
    if not config.exists():
        raise SystemExit(f"{config}: not found; run from the checkout, or give the path on the command line")
    import yaml

    raw = (yaml.safe_load(config.read_text()) or {}).get(key)
    if not raw:
        raise SystemExit(f"{config}: {key} is required")
    return pathlib.Path(str(raw))


def log_dir(config: pathlib.Path = CONFIG) -> pathlib.Path:
    """prompt_log_dir: where the proxy's hook writes the day files."""
    return configured("prompt_log_dir", config)


def span(*, days: int = 7, start: dt.date | None = None, end: dt.date | None = None) -> list[dt.date]:
    """The UTC days to read: the last ``days`` closed ones, or ``start`` to ``end``, in order.

    ``end`` defaults to yesterday, the last closed day; ``start`` to ``days``
    before it. A start after its end is a ValueError.
    """
    end = end or dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    start = start or end - dt.timedelta(days=days - 1)
    if start > end:
        raise ValueError(f"span starts {start}, after it ends {end}")
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
