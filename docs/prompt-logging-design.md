# Prompt log: design

A record of what flows through the gateway, kept so that we can understand
how it is used: who asks, what shape the requests have, which models and
tools, how that changes over time. It is analysed by a separate process,
after the fact. Nothing in the request path analyses anything.

## Decisions

Made 2026-11-01. Each is a choice, recorded so it is not re-derived.

1. **Everything that flows through LiteLLM is recorded.** Every call type
   the proxy serves (chat, completions, embeddings, the Responses API, the
   Anthropic messages API, and the rest), streamed or not, succeeded or
   failed. A request we refused for budget or rate limit is still use.
2. **The prompt is recorded; the response is not.** We want to retain and
   analyse usage, not results. What the model produced (text, reasoning,
   tool-call arguments) is never written. Token counts, timing and the
   finish reason are, because they describe the use.
3. **Analysis is a separate process** that reads the files. The proxy writes
   and does nothing else with them.
4. **Users are told.** A Terms of Service page, linked from the footer beside
   the Privacy Policy, says that prompts are logged, what is kept, who can
   read it and for how long. Every user has signed an ICLA with the
   Foundation; the terms are a layer specific to this service on top of
   that.
5. **The proxy does the recording,** through LiteLLM's callback mechanism in
   `litellm_hooks.py`, the file LiteLLM loads. It is switched on and
   configured in `litellm.yaml`, which Puppet writes and passes to the
   proxy. Not through the environment: `litellm.env` is on its way out.
6. **The files live on `/x1`,** the ASF's standard data volume, which the
   Foundation's backup rules already cover.
7. **Misconfiguration stops the proxy at startup.** The llmao rule: a
   service that cannot do what its config says does not start.
8. **The numbers the post-process needs are in `config.yaml`,** beside the
   other retention settings.
9. **Demand rows feed capacity planning.** One row per model per hour,
   in the store the capacity page already reads, built once a day from a
   single reader of LiteLLM's spend log; everything LiteLLM has that the
   rows might need moves into the store then. LiteLLM's spend data stays
   the truth for tokens and money; the log fills only the gaps, and one
   reconciliation figure compares the two.
10. **No cron.** One llmao runner, daily, does the rollup and spawns the
    log's post-process as a subprocess for the same day. Scheduling is
    config.yaml, not Puppet.
11. **Parked: a Usage Analysis page** for admins in place of reading the
    script's output. The demand rows are shaped as that page's input now,
    so it is a second reader later, not a second pipeline.
12. **loguru writes the files.** The hook hands each record to a loguru
    sink, which owns the day file, the rotation at UTC midnight, the gzip
    of the finished day and the deletion past retention, all in its own
    writer thread. Nothing of ours opens, compresses or deletes a log file.
    Verified in 0.7.3: rotation takes a `datetime.time` with a `tzinfo`,
    the file name takes `!UTC`, a restart mid-day continues the same file
    (it stamps the creation time in an xattr), and a write failure is
    printed to stderr and dropped. Retention goes by the compressed file's
    mtime, so N days keeps about N+1; the terms say "approximately".

## Development plan

This design is under review as a whole. The reviewers may change the
record, the file layout, the writer, the retention, or say no to parts
of it, and nothing below assumes the review comes back as written. The
work sorts into what the review cannot invalidate and what it can.

The data split does the sorting. Of what the record holds, LiteLLM's
own spend log already keeps most of it, per request (the LiteLLM column
in the record table): tokens, model, deployment, provider, key, user,
project, end user, call type, status and error class, the timings, and
the user agent. That is data we gather from LiteLLM rather than manage
ourselves, and nearly the whole demand row comes from it. The new data,
which only our log can hold, is the stream flag, the parameters and the
tools in the request, the finish reason, the messages per request, and
the content. The review is about the new data and the machinery that
keeps it.

### First: the hook apparatus, with a probe inside

