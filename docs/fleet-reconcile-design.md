# Fleet reconciliation: the Deployment step

Status: a working document for the step in progress. Everything before
this step is done and is no longer described here. The full prior version,
with the problem statement, the vocabulary, the identity and dialable-URL
rulings, the policy discussion, the early-work list and the long decision
table, is at
<https://github.com/apache/tooling-llmao/blob/58ae962/docs/fleet-reconcile-design.md>.
When this step and the one after it land, this file is removed and its
outcome is folded into `ARCHITECTURE.md` and `rental-lifecycle.md`.

## Where we are

The seven sources are extracted. Each is a frozen snapshot beside the
reader that produces it, and nothing below `fleet.py` imports `fleet.py`:

| Source | Snapshot | Module |
|---|---|---|
| config.yaml rows, approvals, provider lines | `IntendedDeployment` | `intent.py` |
| the rental's `GET /vllm/config` | `ConfigFetch` | `provisioning.py` |
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

- `dep.status(cfg, now)` is called in five places, each rebuilding the
  same record from the same two objects (it was a pair of module
  functions until 2026-11-26; the rebuilding is what remains).
- `record_probe` takes `in_litellm`, LiteLLM's fact, because the lifecycle
  needs it to tell Unhealthy from Awaiting after a restart, and the only
  object that has both facts is neither of the two that hold them.

Behind both symptoms is one cause: four classes describe one deployment
(2026-11-28). The two source records about it are fine, and stay:
`IntendedDeployment` is what config.yaml said, `LiteLLMDeployment` is
one row LiteLLM reported. They are evidence, not the thing. The other
four are the thing, cut four ways, and each cut has a historical reason
rather than a technical one:

| Class | Holds | Why it exists apart |
|---|---|---|
| `VllmServer` (fleet.py) | a copy of the intent, the probe counters and `state`, the address the listing placed, URL and display helpers | it was the original object, when the fleet was a list of host rows |
| `FleetDeployment` (fleet.py) | one `VllmServer` or one provider line, the LiteLLM rows and health, skew | layered on when provider lines arrived and LiteLLM's reads needed a home that was not a vLLM process |
| `DeploymentSnapshot` (model_status.py) | a frozen copy of the fields the rules read, rebuilt on every call | import direction: model_status.py sits below fleet.py and could not name its classes, so the rules were written against a copy |
| `DeploymentStatus` (model_status.py) | the ten answers the rules compute | the rules were functions returning a bundle |

Every self-hosted deployment is exactly one `VllmServer` and one
`FleetDeployment` (`from_vllm`, one to one), which says that boundary is
not real; it is what forces `in_litellm` into the probe pass. The
snapshot is the rebuilding. The status record would be ten properties if
the rules were methods.

## This step: the Deployment

One class with state, `Deployment` in `deployment.py`, and the four
above go: `VllmServer` and `FleetDeployment` fold into it, and
`DeploymentSnapshot` and `DeploymentStatus` are deleted because their
fields become its fields and its properties. It holds the intent (or
none, for an orphan), the latest of each observation, and its own
history, and it is the only place a lifecycle name is read. It absorbs
`model_status.py`; `model_status()` stays a function, since it is about
a model name and not a deployment, and `ModelStatus` stays as its
answer. `Fleet` becomes the list of deployments and the runners that
fold observations onto them; `Fleet.servers` is the self-hosted subset.

Out of it come:

- the **verdict**, four values and the whole public surface: **Starting**
  (leave it alone), **Serving** (route to it), **Down** (take it out of
  LiteLLM, tell an admin), **NotOurs** (ignore it, say so);
- the **reason**, one sentence for Work needed on `/admin`;
- the **badges**, the skew list the pages show;
- the **needs**, what the next probe should fetch;
- the fine **state**, display only: Awaiting, Loading, Healthy, Unhealthy,
  Stalled, Rejected, Foreign for a rental; Configured, Healthy, Unhealthy for
  a provider line.

Adding a fine state changes `Deployment` and nothing else.

### Observation and history

An **observation** is what one source said at one moment, complete in
itself and saying nothing about the past. A probe at `t` says: `/health`
answered 200, or 503, or nothing; `/v1/models` said 401, or 200, or was
not asked; the window was this. A LiteLLM read says: these rows exist
now. A listing says: these rentals exist now, at these addresses. Each
is a frozen record beside the reader that fetched it (`VllmProbe`,
`LiteLLMDeployments`, `ProviderRentals`), and that is all a source ever
gives: a fact about an instant.

