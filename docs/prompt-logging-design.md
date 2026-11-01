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

## Status

| Step | What | State |
|---|---|---|
| 1 | This design | written |
| 2 | `prompt_log` callback in `litellm_hooks.py`, with tests | planned |
| 3 | Switched on: `litellm.yaml.example`; p6 (callback settings, directory, service, cron) | planned |
| 4 | Terms of Service page, footer link, STATUS.md data handling | planned |
| 5 | `bin/prompt_log.py`: rotate and prune, first reports | planned |
| 6 | Check `no-log` on the running proxy (see below) | planned |

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
  still be quick: one JSON dump and one append, milliseconds.
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

LiteLLM has two per-request switches that would otherwise do exactly
that: `no-log`, which skips every callback for the request, and
`turn_off_message_logging`, which hands callbacks the message
`redacted-by-litellm` in place of the prompt. Whether a client can set
either through the proxy, as opposed to only through the SDK, is what
step 6 checks on the running proxy. If a client can, we disable the
feature on our side: the hook's `async_pre_call_hook` removes both
parameters from every request before LiteLLM reads them, so the request
is logged like any other. Nothing is rejected and the client is not told;
the parameters simply have no effect here.

## The record

One JSON object per line. Field names are ours; the source is the standard
payload.

| Field | From | Meaning |
|---|---|---|
| `ts`, `duration_s`, `ttft_s` | `endTime`, `startTime`, `completionStartTime` | when it finished (UTC, ISO 8601), how long it took, time to first token (`null` unless streamed) |
| `call_id`, `trace_id` | `litellm_call_id`, `trace_id` | the id the client got in `x-litellm-call-id`, which also keys LiteLLM's spend row; the trace groups retries and fallbacks |
| `call_type` | `call_type` | LiteLLM's name for the endpoint (`acompletion`, `aembedding`, ...) |
| `status`, `error` | `status`, `error_information` | `success` or `failure`; on failure the class, code and provider, not the message (an error message can quote the prompt) |
| `model`, `deployment`, `api_base`, `provider` | `model_group`, `model`, `api_base`, `custom_llm_provider` | what the user asked for; what served it; which box or provider |
| `stream` | `stream` | |
| `tokens` | `prompt_tokens`, `completion_tokens`, `total_tokens` | |
| `finish_reason` | the response | the one word we keep from the response: it is how truncation and tool use show up in aggregate. **The exception to decision 2; drop it if that reads as a result.** |
| `key` | `metadata.user_api_key_*` | `hash`, `alias`, `user` (the ASF uid for a personal key, `null` for automation), `project` (the team alias) |
| `end_user` | `end_user` | the request's `user` field, when a tool sends one |
| `user_agent`, `client_headers` | `user_agent`, `metadata.requester_custom_headers` | which tool, and any `x-` headers it sent |
| `params` | `model_parameters` | the request minus the prompt: `temperature`, `max_tokens`, `tools` (the definitions an agent put in front of the model), `tool_choice`, `response_format`, `reasoning_effort`, `chat_template_kwargs`, ... |
| `messages` | `messages` | the prompt as LiteLLM received it: the messages list for chat, the string for completions, the input for embeddings |

`requester_ip_address` is left out: the proxy sits behind the Apache vhost
on loopback and does not read `X-Forwarded-For`, so it would say
`127.0.0.1` on every row.

### Size

A line is the size of the prompt. Agentic tools resend the whole
conversation every turn, so one Claude Code turn at 100k tokens is about
400 KB. **Assumption, not a measurement:** a thousand such requests a day
is 400 MB a day uncompressed; gzip takes prose and code down five to eight
times, so 50 to 80 MB a day, and 90 days is 5 to 7 GB. The capacity rows
already hold prompt tokens per server per hour, so the real daily figure is
one query away and should replace this paragraph.

## Files

- **Directory:** `/x1/llmao/prompt-log/`, root-owned, mode 0700, declared
  by Puppet and added to the proxy service's `ReadWritePaths`. The same
  Puppet value is rendered into `litellm.yaml` (for the hook) and
  `config.yaml` (for the post-process), so there is one place to change
  it.
- **One file per UTC day,** `prompts-2026-11-01.jsonl`, mode 0600, opened
  for append on every write, so the writer holds no state across midnight
  and a finished day can be compressed out from under it.
- **A write that fails** (disk full, permission lost) is logged as an
  error through LiteLLM's logger and the record is dropped. It never
  reaches the request. Disk full is an incident, not something the hook
  manages.
- **A daily cron** (Puppet) runs the post-process's housekeeping: compress
  every day file older than today's, and delete files past
  `prompt_log_retention_days`. It runs a few hours into the day, so the
  file it compresses has been closed to writes for that long; the logging
  worker drains its queue in seconds, so the leeway is for a stalled
  worker, not a normal one. Compression is gzip: `zcat` and Python's
  `gzip` read it, and the reports read both forms.
- **Retention** is `prompt_log_retention_days` in `config.yaml`, beside
  `capacity_retention_days` and `action_retention_days`. The number is
  open; 90 matches the others, and the terms state whatever it is. The
  files on `/x1` are also in the Foundation's backups, under the backup
  policy's own retention; the terms should say so.

## Access

- Readable by root on the gateway host. Site admins read them over `sudo`.
- No page in llmao shows a prompt. What the analysis produces is aggregates.
- The terms say this: what is recorded, who can read it, how long it is
  kept.

## The post-process

`bin/prompt_log.py`, run by cron for housekeeping and by hand for reports
to begin with. It reads the day files, plain and compressed, and has two
jobs:

1. **Housekeeping:** compress finished days, delete past retention.
2. **Reports.** The first ones, chosen because they need no reading of
   prompt content: requests and prompt tokens per day by project, by user,
   by model and by user agent; the distribution of prompt length per
   model; the share of requests that carry tools, that stream, that end
   in `length`; failures by class per model; conversation length (messages
   per request). Text tables, or CSV when someone wants a spreadsheet.

Analyses of the content itself, which is the reason the content is kept
(what people ask for, how agents are used, what a project's work looks
like), come after, and which ones is open. Anything heavy, such as a model
classifying prompts, runs off the gateway host on exported files, and is
not designed here.

## The terms page

`docs/help/terms.md`, served at `/help/terms` like the other help pages,
linked from the footer as "Terms of Service" beside the Privacy Policy,
and left out of the Help nav's guide list the way the FAQ is. It says, in
user-facing words: prompts sent through the gateway are logged, with the
key owner, project, model and time; responses are not; the llmao admins
can read the log; it is kept for N days, and in the Foundation's backups
beyond that; do not send credentials or embargoed material; the ASF
privacy policy applies; using the service is agreeing to this. STATUS.md's
"Data handling" section is rewritten when this lands, since it currently
says prompt text is not retained.