The product needs a LiteLLM hook whatever the review says, and the
apparatus is the stable part: the `callback_settings` section, the
module-level build-once, fail-at-start, the registration in
`litellm.yaml`, and the three methods LiteLLM calls (pre-call, success,
failure). Its first body is a probe, not the writer. It prints what it
sees for a request sent with `no-log` and with
`turn_off_message_logging` in each place a client could put them (the
body, `extra_body`, metadata, headers), against the local proxy and the
mock backend. That answers whether the events fire at all, whether
`messages` in the standard payload is redacted, and whether the pre-call
hook sees the parameter early enough to strip it. The answer goes into
"No opt-out" and decides whether the strip is needed. A user who can opt
out would undo the whole design, so this comes before anything else. The
apparatus keeps its tests in `tests/test_litellm_hooks.py`; the probe
body is thrown away.

| Step | What | State |
|---|---|---|
| H1 | The apparatus, with the probe as its body; `litellm.yaml.example` names the callback | done 2026-11-01 |
| H2 | The probe's answers written into "No opt-out"; the pre-call strip (N5, pulled forward: two switches reached us); `bin/check-no-log.py` kept for upgrades. The probe body stays until N3 replaces it | done 2026-11-01 |

### Track L: gathered from LiteLLM, for capacity planning

This track is capacity planning's, not the log's. The demand rows are
what `/admin/capacity` needs to answer its four questions, and LiteLLM
already holds the data for nearly all of them, so this work is wanted
however prompts end up being logged, and whether or not they are.
Nothing here waits for the review.

| Step | What | State |
|---|---|---|
| L1 | The real daily prompt-token figure, from the spend log and the capacity rows, replaces the Size paragraph's assumption | done 2026-11-02 |
| L2 | The single reader of `/spend/logs/v2` in `llmao/proxy/client.py`, beside the daily reader: one closed UTC day, page by page | done 2026-11-02 |
| L3 | The demand rows: key layout, write, read, prune in `CapacityStore` and `CapacityPruner`; the fold from a day of spend rows to 24 demand rows per model, carrying everything LiteLLM has that the rows might need; the `demand-rollup` runner, daily at 01:00 UTC and at start, which rolls up every closed day the store lacks | done 2026-11-02 |
| L4 | "What was asked of it" on each model's card on `/admin/capacity`: requests per day, tokens, projects, tools, prompt length, refusals by reason, latency. The LiteLLM side of the reconciliation is the rows' request count; the figure itself appears with the log side (N4) | done 2026-11-02 |

### Track N: the new data, after review

