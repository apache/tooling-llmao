# Fleet reconciliation: design (work in progress)

Status: a working document. It is edited as the work lands, so what it
says is the current intent and the current state, not a record of either.
Sections marked **Done** name the commit. The document is meant to become
obsolete: once the reconcile loop is the code, its outcome is folded into
the architecture documentation (`ARCHITECTURE.md`) and this file is
removed. The box side of the fleet, the one request a box makes and what
it gets back, is `box-protocol.md` and is not changed by this work.

## The problem

llmao keeps several sources in step. They run on different clocks, and
today they are folded into flags on shared objects.

| Source | Named | What | Keyed by | Cadence |
|---|---|---|---|---|
| **Intended** | `IntendedDeployment` | one row of `fleet.hosts`, one in-memory approval, or one `config.yaml` provider line; `models.yaml` supplies the recipe | the key | process start, approve, config load |
| **Box protocol** | `ConfigFetch` | the last `GET /vllm/config` a host fetched, and when | host | on each fetch |
| **GPU inventory** | `Rental`, in a `ProviderRentals` set | what one provider reports about one machine: id, status, address, listen-port to public-port map, price | provider id; matched to a host by address, then by the id it had last time | each provider's listing interval |
| **vLLM health** | `VllmProbe` | what the probe found at an address: reached, `/health` ok, the key verdict and served window from `/v1/models`, the cache size (the last three only when asked for; see Needs) | the key | `health_interval_s` |
| **LiteLLM deployments** | `LiteLLMDeployment` | what `GET /model/info` has for the key, found by the `llmao_key` stamp | the key | `skew_interval_s` |
| **LiteLLM health** | `LiteLLMHealth` | LiteLLM's own `GET /health` verdict on a deployment, and when | the key | `litellm_health_interval_s` (~4h) |

Two of the six are per host, not per key: `ConfigFetch` and `Rental`.
Every deployment on the same machine reads the same values through its
host. Nothing else is host-level; the machine is not an object of its
own (see Vocabulary).

Terms. "Deployment" is LiteLLM's word (`Router.get_deployment`), and
ours. LiteLLM calls the whole set its `model_list`; this doc does not,
because `models.yaml` is a different thing and is always named in full.
"Model" on its own means one of our public model names, a row of
`models.yaml`. "The LiteLLM deployments" is the set LiteLLM holds, as
`GET /model/info` reports it.

### Vocabulary (settled 2026-10-26)

Every name says where the thing comes from. One name says what we
concluded. Older sections of this document still say box, instance, pod
and listing; they are rewritten as the code moves, and these names win
where they differ.

**Observed, one per source.** Each is an immutable snapshot of what one
source said, with the time it was read, and nothing of ours on it.

- **Rental**: what a provider reports about one machine we rent: its id,
  status (running, stopped, initializing), address, listen-port to
  public-port map, price. Replaces `box`, `instance` and `pod`.
  **ProviderRentals** is one provider's set of Rentals as of one fetch
  (replaces `vast_boxes`, `runpod_boxes`). A Rental no deployment matches
  is just inventory; a deployment no Rental matches is **unlisted**.
- **ConfigFetch**: the last `GET /vllm/config` a host made, and when. The
  anchor for the loading window. Today a timestamp on each server.
- **VllmProbe**: what one probe of one address found. It carries the
  engine's name because it is not engine-neutral: `/health` and its 200
  versus 503 are vLLM's, the served window is vLLM's `max_model_len`
  field on `/v1/models`, and the cache size is vLLM's
  `cache_config_info` line on `/metrics`. Another engine gets its own
  probe class, not an `if` in this one. Replaces the probe counters on
  `VllmServer`; `VllmServer` itself goes unused.
- **LiteLLMDeployment**: what LiteLLM has for the key, as `GET
  /model/info` reports it. Replaces `ExtraLiteLLM` and the route tuples.
- **LiteLLMHealth**: LiteLLM's own `/health` verdict on that deployment,
  and when. Its own source because it has its own cadence and its own
  staleness; today two fields on the deployment.

**Intended, one.**

- **IntendedDeployment**: one row of `fleet.hosts`, one approval, or one
  commercial provider line: host, listen port, model; the key. Nothing
  intended is ever called a server.

**The join, one.**

- **Deployment**: one per key. Holds the IntendedDeployment (or none,
  for an orphan), the latest of each observation, its own history, and
  the state machine. It is the only object with state, and the only
  place a lifecycle name is read. Out of it come a **verdict**, a
  **reason** sentence, the **needs** (below) and the plan actions. The
  verdict has four values, and they are the whole public surface:
  - **Starting**: leave it alone (Awaiting, Loading inside).
  - **Serving**: route to it (Healthy with the key accepted).
  - **Down**: take it out of LiteLLM, tell an admin (Unhealthy, Stalled,
    Rejected inside).
  - **NotOurs**: ignore it, say so (Foreign).
  The executor adds when a Deployment is wanted and absent, deletes when
  present and not wanted, where wanted is "Serving and the address is
  known". The Remove button, the attention strip and the badge read the
  same verdict. The fine state names are display only; adding one
  changes Deployment and nothing else. A commercial Deployment reads only
  the intended and the two LiteLLM sources, and has the short machine:
  Configured, Serving, Down.

