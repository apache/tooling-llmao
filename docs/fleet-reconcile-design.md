# Fleet reconciliation: design (draft)

Status: thinking, not decided. Nothing here is built.

## The problem

llmao keeps several sources in step. They run on different clocks, and
today they are folded into flags on shared objects.

| Source | What | Cadence today |
|---|---|---|
| **Intended** | `fleet.hosts`, in-memory approvals, `models.yaml`, `config.yaml` providers | process start, approve, config fetch |
| **GPU inventory** | Vast instance list, RunPod pod list | Vast on the probe pass; RunPod 600s |
| **vLLM health** | `/health` (and `/v1/models` on the edge into Healthy) | `health_interval_s` |
| **LiteLLM membership** | GET `/model/info` | `skew_interval_s` |
| **LiteLLM health** | GET `/health` | `litellm_health_interval_s` (~4h) |

A commercial model provider (OpenRouter, xAI) has no GPU listing; it is
intended plus the two LiteLLM sources. A GPU host listing is the inventory
source. Mixing those under one "Live" hid the failure in 65e22c8: the
listing said absent, the probe said serving.

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
`sync_selfhost` deletes when Unhealthy/Stalled, or unlisted and not
Healthy; `remove_refusal` allows Unhealthy/Stalled only; `admin_actions`
offers no buttons at all when unlisted.

## The pattern: observe, compute the gap, act

This is the reconciliation loop: how a Kubernetes controller works, and
Terraform's plan and apply. Each pass works out the gap afresh from what
is observed, rather than updating state step by step from many places.

1. **Sources produce snapshots.** Each source in the table above produces
   an immutable snapshot that records when it was taken. A snapshot is
   `None` until the source has been read for the first time, which is
   different from an empty one. The "unlisted" rule already depends on
   that difference; here it applies to every source, as does staleness. A
   failed fetch leaves the last good snapshot in place; it does not
   publish empty-as-absent.

2. **One pure function decides.**
   `reconcile(snapshots, history) -> (view, plan)`.
   - The **view** holds one **record** per deployment, joined across the
     sources by the key (below). The record is the detail: intended?,
     inventory, dialable URL, probe observation, `config_served_at`,
     LiteLLM membership, LiteLLM health, instance id. Lifecycle, skew
     badges, unlisted, and any summary a page wants are **computed from
     that record when needed**, and never stored — the same pattern as
     `self_hosted_lifecycle` and `deployment_status` today. Use those
     names (`Awaiting`, `Loading`, `Healthy`, `Unhealthy`, `Stalled`).
     The state diagram becomes a decision table that can be read in one
     place (appendix).
   - The **plan** is a list of actions. Each action names a kind, a key
     and a reason. A **policy table** then marks each kind `auto` or
     `needs_human`. Reconcile does not embed that policy.

3. **History is the only state that changes.** It holds only what needs
   memory: first seen, last ok, last dialable URL, last instance id,
   `config_served_at`, the counters that stop one missed probe or one
   missing listing from becoming a verdict, and the last verdict, so a
   change is logged once. It is small and explicit, and **only
   `reconcile()` writes it**, by folding new snapshots into the previous
   history.

   Fetchers replace their snapshot. They do not touch history. Approve
   and a config load publish a new intended snapshot. `GET /vllm/config`
   publishes a "config served for this host at T" observation, not a
   write into history from the HTTP handler. The executor runs plan
   actions and does not write history; the next LiteLLM snapshot
   confirms them (re-fetch after an Add click so the button can change).
   Admin labels and `approved_models` are intended, not history. The
   action log stays its own store.

   Hysteresis is one **pattern**: consecutive misses, reset on a hit, and
   an unread snapshot (`None`) is not a miss. Each counter is its own
   history entry and keeps its own threshold and interval.
   `health_fail_threshold` stays a `fleet.*` config key (probe clock,
   ~45s). `UNLISTED_PASSES` stays a constant (listing clock, 10 min, and
   extra-prune on the skew interval) so there is no p6/eyaml round-trip.
   Unlisted is a **host** verdict — one instance, several listen ports —
   applied to every key on that host.

