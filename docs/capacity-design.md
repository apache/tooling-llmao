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
On a planning page that spend has one use: comparison. What a million
tokens costs there against what our rental costs per million for the same
model, and whether demand on a bought model has grown enough to justify a
rental. The section and its tiles appear only when something was bought
in the period; with every deployment on our own rentals, that is never.

This is a planning page, not a usage report: it exists to answer "rent
more machines, or return some" and, later, what shape the next rental for
a model should have. Every figure on it serves that question or is left
out.

Some figures may later go into a Board report; that report is curated by
hand, and this page is not it.

## The page

One card per model, the catalog's display name and id, in two halves
joined by model name from the two stores: **What it served**, from the
hourly capacity rows, and **What was asked of it**, from the demand rows.
Either half may be missing (sampling started later than the spend log
reaches back; a day's demand arrives the morning after) and the card
shows whichever it has. Models with a deployment now come first, by
rental cost; one with nothing serving it is marked. A model with nothing
serving it and nothing seen for a week is a footnote line with its last
day and what it cost, not a card.

Under "What it served", the rentals table has one line per vLLM process,
a rental and a listen port, named by the rental stamp on the rows ("Vast
53865410:8001" with the rental's label beside it), with the first and
last day seen and what became of the machine: "ended" when no provider
lists it now, the provider's own word ("exited") when listed but not
running, "gone" for a pre-stamp address no server has now. Each line
carries its own preemptions, so when the model's total says vLLM ran
out of cache, a Preempted column says on which rental; the column
appears only when some line has a count.

What never happened is left out rather than shown as zero: the waiting
meter and the worst wait (vLLM's smallest bucket, under 0.3 s, is the
same news as none), preemptions, the Refused column, the Waiting column.
The cost per million tokens is over the priced hours alone, so the few
unpriced hours after a start neither hide it nor dilute it.

The day-by-day charts (tokens, requests, spend) are drawn by Apache
ECharts, vendored by `bin/fetch-thirdparty.sh` and loaded by this page
alone, from a JSON payload per chart (`_chart` in `pages.py`, on a
`data-chart` attribute) that covers every day of the period, a day with
no row as zero: the x-axis is the calendar, so a series that stopped
reads as having stopped, and one that started late starts late. The
look is set in `llmao.js`: one hue, bars no wider than 24px, a hairline
grid, a tooltip per day, no legend since each chart is one series.

The page speaks of rentals and deployments, never boxes or servers, and
how its figures are made sits in a disclosure at the foot, closed until
someone asks.

## Status

| Step | What | State |
|---|---|---|
| 1 | Vast and RunPod keys under `providers:` | done |
| 2 | Hourly rollups, stored | done |
| 3 | Rental prices from the Vast and RunPod APIs | done |
| 4 | Provider spend from LiteLLM's daily activity | done |
| 5 | The page, `/admin/capacity` | done |
| 6 | "Add a box / remove a box" suggestions | after a few weeks of data |
| 7 | Demand rows: what was asked per model per hour, from LiteLLM's spend log, rolled up once a day; "What was asked" on the page | done 2026-11-02 |

Demand, the other side of the capacity rows (what was asked, against what
each server did), is designed in `docs/prompt-logging-design.md` under
"Demand rows", and lives in the same module and store: `fold_demand` and
`DemandRollup` in `llmao/capacity.py`, keys `llmao:demand:<hour>:<model>`
beside the capacity keys, pruned together. What the rows say is read in
`llmao/planning.py`: `summarise_capacity`, `summarise_demand` and
`summarise_spend`, the summaries behind the page.

Each rolled-up day leaves a marker, `llmao:demand-day:<day>`, holding
the row count, which sources wrote it, and the `FOLD_VERSION` the fold
had at the time (a constant in `llmao/capacity.py`, 2 since the
deployment-id exclusion of 2026-11-02). The runner treats a day whose
marker carries an older version as missing, so bumping the constant
makes the next run clear and re-roll every stored day, oldest first.
That is how a change to what a demand row holds, or to what counts as
demand, reaches history. Bump it whenever `fold_demand` changes its
output; the capacity rows have no such version, since they are
written once from readings that cannot be re-taken.

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
| `rental`, `rental_label` | the machine as its provider names it (`Rental.ident`, "Vast 53865410") and labels it ("RTX 3060 x2"); empty until a provider places the server. Rows from before 2026-11-02 have neither |

The server is an address and listen port. A provider changes the address
when it moves or restarts the machine, and the address means nothing once
the rental has ended, so the hours of one rental can sit under two servers
and an ended rental's hours under an address nothing names. The rental
stamp is what joins them.

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
come from the providers, through the box the listing places the server on
(by address, else the instance or pod it was on before): Vast's `dph_total` from the
instance list the fleet already reads for ports, and RunPod's `costPerHr`
from its pod list (`providers.runpod.api_key`), read every 10 minutes by its
own runner. A box no provider prices has no price, never $0, so the report
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
- **Model providers:** read from LiteLLM's daily activity, broken down by the
  provider's own model string (`xai/grok-4`, `openrouter/x-ai/grok-4`), which
  is what tells one provider from another when both serve one model; mapped
  back through `providers.<name>.models`. Totals per provider: the period,
  today, the month to date, and a projection to month end at the month's
  daily average so far, with the models and projects behind them.
  LiteLLM prices each request from its model cost map,
  reloaded every 24 hours. Spend is fixed when a request is made, so it is
  LiteLLM's estimate at the time, not the provider's bill. Close enough for
  planning.
