# Capacity and spend report: design

A page for llmao's site admins (the people who see Deployments on `/models`),
at `/admin/capacity`, linked from `/admin`. It answers, per model:

1. **Is there enough capacity?** How often requests queue, how full the KV
   cache gets at peak, and whether vLLM is preempting.
2. **Is there too much?** How busy each box is, and tokens served against
   what the box can do.
3. **What does it cost?** Rental dollars per day, and the real cost per
   million tokens: rental divided by tokens served.
4. **Which way is demand going?** Tokens per day, and the busiest hours.

And, for the model providers we pay (xAI, OpenRouter), what we spend: per
day, month to date, a projection to month end, and which projects spend it.

Some figures may later go into a Board report; that report is curated by
hand, and this page is not it.

## Status

| Step | What | State |
|---|---|---|
| 1 | Vast and RunPod keys under `providers:` | done |
| 2 | Hourly rollups, stored | done |
| 3 | Rental prices from the Vast and RunPod APIs | done |
| 4 | Provider spend from LiteLLM's daily activity | planned |
| 5 | The page | planned |
| 6 | "Add a box / remove a box" suggestions | after a few weeks of data |

**Future, on demand only:** a CSV download of the report. Not built until
someone asks for it.

## Measuring the boxes

llmao has no database (see ARCHITECTURE.md), so the measurements live in
LiteLLM's `/v1/memory`, like access requests and tier changes.

Four parts, each knowing one thing:

| Part | Knows | Runs |
|---|---|---|
| **Sampler** (`llmao/sampler.py`) | which boxes are serving; fetching and parsing vLLM's `/metrics` | every `sample_interval_s` |
| **Rollup** (`HourlyRollup`) | turning readings into one row per server per clock hour | no; fed by the sampler |
| **Writer** (`CapacityWriter`) | when an hour is finished; handing it to the store | about every minute |
| **Store** (`CapacityStore`) | the `/v1/memory` key layout; write, read, prune | no; used by the writer, pruner and page |

Plus the **pruner** (`CapacityPruner`), once a day.

The sampler is the only reader of each box's `/metrics`. The writer is
separate from the sampler so that a slow or absent LiteLLM delays storing
rows without stalling the readings; finished hours wait in memory until it
answers.

### What a row holds

One row per server per UTC clock hour, keyed by hour then server:

    llmao:capacity:2026-10-18T09:203.0.113.10:8001

| Field | Meaning |
|---|---|
| `hour`, `server`, `model_name` | which hour, which server (host:listen port), which model |
| `covered_s` | how much of the hour was sampled |
| `busy_frac` | share of readings with any request running |
| `waiting_frac`, `waiting_peak` | share of readings with any request waiting; the most waiting at once |
| `kv_avg`, `kv_peak` | KV cache use, as a fraction |
| `prompt_tokens`, `generation_tokens`, `requests`, `preemptions` | growth of vLLM's counters over the hour |
| `queue_avg_s`, `queue_p95_s` | time requests spent queued, from vLLM's histogram |
| `usd_per_hour` | this server's share of its box's rental price; empty if no provider prices the box |

Counters are vLLM's totals since it started. A total that goes down means
the server restarted, and the new total is counted from zero. A reading the
box did not answer breaks the chain: the next reading starts afresh rather
than crediting the gap to one hour.

`waiting_frac` is the evidence for "add a box": it shows whether requests
actually queue, which the spend log cannot (it records what ran, not what
waited). It replaces the contention log lines the sampler used to write.

The price is on each row because a box's rental price is fixed for as long
as we hold it, and a returned box vanishes from its provider's API. Kept on
the row, a returned box keeps its price in history.

A row carries its server's **share** of the box: a box running two servers
puts half its price on each, so adding rows up gives the real bill. Prices
come from the providers, by the box's public IP: Vast's `dph_total` from the
instance list the fleet already reads for ports, and RunPod's `costPerHr`
from its pod list (`providers.runpod.api_key`), asked only while some server
is unpriced. A box no provider prices has no price, never $0, so the report
can tell "free" from "unknown".

### Size

20 boxes x 24 hours x 90 days is about 43,000 rows, roughly 17 MB to read
for a 90-day view. Reads go one day per request (24 rows per server, within
the memory API's page of 500 up to twenty servers), all at once, timed with
a Stopwatch. If the 90-day view proves too slow even for two admins, rows
older than a week can be merged into one per server per day, about 5,000
rows for 90 days. Not built until the timings say so.

Rows older than `capacity_retention_days` (required; config.yaml.example
suggests 90) are deleted daily. Each day
past retention is swept seven times, so a few days with llmao down still
get cleared.

### Accepted gaps

- **An llmao restart loses the hour in progress.** Each row's `covered_s`
  shows how much of its hour was seen. We accept this: the report is read
  for trends over weeks, and an hour against 90 days is noise. Keeping the
  hour across restarts would need a second store.
- **Bandwidth** is billed by Vast per GB, separately from the hourly price,
  and is left out.

## Cost

- **Self-hosted:** rental dollars per hour (Vast `dph_total`, RunPod
  `costPerHr`), set against tokens served. There is no hand-set price: every
  box is on a provider that reports one. A box that is not would show as
  unpriced, and a setting can be added when one exists. LiteLLM records self-hosted requests at $0.00 on
  purpose, since a price there would count against team budgets.
- **Model providers:** LiteLLM prices each request from its model cost map,
  reloaded every 24 hours. Spend is fixed when a request is made, so it is
  LiteLLM's estimate at the time, not the provider's bill. Close enough for
  planning.