| Step | What | Waits for |
|---|---|---|
| N1 | The record: which fields, and the content (`messages`, `params`, `finish_reason`) | done 2026-11-03 (`llmao/prompt_log.py`) |
| N2 | The writer: the loguru sink or what replaces it; directory, naming, rotation, compression, retention | done 2026-11-03 (the sink in `litellm_hooks.py`) |
| N3 | The hook's real body: payload to record, the write; the dependency in `pyproject.toml` | done 2026-11-03; the probe is gone |
| N4 | The reader: one day file, plain or compressed, to `rollup --json <day>`; `rollup_day` in the module, which spawns it; the runner calls it for each day it rolls up and folds the log-only fields and the log side of the reconciliation into the same rows | N2 |
| N5 | The pre-call strip of `no-log` and the two redaction headers | done with H2 |
| N6 | Terms of Use page and footer link: done 2026-11-01, with the Responsible AI Initiative's terms folded in; the words follow the review. STATUS.md data handling is rewritten at N8, when the log exists; the retention number stays "approximately 90 days" until decided | decision 4, "Access", "The terms page" |
| N7 | The reports in `bin/llmao-prompt-log` | N4 |
| N8 | Switched on: p6 (`init.pp` directory and parameters, `litellm.yaml.epp` callback settings, the proxy unit's `ReadWritePaths`) | all of the above |
| N9 | Which content analyses, and where they run | open |
| N10 | Usage Analysis page | parked |

### Step numbers used elsewhere

Earlier drafts numbered the steps 1 to 8; the text below still says
"step 6" for the `no-log` check (now H1).

## How the proxy records

LiteLLM calls a `CustomLogger`'s `async_log_success_event` and
`async_log_failure_event` once per request. Verified in 1.105.0, the pinned
version:

- **Off the request path.** The proxy's async wrapper hands the success
  handler to its logging worker (`litellm_core_utils/logging_worker.py`): a
  queue of up to 50,000 coroutines drained by one background task, each
  given 20 seconds. The reply has already gone to the client. This is the
  opposite of the 2026-07 incident that `litellm.yaml.epp` still warns
  about, where a spend-log write ran inline through Prisma and wedged the
  request path. The worker shares the event loop, so the write itself must
  still be quick: one JSON dump and a put on loguru's queue; the append,
  and once a day the gzip, happen in loguru's writer thread.
- **Streaming fires once, at the end.** LiteLLM assembles the stream and
  calls the success event with the complete payload. Since we keep no
  response, a streamed request and a plain one produce the same record,
  apart from `stream` and the time to first token.
- **Failures fire too**, with the same payload plus the error's class and
  code. Budget, rate limit, upstream errors, malformed requests.
- **The payload** is `kwargs["standard_logging_object"]`, LiteLLM's
  `StandardLoggingPayload`. The record is built from it alone, never from
  the raw request, so a change in how the proxy parses requests does not
  change what we write.

**Why not LiteLLM's own storage.** `store_prompts_in_spend_logs` would put
the prompt in Postgres with zero code, but it truncates every string at
2,048 characters, stores the response too, and ties the data to LiteLLM's
schema and its retention. A file we own is simpler to analyse, to ship
elsewhere, and to delete.

### Configuration, and failing at start

`litellm.yaml` has a top-level `callback_settings` section, keyed by
callback name, which LiteLLM stores on its module before it loads any
callback:

```yaml
callback_settings:
  prompt_log:
    dir: /x1/llmao/prompt-log
    retention_days: 90

litellm_settings:
  callbacks: ["litellm_hooks.empty_completion_guard", "litellm_hooks.prompt_log"]
```

LiteLLM resolves `litellm_hooks.prompt_log` by importing the module and
reading the name. The module builds the instance at that moment (a
module-level `__getattr__`, built once and kept), from
`litellm.callback_settings["prompt_log"]`, and raises if the section is
missing or the directory cannot be written. The proxy then does not start,
and the message names the setting. A proxy that serves while silently
dropping a log the terms promise is the worse failure, and a start-up
failure is caught by the deploy, not weeks later. `import litellm_hooks`
on its own, as the tests do, builds nothing.

### Not recorded

- **Requests that never reach a callback.** An invalid or expired key is
  rejected before LiteLLM builds a logging object. Those are in the proxy's
  own log, with no prompt. Accepted: they are not use of a model.
### No opt-out

Users cannot opt out of their prompts being recorded. Logging is a
condition of use, stated in the terms, and no request parameter switches
it off.

LiteLLM has per-request switches that would otherwise do exactly that.
Measured on the pinned 1.105.0, through a local proxy with the hook
loaded and `bin/check-no-log.py` sending one request per switch
(2026-11-01):

| A client sends | What happened |
|---|---|
| `"no-log": true` in the body, streamed or not | every logging callback skipped, ours included; the pre-call hook still ran |
| `"no-log": true` inside `metadata` | nothing; logged normally |
| the header `litellm-enable-message-redaction: true`, or `x-litellm-enable-message-redaction: true` | the event fired with every message replaced by `redacted-by-litellm` |
| `turn_off_message_logging` in the body, `metadata` or `litellm_metadata` | rejected by the proxy with a 401 before any hook ran |
| the header `x-litellm-disable-callbacks` | ignored; it needs a premium licence |

So two switches reach us. The hook's `async_pre_call_hook` removes the
`no-log` field and both redaction headers from every request before
LiteLLM reads them, and the request is logged like any other. Nothing is
rejected and the client is not told; the switches simply have no effect
here. The proxy's own log gets a WARNING naming the user, the key, the
team and what was sent, so an attempt is visible to the admins even
though it is invisible to the client. `litellm.yaml` also sets
`global_disable_no_log_param: true`,
LiteLLM's own switch for `no-log`; the strip is the one we control, and
holds on its own (measured with the setting off).

`bin/check-no-log.py` is kept for the next LiteLLM upgrade: it runs the
same cases against any live proxy and reads the log back. A case that
starts failing is a hole in the terms, and a case the proxy stops
rejecting must then be logged like the rest.

## The record

One JSON object per line. Field names are ours; the source is the standard
payload.

The LiteLLM column says whether LiteLLM's own spend log
(`LiteLLM_SpendLogs`, one row per request, checked against the 1.105.0
schema) already keeps the field. Those are gathered from LiteLLM by
track L; the rest is the new data the log exists for.

| Field | From | LiteLLM | Meaning |
|---|---|---|---|
| `ts`, `duration_s`, `ttft_s` | `endTime`, `startTime`, `completionStartTime` | yes | when it finished (UTC, ISO 8601), how long it took, time to first token (`null` unless streamed) |
| `call_id`, `trace_id` | `litellm_call_id`, `trace_id` | the call id | the id the client got in `x-litellm-call-id`, which also keys LiteLLM's spend row; the trace groups retries and fallbacks |
| `call_type` | `call_type` | yes | LiteLLM's name for the endpoint (`acompletion`, `aembedding`, ...) |
| `status`, `error` | `status`, `error_information` | yes | `success` or `failure`; on failure the class, code and provider, not the message (an error message can quote the prompt) |
| `model`, `deployment`, `api_base`, `provider` | `model_group`, `model`, `api_base`, `custom_llm_provider` | yes | what the user asked for; what served it; which box or provider |
| `stream` | `stream` | no | |
| `tokens` | `prompt_tokens`, `completion_tokens`, `total_tokens` | yes | |
| `finish_reason` | the response | no | the one word we keep from the response: it is how truncation and tool use show up in aggregate. **The exception to decision 2; drop it if that reads as a result.** |
| `key` | `metadata.user_api_key_*` | yes | `hash`, `alias`, `user` (the ASF uid for a personal key, `null` for automation), `project` (the team alias) |
| `end_user` | `end_user` | yes | the request's `user` field, when a tool sends one |
| `user_agent`, `client_headers` | `user_agent`, `metadata.requester_custom_headers` | the user agent | which tool, and any `x-` headers it sent |
| `params` | `model_parameters` | no | the request minus the prompt: `temperature`, `max_tokens`, `tools` (the definitions an agent put in front of the model), `tool_choice`, `response_format`, `reasoning_effort`, `chat_template_kwargs`, ... |
| `messages` | `messages` | no | the prompt as LiteLLM received it: the messages list for chat, the string for completions, the input for embeddings. LiteLLM has the column, switched off |

`requester_ip_address` is left out. LiteLLM's spend log already holds it
(populated with the client's address in production, measured 2026-11-02,
so the vhost does pass it through), and nothing planned reads it from
the record.

### Size

A line is the size of the prompt. Agentic tools resend the whole
conversation every turn, so one coding-agent turn at 100k tokens is about
400 KB.

Measured on production over the week to 2026-11-01, from the spend log
and the capacity rows: between 900 and 13,600 model calls a day, and
between 36 million and 164 million prompt tokens a day. The median
prompt was 580 tokens and the 95th percentile 92,000, which is the
agents resending their context. At four bytes a token that is 150 MB to
650 MB a day uncompressed; gzip takes prose and code down five to eight
times, so 20 to 130 MB a day, and 90 days is 2 to 12 GB.

The gateway host has no separate `/x1` mount yet; its root volume is 100
GB with 88 GB free (2026-11-02). Whether `/x1` becomes a mount is
Puppet's concern, not the application's. **To do, as users grow:** disk
usage needs watching, through a DataDog monitor on the host and/or a
free-space figure on `/admin` beside the LiteLLM version check.

## Files

- **Directory:** `/x1/llmao/prompt-log/`, root-owned, mode 0700, created
  by Puppet (a `file` resource in `init.pp`) and added to the proxy
  service's `ReadWritePaths`. The hook does not create it: in production
  a missing directory means the data volume is not mounted, and creating
  one would log to the root disk without anyone noticing. The hook
  refuses to start instead (decision 7); the check is ours, since loguru
  would otherwise create the directory itself. The same Puppet value is
  rendered into `litellm.yaml` (for the hook) and `config.yaml` (for the
  post-process), so there is one place to change it. The post-process
  only reads there. Both the portal and the proxy run as root (the
  pipservice define's default, which the node yaml does not override; the
  proxy unit sets no `User=`), so the files need no group or ACL.
- **One file per UTC day,** `prompts-2026-11-01.jsonl`, mode 0600 through
  an `opener`, written by loguru's sink: the name pattern is
  `prompts-{time:YYYY-MM-DD!UTC}.jsonl` and the rotation is
  `datetime.time(0, 0, tzinfo=utc)`, so the day boundary is UTC whatever
  the host's zone. The sink holds the file open and appends from its own
  thread; a restart mid-day reopens the same file.
- **A write that fails** (disk full, permission lost) is caught by loguru,
  printed to stderr (the journal) and the record is dropped. It never
  reaches the request. Disk full is an incident, not something the hook
  manages.
- **At rotation,** the first write after UTC midnight, the sink gzips the
  finished day and deletes files past retention, in the writer thread.
  zlib releases the GIL while compressing, so the proxy keeps serving;
  it is CPU on the gateway host once a day. Compression is gzip: `zcat`
  and Python's `gzip` read it, and the reports read both forms. A quiet
  night delays the rotation until the first request of the new day,
  which changes nothing: the file is named for the day it started.
- **Retention** is `prompt_log_retention_days` in `config.yaml` and the
  same number in `litellm.yaml` under `callback_settings.prompt_log`,
  both from one Puppet value. The number is open; 90 matches the others.
  loguru measures from the compressed file's mtime, so N keeps about N+1
  days, and the terms say "approximately". The files on `/x1` are also in
  the Foundation's backups, under the backup policy's own retention; the
  terms should say so.

## Access

- Readable by root on the gateway host. Site admins read them over `sudo`.
- No page in llmao shows a prompt. What the analysis produces is aggregates.
- The terms say this: what is recorded, who can read it, how long it is
  kept.

## The post-process

Two files with two names, because they are two concerns:

- **`llmao/prompt_log.py`, the module:** where the work lives. The
  record's field names, the file name pattern, reading a day file plain
  or compressed, the rollup of a day, and `rollup_day(day)`, which the
  runner calls: it spawns the tool as a child for that day and parses
  the JSON it prints. It imports nothing heavy, not even loguru,
  because the hook imports it too, inside the proxy process, for the
  record definition and the file naming.
- **`bin/llmao-prompt-log`, the tool:** argparse over the module, named
  like the other admin tools in `bin/` (`llmao-fleet`, `llmao-budget`).
  It never opens a connection to LiteLLM, and never writes in the log
  directory.

The dependency runs one way: the tool, the hook and `capacity.py` (for
`rollup_day`) import the module, and the module imports none of them.

The tool has two subcommands:

1. **`rollup --json <day>`:** read that day's file, plain or compressed,
   and print one JSON document to stdout: the log-only fields for each of
   the day's 24 hours per model, and the day's request count per model
   for the reconciliation. No cursor: a day is the unit, and the runner
   decides which days are missing.
2. **Reports,** for an admin at a shell on the host. The first ones,
   chosen because they need no reading of prompt content: requests and
   prompt tokens per day by project, by user, by model and by user agent;
   the distribution of prompt length per model; the share of requests
   that carry tools, that stream, that end in `length`; failures by class
   per model; conversation length (messages per request). Text tables,
   or CSV when someone wants a spreadsheet.

Analyses of the content itself, which is the reason the content is kept
(what people ask for, how agents are used, what a project's work looks
like), come after, and which ones is open. Anything heavy, such as a model
classifying prompts, runs off the gateway host on exported files, and is
not designed here.

### Two words, kept apart

In this codebase **usage** already means what LiteLLM's daily aggregate
reports: tokens and spend per project, per key, per model, the numbers
budgets run on (`test_usage.py`, `project_usage`). What the log yields is
called **demand** throughout: demand rows, the `demand-rollup` runner,
the `llmao:demand:` keys. The two are compared once, below, and never
mixed.

### The runner

One runner, **`demand-rollup`**, in `llmao/capacity.py` beside the
capacity writer and registered in `main.py`. It runs at portal start and
then once a day at 01:00 UTC, an hour after the day closes: the grace
the daily reader already allows for LiteLLM's background flush. Each run
lists the closed UTC days inside retention, subtracts the days the store
already holds, and rolls up each missing one. On a normal day that is
one day, yesterday. After downtime it is several, done in the same run.
The current day is never read, and nothing is kept between runs.

Rolling up one day:

1. Read the whole day from `/spend/logs/v2`, page by page.
2. Fold the rows into 24 hourly rows per model, with everything LiteLLM
   has that the rows might need.
3. Once track N exists, call `prompt_log.rollup_day(day)`, which spawns
   `llmao-prompt-log rollup --json <day>` for that day's file and
   returns what it printed; fold those fields into the same rows.
4. Write the rows to `CapacityStore`, and a marker that the day is done.

One day, one write, both sources. Everything else, the capacity page,
the reconciliation, the parked Usage Analysis page, reads the store;
LiteLLM is not asked again for a day it has written.

The spawn in step 3: `asyncio.create_subprocess_exec`, the child under
`nice` and `ionice` since it shares the host with the proxy, a timeout,
and a non-zero exit recorded as an attention item, never raised; the
day is then written from LiteLLM alone, its marker says the log side
is missing, and the next run tries that side again. The
parsing of a day's file happens in the child on another core; the event
loop waits on a pipe. llmao never opens a log file itself, and the tool
never holds the master key.

### Demand rows

One row per model per UTC hour, keyed `llmao:demand:<hour>:<model>`, in
the same `/v1/memory` store as the capacity rows, pruned by
`CapacityPruner` under the same `capacity_retention_days`. The capacity
rows describe supply, what each server did; these describe demand, what
was asked, and join to them on hour and model. Each holds:

| Field | Source | Meaning |
|---|---|---|
| `requests`, `prompt_tokens`, `completion_tokens` | LiteLLM | totals for the hour |
| `by_project`, `by_tool` | LiteLLM | the same three, split by team alias and by user-agent family |
| `refused` | LiteLLM | requests that failed, by LiteLLM's `normalized_error` (`429_RATE_LIMIT_EXCEEDED`, `400_CONTEXT_WINDOW_EXCEEDED`, ...). `waiting_frac` on the capacity rows sees requests queueing on a box; a request the proxy rejected never reaches one and is invisible to `/metrics` |
| `prompt_len` | LiteLLM | counts in token-length buckets (`<1k`, `1k-8k`, `8k-32k`, `32k-128k`, `>128k`), for whether anyone uses the context we pay KV cache for |
| `duration_p50_s`, `duration_p95_s` | LiteLLM | latency as the client felt it, successful requests, to set against the box's queue times |
| `ttft_p50_s`, `ttft_p95_s` | the log | time to first token: the spend log sets `completionStartTime` on every request, streamed or not, so it cannot tell |
| `streamed`, `with_tools`, `finish_length` | the log | counts |
| `sources` | | which of the two have written to the row |

What counts: a call to a model. A spend row with no model group (a bad
key refused at the door, a `/v1/models` listing) is not use of a model,
and LiteLLM's own health checks, which run as the team
`litellm-internal-health-check`, are not anyone's demand. Both are left
out; on 2026-11-01 they were 817 of the day's 1,764 spend rows. Nor is
a request that names a deployment by its LiteLLM id instead of a model:
the fleet tools and health probes do that, one eight-token completion
per deployment, and the id then appears as the model group (sixty of
them over 281 rows in early October 2026). A refused call to a model
does count, in `requests` and in `refused`, with no tokens. The
log-only fields are absent from a row until track N writes them.

The day marker carries `FOLD_VERSION`. When the fold changes what a row
holds or what counts, the constant is bumped and the next run rolls
every stored day up again, clearing the day's old rows first, so a
correction reaches history without anyone touching the store.

No prompt content and no per-user split: the capacity page is for
planning, and per project and per tool is what planning needs. Per user
stays in the reports on the host.

Size: six models by 24 hours by 90 days is about 13,000 small rows.

### What LiteLLM already holds, and the reconciliation

LiteLLM's spend log keeps one row per request with most of what the
record holds (the LiteLLM column above), and its daily aggregate is what
budgets run on, so LiteLLM stays the truth for tokens and money. There
is a single reader of the spend log, the runner above, and everything
LiteLLM has that the demand rows might ever need moves into the store
when a day is rolled up. The log then fills only the gaps: the stream
flag, tools in the request, the finish reason, messages per request.

The endpoint is `/spend/logs/v2` (1.105.0): paginated, up to 1,000
rows a page with the total in the response, and it takes a date or a
date with a time. The older `/spend/logs` is not used: it takes dates
only, caps the answer at 10,000 rows with a header saying so, and with
a single date ignores it and returns the most recent rows, the trap the
daily reader in `llmao/proxy/client.py` fell into and documents.

Where the two overlap, one figure per day and model is kept: requests
seen in the log against requests LiteLLM billed. A gap one way is logging
loss (a dropped worker event, a `no-log` that got through); a gap the
other way is spend-log loss, the 2026-07 class of failure. That one
comparison is a health check on both pipelines. Duplicating the token
tables from both sources would only invite arguing about which is right.

### What the capacity page gains

Against its four questions: enough capacity gains the refused count and
the user-felt latency; too much capacity gains who would notice if a box
went away; which way demand is going gains the split by project and by
tool, the only version of that question a human can act on; and the step
6 suggestions get prompt-length and refusal inputs they cannot get from
`/metrics`.

## Placement

One new module as the focal point; everything else goes into the file
that already owns the neighbouring role.

| Role | Lives in |
|---|---|
| The callback: settings from `callback_settings`, built once by a module `__getattr__`, the directory check, the loguru sink, payload to record, the pre-call strip | `litellm_hooks.py`; tests in `tests/test_litellm_hooks.py` |
| Record shape, file name pattern, reading a day file, the rollup of a day | `llmao/prompt_log.py` (new; imports nothing heavy); tests in `tests/test_prompt_log.py` |
| The tool: `rollup --json`, reports | `bin/llmao-prompt-log`, argparse over the module |
| The `demand-rollup` runner, daily | `llmao/capacity.py` beside `CapacityWriter`; registered in `main.py`; calls `prompt_log.rollup_day` for the log side |
| `rollup_day(day)`: spawns the tool for one day, parses its JSON | `llmao/prompt_log.py` |
| The single reader of `/spend/logs/v2`, one closed day, page by page | `llmao/proxy/client.py`, beside the daily reader |
| Demand rows: key layout, write, read, prune; the rollup from spend rows | `llmao/capacity.py`, `CapacityStore` and `CapacityPruner` |
| Reconciliation figure and demand tables | `llmao/planning.py` beside `summarise_capacity`; rendered by the `/admin/capacity` handler in `pages.py` |
| Settings: `prompt_log_dir`, `prompt_log_retention_days`, the rollup interval | `config.yaml.example`, flat keys beside `capacity_retention_days`; the sink reads its copy of the directory and retention from `litellm.yaml` |
| Terms of Service | `docs/help/terms.md`; link in `templates/footer.ezt`; excluded from the guides list in `pages.py` |
| p6 | `init.pp` (the directory, root 0700; one parameter each for the directory and the retention, rendered into both YAML files), `litellm.yaml.epp` (callback settings and the callback), `llmao-litellm.service.epp` (`ReadWritePaths`). No cron |

## The terms page

`docs/help/terms.md`, served at `/help/terms` like the other help pages,
linked from the footer as "Terms of Use" beside the Privacy Policy, and
left out of the Help nav's guide list the way the FAQ is (`HELP_UNLISTED`
in `pages.py`). It names the commit it is served from, through the
`repo` and `commit` fields the help renderer hands an article.

It says, in user-facing words: the service is the Responsible AI
Initiative's, for PMC members and committers, for work on ASF codebases;
prompts sent through the gateway are logged, with the key owner,
project, model, tool and time; responses are not; no parameter switches
the logging off; the site admins can read the log, and usage is
monitored for non-ASF work, malicious use of AI and criminal activity;
it is kept for approximately N days, and in the Foundation's backups
beyond that; Private models are where unpublished security work belongs
and External or Mixed ones are not, and credentials, secrets and other
people's personal data go nowhere; keys are personal; the ASF privacy
policy applies; using the service is agreeing to this; updates come
through the initiative's list and Slack channel.

The page went up before the log exists (N6, 2026-11-01), which errs on
the side of telling people early. STATUS.md's "Data handling" section is
rewritten at N8, when the log exists, since it currently says prompt
text is not retained and that is still true.
