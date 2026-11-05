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
| `prompt-judge` | the end result: a model's verdict per cluster and the reports an admin acts on. Its own section below | the store, the gateway |
| `judge-prompt.md` | the judge's fixed questions and answer shape | |
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

## The judge

`prompt-judge` is what the rest is for. It asks a model three questions
about each cluster, keeps the answers, and prints the reports an admin
reads: which clusters are not Foundation work, which run on a heavier
model than the work needs, and what people are doing with the gateway.

**The questions** are `judge-prompt.md`, sent as the system message with
every call: is this Foundation work (yes, no, unclear); the lowest tier
that would serve it (low, medium, high); the activity, one of eight
labels. The exemplar goes as the user message: the cluster's figures,
the head of its instructions, up to three of its user messages. The
answer is one JSON object, checked against the three answer sets, with
a sentence of reasoning.

Two of the figures carry rules the judge is told to follow. The
completion length decides the tier before the text does: a few tokens
out is a label and low, long completions over agent work are high, and
in doubt the higher tier, since underestimating the work is the costly
mistake. The paired count, how many of the cluster's requests were the
same request sent to more than one model, marks a person comparing
models, which is Foundation work whatever the requests contain. Three
judges on one day agreed on Foundation work 23 of 23 times and on the
activity 19 to 22 of 23, but on the tier only 8 to 13 of 23 before
those rules were written: a strong model finds everything easy.

**Several judges** is the normal operation: `analysis_models` in
`config.yaml` lists them, and every cluster gets a verdict from each.
A verdict is kept per cluster, judge prompt and model, with the hash of
the exemplar it was given. Any of the three changing makes it stale:
rewording the questions re-judges every cluster, a re-picked exemplar
re-judges one, and a model added to the list judges everything afresh
while the others' verdicts stay. Each verdict is its own transaction,
written as it returns, so a run that stops at 200 of 1,000 keeps 200
and resumes at 201. The answers are stored as integers beside the
judge's words, so the consensus is plain aggregation.

**The consensus** is a view in the store, `consensus`, over the verdicts
that still hold (`current_verdicts`, those given the cluster's present
exemplar): per cluster and judge prompt, how many judges answered, the
lowest answer on Foundation work (one "no" is a "no", for an admin to
look at), the highest tier (a colleague's point: underestimating the
work is the costly mistake), the majority activity, and how many
distinct answers each question drew. The listing reads the view, and so
can anyone with the `sqlite3` command.

**Running it.** A dry run by default: the first request it would send
and how many clusters each judge still has to answer, nothing sent.
`--judge` sends them, one request in flight through the front door
(`analysis_gateway`) with the `llmao-analysis` service key
(`analysis_key`), judge after judge. `--model` names one or more judges
instead of the list; `--limit` caps the calls per judge in one run.
Clusters from the master key are the operators' smoke tests and are
never judged; the listing marks them with a footnote.

**The reports**, printed after judging or alone with `--findings`:

- *Verdicts:* every cluster of the span with the consensus answers and
  the number of judges; a `*` marks an answer the judges did not all
  agree on.
- *Tier:* for each judged cluster, the consensus tier, the model that
  tier recommends, the models the cluster used, and how many of its
  requests went to a heavier model than the work needs (the saving to
  suggest) or a lighter one (the risk). Heaviest excess first.
  `analysis_tiers` in `config.yaml` says which model serves which tier.
- *Compare:* with `--compare`, each judge's answers side by side on the
  clusters where they differ, for weighing the judges against each
  other.
- *Reasons:* with `--reasons`, each judge's sentence of reasoning,
  cluster by cluster.

`--json` prints the clusters with their verdicts instead.

**Startup checks.** Every model a run mentions, the judges and all of
the tier map, must be defined in `models.yaml`, or the script stops and
names the unknown ones. And a judge must run only on our boxes: a model
that a paid provider in `config.yaml` also serves is refused, since the
gateway falls back to the provider when our boxes cannot answer, and a
judge's request carries other projects' prompts.

**What a day costs.** The scan runs every day and touches every record;
the judges answer only for clusters without a verdict from this judge
prompt, this model and this exemplar. A fingerprint is the project, the
person, the tool and the opening of the instructions, and none of those
change from one day to the next for a bot or an agent, so in steady
state a day brings a handful of new clusters and the judges a handful
of calls, often none. The full bill, clusters times judges at about 2.5
seconds a call, is paid only after the judge prompt changes.

## The store

`analysis_dir/prompts.db`, SQLite, four tables: `days` (which days are
in), `clusters` (what does not change per day, plus the exemplar),
`cluster_days` (the figures per cluster per day, the paired count among
them), and `verdicts` (one
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
A change to the tables themselves has no migration: delete the store
and start again. The views are recreated at every open.

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
(the front door, `https://llm.apache.org`) with each of
`analysis_models`, so it runs the same from a developer's machine
against a copy of the store as from the host.

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
