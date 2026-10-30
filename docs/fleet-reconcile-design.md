# Fleet reconciliation: the Deployment step

Status: a working document for the step in progress. Everything before
this step is done and is no longer described here. The full prior version,
with the problem statement, the vocabulary, the identity and dialable-URL
rulings, the policy discussion, the early-work list and the long decision
table, is at
<https://github.com/apache/tooling-llmao/blob/58ae962/docs/fleet-reconcile-design.md>.
When this step and the one after it land, this file is removed and its
outcome is folded into `ARCHITECTURE.md` and `box-lifecycle.md`.

## Where we are

The seven sources are extracted. Each is a frozen snapshot beside the
reader that produces it, and nothing below `fleet.py` imports `fleet.py`:

| Source | Snapshot | Module |
|---|---|---|
| config.yaml rows, approvals, provider lines | `IntendedDeployment` | `intent.py` |
| the box's `GET /vllm/config` | `ConfigFetch` | `provisioning.py` |
| the providers' rentals | `Rental`, `ProviderRentals` | `rental.py` |
| the probe of one address | `VllmProbe` | `vllm_client.py` |
| LiteLLM's deployments | `LiteLLMDeployment`, `LiteLLMDeployments` | `proxy/deployments.py` |
| LiteLLM's own `/health` | `LiteLLMHealth` | `proxy/deployments.py` |
| the `/metrics` reading | `Sample` | `vllm_client.py` |

`Fleet` folds them through visible `update_*` calls, and the joins are
one rule: an observation joins by **key** when it carries one (a LiteLLM
row by its stamp, a probe by the address it dialed) and by **name** when it
is machine-level (a config fetch, a Rental). What joins nothing is skew,
shown as the observation it is: a stranger's fetch, a rental nobody
intends, an orphan, a duplicate.

What has not moved is the **state**. The lifecycle counters still sit on
`VllmServer` (`fails`, `last_ok`, the two loading starts, the key
verdict, `rejected_passes`, `state`), the LiteLLM reads on
`FleetDeployment`, and `model_status.py` recomputes a name from a dozen
counters on every call. Two symptoms of that:

- `deployment_status(snapshot_for_status(dep), cfg, now)` is called in five
  places, each rebuilding the same record from the same two objects.
- `record_probe` takes `in_litellm`, LiteLLM's fact, because the lifecycle
  needs it to tell Unhealthy from Awaiting after a restart, and the only
  object that has both facts is neither of the two that hold them.

## This step: the Deployment

One class with state, `Deployment` in `deployment.py`. It holds the intent
(or none, for an orphan), the latest of each observation, and its own
history, and it is the only place a lifecycle name is read. It absorbs
`model_status.py`.

Out of it come:

- the **verdict**, four values and the whole public surface: **Starting**
  (leave it alone), **Serving** (route to it), **Down** (take it out of
  LiteLLM, tell an admin), **NotOurs** (ignore it, say so);
- the **reason**, one sentence for the strip;
- the **badges**, the skew list the pages show;
- the **needs**, what the next probe should fetch;
- the fine **state**, display only: Awaiting, Loading, Healthy, Unhealthy,
  Stalled, Rejected, Foreign for a box; Configured, Healthy, Unhealthy for
  a provider line.

Adding a fine state changes `Deployment` and nothing else.

### The programming model

The prior version's decision table had about forty-five rows. It was
written before the code moved, as the readable statement of everything
production had been seen to do, and as a list of test fixtures it stands.
As a shape for the code it mixed four things that want to be four small
pieces, and only the first is a table:

1. **The transition table**, `step(history, observed, clock) -> history`.
   Its first column is the previous state. That is what makes it short:
   today's `self_hosted_lifecycle` has to work out where a deployment was
   from a dozen counters on every call (`last_ok` set and `fails` under
   threshold means it was Healthy; a loading start with no `last_ok`
   means it was Loading; and so on), and every new state has added a
   clause. With the previous state as an input, a row only says what
   this pass observed. In code: one pure function per previous state,
   each a short ordered chain of conditions over the observation, and a
   dict from state to function. Each cell of the matrix below gets one
   test, named for the cell.

   Not a list of row objects with predicate lambdas. That reads worse,
   debugs worse, and buys nothing the tests do not already give.