4. **An executor carries out the plan.** It runs the `auto` actions
   idempotently. It never trusts its own writes: the next LiteLLM
   snapshot confirms them.

5. **The `needs_human` actions are the buttons.** The "Work needed"
   strip, the reason text and the action buttons on /models and /admin
   all come from the plan. The pages show the view and the plan, and no
   longer reason for themselves. Because the automatic path and the
   button read the same table, "the same gate" cannot drift apart again.

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
known endpoint construction (`_norm_base` is most of it today):

- Self-hosted: `http://{host}:{listen_port}/v1` (peer IP normalised). The
  listen port is the one the vLLM key is derived from.
- Commercial: the provider's `api_base`, normalised the same way.

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
  below). They are two fields. Today's `model_info.asf_api_base` is the
  dialable URL (`_norm_base(dep.api_base)`). The stamp is the canonical
  URL. A remapped port leaves the key, and its history, as they are.
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
last-known dialable URL.

Backfill in place if the proxy can update `model_info` without dropping
the route. This tree only calls `/model/new` and `/model/delete`; confirm
`/model/update` (or equivalent) on the pinned proxy before relying on it.
Otherwise delete the llmao-managed deployments in a window and let add
recreate them for every Healthy, dialable, intended row. Brief gap, then
one generation of stamped rows.

While both generations exist, an unstamped row is not an orphan. Prune
only unmatched rows after backfill (or after the wipe) has finished.
`LEGACY_SERVERS` are matched from their known listen ports and stamped;
the executor still passes `fleet.selfhost_api_key` for those pairs.

This is small, works with today's code, and removes much of the port
matching in the skew checks. It could come first, before any other part of
this design.

## The dialable URL: one resolver at the edges

The URL that can actually be reached, `http://host:public_port/v1` on a
Vast box, comes from **one resolver**. Given a key, it returns, in order:
the public port pinned on the intended row, else the current inventory
HostPort (or RunPod pin), else the last-known dialable URL in history,
else nothing (unplaced). For commercial providers and for a pin that
equals the listen port, the dialable URL is the canonical one.

Last-known is load-bearing. `place_servers` already leaves `public_port`
in place when it clears `srv.box` for an unlisted host, so probes of a
box that still answers can continue. A resolver that only consults the
current listing would make that box unplaced, unprobeable, then deleted —
the e1567cb failure again. Health snapshot rows record the dialable URL
they used, so a remap does not attribute an old probe to a new port.

Only code that opens a connection calls the resolver:

- the body of the LiteLLM deployment (`api_base`);
- the health probe (`/health`);
- the `max_model_len` fetch (`/v1/models`);
- the metrics sampler (`/metrics`).

All four are I/O at the edges, in the fetchers and the executor.
Everything between them works with the key alone: reconcile, the view, the
plan, the pages, capacity rows, the action log, log lines, and future
access grants. The sampler already does half of this: it keys capacity rows
by host and listen port and dials the public URL. Today
`api_base`/`health_url`/`root_url`/`public_port` appear about 100 times
across the code.

Two cases become plain:

- **Unplaced.** The resolver has no address yet, so the plan cannot probe
  or add that deployment, and says why. This is `check_config_skew`'s
  `unplaced` guard, made general.
- **A port remap.** The key is the same, but the dialable URL no longer
  matches the `api_base` LiteLLM holds, so the plan says "update this
  deployment's api_base". Today that case is spread across
  `at_listen_port`, `same_deployment` and `on_listen_port`. Until an
  update endpoint is verified, that action is delete+add, a brief route
  gap.

## Policy

`reconcile()` emits actions with a kind and a reason. The policy table
marks each kind auto or `needs_human`. v1 matches today's gates:

- **Auto:** add when Healthy, dialable, and absent from LiteLLM; delete
  when Unhealthy or Stalled, or unlisted and not Healthy; prune unmatched
  extras after `UNLISTED_PASSES`.
- **Human:** approve, labels, remove from `fleet.hosts`, anything that
  spends money or destroys a box.
- No v1 switch that holds production LiteLLM writes for approval. The
  read-only alongside phase is that net.

