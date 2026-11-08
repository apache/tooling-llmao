# analysis/

What is done with the prompt log's records for a person: the reports
that need no content, the clusters, the judges' verdicts on them. This
is R&D, and it stays out of the `llmao` package until it settles:
nothing in `llmao/` or `bin/` imports from here, and the wheel does not
carry it. Design: `docs/analysis-design.md`; how the records come to
exist is `docs/prompt-logging-design.md`.

One entry point, `prompts`, with a command per question. Two of them
write the store, the rest read it or the day files:

```
prompts build        cluster the span's day files into the store           writes
prompts judge        the judges' verdicts on the clusters that lack one     writes (dry run; --judge sends)

prompts report       who asks what of which model, no content read         the day files
prompts clusters     who is sending what kind of request, over the span    the store
prompts show <fp>    one cluster, exemplar in full                          the store
prompts verdicts     the judges' consensus per cluster, where they split   the store
prompts tiers        what was used against what the judges say would do   the store
```

`prompts --help` is the map, `prompts <command> --help` each one's
options. Every command's arguments are defined in `prompts`; the modules
do the work:

| | what |
|---|---|
| `prompts` | the entry point: every command's arguments, and which module runs it |
| `_core.py` | what the commands share: `config.yaml`, the span of days, the fingerprint, the exemplar choice, the store and its views, the judge's call, the tier report, the table printer |
| `_clusters.py` | `build`, `clusters`, `show` |
| `_judge.py` | `judge`, `verdicts`, `tiers` |
| `_report.py` | `report` |
| `judge-prompt.md` | the judges' fixed questions and answer shape |
| `tests/` | the tests, in the suite with `uv run pytest` |

## Running on the gateway host

The log directory is root only, and the entry point's shebang runs it
through `uv`, which finds the project from the current directory, so run
it from anywhere inside the checkout. It finds `config.yaml` beside its
own directory, the checkout root, and takes the config's relative paths
from there, so `./analysis/prompts` and `cd analysis && ./prompts` read
the same file and the same store. As root:

```bash
cd /opt/llmao
sudo ./analysis/prompts build --days 30                 # the days the store lacks, read once
sudo ./analysis/prompts judge --days 30                 # dry run: what --judge would do
sudo ./analysis/prompts judge --days 30 --judge         # send them, judge after judge
sudo ./analysis/prompts verdicts --days 30 --compare    # the consensus, and where the judges split
sudo ./analysis/prompts tiers --days 30
sudo ./analysis/prompts show 1f2e3d4c5b6a7980
sudo ./analysis/prompts report --from 2026-11-01 --to 2026-11-03 --csv
```

The commands share `--days`, `--from` and `--to` for the span of closed
UTC days. The log directory is `--log-dir`, else `prompt_log_dir` from
`config.yaml`; the store is `--analysis-dir`, else `analysis_dir`. A
missing file or key stops the command: a script fails harder than the
portal on purpose, since a startup error there takes a service down and
here it makes the admin fix their invocation. Nothing here opens a
connection to LiteLLM except the judge's model calls, and nothing writes
in the log directory.

The judge needs `analysis_key` in `config.yaml`, a service key whose
purpose is `llmao-analysis`, under the `rai` project (the analysis
recognises the judge's own requests by that purpose and never clusters
them), and calls `analysis_gateway`
(the front door, `https://llm.apache.org`) with each of
`analysis_models`, so it runs the same from a developer's machine
against a copy of the store as from the host.

## Words

