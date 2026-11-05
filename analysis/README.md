# analysis/

What is done with the prompt log's records for a person: the reports
today, the content analyses as they are built. This is R&D, and it stays
out of the `llmao` package until it settles: nothing in `llmao/` or
`bin/` imports from here, and the wheel does not carry it. Design:
`docs/prompt-logging-design.md`, "The post-process" and the N9 rows.

| | what | reads |
|---|---|---|
| `prompt-report` | requests and prompt tokens by project, user, model and tool; prompt length per model; the share that stream, carry tools or end at max_tokens; failures by class; messages per request. No prompt content | the day files |
| `_core.py` | what the scripts share: `config.yaml`, the log directory, a span of days | |
| `tests/` | the scripts' tests, in the suite with `uv run pytest` | |

## Running on the gateway host

The log directory is root only, and each script's shebang runs it through
`uv`, which finds the project from the current directory. So from the
checkout, as root:

```bash
cd /opt/llmao
sudo ./analysis/prompt-report                 # the last 7 closed UTC days
sudo ./analysis/prompt-report --days 30
sudo ./analysis/prompt-report --from 2026-11-01 --to 2026-11-03 --csv
```

The log directory is `--log-dir`, else `prompt_log_dir` from `config.yaml`
in the current directory; without either the script stops. A script fails
harder than the portal on purpose: a startup error there takes a service
down, here it makes the admin fix their invocation. A script never opens a
connection to LiteLLM and never writes in the log directory.

## Not here

`bin/llmao-prompt-log rollup --json <day>` is the portal's: the
`demand-rollup` runner spawns it for each day it rolls up. It stays in
`bin/` with the other admin tools, and is given its directory by the
runner rather than reading `config.yaml`.