Also:

- `ensure_providers` is startup-only today. Under reconcile a missing
  commercial route is added every pass.
- First cut diffs membership and dialable `api_base`. Body fields
  (`access_groups`, `order`, `hosted_vllm/` prefix, working API key) are
  later skew, not v1.
- `adopt_vast_instances` materializing a server from live `/v1/models`
  with no approval is **discovered**, not `fleet.hosts`.
- Unknown fleet-key fetches stay an onboarding strip: still 404 until
  approved, still not a LiteLLM row.
- Served-model skew (GET `/v1/models` vs the intended HF id) stays
  deferred. The resolver makes that fetch honest; it is not membership
  skew.

## Runners

- One fetcher per source, each on its own schedule (as the RunPod listing
  runner is today). Each one publishes a new snapshot.
- One reconcile loop, woken by an `asyncio.Event` when any snapshot
  changes, and by a slow periodic tick.

The metrics sampler and the capacity runners stay as they are. They read
the view, and through the resolver they dial.

## Where the code goes

No new swarm of modules. The sources stay where they are: `vast_client`,
`runpod_client`, the proxy client, the vLLM probe. `fleet.py` keeps the
box protocol: host rows, HMAC, `GET /vllm/config` JSON, listen-port
rules, `LEGACY_SERVERS`, materialize and approve.

The snapshots, key, view, plan, `reconcile()` and the resolver are a new
`llmao/reconcile.py` — pure, table-tested. `proxy/deployments.py` becomes
the executor (`add_deployment` / `delete_deployment`). `model_status.py`
stays and is the compute layer for lifecycle and skew; extend
`DeploymentSnapshot` rather than adding a second status module.

Pages, capacity, the sampler, and the action log read the view and the
resolver. They stop calling `add_refusal` / `remove_refusal` once the
plan is the gate.

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
   `LEGACY_SERVERS`).
2. Stamp the canonical URL on every new `/model/new`. Backfill in place,
   or wipe and recreate. Do not prune unstamped rows while both
   generations exist.
3. Build the snapshots, the key, the resolver and `reconcile()`, and run
   them **read only**, alongside the current code. Log every disagreement
   between the computed plan and what `sync_selfhost` / `_prune_extras`
   did, and with stored `srv.state` / `dep.skew`.
4. Flip auto actions one kind at a time (add, then delete, then
   extra-prune), deleting each old path as it goes.
5. Point `/models` and `/admin` at the view and the plan.

Restart-empty hysteresis is a feature: `_prune_extras` starts its counter
at 0 so a portal coming up to stale routes clears them after
`UNLISTED_PASSES` rather than on the first pass. History stays in process
memory.

## Open questions

1. **Auto or human?** Which actions run unattended and which wait for an
   admin. This is a policy table worth writing down, including whether
   production LiteLLM writes ever run unattended.

   **Answer:** Policy table after reconcile, contents as in Policy
   above. Production membership writes stay auto, as they are today.

2. **Is health its own source,** separate from the live inventory? It
   changes in seconds, and the listings in minutes.

   **Answer:** Yes. GPU inventory, vLLM health, LiteLLM membership, and
   LiteLLM `/health` are four live sources plus intended. See the sources
   table.

3. **The key's name in `model_info`,** and how to backfill the existing
   deployments, including the legacy servers in `LEGACY_SERVERS`.

   **Answer:** Stamp the canonical URL as `llmao_key` (name can stay an
   example until we pick it). Keep `asf_api_base` as the dialable URL.
   Backfill in place if the proxy can update `model_info`; otherwise wipe
   llmao-managed deployments and let add recreate. `LEGACY_SERVERS` are
   stamped from their known listen ports; the executor still passes
   `fleet.selfhost_api_key` for those pairs. Unstamped is not an orphan
   during a mixed generation.

4. **Where history lives.** In memory, as today, so a restart forgets the
   hysteresis counters and starts cautious? Or in the memory store?

   **Answer:** Process memory, start cautious on restart. Only
   `reconcile()` writes history. Approvals/labels persisting is a
   separate product question.