An observation alone cannot name the state, because the same observation
means different things depending on what came before it:

- **Silence** is a rental still pulling its image (Awaiting), a rental
  that died before it ever answered (Stalled), a serving rental's one
  missed probe (still Healthy, the hold-over), or a serving rental that
  has gone down (Unhealthy, at the threshold).
- **A 503** is a rental coming up (Loading), or one that had its grace
  and never got there (Stalled).
- **A 200 that refuses our key** is Rejected; the same answer for
  `REJECTED_PASSES` passes at a host no listing names is Foreign.

Every one of those distinctions is a fact about the past: whether a 200
was ever seen, when the first attempt was, when loading began, how many
passes in a row have said the same thing. None of it is in the probe.

**History** is the smallest memory of the past that disambiguates the
present observation. It is of three kinds, and the Deployment keeps all
three, but they are not the same thing:

1. **The position**: where the deployment was, the previous state. This
   is the first column of the matrix below, and what makes each cell
   small: a cell says only what this pass's observation does from here.
2. **The clocks and counts** the next step needs: `first_probed_at` (the
   grace runs from it while nothing has answered), the loading start
   (the grace runs from it once something has), `last_ok` and `fails`
   (the hold-over), `rejected_passes` (the Foreign debounce). A duration
   in config is meaningless without the moment it is measured from, and
   that moment is history; so `now` is an input to the step beside the
   observation, and the step is a function of three things, not one.
3. **Standing observations**: the last answer to a question that is not
   asked every pass, kept until it is asked again. The window
   (`/v1/models` is asked only when the deployment needs it), the key
   verdict (same), the config fetch time, the address the listing
   placed. These are observations, not history of position; they are
   kept because the source does not repeat them, and "serving" in the
   matrix can mean "not asked and the last verdict stands" because of
   them.

What history is **not**: anything another source said about the same
deployment. Whether LiteLLM has a route to it is LiteLLM's observation,
read by the plan and never by the step. Nothing derived is carried
between passes except through history; the last state is an input to the
step, not only a log line.

Today's code keeps kind 2 and kind 3 and recomputes kind 1 from them on
every call: `last_ok` set and `fails` under the threshold means it was
Healthy; a loading start with no `last_ok` means it was Loading; and so
on. That is history stored as evidence and re-derived into a position
each time, and it is why every new state has added a clause to one
function and why the hold-over cannot tell a Rejected rental from a
Healthy one (quirk 1 in `rental-lifecycle.md`). The matrix stores the
position and reads it.

History is **process memory**. It exists in this process and nowhere
else; after a restart every deployment is Awaiting with empty clocks,
whatever its rental is doing, and the Awaiting row is the one that has
to cope with that. The one thing that clears history short of a restart
is a new rental at the same key, which is a new rental to be checked
afresh.

**Where the history lives** (decided 2026-11-28): in its own class. A
frozen `History` holds the position and the clocks, kinds 1 and 2, and
nothing else; the Deployment holds one, beside its intent and its
standing observations. The step is a pure function, `step(history,
probe, now) -> History`, and `Deployment.observe(probe, now)` is one
line: store what `step` returns. Each cell test builds a `History` and a
probe and compares records.

The alternative was to keep the position and the clocks as fields of
`Deployment` and let `observe` move them in place: one class fewer. It
was rejected because the class is the point. The three kinds above are
distinct things, and a Deployment with a dozen fields of mixed kind is
exactly what `VllmServer` is today, where `first_probed_at` sits beside
`observed_version` and `public_port` and nothing says which of them the
lifecycle reads and which it does not. Naming the history as a record
draws that line in the code: what the step reads and returns is in
`History`, and anything not in it is by construction not history. There
is no `Observed` class: the observation is the `VllmProbe` the runner
hands over, and the standing observations are the Deployment's own
fields.

### The programming model

The prior version's decision table had about forty-five rows. It was
written before the code moved, as the readable statement of everything
production had been seen to do, and as a list of test fixtures it stands.
As a shape for the code it mixed four things that want to be four small
pieces, and only the first is a table:

1. **The transition table**, `step(history, probe, now) -> History`: the
   previous position and clocks, plus this pass's probe and `now`, to the
   next position and clocks. Its first column is the previous state, for
   the reason the section above gives. In code: one pure function per
   previous state, each a short ordered chain of conditions over the
   observation, and a dict from state to function. Each cell of the
   matrix below gets one test, named for the cell.

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
   and absent from LiteLLM; delete when present and Down; delete when
   present, Awaiting and silent, the restart case, where the route
   predates the process and nothing has answered since; update the
   `api_base` when present and it differs from the dialable URL; delete
   the extra copies under one key. The policy table marks each kind auto
   or human. Whether to delete a route is decided here and nowhere else:
   the state is computed from the rental alone and never reads whether
   LiteLLM has the deployment. A Loading rental keeps its route, as
   today, until the hold-over or the grace runs out.
   "Unlisted and not Healthy" stops being a delete gate: once `last_ok`
   is set a rental that is not Healthy is Unhealthy, and Down already
   deletes that. The plan is the next step's work (`reconcile.py`); this
   step builds what it reads.

Two rulings carried forward unchanged: history is process memory, so a
restart starts cautious; and nothing derived is carried between passes
except through history, so the last verdict is an input to the step, not
only a log line.

### The transition matrix

Rows are where the deployment was. Columns are what this pass's probe
said about the rental, and there are only four things it can say:

- **serving**: `/health` answered 200 and `/v1/models` accepted our key,
  or was not asked and the last verdict stands as accepted;
- **refusing**: `/health` answered 200 and `/v1/models` answered 401 with
  the key the intent was minted with;
- **listening**: `/health` answered, but not 200; a 503 while the model
  loads;
- **silent**: no connection, a timeout, or no address to dial.

A listed host with no address yet is not probed at all: a provider names
the machine and has not placed it, so its state and clocks stand until
the listing does. Only an unlisted host with no address is probed, as
silent (`VllmProbe.no_address`), because nothing will ever place it.

Each cell is the next state. `grace` is `health_grace_s`; `threshold` is
`health_fail_threshold`; `fails` is the consecutive probes without a 200,
reset by any 200.

| Was | serving | refusing | listening | silent |
|---|---|---|---|---|
| **Awaiting** | Healthy | Rejected | Loading | Stalled once `grace` has passed since the first attempt; else Awaiting |
| **Loading** | Healthy | Rejected | Loading inside `grace` of the loading start; Stalled past it | same as listening |
| **Healthy** | Healthy | Rejected | Healthy while `fails < threshold`; Unhealthy at it | same as listening |
| **Unhealthy** | Healthy | Rejected | Unhealthy | Unhealthy |
| **Stalled** | Healthy | Rejected | Loading when no loading start existed before this probe; otherwise Stalled | Stalled |
| **Rejected** | Healthy | Foreign if the host is unlisted and the refusal has run `REJECTED_PASSES` passes; else Rejected | Rejected while `fails < threshold`; Unhealthy at it | same as listening |
| **Foreign** | Healthy | Foreign while the host is unlisted; Rejected once a listing names it again | Foreign while `fails < threshold`; Unhealthy at it | same as listening |

One thing moves a deployment that is not a probe: a config fetch seen for
its name sets the loading start. From Awaiting that is Loading, the same
as the listening column. From Stalled it is Loading only when no loading
start existed yet, the never-reached kind, and the grace runs from the
fetch. A later fetch does not move the start: the start is the earlier
of the two.

**The two left columns are the same from everywhere.** A rental that
answers 200 and takes our key is Healthy, whatever it was a pass ago: a
rental found already up after a restart, one that just finished loading,
one that was down and came back, one whose key an admin just fixed. A
rental that answers 200 and refuses our key is Rejected from everywhere but
the two refusal states, and it is deleted from LiteLLM on that first
401, because every request through the deployment would fail. Rejected
has no clock: it ends when the key is accepted, when the rental stops
answering, or by becoming Foreign. That needs the refusal to have run
`REJECTED_PASSES` passes *and* the host to be unlisted, because Foreign
means something else is serving at an address that was ours, and a host
a listing still names is ours whatever it answers. The count is a
debounce against vLLM restarting before its key is in place, about two
minutes; the time an admin needs to fix the key comes from Rejected
having no clock, not from the count.

**All the history is in the two right columns**, because a rental that is
not serving means different things depending on what it was doing:

- From *Awaiting*, nothing has been heard yet, and this is the only row
  that has to cope with a process restart, since after one every
  deployment begins here whatever its rental is doing. A 503 is the rental
  coming up: the loading start is set, and the grace runs from it.
  Silence is two things. If nothing has ever answered and `grace` has
  passed since the first attempt, it is a rental that died: Stalled,
  which is what lets the unlisted sentence show. Otherwise it is a rental
  still pulling its image, and it must not read Stalled five minutes
  after an admin created it. Whether LiteLLM still has a route to it is
  not the state's business: a route to a silent rental nothing has heard
  from since the restart is the plan's delete, and it fires on this same
  pass whether the rental reads Awaiting or Stalled. Today's code reads
  Unhealthy here when the route exists, by taking LiteLLM's fact into the
  probe pass; that is the crossed concern the step removes.
- From *Loading*, there is a clock and nothing else: `grace` from the
  loading start to answer a 200, Stalled past it. Listening and silent
  are the same here, since the start is already set. The start is the
  earlier of the config fetch and the first reached probe on purpose: an
  approved host that fetched its config before a restart never fetches
  again, so its 503s have to count as a start or it would read Awaiting
  for ever.
- From *Healthy*, the hold-over: one failed probe does not take a
  serving rental out of LiteLLM, `threshold` consecutive failures do. This
  is the row that makes a listing gap harmless, since the rental keeps
  answering and the listings have nothing to do with it.
- *Unhealthy* and *Stalled* are the two kinds of down and differ only in
  how they were reached: Unhealthy was serving once, Stalled never was.
  Unhealthy leaves only on a 200. Stalled leaves on a 200, and also the
  first time the rental is heard from when no loading start existed yet: a
  reached probe, or a config fetch. The grace runs from that moment, so
  the row reads Loading. A 503 after the window already ran does not
  restart it. The rental had its grace.
- *Rejected* and *Foreign* under silence behave as Healthy does, with the
  hold-over and then Unhealthy. This is a change from today: a Rejected
  rental that misses one probe currently reads Healthy, because the
  hold-over is computed from `last_ok` and `fails` without knowing the
  rental was Rejected (quirk 1 in `rental-lifecycle.md`). With the previous
  state in the row it stays Rejected under the threshold and goes
  Unhealthy at it. That is one of two behaviours the matrix changes. The
  other is the first pass after a restart: a silent rental with a route
  in LiteLLM reads Awaiting, not Unhealthy, and the route is deleted on
  that pass by the plan rather than by the state, so what LiteLLM sees is
  the same. Everything else reproduces `DeploymentSnapshot.lifecycle`, and the
  fixtures from the prior version's table are the proof.

**What the step reads and keeps.** The observation for one pass is the
probe (its column, and the window when `/v1/models` carried one), the
loading-start candidates (the config fetch time, the first reached
probe), and whether the host is unlisted. The history the step keeps is
`state`, `fails`, `last_ok`, `first_probed_at`, `first_reached_at`,
`config_served_at`, `key_rejected` (standing until asked again, which is
why "serving" can mean "not asked and last accepted"), `rejected_passes`,
and the observed window. Whether LiteLLM has the deployment is neither:
the step never reads it. The Deployment holds its own LiteLLM rows for
the plan, which is the only reader of that fact. The one thing that
clears history is a new rental at the same key, which is a new rental to
be checked afresh.

**A provider line** has no vLLM probe. Its machine is three states and
one source: Configured until LiteLLM's `/health` has spoken, then Healthy
or Unhealthy as it says, back to Configured when that read is stale. Its
observation is the `LiteLLMHealth` the health read produced for it,
where the rental's is a `VllmProbe`: the same step, a different
observation argument. There is no `LiteLLMProbe` class, because LiteLLM
is two sources on two clocks, `/model/info` (the rows, read by the plan)
and `/health` (the verdict, read by the step), and each already has its
record beside its reader. One addition is owed: `LiteLLMHealth` carries
only the boolean and `at`, while the `/health` body gives an error string
for each unhealthy endpoint and `VllmProbe` has an `error` field for the
same purpose. `LiteLLMHealth.error` gives the admin the reason on the
Deployments row.

### Needs

The probe runner, each pass, for each Deployment with an address: fetch
`/health`, hand the probe over, then ask the Deployment what it needs
now, fetch that, hand it over. "Once, on the edge" falls out: after a
rental restarts the Deployment steps back through Starting, its window and
cache size are unknown again, and it asks. The runner reads the
Deployment's answer, not its own notion of a transition, and holds no
state.