2. **Guards**, applied before stepping: a source that is unread or stale
   moves nothing. A stale probe is the `stale` signal; stale LiteLLM
   deployments add, delete and prune nothing; a stale listing does not
   advance the unlisted count. One rule, one multiplier
   (`health_fail_threshold` times the source's own interval), already
   what `_stale` computes. The old table listed these as rows; they are
   not rows, they are the condition for stepping at all.

3. **Reads off the stepped state**: `verdict(state)` is a seven-to-four
   map; `badges` and `reason` are pure functions of the history and the
   observation; `needs` says whether the next probe should ask
   `/v1/models` (window unknown, key refused last time, or the host
   unlisted) or `/metrics` (cache size unknown). The old table's five
   "unlisted, and the lifecycle is X" rows live here: unlisted changes
   the sentence, never the state, except for the one cell where it makes
   Rejected into Foreign.

4. **The plan**, `actions(history, observed)`: add when Serving, dialable
   and absent from LiteLLM; delete when present and Down; update the
   `api_base` when present and it differs from the dialable URL; delete
   the extra copies under one key. The policy table marks each kind auto
   or human. "Unlisted and not Healthy" stops being a delete gate: once
   `last_ok` is set a box that is not Healthy is Unhealthy, and the Down
   verdict already deletes that. The plan is the next step's work
   (`reconcile.py`); this step builds what it reads.

Two rulings carried forward unchanged: history is process memory, so a
restart starts cautious; and nothing derived is carried between passes
except through history, so the last verdict is an input to the step, not
only a log line.

### The transition matrix

Rows are where the deployment was. Columns are what this pass's probe
said about the box, and there are only four things it can say:

- **serving**: `/health` answered 200 and `/v1/models` accepted our key,
  or was not asked and the last verdict stands as accepted;
- **refusing**: `/health` answered 200 and `/v1/models` answered 401 with
  the key the intent was minted with;
- **listening**: `/health` answered, but not 200; a 503 while the model
  loads;
- **silent**: no connection, a timeout, or no address to dial.

Each cell is the next state. `grace` is `health_grace_s`; `threshold` is
`health_fail_threshold`; `fails` is the consecutive probes without a 200,
reset by any 200.

| Was | serving | refusing | listening | silent |
|---|---|---|---|---|
| **Awaiting** | Healthy | Rejected | Loading | Unhealthy if LiteLLM has the deployment; Stalled once `grace` has passed since the first attempt; else Awaiting |
| **Loading** | Healthy | Rejected | Loading inside `grace` of the loading start; Stalled past it | same as listening |
| **Healthy** | Healthy | Rejected | Healthy while `fails < threshold`; Unhealthy at it | same as listening |
| **Unhealthy** | Healthy | Rejected | Unhealthy | Unhealthy |
| **Stalled** | Healthy | Rejected | Loading when this probe is the first to reach and no loading start exists yet; otherwise Stalled | Stalled |
| **Rejected** | Healthy | Foreign if the host is unlisted and the refusal has run `REJECTED_PASSES` passes; else Rejected | Rejected while `fails < threshold`; Unhealthy at it | same as listening |
| **Foreign** | Healthy | Foreign | Foreign while `fails < threshold`; Unhealthy at it | same as listening |

One thing moves a deployment that is not a probe: a config fetch seen for
its name sets the loading start. From Awaiting that is Loading, the same
as the listening column. From Stalled it is Loading only when no loading
start existed yet, the never-reached kind, and the grace runs from the
fetch. A later fetch does not move the start: the start is the earlier
of the two.

**The two left columns are the same from everywhere.** A box that
answers 200 and takes our key is Healthy, whatever it was a pass ago: a
box found already up after a restart, one that just finished loading,
one that was down and came back, one whose key an admin just fixed. A
box that answers 200 and refuses our key is Rejected from everywhere but
the two refusal states, and it is deleted from LiteLLM on that first
401, because every request through the deployment would fail. Rejected
has no clock: it ends when the key is accepted, when the box stops
answering, or by becoming Foreign. That needs the refusal to have run
`REJECTED_PASSES` passes *and* the host to be unlisted, because Foreign
means something else is serving at an address that was ours, and a host
a listing still names is ours whatever it answers. The count is a
debounce against vLLM restarting before its key is in place, about two
minutes; the time an admin needs to fix the key comes from Rejected
having no clock, not from the count.

**All the history is in the two right columns**, because a box that is
not serving means different things depending on what it was doing:

- From *Awaiting*, nothing has been heard yet, and this is the only row
  that has to cope with a process restart, since after one every
  deployment begins here whatever its box is doing. A 503 is the box
  coming up: the loading start is set, and the grace runs from it.
  Silence is three different things. If LiteLLM has the deployment, the
  box was serving before we restarted and is down now: Unhealthy, and a
  delete, not patience. If nothing has ever answered and `grace` has
  passed since the first attempt, it is a rental that died: Stalled,
  which is what lets the unlisted sentence show. Otherwise it is a box
  still pulling its image, and it must not read Stalled five minutes
  after an admin created it.
- From *Loading*, there is a clock and nothing else: `grace` from the
  loading start to answer a 200, Stalled past it. Listening and silent
  are the same here, since the start is already set. The start is the
  earlier of the config fetch and the first reached probe on purpose: an
  approved host that fetched its config before a restart never fetches
  again, so its 503s have to count as a start or it would read Awaiting
  for ever.
- From *Healthy*, the hold-over: one failed probe does not take a
  serving box out of LiteLLM, `threshold` consecutive failures do. This
  is the row that makes a listing gap harmless, since the box keeps
  answering and the listings have nothing to do with it.
- *Unhealthy* and *Stalled* are the two kinds of down and differ only in
  how they were reached: Unhealthy was serving once, Stalled never was.
  Unhealthy leaves only on a 200. Stalled leaves on a 200, and also the
  first time the box is heard from when no loading start existed yet: a
  reached probe, or a config fetch. The grace runs from that moment, so
  the row reads Loading. A 503 after the window already ran does not
  restart it. The box had its grace.
- *Rejected* and *Foreign* under silence behave as Healthy does, with the
  hold-over and then Unhealthy. This is a change from today: a Rejected
  box that misses one probe currently reads Healthy, because the
  hold-over is computed from `last_ok` and `fails` without knowing the
  box was Rejected (quirk 1 in `box-lifecycle.md`). With the previous
  state in the row it stays Rejected under the threshold and goes
  Unhealthy at it. That is the one behaviour the matrix changes;
  everything else reproduces `self_hosted_lifecycle`, and the fixtures
  from the prior version's table are the proof.

**What the step reads and keeps.** The observation for one pass is the
probe (its column, and the window when `/v1/models` carried one), the
loading-start candidates (the config fetch time, the first reached
probe), whether LiteLLM has the deployment, and whether the host is
unlisted. The history the step keeps is `state`, `fails`, `last_ok`,
`first_probed_at`, `first_reached_at`, `config_served_at`, `key_rejected`
(standing until asked again, which is why "serving" can mean "not asked
and last accepted"), `rejected_passes`, and the observed window.
`in_litellm` is not history and not a probe argument: the Deployment
holds its own LiteLLM rows and reads them in the Awaiting row. The one
thing that clears history is a new rental at the same key, which is a
new box to be checked afresh.

**A provider line** has no probe. Its machine is three states and one
source: Configured until LiteLLM's `/health` has spoken, then Healthy or
Unhealthy as it says, back to Configured when that read is stale.

### Needs

The probe runner, each pass, for each Deployment with an address: fetch
`/health`, hand the probe over, then ask the Deployment what it needs
now, fetch that, hand it over. "Once, on the edge" falls out: after a
box restarts the Deployment steps back through Starting, its window and
cache size are unknown again, and it asks. The runner reads the
Deployment's answer, not its own notion of a transition, and holds no
state.

### Commits

Small, each acknowledged and pushed before the next:

1. **`deployment.py`: `History`, `Observed`, `step`, `verdict`.** The
   matrix above as code, one test per cell in `tests/test_deployment.py`,
   plus the walked fixtures (the dead rental, the reassigned address, the
   listing gap, the restarts). Used by nothing yet.
2. **The box's history moves onto the deployment.** `FleetDeployment`
   holds a `History`; `record_probe` becomes `FleetDeployment.observe`,
   which reads the deployment's own LiteLLM rows, so `in_litellm` leaves
   the probe pass. `VllmServer.state` and the counters become reads of
   the history. `self_hosted_lifecycle` is deleted.
3. **The pages read the deployment.** `badges`, `reason` and the signals
   become methods; the five `deployment_status(snapshot_for_status(dep))`
   calls become `dep.status()`, and the parked pair is gone.
4. **The provider line's machine** goes through the same step;
   `commercial_lifecycle` is deleted.
5. **`model_status.py` is deleted.** The constants and `model_status`
   (the roll-up over one model name, which stays a pure function) move
   to `deployment.py`; `DeploymentSnapshot` and `DeploymentStatus` go.

### After this step

`reconcile.py` gains the join and the plan: `Unintended` becomes its
grouping, orphans and duplicates become plan actions, the needs replace
the `/v1/models` rule in `probe_all`, and the executor runs the plan.
Durations in place of the pass counts wait on the p6 knob rename. Then
this file is folded into `ARCHITECTURE.md` and `box-lifecycle.md`, and
the box-to-rental terminology sweep follows.

## Import direction

Unchanged. The modules are layered and an import only ever points down:
`models.py`, `reconcile.py` (the key) and the seven source modules at the
bottom, each a leaf; then `deployment.py`, which reads the snapshot
shapes and nothing else; then `fleet.py`, the runners and the view; at
the top `main.py`, `pages.py` and `api.py`. Where a lower module needs
something a higher one owns, `main.py` constructs it once and hands it
down.