5. **The decision table itself:** worked out from today's real states and
   transitions, before any code.

   **Answer:** Appendix below.

## Appendix: decision table

Each row is snapshots + history → computed fields + plan. Policy then
marks auto vs human. Lifecycle names are the ones `model_status.py`
already uses.

The **record** holds the detail (stored only as snapshots + history):
key, intended (hosts / approved / discovered / absent), commercial
provider or self-hosted, inventory (unread / listed(id) / absent),
dialable URL, probe observation (including which URL was probed),
`config_served_at`, LiteLLM membership (unread / absent / present with
id, api_base, stamped), LiteLLM health (unread / up / down / missing),
history counters and last verdict.

**Computed when needed:** lifecycle (`self_hosted_lifecycle` /
`commercial_lifecycle`), skew badges (`deployment_status`), unlisted
(host-level), plan actions.

| Fixture | Given | Computed | Plan |
|---|---|---|---|
| Unplaced | intended self-host, no dialable | Awaiting or last lifecycle; no probe | cannot add; reason: public address unknown |
| Awaiting, never probed | no config_served, probe unread, not in LiteLLM | Awaiting | none |
| Loading | config_served, probe not ok, inside `health_grace_s` | Loading | none |
| Stalled, in LiteLLM | config_served, never `last_ok`, past grace | Stalled | delete_litellm |
| Healthy, absent | probe ok, dialable, not in LiteLLM | Healthy; skew intended_not_in_litellm | add_litellm |
| Healthy, present, api_base matches | probe ok, membership api_base == dialable | Healthy | none |
| Port remap | same key, dialable ≠ LiteLLM api_base (including a listen-port row) | Healthy; skew at_listen_port or mismatch | update_api_base (delete+add until update exists) |
| Unhealthy / Stalled, in LiteLLM | probe failed past threshold, or stalled | Unhealthy / Stalled | delete_litellm |
| 65e22c8 serving through a listing gap | inventory absent or unlisted counting up; probe ok on last-known dialable | Healthy; not unlisted until `UNLISTED_PASSES`; keep route | none (keep probing last-known URL) |
| Unlisted, not Healthy, in LiteLLM | host verdict true; lifecycle ≠ Healthy | unlisted + lifecycle | delete_litellm; needs_human: remove from `fleet.hosts` |
| Unlisted, already absent from LiteLLM | host verdict true | unlisted | needs_human: remove from `fleet.hosts` |
| #29 orphan, early | not intended, membership present, extra_passes < `UNLISTED_PASSES` | litellm_only skew | none (count) |
| #29 orphan, persisted | extra_passes ≥ `UNLISTED_PASSES` | litellm_only | prune_orphan |
| Extra that is our listen port | membership matches a key's listen URL | at_listen_port | update_api_base, not prune |
| New rental, same address | instance id changed under the same key | history reset | probe afresh; add/delete from new observations |
| Commercial, missing | intended provider, not in LiteLLM | Configured / Healthy per LiteLLM health | add_litellm (every pass, not only startup) |
| Commercial listing | no GPU inventory for that key | never unlisted | membership/health only |
| Inventory unread | that snapshot `None` | last unlisted verdict stands; do not increment | no unlisted transition |
| LiteLLM membership unread | that snapshot `None` | do not invent extras or absences | no add/delete/prune |
| Restart, extras present | empty extra_passes, routes still there | extras at pass 1 | no prune until `UNLISTED_PASSES` |
| Host never listed | other provider's list is complete; this host never appeared | `ever_listed` false | not unlisted |
| Two public names, one provider model | two keys, same canonical provider URL, different `model_name` | two records | two add/delete lives |
| `LEGACY_SERVERS` | listen 10100 / 8004 / 8003 as in the set | key from that listen port | executor uses shared `selfhost_api_key` |
| Sibling ports | two keys on one host | two records; one host unlisted verdict | host gone applies to both |
| Restart, probe ok, no config_served | `probe_ok` true | Healthy (current `self_hosted_lifecycle`) | add if absent |
| Restart, in LiteLLM, probe failed, no config_served | | Unhealthy | delete_litellm |