### Commits

Two phases. Phase A is the collapse of the four classes into one and
changes no behaviour: every commit keeps the whole suite passing with
only mechanical edits to tests, in the manner of the three commits of
2026-11-26 and 27 (`DeploymentSnapshot.status`, `FleetDeployment.snapshot`
and `status`, `DeploymentSnapshot.lifecycle`). Phase B is the matrix,
which changes the two behaviours named above, and rewrites methods of
one class instead of relocating state across three. Small commits, each
acknowledged and pushed before the next.

**Phase A: one class.**

1. **`VllmServer` folds into `FleetDeployment`.** The server's fields and
   methods move onto the deployment, `from_vllm` goes, `Fleet.servers`
   becomes the self-hosted deployments, and `dep.vllm.x` reads become
   `dep.x`. Tests that built a `VllmServer` build a deployment. The
   largest sweep, scripted like the 27-call sweep of 2026-11-26; it can
   be two commits if the diff is too long to review (the probe fields
   and `record_probe` first, the address and URL helpers second).
2. **The deployment holds its intent.** `intent: IntendedDeployment |
   None` replaces the copied fields (`model_name`, `name`, `api_base`,
   `provider`, `litellm_model`, the recipe's values), which become
   properties reading it. None is an orphan, when orphans arrive.
3. **The rules read the deployment.** `lifecycle` and `status` become
   methods of `FleetDeployment` reading its own fields; `record_probe`
   reads `self.in_litellm` instead of taking it, and `DeploymentSnapshot`
   is deleted. (Import direction holds: fleet.py imports model_status.py
   for the constants; the rules move up, not the class down.)
4. **`DeploymentStatus` goes.** Its ten fields become properties and
   methods of the deployment: `lifecycle`, `vllm_signal`,
   `litellm_signal`, `skew`, `counted`. The thresholds they read
   (`health_grace_s`, `health_fail_threshold`, the two intervals) are
   handed to the deployment once, by `Fleet` at construction, so the
   reads take `now` and nothing else; `model_status()` takes the
   deployments of one name.
5. **`FleetDeployment` becomes `Deployment` in `deployment.py`**, with
   model_status.py's constants, `ModelStatus` and `model_status()`;
   model_status.py is deleted. deployment.py imports the source records
   and nothing of fleet.py; fleet.py imports deployment.py.

After Phase A there is one class with state per deployment, and the two
symptoms under "Where we are" are gone: nothing is rebuilt on a call, and
`in_litellm` is a read of the deployment's own rows. The lifecycle is
still recomputed from the counters on every read.

**Phase B: the matrix.**

6. **`History` and `step` in deployment.py.** The frozen record of the
   position and the clocks, and the matrix above as code, one test per
   cell in `tests/test_deployment.py`, plus the walked fixtures (the dead
   rental, the reassigned address, the listing gap, the restarts). Used
   by nothing yet.
7. **The deployment holds a `History`.** `Deployment.observe(probe, now)`
   stores what `step` returns; the counters on the deployment become
   reads of the history, and the recomputation (`lifecycle`) is deleted.
   The two decided behaviour changes land here: Rejected under silence
   holds over and goes Unhealthy, and the first pass after a restart
   reads Awaiting with the route deletion decided in the list of
   actions.
8. **The reads off the stepped state**: `verdict`, `reason`, `badges`
   and `needs`, and the pages read them.
9. **The provider line's machine** goes through the same step, with its
   `LiteLLMHealth` as the observation; `LiteLLMHealth.error` is added
   from the `/health` body.

### After this step

`reconcile.py` gains the join and the plan: `Unintended` becomes its
grouping, orphans and duplicates become plan actions, the needs replace
the `/v1/models` rule in `probe_all`, and the executor runs the plan.
Durations in place of the pass counts wait on the p6 knob rename. Then
this file is folded into `ARCHITECTURE.md` and `rental-lifecycle.md`.

## Import direction

Unchanged. The modules are layered and an import only ever points down:
`models.py`, `reconcile.py` (the key) and the seven source modules at the
bottom, each a leaf; then `deployment.py`, which reads the snapshot
shapes and nothing else; then `fleet.py`, the runners and the view; at
the top `main.py`, `pages.py` and `api.py`. Where a lower module needs
something a higher one owns, `main.py` constructs it once and hands it
down.