- **Fingerprint:** the hash key over a record: the project, the person
  (the key's user, else its purpose), the tool family, and the opening of
  the system prompt, its first sentence or line. Not the key, because a
  person evaluating models holds one key per model and the work is the
  same. Not more of the prompt, because a template carries its variable
  part early and a longer cut makes a cluster per file. blake2b at 8 bytes.
- **Cluster:** the records sharing a fingerprint. Pure string work over
  the day files.
- **Exemplar:** what a person or the judges read to understand a
  cluster: the first 1,024 characters of one record's instructions and
  up to three of the cluster's last user messages, the ones whose prompt
  length is nearest the cluster's median. Chosen when the cluster is
  first seen, re-picked when the cluster has at least doubled since, or
  when the day it was picked from has left the store.
- **Activity:** what the people behind a cluster were doing. The judges'
  answer, never the cluster's name.

## The judges

`judge`, `verdicts` and `tiers` are what the rest is for: a model's
answer to three questions about each cluster, kept, and the reports an
admin reads from the answers: which clusters are not Foundation work,
which run on a heavier model than the work needs, and what people are
doing with the gateway.

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
same request sent to more than one model, is a sign of a person
comparing models, to weigh with the content. Three judges on one day
agreed on Foundation work 23 of 23 times and on the activity 19 to 22
of 23, but on the tier only 8 to 13 of 23 before those rules were
written: a strong model finds everything easy.

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
distinct answers each question drew. `verdicts` reads the view, and so
can anyone with the `sqlite3` command.

**`judge`** is a dry run by default: the clusters that lack a verdict
and which judges each lacks, the calls `--judge` would make and about
how long they take, and where they go; nothing sent. `--request` adds
the first request as JSON. `--judge` sends them, one request in flight through the front door
(`analysis_gateway`) with the `llmao-analysis` service key
(`analysis_key`), judge after judge, and ends with a summary of what
the run produced and the commands to run next. `--model` names one or
more judges instead of the list; `--limit` caps the calls per judge in
one run. Clusters from the master key are the operators' smoke tests
and are never judged; the listings mark them with a footnote.

**`verdicts`** lists every cluster of the span with the consensus
answers and the number of judges; a `*` marks an answer the judges did
not all agree on. `--compare` adds each judge's answers side by side on
the clusters where they differ, `--reasons` each judge's sentence of
reasoning, `--json` the clusters with their verdicts as JSON.

**`tiers`** sets, for each judged cluster, the consensus tier against
the models the cluster used: the model that tier recommends, and how
many of the cluster's requests went to a heavier model than the work
needs (the saving to suggest) or a lighter one (the risk). Heaviest
excess first. `analysis_tiers` in `config.yaml` says which model serves
which tier.

**Startup checks.** Every model a run mentions, the judges and all of
the tier map, must be defined in `models.yaml`, or the command stops and
names the unknown ones. And a judge must run only on our boxes: a model
that a paid provider in `config.yaml` also serves is refused, since the
gateway falls back to the provider when our boxes cannot answer, and a
judge's request carries other projects' prompts.

**What a day costs.** `build` touches every record of the days it
reads; the judges answer only for clusters without a verdict from this
judge prompt, this model and this exemplar. A fingerprint is the
project, the person, the tool and the opening of the instructions, and
none of those change from one day to the next for a bot or an agent, so
in steady state a day brings a handful of new clusters and the judges a
handful of calls, often none. A cluster's count is the sum of its
retained days, so a steady cluster doubles at longer and longer
intervals and, once the prune caps its count, never again; what keeps
its exemplar from freezing is the prune itself: when it takes the day
the exemplar was picked from, the cluster gets a new exemplar the next
day it is seen, and a new verdict, once per retention. The full bill,
clusters times judges at about 2.5 seconds a call, is paid only after
the judge prompt changes.

## The store

`analysis_dir/prompts.db`, SQLite, four tables: `days` (which days are
in), `clusters` (what does not change per day, plus the exemplar),
`cluster_days` (the figures per cluster per day, the paired count among
them), and `verdicts` (one per cluster, judge prompt and model, with the
exemplar hash it was given); and the two views above. Every row older
than `analysis_retention_days` is pruned at each `build`, a pruned
cluster's verdicts with it; `build --rebuild` forgets the days and
clusters and reads the span again, and never touches a verdict.

The store is derived from the day files, and `build` writes what the
code of the day said: the fingerprint's parts, the person's name, the
exemplar. While this is R&D, a change to any of that leaves an existing
store stale until the days are read again, so after pulling such a
change run `prompts build --rebuild` over your span. A verdict keyed on
a fingerprint that no longer exists is dropped at the next prune. A
change to the tables themselves has no migration: delete the store and
start again. The views are recreated at every open.

A rebuild is cheap: about 12 seconds for a day of 4,800 records, string
work and SQLite. Judging is what takes time, about 2.5 seconds per
cluster on the gateway, one request in flight, which is why verdicts are
kept across rebuilds and only re-judged when the judge prompt, the
exemplar or the model changed.

## Not here

`bin/llmao-prompt-log rollup --json <day>` is the portal's: the
`demand-rollup` runner spawns it for each day it rolls up. It stays in
`bin/` with the other admin tools, and is given its directory by the
runner rather than reading `config.yaml`.