**Not objects.** The machine itself is not one: everything true of it is
either a Rental (the provider's) or a per-host observation a Deployment
reads through its host. The admin labels (Approved, Reboot requested)
stay a host-keyed annotation on `Fleet`, outside the machine. The
metrics sampler is a consumer of the view, not a source: it reads
`/metrics` for the Serving deployments on its own interval and hands
samples to the capacity rollup, and it no longer writes the cache size
onto anything; the probe owns that read.

**Placing** a host is matching it to a Rental, by address first and then
by the id of the Rental it matched last time, which is what keeps a
deployment on a stopped pod with no address, or on a restarted one under
a new address. The **placing provider** is read off the Rental and
remembered nowhere. A public port is not a field of `fleet.hosts`; both
providers' Rentals carry the port map, and that map is the dial address.
**Rejected** and **Foreign** are fine states added here. Rejected: the
address answers vLLM's `/health`, which takes no key, but rejects the key
`working_api_key` returns on `/v1/models` (`fleet.selfhost_api_key` for
`LEGACY_SERVERS`, the derived key otherwise). Our box with the wrong key,
or the salt changed; a person fixes it. Foreign: Rejected for
`REJECTED_PASSES` passes running at an address no Rental names.
Something else is serving there.

**Durations, not pass counts, for the knobs that face config.** "Down
after `health_fail_threshold` failed passes" becomes "down after N
seconds without a passing probe", and "stale after three missed
intervals" becomes "stale after N seconds", so a change of probe pace
changes no verdict. The internal debounces (`UNLISTED_PASSES`,
`ORPHAN_PASSES`, `REJECTED_PASSES`) stay counts; they are not config.
Settled 2026-10-26; the p6 change that renames the knob comes with the
code that reads it.

A commercial model provider (OpenRouter, xAI) has no GPU listing; it is
intended plus the two LiteLLM sources. A GPU host listing is the inventory
source. A GPU provider with no API key is not a source: `place_servers`
already treats that as `vast_wanted` / `runpod_wanted` false. `None` means
configured and not yet read, which is different from an empty list and
different from not configured. Mixing inventory and probes under one
"Live" hid the failure in 65e22c8: the listing said absent, the probe
said Healthy.

The box protocol is an edge, not a level: a host fetches its config once.
It still gets a snapshot of its own (host → stamp of the last fetch),
replaced by the handler and folded by reconcile, so the HTTP handler
never writes history. Today that stamp is `config_served_at` on the
server object, set from three places: the handler, and
`adopt_vast_instances` for an approved host answering 503 and for a
discovered Healthy port. Those two adopt stamps go away with adopt.

The stamp anchors the Loading window: Loading is inside `health_grace_s`
of it, Stalled is past it. One of the adopt stamps existed because an
approved host that fetched its config before an llmao restart never
fetches again, so without a stamp it would read Awaiting for ever and
never Stalled. The anchor is therefore **the config fetch or the first
probe that reached the box (`reached`, a 503 counts), whichever is
earlier**. A host that has done neither reads Awaiting. The anchor is a
value reconcile computes from two snapshots; it is not kept in history.

The work of keeping them in step is spread around and tightly coupled.
Different runners set mutable flags on shared objects: `srv.state`,
`srv.box`, `srv.unlisted`, `dep.in_litellm`, `dep.at_listen_port`. Each
place that reads those flags then decides for itself what they mean:
`sync_selfhost`, `check_config_skew`, `check_health_skew`,
`add_refusal`/`remove_refusal` and `place_servers`, and on the pages
`_needs_attention`, `_deployment_reason` and `admin_actions`. A new fact,
such as "unlisted", means touching all of them, and they drift apart.

65e22c8 shows the cost. `sync_selfhost` and `remove_refusal` are meant to
apply "the same gate". An earlier commit changed one of them and not the
other, and a serving box's deployment was deleted and re-added every 47
seconds for as long as its provider's listing left it out. The fix added
three more pieces of history, each with its own hand-written rule, in two
classes: `unlisted_passes` and `ever_listed` on `VllmServer`, and
`_extra_passes` on `DeploymentManager`. After that fix the serving-box
invariant holds, and the delete condition is still two tables:
`sync_selfhost` deletes when Unhealthy, Stalled, Rejected or Foreign, or
unlisted and not Healthy; `remove_refusal` allows the four states but
knows nothing of unlisted; `admin_actions` offers no buttons at all when
unlisted.

## The pattern: observe, compute the gap, act

This is the reconciliation loop: how a Kubernetes controller works, and
Terraform's plan and apply. Each pass works out the gap afresh from what
is observed, rather than updating state step by step from many places.

1. **Sources produce snapshots.** Each source in the table above produces
   an immutable snapshot that records when it was taken. A snapshot is
   `None` until the source has been read for the first time, which is
   different from an empty one. The "unlisted" rule already depends on
   that difference; here it applies to every source. A failed fetch
   leaves the last good snapshot in place; it does not publish
   empty-as-absent. One exception: a fetch that was confirming a write
   to LiteLLM. If that GET fails, the LiteLLM deployments are unread
   until a GET succeeds. The pre-write snapshot must not stay in force
   and look like a fresh absence, because `/model/new` is not an upsert
   and the next pass would add a second deployment. Unread means no add,
   no delete, and no prune. Inventory has a third state, not configured,
   for a GPU provider with no API key. That source is ignored. `None`
   (configured, never read) does not count as an absence.

   An inventory snapshot keeps every instance the provider reports, with
   its status, not only the ones llmao would use. Until e4a5d6b,
   `boxes_from_pods` kept a RunPod pod only when its `desiredStatus` was
   `RUNNING` and it had a `publicIp`, so a pod in any other status, or
   briefly without an address, was absent from llmao's view while vLLM
   on it may still have been answering. That was one way a serving box
   "left the list". A host that is in the listing with another status
   is **listed, not running**: it has no usable address from the
   listing, but it is not a miss for the unlisted count. Only absence
   from the listing is a miss. The same fields, including the port map,
   and the same listed-not-running rule, for Vast and for RunPod. Both
   inventories are keyed by the provider's instance or pod id and carry
   `host` and `status`; a server is placed by address first, then by the
   id of the box it was on, which is what keeps it on a stopped pod with
   no address, or on a restarted one under a new address.

   **Staleness is one rule.** A snapshot kept past its fetch failing
   ages, so a snapshot older than `health_fail_threshold` times its own
   interval counts as unread: a stale inventory does not advance an
   unlisted count, a stale read of the LiteLLM deployments adds, deletes
   and prunes nothing, a stale probe is the `stale` signal
   `model_status._stale` already computes. The same rule, the same
   multiplier, for every source. That multiplier is already what
   `_stale` uses for the probe and for LiteLLM `/health`.

   A counter advances only when that source publishes a new snapshot,
   one whose taken-at changed. Reconcile waking because some other
   source changed, or because the tick fired, is not another miss.

2. **One pure function decides.**
   `reconcile(snapshots, history) -> (view, plan)`.
   - The **view** holds one **record** per deployment, joined across the
     sources by the key (below). The record is the detail: intended?,
     inventory, dialable URL, probe observation, `config_served_at`,
     presence among the LiteLLM deployments, LiteLLM health, instance id,
     and the verdicts reconcile reached on this pass: lifecycle
     (`Awaiting`, `Loading`, `Healthy`, `Unhealthy`, `Stalled`, the names
     `model_status.py` uses today, plus `Rejected` and `Foreign`), skew badges,
     unlisted. Those are
     **computed once per pass, by reconcile, and written on the record**;
     reconcile needs them to plan (delete when Stalled), and the pages
     read them rather than computing them a second time. Nothing derived
     is carried from one pass to the next except through history, and
     the lifecycle is carried that way on purpose: the step reads the
     last verdict (see Needs, the ruling of 2026-10-26). The
     record is `DeploymentSnapshot`, extended, not something it is
     converted into. The state diagram becomes a transition table that
     can be read in one place: previous state, observation, next state
     (the appendix's decision table gains that first column).
   - **Discovered servers are computed, not added.** Today
     `adopt_vast_instances` probes inventory hosts that are not in
     `fleet.hosts` and, on a match, mutates the server list. Under
     reconcile, the vLLM health fetcher probes those hosts too, from the
     inventory snapshot's listen-port to public-port map, and
     "discovered" is a record reconcile makes from an inventory entry
     with no intended row and a probe that named a `models.yaml` model.
     The view cannot be the target list for that probe: the host has no
     record until the probe has named the model.
   - The **plan** is a list of actions. Each action names a kind, a key
     and a reason. A **policy table** then marks each kind `auto` or
     `needs_human`. Reconcile does not embed that policy.

3. **History is the only state that changes.** It holds only what needs
   memory: first seen, last ok, last dialable URL, last instance id, the
   counters that stop one missed probe or one missing listing from
   becoming a verdict, and the last verdict, which the step reads as its
   starting state and which also lets a change be logged once.
   It is small and explicit. (`config_served_at` is not in it: that is
   the box protocol snapshot, and a restart loses both alike.) The pure
   function returns the next history and the loop stores it. Fetchers,
   the executor, and the pages do not write it. Nothing of it is in the
   memory store.

   Fetchers replace their snapshot. They neither read nor write history;
   what they need to know (which URL to dial) they take from the view.
   Approve and a config load publish a new intended snapshot. `GET
   /vllm/config` publishes the box protocol snapshot. The executor runs
   plan actions and does not write history: **every write to LiteLLM is
   followed at once by a fetch of the LiteLLM deployments**, which publishes a new
   snapshot and wakes reconcile. That is how a write is confirmed, and
   how a button changes state by the next page load. Admin labels and
   `approved_models` are intended, not history. The action log stays its
   own store.

   Hysteresis is one **pattern**: consecutive misses, reset on a hit, and
   an unread or stale snapshot is not a miss. A miss is a new snapshot.
   Each counter is its own history entry and keeps its own threshold.
   `health_fail_threshold` stays a `fleet.*` config key (probe clock,
   ~45s). Two constants, so there is no p6/eyaml round-trip.
   `UNLISTED_PASSES` counts passes on which both listings were fresh and
   neither named the host. `ORPHAN_PASSES` counts new snapshots of the LiteLLM
   deployments (`skew_interval_s`, 180s). A pass is one fresh round of
   both listings: the count moves when both snapshots are fresh and
   each has a taken-at newer than the one counted last, so a fast
   fetcher is never counted twice against one read of the other. Both
   providers are polled on one listing interval, a constant of about a
   minute (today Vast rides on the probe pass and RunPod is on its own
   600s runner, chosen because placing was not urgent, not from a rate
   limit; RunPod's documented limit for the pods endpoint is to be
   checked once before the constant moves). A few passes is the whole
   wait, for a Vast host and a RunPod host alike. That is short by
   choice: `UNLISTED_PASSES` no longer has to outlast a provisioning
   gap, because Loading is quiet and unlisted is attention-only; the
   constant's comment in `fleet.py` already says so. A listing that does
   not answer is stale, and the count does not move. A long silence
   from either provider is not an absence.

   Unlisted is a **host** verdict — one instance, several listen ports —
   applied to every key on that host. The "remove it from `fleet.hosts`"
   sentence waits until the verdict is true and the lifecycle is
   Unhealthy, Stalled, or Foreign. Awaiting, Loading and Healthy stay
   quiet; Healthy keeps the route, because the probe still passes. When
   the probe fails, the delete is the probe gate's and the sentence is
   unlisted's.

   **A probe that does not reach, with no anchor, runs out of grace.**
   Before item 9, a failed probe with no anchor read Unhealthy only while
   the deployment existed (`model_status.py`); the moment the probe gate
   deleted it, the same box read Awaiting, and a rental that died before
   a restart never showed the sentence once the delete landed. Since
   c2e3c14 it reads Stalled once `health_grace_s` has passed since the
   first probe attempt (`first_probed_at`); the rest of this paragraph is
   the rule as reconcile keeps it.
   Under reconcile, Awaiting means no probe has been attempted in this
   process. A record with no anchor whose probes do not reach (no
   connection, `reached` false) is Awaiting until `health_grace_s`
   after the first attempt, then **Stalled**. No new lifecycle, and the
   sentence rule stays the three names. The first attempt is not an
   anchor: a dead rental with its deployment still present reads
   Unhealthy on the no-anchor rule, as today, and is deleted at once.
   After that delete the record is Awaiting until the grace runs out,
   about thirty minutes after the restart, and the sentence shows then.
   That gap is accepted. The clock is the grace constant, and not
   `UNLISTED_PASSES`, because on a one-minute listing clock a new
   RunPod row is unlisted within minutes, while the pod it waits for is
   absent from the listing for over ninety minutes and fetches
   `/vllm/config` only after its image pull; the sentence must not show
   for a box the admin created five minutes ago. A probe that reaches
   (a 503) is the anchor and the row above.

   **An unlisted host is probed with the key we believe that process
   has.** `/health` takes no key, so a passing probe alone cannot tell
   our box from another tenant's vLLM at an address a provider has
   reassigned. llmao calls `/v1/models` on the edge into Healthy
   (for `max_model_len`). It used to throw a 401 away, and to send
   `srv.api_key`, the derived key, including for `LEGACY_SERVERS`, so a
   legacy box answered 401 on every pass and never got
   `observed_max_model_len`; under the Foreign rule the derived key
   would have deleted those boxes. Both are fixed (items 5 and 6): the
   rule that picks the key is `working_api_key` in `fleet.py`, a function
   of host, listen port and config, which the deployment body and the
   `/v1/models` fetch (`fetch_served_models`) both call. The fetch sends
   that key, the status is kept, and the call
   is repeated on every probe pass while the last answer was a 401 or
   the host is unlisted. A 401 makes the lifecycle **Rejected** (ruled
   2026-10-25): the deployment is deleted by the probe gate on the first
   401, because no request through it could succeed; the record stays
   and is probed on; the attention item says the box is up but rejects
   our key, and asks for the key on the box to be checked. Rejected has
   no clock. It ends when a later `/v1/models` accepts the key (Healthy,
   and the add fires on that pass), when `/health` stops answering (the
   ordinary probe rules), or when the host is also unlisted and the 401s
   have run for `REJECTED_PASSES` passes, which is **Foreign**: the
   sentence shows. A host a listing still names is ours whatever it
   answers, so it is never Foreign. `REJECTED_PASSES` is a debounce
   against a transient 401 (vLLM restarting before its key is in
   place): three passes, about two minutes at the 45-second interval.
   The time an admin needs to notice and fix the key comes from Rejected
   having no clock, not from the count. A wrong `vllm_api_salt` shows up
   as Rejected on every box, which is right.

   **Unlisted is computed from both listings.** Production config
   provides the Vast key and the RunPod key, so every pass reads both.
   A missing key is not a fleet mode, and a Vast host does not get a
   faster count by ignoring RunPod. On `UNLISTED_PASSES` passes running,
   both listings were read and fresh, and neither named the host.
   Nothing records which provider a host belongs to. A pass on which
   either listing is unread or stale is not a miss, and the count does
   not move, so one provider's outage says nothing about anyone's hosts.
   A fleet with neither key (a laptop) has no listings, and unlisted is
   never computed; it is not a mode, only two sources that are not
   configured.

   This drops `ever_listed`. That guard existed for a host rented from a
   provider with no key, which would be absent from every listing and
   called unlisted for ever. Production has both keys, so that host does
   not exist. The guard is not worth what it costs on the hosts that do:
   the wrong verdict would be a sentence, "remove it from `fleet.hosts`",
   and never an action. The probe gate alone deletes, and it never
   deletes a box that answers with our key.

   What the guard cost was the restart hole and the fix for it. A rental
   that ends while llmao is down is, after the restart, a host no listing
   has ever named, so `ever_listed` never fires and the sentence never
   shows. Closing that meant persisting which provider placed the host,
   keyed by IP, in the memory store: a guess that lives for ever, goes
   stale when a provider reuses an address, and needed a hand rule
   (remove the row and add it back) to clear. Without the guard there is
   no hole: after a restart the listings are read within a pass and the
   verdict returns after `UNLISTED_PASSES`.

   Discovered hosts are not in `fleet.hosts`, so the sentence never
   applies to them. When one leaves the inventory, a LiteLLM deployment
   it still has is an orphan.

4. **An executor carries out the plan.** It runs the `auto` actions
   idempotently. It never trusts its own writes: the LiteLLM deployments
   it fetches straight after confirm them. If that fetch fails, the
   source is unread until a later fetch succeeds.

5. **The plan is what /models and /admin show.** The "Work needed"
   strip, the reason text, and the action buttons all come from it. The
   pages no longer reason for themselves, so the automatic path and the
   button cannot drift apart. `needs_human` is not always a button.
   Add, Stop tracking, Approve, and the labels post. "Remove it from
   `fleet.hosts`" is a sentence when the host is unlisted and the
   lifecycle is Unhealthy, Stalled, or Foreign. Awaiting, Loading, and
   Healthy stay quiet. The page does not write config.yaml.

## Identity: (canonical URL, model)

Every source names every deployment by one key: a **canonical base URL**
and a **model name**. One shape covers our boxes and the providers we buy
from alike. Today a provider deployment is already identified by
(api_base, model), because one provider base serves many models. Our boxes
are identified by api_base alone, plus matching on host and port. That
split, and the separate "ours" and "bought" sets in `check_config_skew`,
both go away.

config.yaml names a host and a listen port, not a URL. One function
fashions the canonical URL from the provider, the host and port, and a
known endpoint construction, then runs `_norm_base`. The stored key, the
comparison, and `model_info.llmao_key` are that result, the same bytes.

`_norm_base` strips a trailing slash and a trailing `/v1`. That is why
`asf_api_base` is stored without `/v1` today. The self-hosted input is
`http://{host}:{listen_port}/v1` (peer IP normalised, listen port the one
the vLLM key is derived from). After `_norm_base` the canonical URL is
`http://{host}:{listen_port}`. A commercial canonical URL is
`_norm_base(api_base)`.

The key is that URL plus our public model name.

- **The model is our public model name,** LiteLLM's `model_name`, not the
  provider's model string. Two of our names can map to one provider model.
  The access-control draft proposes `gemma4-26b` and a paid-at-will variant
  on the same OpenRouter model, for example; they must stay two
  deployments.
- **One function builds every key.** It handles the scheme, the trailing
  slash, `/v1`, the case of the host name and normalising the peer IP.
  If two code paths each build keys their own way, the identity bugs
  return.
- **Canonical is not dialable on a remapped box.** The listen-port URL is
  the identity; the public HostPort URL is what LiteLLM dials (see
  below). They are two fields. `model_info.asf_api_base` is the dialable
  URL last written into LiteLLM (`_norm_base` of `api_base` at
  `/model/new`). After a remap it lags the record's dialable URL until
  that update lands. The stamp is the canonical URL. A remapped
  port leaves the key, and its history, as they are.
- **The key is never rendered as a URL.** A page shows a self-hosted key
  as `host:listen_port` and a commercial one by its provider's name, the
  forms the pages use today. The canonical URL is not dialable, and a URL
  on a page gets pasted. Capacity rows are keyed by the key and shown the
  same way; their store key is already `host:port`.
- **The key is not driven by Vast.** A Vast instance id exists only at
  Vast, after we ask; config.yaml, LiteLLM, RunPod and the commercial
  providers have none. As a key it would make every non-Vast record a
  special case. It is an **attribute** the live inventory attaches to a
  key, with the RunPod pod id beside it. A different instance id under the
  same key means a new rental at the same address: its history is reset
  and the box is checked afresh.

### Stamping the key into LiteLLM

llmao creates every self-hosted LiteLLM deployment, so it can record the
canonical URL in the deployment's `model_info` (for example `llmao_key`).
LiteLLM's deployments then say which deployment they are, with no
translation from public port to listen port. `asf_api_base` stays the
dialable URL last registered, which can lag the record's dialable URL.

**Done (2026-10-22).** The stamp is `model_info.llmao_key`, written on
every `/model/new` (52818a2), and the id lookup reads it first. The
backfill was done in place, by hand, against the running proxy. What the
hand check found, in LiteLLM 1.105.0:

- `POST /model/update` **never writes `model_info`.** It merges
  `litellm_params` key by key (a key sent as null keeps the stored
  value), can rename, and requires `litellm_params` in the body. It is
  the call for a port remap, and nothing else.
- `PATCH /model/{id}/update` **merges `model_info`** with the stored
  blob, and leaves `litellm_params` alone when they are omitted. The
  body is `{"model_info": {"id": ..., "llmao_key": ...}}`. Our fields
  (`self_hosted`, `asf_api_base`, `access_groups`) survived.
- The merge strips stored cost-map echoes, and that set includes
  `max_tokens`, `max_input_tokens` and `max_output_tokens`. All three
  rows lost them (a copy of a lookup made the day each row was created;
  nothing re-derives it for a hosted vLLM model). llmao reads none of
  them: the context window comes from vLLM. LiteLLM reads
  `max_input_tokens` only for its pre-call context check, which
  production does not enable. Accepted.
- Both handlers update the router before answering, so a `GET
  /model/info` straight after a write sees it (the confirm read, 7bd46bc).

Every production row is stamped, so the generation is one. The address
match in the id lookup stays only as the fallback for a row no llmao
stamped, and goes when the executor is the only writer.
`LEGACY_SERVERS` were stamped from their known listen ports; the executor
still passes `fleet.selfhost_api_key` for those pairs.

## The dialable URL: a field on the record

The URL that can actually be reached, `http://host:public_port/v1` on a
Vast box, is **a field reconcile computes** on each record, from, in
order: the current port map from the listing that has the host and is
running (Vast or RunPod; both are configured in production), else the
last-known dialable URL, which history carries forward, else the
canonical listen URL. The last step is unconditional: it is what a
laptop with neither key dials, and what our own box dials in a fleet
with both keys, a host neither listing names and so never maps. A dead
rental after a restart is probed at its listen port on an address the
provider may have reassigned; that fails, or goes Rejected and then
Foreign, and is harmless. Unplaced, a record with no address, is therefore only a
record with no host. For commercial providers the dialable URL is the
canonical one. A public port written on the `fleet.hosts` row is not a
dial address.

It is a field and not a function so that the decision is made in one
place and tested as table rows, and so that fetchers never consult
history: they take their targets from the view. Last-known is
load-bearing. `place_servers` already leaves `public_port` in place when
it clears `srv.box` for an unlisted host, so probes of a box that still
answers can continue. Computing the address from the current listing
alone would make that box unplaced, unprobeable, then deleted — the
e1567cb failure again. Health snapshot rows record the dialable URL they
used, so a remap does not attribute an old probe to a new port.

Only code that opens a connection reads the field:

- the body of the LiteLLM deployment (`api_base`);
- the health probe (`/health`);
- the `max_model_len` fetch (`/v1/models`);
- the metrics sampler (`/metrics`).

All four are I/O at the edges, in the fetchers and the executor.
Everything else works with the key alone: reconcile, the plan, the
pages, capacity rows, the action log, log lines, and future access
grants. The sampler already does half of this: it keys capacity rows by
host and listen port and dials the public URL. Today
`api_base`/`health_url`/`root_url`/`public_port` appear about 100 times
across the code.

Two cases become plain:

- **Unplaced.** The record has no address yet, so the plan cannot probe
  or add that deployment, and says why. This is `check_config_skew`'s
  `unplaced` guard, made general.
- **A port remap.** The key is the same, but the dialable URL no longer
  matches the `api_base` LiteLLM holds, so the plan says "update this
  deployment's api_base". Today that case is spread across
  `at_listen_port`, `same_deployment` and `on_listen_port`.
  `POST /model/update` merges `litellm_params`, which is where `api_base`
  lives, so the action is that call, and it does not touch `model_info`,
  so the stamp is safe across it (verified in the backfill; see the
  stamping section).

## Needs: the state holder says what to fetch

Settled 2026-10-26. A runner fetches; it does not know what an edge is.
Today `probe_all` decides when to read `/v1/models` with a rule that
reads the server's previous state, whether the window has been read,
whether the key was refused and whether the host is unlisted. That is
lifecycle knowledge in the fetch loop, and every new state has extended
it. It moves into Deployment.

- A Deployment exposes its **needs**, computed from its own state and
  nothing else: "the window and the key verdict" (`/v1/models` with the
  working key), "the cache size" (the `cache_config_info` line, which
  means one `/metrics` read), or nothing. A Deployment that is not yet
  Serving needs only `/health`. One whose window is unknown, or whose key
  was refused, or whose host no Rental names, needs `/v1/models`. One
  whose cache size is unknown needs the cache line. One that is Serving
  with everything known needs nothing, and its pass is one cheap request.
- The probe runner, each pass, for each Deployment with an address:
  fetch `/health`, hand the VllmProbe to the Deployment, then ask it what
  it needs now, fetch that, hand it over. The same pass, because the
  runner reads the Deployment's answer after the first result, not its
  own notion of a transition. No memory between passes, no state names.
- "Once, on the edge" falls out rather than being coded: after a box
  restarts the Deployment steps back through Starting, its window and
  cache size are unknown again, and it asks. Nothing re-reads a fixed
  value while the process lives.
- A need is a read action, beside the write actions the plan already
  emits for LiteLLM. The runner executes the reads, the executor the
  writes, and neither decides anything.

**Settled 2026-10-26: the last verdict in history is read by the step,
not only logged.** History already holds the last verdict (rule 3 of
the pattern, "so a change is logged once"), and the lifecycle function
has never been handed it. Today the lifecycle is recomputed from a dozen
counters on every pass, which is why the anchor makes the same
observation name a different state, and why a Rejected deployment that
misses one probe reads Healthy through the hold-over rule
(`box-lifecycle.md`, quirk 1). The counters are history too; the one
fact that was never an input is what we concluded last time. So the
Deployment's state is its history, and each arriving observation steps
it through one transition table: `step(state, observation) -> state`,
pure and testable. The previous state is the table's first column; the
table is the diagram, and the diagram is checkable against the code, one
test per row. The pattern below stands unchanged: one pure function, one
history store, nothing derived carried between passes except through
history. This does not reintroduce edges into the runners; the Deployment
holds the history, the runners hold none.

## Policy

`reconcile()` emits actions with a kind and a reason. The policy table
marks each kind auto or `needs_human`. v1 matches today's gates, and
self-hosted and commercial are different gates:

- **Auto, self-hosted:** add when Healthy, dialable, and absent from the
  LiteLLM deployments. Delete when the deployment exists and vLLM is
  Unhealthy, Stalled, Rejected or Foreign; Rejected deletes on the
  first 401, with no hold, because the refusal is deterministic and
  every request through the deployment would fail. A failed probe under
  `health_fail_threshold`, with `last_ok` set, stays Healthy and is not
  deleted. Prune unmatched orphans after `ORPHAN_PASSES`.
- **Unlisted is not a delete gate.** Today `sync_selfhost` also deletes
  when unlisted and not Healthy. Once `last_ok` is set, a box that is not
  Healthy is Unhealthy, and the probe gate already deletes that. The
  extra clause tied the listing count to the delete, which is what
  produced 65e22c8. Unlisted is an attention verdict. The sentence, and
  no buttons, show when the lifecycle is Unhealthy, Stalled, or Foreign.
  Awaiting means not attempted yet, and a not-reached record with no
  anchor becomes Stalled when the grace runs out. Loading stays quiet.
  While the probe passes with our key, a listing gap keeps the route
  and stays quiet.
- **Auto, commercial:** add when absent from the LiteLLM deployments, including
  while LiteLLM health is still unknown (`Configured`). Do not delete when
  LiteLLM later marks the endpoint unhealthy. `ensure_providers` is
  startup-only today; under reconcile this add runs every pass.
- **Deployments are config-truth for membership and api_base.** v1
  reverts two hand edits, and the action log records the revert. A
  deployment deleted in LiteLLM's UI is added again when the add gate is
  open: self-hosted Healthy and dialable, or commercial and absent. An
  Unhealthy row stays absent. An `api_base` that does not match the
  dialable URL is updated. Body fields (`access_groups`, `order`,
  `hosted_vllm/` prefix, working API key) are later skew, and v1 does
  not rewrite them. `watch_outside_edits` keeps reporting pretend-project
  team drift, which is all `check_outside_edits` does today. It does not
  report keys, it does not revert, and reconcile does not take it over.
- **Human:** approve, labels, and anything that spends money or destroys
  a box. "Remove it from `fleet.hosts`" is a sentence on the attention
  item, not a POST.
- No v1 switch that holds production LiteLLM writes for approval. The
  read-only alongside phase is that net.
- vLLM-up / LiteLLM-down, and the reverse, are badges. LiteLLM `/health`
  is on a four-hour clock. They do not add or delete.

Also:

- First cut diffs presence among the LiteLLM deployments and dialable `api_base`. Body fields
  (`access_groups`, `order`, `hosted_vllm/` prefix, working API key) are
  later skew, not v1.
- `adopt_vast_instances` materializing a server from live `/v1/models`
  with no approval is **discovered**, not `fleet.hosts`. Reconcile
  computes the record; it does not add a server. While the record is
  Healthy and dialable, add runs, as it does today. A restart does not
  forget it: the next pass recomputes it while the listing and the probe
  still match. The attention sentence is that it is not in `fleet.hosts`,
  not "remove it". It is not in `fleet.hosts`, so the unlisted sentence
  never applies to it; when the listing drops it, its record goes and
  its deployment is an orphan.
- Two LiteLLM ids for one key: keep the stamped row if there is one,
  else the row whose `api_base` matches the dialable URL. Two stamped
  rows (a double add that slipped past the confirm rule): keep the one
  with the lower LiteLLM id. Any further id is an orphan and follows
  `ORPHAN_PASSES`.
- Unknown fleet-key fetches stay an onboarding strip: still 404 until
  approved, still not a LiteLLM row.
- Served-model skew (GET `/v1/models` vs the intended HF id) stays
  deferred. The dialable field makes that fetch honest; it is not model
  list skew.

## Runners

- One fetcher per source, each on its own schedule (as the RunPod listing
  runner is today). Runners are cheap; two reads with different
  endpoints, cadences and consumers are two runners, never one loop at
  a compromise pace. Each one publishes a new snapshot. The probe runner
  dials the dialable URL on each record in the view, fetches what each
  Deployment says it needs (see Needs), and probes Rentals that have no
  record yet, using that Rental's listen-port to public-port map. It
  does not read history and knows no state names.
- One reconcile loop, woken by an `asyncio.Event` when any snapshot
  changes, and by a slow periodic tick.

The metrics sampler and the capacity runners stay as they are: consumers
of the view, dialing the record's dialable URL for the Serving
deployments on `sample_interval_s`. The sampler hands samples to the
capacity rollup and writes nothing else; the cache size it used to copy
onto the server is the probe's read now (a need, asked once).

## Where the code goes

Settled 2026-10-26. No swarm of modules: three new, one removed, each
with a job of its own. A source's snapshot type lives next to the
fetcher that produces it; a stateful thing lives alone.

- **`rental.py`** (new) knows machines we rent: `Rental`, the frozen row;
  `ProviderRentals`, one provider's set with its fetch time and the
  unread and stale distinctions; the matching rule, a host to its Rental
  by address first and then by the id it matched last time; and the
  host-side verdict, listed, stopped or unlisted, with its one counter.
  `vast_client` and `runpod_client` import `Rental` and return Rentals;
  they keep their parsing of each provider's JSON. `fleet.py` loses
  placement.
- **`vllm_client.py`** (new) knows vLLM: `/health` and its 200 versus
  503, `/v1/models` and the `max_model_len` field, `/metrics` and the
  `cache_config_info` line and the gauges. It produces `VllmProbe`, and
  the metrics `Sample` the sampler hands to the rollup. The window and
  cache-line parsers now in `fleet.py` and `sampler.py` move here; the
  sampler keeps its runner and its rollup. Another engine gets its own
  client module, not an `if` in this one.
- **`provisioning.py`** (new) is the server side of a box provisioning
  itself, the one request a box makes (`hosting/provision.sh` →
  `install_set.py` → `GET /vllm/config`) and what it gets back: the
  endpoint handler, bound to `asfquart.APP` like `pages.py` and
  `api.py` and imported after them in `main.py`; how a box names itself
  (`X-LLMAO-Host`, else the forwarded address, else the peer); the
  payload, host rows or approval to servers JSON; the bearer derivation
  the payload carries and `working_api_key` also calls; `ConfigFetch`,
  host to time of the last fetch; and the unknown-box record (first
  seen, last seen, count, the cap). It replaces `docs/box-protocol.md`'s
  "box" with the box's own verb. Two commits: ConfigFetch and the
  unknown-box record first, then the endpoint, the naming rule, the
  payload and the bearer move in.
- **`fleet.py`** keeps the runners, the view, placement, approvals as
  intent, the host-row parsing and listen-port rules, `LEGACY_SERVERS`,
  and materialize. It produces `IntendedDeployment` (one function over
  `fleet.hosts`, the approvals and the provider lines, replacing the ad
  hoc construction in `from_cfg`). It loses the protocol half to
  `provisioning.py`, the probe and the parsers to `vllm_client.py`, the
  lifecycle counters on `VllmServer`, and in the end `VllmServer` itself.
  `config_served_at` on a server is the Loading anchor, a lifecycle fact
  set from a ConfigFetch or from Vast adoption; it is not the fetch
  record and moves into `deployment.py` with the rest of the history.
- **`proxy/deployments.py`** produces `LiteLLMDeployment` (one per
  `/model/info` entry, found by the stamp; replaces the route tuples,
  the index and `ExtraLiteLLM`) and `LiteLLMHealth` (replaces the two
  fields written onto each deployment), and becomes the executor
  (`add_deployment` / `delete_deployment` / `update_deployment`, and
  the fetch of the LiteLLM deployments that follows each). Renaming it
  `proxy/executor.py` then ends the clash with `deployment.py`.
- **`deployment.py`** (new) is the one class with state: `Deployment`,
  its transition table, verdicts, reasons and needs, and nothing else.
  The only file that knows what Loading or Rejected means. It absorbs
  `model_status.py`, which is deleted: `self_hosted_lifecycle`,
  `commercial_lifecycle` and `deployment_status` become the table and
  the verdict.
- **`reconcile.py`** keeps the key and gains the join and the plan:
  `reconcile(snapshots, history) -> (view, plan)` feeds each Deployment
  its observations and collects the verdicts and actions. `model_rollup`
  and privacy, including a `litellm_only` row forcing MIXED, stay pure
  functions over the records of one model name; they are not fields of
  one record. One status layer, computed once per pass, not a compute
  layer the pages call again on the same data. The loop stores the
  history the pure function returns; nothing in this design writes the
  memory store.

The order of work: the six sources first, each one commit that
produces its snapshot type beside its fetcher, switches the consumers to
read it and deletes the old copy of the fact in the same commit, so no
fact has two homes. Then `deployment.py` against six known shapes.

1. Rental. **Done:** 92798c9 (`llmao/rental.py`: `Rental`,
   `ProviderRentals`, `match_rental`; the host-side verdict still sits
   in `place_servers` and moves with the Deployment work).
2. ConfigFetch. **Done:** 57aff8b (`llmao/provisioning.py`:
   `ConfigFetch`, `UnknownFetch`, `ProvisioningRecord`; the endpoint,
   the naming rule, the payload and the bearer derivation follow in
   their own commit).
3. LiteLLMHealth.
4. LiteLLMDeployment.
5. VllmProbe (with `vllm_client.py`).
6. IntendedDeployment.

Pages, capacity, the sampler, and the action log read the view. They stop
calling `add_refusal` / `remove_refusal` once the plan is the gate.

## Testing

`reconcile()` is pure, so its tests are tables: these snapshots and this
history give this view and this plan. No httpx mocks are needed. Each
incident becomes a row: the provisioning gap in 65e22c8, an orphan after a
host is removed, a port remap, a new rental at an old address. The tables
are the readable statement of the state diagram. Success is that one
table, so the next `unlisted`-like fact cannot split `sync_selfhost` from
the button.

## Migration: alongside, then instead

1. Freeze the key function with tests (norm, IPv6 `::ffff:`, host case,
   `/v1`, provider bases, two names on one provider model,
   `LEGACY_SERVERS`). **Done:** 9fbee0e (`llmao/reconcile.py`).
2. Stamp the canonical URL on every new `/model/new`, and backfill the
   existing rows. **Done:** 52818a2, and the three production rows were
   stamped in place through `PATCH /model/{id}/update` on 2026-10-22.
   Before step 3, read RunPod's `portMappings` into the inventory (the
   parked RunPod step 2, now a prerequisite): with the pin gone, a
   RunPod box has no dial address until the listing supplies one.
   **Done:** f76a42f. A four-element `fleet.hosts` row still loads, with
   a warning, and the value only seeds the port until the listing names
   the box: a stale row never stops llmao (the user's ruling, 2026-10-26,
   over the earlier "fails at load"). The p6 node data drops the fourth
   element in its own PR.
3. Build the snapshots, the key, the record with its dialable field, and
   `reconcile()`, and run them **read only**, alongside the current code.
   Log every disagreement between the computed plan and what
   `sync_selfhost` / `_prune_extras` did, and with stored `srv.state` /
   `dep.skew`.
4. Flip auto actions one kind at a time (add, then delete, then
   orphan-prune), deleting each old path as it goes.
5. Point `/models` and `/admin` at the view and the plan.

Restart-empty hysteresis is a feature: `_prune_extras` starts its counter
at 0 so a portal coming up to stale deployments clears them after
`ORPHAN_PASSES` rather than on the first pass. All counters stay in
process memory; the memory store holds nothing of this design.

## Open questions

1. **Auto or human?** Which actions run unattended and which wait for an
   admin. This is a policy table worth writing down, including whether
   production LiteLLM writes ever run unattended.

   **Answer:** Policy table after reconcile, contents as in Policy
   above. Production deployment writes stay auto, as they are today.

2. **Is health its own source,** separate from the live inventory? It
   changes in seconds, and the listings in minutes.

   **Answer:** Yes. GPU inventory, vLLM health, the LiteLLM deployments,
   and LiteLLM `/health` are four live sources, plus intended and the box
   protocol. See the sources table.

3. **The key's name in `model_info`,** and how to backfill the existing
   deployments, including the legacy servers in `LEGACY_SERVERS`.

   **Answer:** the stamp is `llmao_key`, the post-`norm_base` canonical
   URL, `http://{host}:{listen_port}` for a box; the row's `model_name`
   is the other half of the key. `asf_api_base` stays the dialable URL
   last registered, which lags the record's dialable URL after a remap.
   Backfilled in place on 2026-10-22 through `PATCH /model/{id}/update`
   (the stamping section has what the hand check found). `LEGACY_SERVERS`
   were stamped from their known listen ports; the executor still passes
   `fleet.selfhost_api_key` for those pairs.

4. **Where history lives.** In memory, as today, so a restart forgets the
   hysteresis counters and starts cautious? Or in the memory store?

   **Answer:** Process memory, start cautious on restart. `reconcile()`
   returns the next history and the loop stores it. Nothing in the
   memory store: the one candidate, which listing placed a host, was
   dropped along with `ever_listed` (see History above), and with it the
   restart hole it was meant to close. Approvals and labels persisting is
   a separate product question.

5. **The decision table itself:** worked out from today's real states and
   transitions, before any code.

   **Answer:** Appendix below.

## Appendix: decision table

Each row is snapshots + history → computed fields + plan. Policy then
marks auto vs human. Lifecycle names are the ones `model_status.py`
already uses.

The **record** holds the detail (stored only as snapshots + history):
key, intended (hosts / approved / discovered / absent), commercial
provider or self-hosted, inventory per configured listing (not
configured / unread / stale / listed(id) / listed, not running / absent),
dialable URL, probe observation (including which URL was probed),
`config_served_at`, the LiteLLM deployment (unread / stale / absent /
present with id, api_base, stamped), LiteLLM health (unread / up / down
/ missing), history counters and last verdict.

**Computed once per pass and written on the record:** lifecycle, skew
badges, unlisted (host-level), and the plan actions.

| Fixture | Given | Computed | Plan |
|---|---|---|---|
| Unplaced | a record with no host (every intended row dials at least its listen URL) | no probe | cannot add; reason: no address |
| Awaiting, never probed | no anchor, no probe attempted, not in LiteLLM | Awaiting | none |
| Not reached, no anchor | probe attempted, `reached` false, no config fetch, not in LiteLLM; inside `health_grace_s` of the first attempt | Awaiting | none |
| Not reached, grace run out | as above, past `health_grace_s` of the first attempt | Stalled | sentence if unlisted. No deployment to delete |
| Loading | anchor set (config fetch or first reached probe), probe not ok, inside `health_grace_s` of it | Loading | none |
| Stalled, in LiteLLM | anchor set, never `last_ok`, past grace | Stalled | delete_litellm |
| Approved after a restart, answering 503 | no config fetch this process; probe reached | anchor = first reached probe; Loading, then Stalled past grace | none, then delete_litellm (the adopt stamp this replaces) |
| Rejected | `/health` ok, `/v1/models` with our key 401 | Rejected | delete_litellm on the first 401; attention: up but rejects our key, check the key on the box. No clock; a 200 clears it |
| Foreign | Rejected on `REJECTED_PASSES` passes running, and unlisted | Foreign | sentence: remove from `fleet.hosts`. A listed host never goes Foreign |
| Healthy, absent | probe ok, dialable, no LiteLLM deployment | Healthy; skew intended_not_in_litellm | add_litellm |
| Probe failed, under threshold | `last_ok` set, `fails` < `health_fail_threshold` | stays Healthy | none |
| Healthy, present, api_base matches | probe ok, the deployment's api_base == dialable | Healthy | none |
| Port remap | same key, dialable ≠ LiteLLM api_base (including a listen-port row) | Healthy; skew at_listen_port or mismatch | update_api_base via `POST /model/update` (it merges `litellm_params` and leaves `model_info`, the stamp included, alone). `asf_api_base` lags until that write |
| LiteLLM health skew | vLLM up and LiteLLM `/health` down, or the reverse | badge only (`vllm_up_litellm_down` / `vllm_down_litellm_up`) | none |
| Unhealthy / Stalled, in LiteLLM | self-hosted; probe failed past threshold, or stalled | Unhealthy / Stalled | delete_litellm |
| 65e22c8 serving through a listing gap | both listings fresh, neither names the host; probe ok on last-known dialable | Healthy. The count may reach `UNLISTED_PASSES`. The route stays | none. No sentence while Healthy. Keep probing the last-known URL |
| Unlisted, deployment present, down | host verdict true; lifecycle is Unhealthy, Stalled or Foreign | unlisted + lifecycle | no delete from this row (the probe rows delete); sentence, no POST, no buttons: remove from `fleet.hosts` |
| Unlisted, not reached, no anchor | host verdict true; probes do not reach; deployment already absent | unlisted + Awaiting, then Stalled past `health_grace_s` of the first attempt | none, then the sentence, no POST: remove from `fleet.hosts`. No deployment to delete |
| Unlisted, Loading | host verdict true; anchor set, inside `health_grace_s` | unlisted + Loading | none. No sentence |
| Unlisted, still Healthy with our key | host verdict true; `/health` ok and `/v1/models` accepts our key | unlisted + Healthy | none. No sentence, no buttons. The route stays |
| Unlisted, deployment already absent | host verdict true; row is in `fleet.hosts`; lifecycle Unhealthy, Stalled or Foreign | unlisted | sentence, no POST: remove from `fleet.hosts`. A Healthy absent row is the add row, not this one |
| Listed, not running | either listing has the host with a non-running status, or with no public address | listed, not running: not a miss; no address from that listing; dialable is last-known | keep probing last-known; the unlisted count does not move |
| #29 orphan, early | not intended, a LiteLLM deployment exists, orphan_passes < `ORPHAN_PASSES` | litellm_only skew | none (count) |
| #29 orphan, persisted | orphan_passes ≥ `ORPHAN_PASSES` | litellm_only | prune_orphan |
| Orphan that is our listen port | a LiteLLM deployment matches a key's listen URL | at_listen_port | update_api_base, not prune |
| Hand edit in LiteLLM's UI | intended row; its deployment is gone, or its api_base differs | skew | add when the add gate is open; update_api_base when api_base differs. Body fields are not reverted in v1. Action log records the revert |
| Two LiteLLM ids, one key | two rows for one key | keep the stamped id, else the api_base that matches the dialable URL; two stamped: the lower id | the other id is an orphan (`ORPHAN_PASSES`) |
| Confirm fetch failed | a write just happened; the follow-up GET of the LiteLLM deployments failed | that source is unread, not "still absent" | no add, no delete, no prune |
| Same snapshot, reconcile woken again | taken-at unchanged | counters do not move | none |
| New rental, same address | instance id changed under the same key | history reset | probe afresh; add/delete from new observations |
| Commercial, missing | intended provider, no LiteLLM deployment, health unknown or known | Configured or Healthy | add_litellm (every pass, not only startup) |
| Commercial, LiteLLM unhealthy | deployment exists, LiteLLM `/health` says down | Unhealthy | none (keep the deployment) |
| Commercial listing | no GPU inventory for that key | never unlisted | LiteLLM deployment and health only |
| Neither GPU key | a laptop; production configures both | both inventory sources not configured; dialable is the canonical listen URL | probe and add as a self-hosted row. Unlisted is never computed |
| Our own box | `fleet.hosts` row that neither listing names; both keys are configured | counted as a miss on every fresh pass of both listings; dialable is the canonical listen URL (no map, no last-known) | unlisted after `UNLISTED_PASSES`; the sentence shows once it is Unhealthy, Stalled, or Foreign, and is wrong for this host. Accepted: a sentence, never an action. Probe and add on the listen URL |
| Discovered, Healthy | inventory host not in `fleet.hosts`, no approval, live `/v1/models` matched a `models.yaml` row, probe ok, dialable | a record reconcile computed, not a server it added; Healthy | add_litellm. Sentence: not in `fleet.hosts`. A restart recomputes it on the next pass while the listing and the probe still match. The unlisted sentence never applies |
| Inventory unread or stale | that snapshot `None`, or older than the threshold × its interval, or the same taken-at as the last count | last unlisted verdict stands; do not increment | no unlisted transition |
| LiteLLM deployments unread or stale | that snapshot `None`, or past the staleness rule | do not invent orphans or absences | no add/delete/prune |
| Restart, orphans present | empty orphan_passes, deployments still there | orphans at pass 1 | no prune until `ORPHAN_PASSES` |
| One listing stale or unread | one of the two listings is stale or unread; the other is fresh | not a miss for any host: the count needs both fresh | no unlisted transition until both have been read again |
| Two public names, one provider model | two keys, same canonical provider URL, different `model_name` | two records | two add/delete lives |
| `LEGACY_SERVERS` | listen 10100 / 8004 / 8003 as in the set | key from that listen port | executor uses shared `selfhost_api_key` |
| Sibling ports | two keys on one host | two records; one host unlisted verdict | host gone applies to both |
| Restart, probe ok, no config_served | `probe_ok` true | Healthy (current `self_hosted_lifecycle`) | add if absent |
| Restart, deployment present, probe failed, no config_served | | Unhealthy | delete_litellm |
| Rental died before a restart | after the restart both listings are read; neither names the host, `UNLISTED_PASSES` times; probe does not reach | Unhealthy while the deployment is present, then unlisted; after the delete, Awaiting, then Stalled past `health_grace_s` of the first attempt | delete_litellm from the probe; the sentence from unlisted once Stalled, about thirty minutes after the restart. (Today's `ever_listed` would never fire here, so the sentence never showed) |
| Address reassigned to another tenant's vLLM | rental ended; `/health` at the last-known URL still ok; `/v1/models` 401 | Rejected at the first 401; Foreign once unlisted and `REJECTED_PASSES` 401s have run | delete_litellm from the probe at the first 401; the sentence from Foreign |

## Early work, independent of the rest

Each of these works in today's code, survives the rewrite, and closes a
real gap on its own. In order:

1. **Freeze the key function** with tests (migration step 1). Done:
   9fbee0e (`llmao/reconcile.py`, `tests/test_reconcile.py`).
2. **Stamp the canonical URL** on every `/model/new`, and backfill the
   existing rows (migration step 2). Done: 52818a2 and the backfill of
   2026-10-22.
3. **Two constants.** Split the one `UNLISTED_PASSES` into
   `UNLISTED_PASSES` (listing passes) and `ORPHAN_PASSES` (skew passes).
   Done: b9b5ce3.
4. **Re-read `/model/info` after every write.** `sync_selfhost` and
   `_prune_extras` do this, so a write is confirmed by the next pass and
   not assumed. Done: 7bd46bc (`_confirm`).
5. **One working-key function.** `working_api_key(cfg, srv)` in
   `fleet.py`, called from the deployment body and from the `/v1/models`
   fetch, so a legacy box no longer 401s on every pass and reports its
   context window. Done: 039c403.
6. **Keep the `/v1/models` status, and re-check it while rejected or
   unlisted.** A 401 with the working key is Rejected: delete the
   deployment, keep probing, say the box rejects our key. Unlisted and
   `REJECTED_PASSES` 401s running is Foreign, and the sentence shows.
   Done: 5efa688. `fetch_served_models` keeps the status;
   `key_rejected` and `rejected_passes` on the server and the snapshot;
   `REJECTED_PASSES` lives in `model_status.py`, where the rule is.
7. **Carry the pod status in the RunPod inventory** instead of filtering
   to `RUNNING` with a `publicIp` in `boxes_from_pods`, so "listed, not
   running" stops counting as an absence. Done: e4a5d6b. Both
   inventories keyed by id with `host` and `status` on every box (Vast's
   `actual_status` too); `Fleet.apply_boxes` matches by address, then by
   the box's id, and returns the placed servers; the location label on
   /models carries the status when it is not running. A box with every
   field guaranteed (a `Box` type, instead of the edict the two clients
   and the test fixtures build) is a follow-up after item 8.
8. **Read RunPod's `portMappings`** into the same inventory, so the
   listing is the dial address for a RunPod box as it is for a Vast
   box. Then drop the fourth element of a `fleet.hosts` row. Done:
   f76a42f. Every box carries `ports` (container port → public port,
   Vast's HostPort and RunPod's portMappings alike); `apply_boxes`
   stamps `public_port` from the box a server is placed on, through
   the one stamp function `apply_port_map` and Vast adoption also use;
   `refresh_public_ports` no longer applies the map itself (it still
   materializes approved hosts and runs Vast adoption). The fourth
   element is deprecated,
   not refused: `from_cfg` warns and the value seeds the port until the
   box is placed; `public_port_pinned` is gone. The p6 node data drops
   the three fourth elements in its own PR.
9. **Gate the `fleet.hosts` sentence** on Unhealthy, Stalled or Foreign,
   not on "not Healthy", with the not-reached grace clock above. Done:
   c2e3c14 (Unhealthy or Stalled; Foreign follows with item 6). The
   clock runs from the first probe attempt, `first_probed_at` on the
   server; an unlisted host with no address to dial counts as an attempt.
   A reached probe (a 503) with no config fetch is not anchored yet and
   stays Awaiting: the first-reached-probe anchor is reconcile's.
