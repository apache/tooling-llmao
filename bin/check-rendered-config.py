#!/usr/bin/env python3
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

"""Would llmao start with this config.yaml?

    uv run python bin/check-rendered-config.py /path/to/rendered-config.yaml
    uv run python bin/check-rendered-config.py config.yaml.example

Needs `uv run`, not `./bin/check-rendered-config.py`: it imports llmao, so it
needs the venv. That makes it the odd one out in bin/ -- the rest avoid
third-party imports so they run on the gateway with the system python.

Run from an llmao checkout. Point it at a config.yaml rendered from Puppet's
template and it answers the only question a p6 review really has: does the
rendered file satisfy the validators llmao runs before it serves anything?

WHY A SCRIPT AND NOT A READ-THROUGH

llmao's startup is fail-fast by design (main.py): load models.yaml, then
validate_fleet, sample_interval, capacity retention, action retention -- each
raises rather than defaulting, so a config that is wrong in a way the Puppet
types do not catch does not degrade, it refuses to boot. Several of those
checks are not local to a single key:

  providers.<name>.models    every model_name must be in the DEPLOYED
                             models.yaml, so a config.yaml can be valid
                             against one llmao revision and fatal against
                             another

  providers.<name>           a GPU host (vast, runpod) takes ONLY api_key;
                             any extra key raises. A model provider needs
                             api_key, an http(s) api_base AND a non-empty
                             models mapping

  fleet.<gpu host>           having the key under both fleet.vast and
                             providers.vast raises; under fleet.vast alone it
                             warns

Reading the template cannot tell you about the model-name coupling at all,
and an eyaml entry missing api_base renders as a blank value that looks fine
in a diff.

This runs the real validators from the real checkout, in main.py's order, and
reports every failure rather than stopping at the first.

EXIT STATUS

0 when llmao would start. 1 when it would not. The output names the key.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys

# Repo root, so `from llmao import ...` works however this is invoked.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config", help="a rendered config.yaml")
    ap.add_argument("--models", help="models.yaml to check provider models against (default: the config's models_path)")
    args = ap.parse_args()

    # Unlike the other bin/ scripts, this one cannot be dependency-free: its
    # whole purpose is to run llmao's real validators, so it needs llmao and
    # everything llmao imports. That means the venv, which `./bin/...` does
    # not get -- the shebang finds the system python. Say so rather than
    # showing a traceback for a missing module nobody asked about.
    try:
        import yaml
        from easydict import EasyDict as edict

        from llmao import actions
        from llmao.capacity import retention_days as capacity_retention_days
        from llmao.fleet import validate_fleet
        from llmao.models import load_models, validate_models, warn_deprecated
        from llmao.sampler import sample_interval
    except ImportError as e:
        print(
            f"{e.name} is not importable, so this is the system python rather than the venv.\n"
            f"\n"
            f"  uv run python bin/{pathlib.Path(__file__).name} {args.config}\n"
            f"\n"
            f"This script has to import llmao to run its validators, so it needs\n"
            f"`uv run` (or `make install` first). The other bin/ scripts are\n"
            f"dependency-free on purpose and do run with ./.",
            file=sys.stderr,
        )
        return 1

    # The deprecation warnings ARE results here -- fleet.vast.api_key still
    # working is the whole reason the deploy order is flexible, so a reviewer
    # needs to see whether it fired.
    logging.basicConfig(level=logging.WARNING, format="  warning: %(message)s")

    path = pathlib.Path(args.config)
    try:
        cfg = edict(yaml.safe_load(path.read_text(encoding="utf-8")))
    except OSError as e:
        print(f"cannot read {path}: {e}", file=sys.stderr)
        return 1
    except yaml.YAMLError as e:
        # The most likely EPP failure: a loop that renders a key with nothing
        # under it, or indentation that shifts a block.
        print(f"{path} is not valid YAML -- the template rendered badly:\n{e}", file=sys.stderr)
        return 1

    if not isinstance(cfg, dict):
        print(f"{path} did not parse as a mapping", file=sys.stderr)
        return 1

    if args.models:
        cfg.models_path = args.models

    failures: list[tuple[str, str]] = []

    def check(label, fn):
        """Run one validator, keep going on failure."""
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001 - reporting every failure is the point
            print(f"  FAIL  {label}: {e}")
            failures.append((label, str(e)))
            return None
        print(f"  ok    {label}")
        return result

    print(f"{path}\n")

    # main.py's order. load_models first: validate_fleet needs the definitions,
    # and a provider naming an undefined model fails inside it.
    definitions = check("models.yaml loads", lambda: load_models(cfg=cfg))
    if definitions is None:
        print("\ncannot continue without models.yaml", file=sys.stderr)
        return 1

    check("models.yaml is valid", lambda: validate_models(definitions))
    warn_deprecated(definitions)
    check("fleet and providers", lambda: validate_fleet(cfg, models=definitions))
    check("sample_interval_s", lambda: sample_interval(cfg))
    check("capacity_retention_days", lambda: capacity_retention_days(cfg))
    check("action_retention_days", lambda: actions.retention_days(cfg))

    print()
    if failures:
        print(f"llmao would NOT start: {len(failures)} check(s) failed")
        return 1
    print("llmao would start with this config")
    return 0


if __name__ == "__main__":
    sys.exit(main())
