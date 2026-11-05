# analysis/

What is done with the prompt log's records for a person: the reports
today, the content analyses as they are built. This is R&D, and it stays
out of the `llmao` package until it settles: nothing in `llmao/` or
`bin/` imports from here, and the wheel does not carry it. Design:
`docs/analysis-design.md`; how the records come to exist is
`docs/prompt-logging-design.md`.

| | what | reads |
|---|---|---|
| `prompt-report` | requests and prompt tokens by project, user, model and tool; prompt length per model; the share that stream, carry tools or end at max_tokens; failures by class; messages per request. No prompt content | the day files |
| `prompt-clusters` | who is sending what kind of request: the records of a span clustered by fingerprint, with figures per cluster and one exemplar each, kept in the store so a day is read once. `--show <fingerprint>` prints a cluster's exemplar in full. No model involved | the day files, the store |
| `prompt-judge` | a model's verdict per cluster, from `judge-prompt.md`: Foundation work or not, the lowest tier that would do, the activity. A dry run by default: the first request and the count, nothing sent. `--judge` sends them, one in flight through the front door with the `llmao-analysis` key, each verdict written as it returns; a cluster is judged again only when the judge prompt, the exemplar or the model changed. `--findings` is the listing alone | the store, the gateway |
| `judge-prompt.md` | the judge's fixed questions and answer shape; its hash is part of what makes a verdict stale, so rewording it re-judges every cluster | |
| `_core.py` | what the scripts share: `config.yaml`, the span of days, the fingerprint, the exemplar choice, the SQLite store | |
| `tests/` | the scripts' tests, in the suite with `uv run pytest` | |

## Words

- **Fingerprint:** the hash key over a record: the project, the person
  (the key's user, else its alias), the tool family, and the opening of
  the system prompt, its first sentence or line. Not the key, because a
  person evaluating models holds one key per model and the work is the
  same. Not more of the prompt, because a template carries its variable
  part early and a longer cut makes a cluster per file. blake2b at 8 bytes.
- **Cluster:** the records sharing a fingerprint. Pure string work over
  the day files.
- **Exemplar:** what a person or the judge reads to understand a cluster:
  the first 1,024 characters of one record's instructions and up to
  three of the cluster's last user messages, the ones whose prompt length
  is nearest the cluster's median. Chosen when the cluster is first seen,
  re-picked only when the cluster has at least doubled since.
- **Activity:** what the people behind a cluster were doing. The judge's
  answer, never the cluster's name.

## The store

`analysis_dir/prompts.db`, SQLite, four tables: `days` (which days are
in), `clusters` (what does not change per day, plus the exemplar),
`cluster_days` (the figures per cluster per day), and `verdicts` (one
per cluster, judge prompt and model, with the exemplar hash it was given).
Every row older than `analysis_retention_days` is pruned at each run, a
pruned cluster's verdicts with it; `--rebuild` forgets the days and
clusters and reads the span again, and never touches a verdict.

The store is derived from the day files, and the scan writes what the
code of the day said: the fingerprint's parts, the person's name, the
exemplar. While this is R&D, a change to any of that leaves an existing
store stale until the days are scanned again, so after pulling such a
change run `prompt-clusters --rebuild` over your span. A verdict keyed
on a fingerprint that no longer exists is dropped at the next prune.

A rebuild is cheap: about 12 seconds for a day of 4,800 records, string
work and SQLite. Judging is what takes time, about 2.5 seconds per
cluster on the gateway, one request in flight, which is why verdicts are
kept across rebuilds and only re-judged when the judge prompt, the
exemplar or the model changed.

## Running on the gateway host

The log directory is root only, and each script's shebang runs it through
`uv`, which finds the project from the current directory, so run them
from anywhere inside the checkout. The scripts find `config.yaml` beside
their own directory, the checkout root, and take its relative paths from
there, so `./analysis/prompt-judge` and `cd analysis && ./prompt-judge`
read the same file and the same store. As root:

```bash
cd /opt/llmao
sudo ./analysis/prompt-report                 # the last 7 closed UTC days
sudo ./analysis/prompt-report --days 30
sudo ./analysis/prompt-report --from 2026-11-01 --to 2026-11-03 --csv
sudo ./analysis/prompt-clusters --days 30
sudo ./analysis/prompt-clusters --show 1f2e3d4c5b6a7980
sudo ./analysis/prompt-judge --days 30                # dry run: the first request, the count
sudo ./analysis/prompt-judge --days 30 --judge --limit 20
sudo ./analysis/prompt-judge --findings
```

The judge needs `analysis_key` in `config.yaml`, a service key named
`llmao-analysis` under the `rai` project, and calls `analysis_gateway`
(the front door, `https://llm.apache.org`) with `analysis_model`
(`qwen3-8b`), so it runs the same from a developer's machine against a
copy of the store as from the host.

The log directory is `--log-dir`, else `prompt_log_dir` from `config.yaml`;
without either the script stops. A script fails
harder than the portal on purpose: a startup error there takes a service
down, here it makes the admin fix their invocation. A script never opens a
connection to LiteLLM and never writes in the log directory.

## Not here

`bin/llmao-prompt-log rollup --json <day>` is the portal's: the
`demand-rollup` runner spawns it for each day it rolls up. It stays in
`bin/` with the other admin tools, and is given its directory by the
runner rather than reading `config.yaml`.
