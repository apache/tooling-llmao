# Fleet reconciliation: design (draft)

Status: thinking, not decided. Nothing here is built.

## The problem

llmao keeps three fleets in step:

- **Intended:** what we mean to run. The config.yaml fleet, hosts an admin
  has approved, models.yaml, and the providers we buy from.
- **Live:** what is actually there. The Vast and RunPod instance lists, and
  what each box answers when probed. A commercial provider counts as always
  present.
- **LiteLLM:** the deployments LiteLLM holds, and their health.

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
`_extra_passes` on `DeploymentManager`.

## The pattern: observe, compute the gap, act

This is the reconciliation loop: how a Kubernetes controller works, and
Terraform's plan and apply. Each pass works out the gap afresh from what
is observed, rather than updating state step by step from many places.

1. **Sources produce snapshots.** Each source (intended, live inventory,
   live health, LiteLLM) produces an immutable snapshot that records when it
   was taken. A snapshot is `None` until the source has been read for the
   first time, which is different from an empty one. The "unlisted" rule
   already depends on that difference; here it applies to every source, as
   does staleness.

2. **One pure function decides.**
   `reconcile(snapshots, history) -> (view, plan)`.
   - The **view** holds one record per deployment, joined across the
     sources by the key (below). Its status (serving, loading, stalled,
     unlisted, missing from LiteLLM, an orphan in LiteLLM, and so on) is
     worked out again on every pass and never stored. The state diagram
     becomes a decision table that can be read in one place.
   - The **plan** is a list of actions. Each action names a kind, a key and
     a reason, and is marked either `auto` or `needs_human`.

3. **History is the only state that changes.** It holds only what needs
   memory: first seen, last ok, the counters that stop one missed probe or
   one missing listing from becoming a verdict, and the last verdict, so a
   change is logged once. It is small and explicit, and only reconcile owns
   it. `unlisted_passes`, `ever_listed`, `_extra_passes` and
   `health_fail_threshold` all become entries in it, under one rule for
   hysteresis.

4. **An executor carries out the plan.** It runs the `auto` actions (add
   or delete a LiteLLM deployment, put skew right) idempotently. It never
   trusts its own writes: the next LiteLLM snapshot confirms them. Policy
   lives here too. For example, a setting could make production writes
   to LiteLLM wait for approval, without the decision logic changing.

5. **The `needs_human` actions are the buttons.** The "Work needed" strip,
   the reason text and the action buttons on /models and /admin all come
   from the plan. The pages show the view and the plan, and no longer
   reason for themselves. Because the automatic path and the button read
   the same table, "the same gate" cannot drift apart again.

## Identity: (canonical URL, model)

Every source names every deployment by one key: a **canonical base URL**
and a **model name**. One shape covers our boxes and the providers we buy
from alike. Today a provider deployment is already identified by
(api_base, model), because one provider base serves many models. Our boxes
are identified by api_base alone, plus matching on host and port. That
split, and the separate "ours" and "bought" sets in `check_config_skew`,
both go away.

- **The canonical URL is the one config.yaml names.** For one of our boxes
  that is `http://host:listen_port/v1`, the listen port being the one the
  vLLM key is derived from. It is not the URL LiteLLM dials (see below), so
  a remapped port leaves the key, and its history, as they are. For a
  provider it is the provider's base URL.
- **The model is our public model name,** LiteLLM's `model_name`, not the
  provider's model string. Two of our names can map to one provider model.
  The access-control draft proposes `gemma4-26b` and a paid-at-will variant
  on the same OpenRouter model, for example; they must stay two
  deployments.
- **One function builds every key.** It handles the scheme, the trailing
  slash, `/v1`, the case of the host name and normalising the peer IP;
  `_norm_base` is most of it today. If two code paths each build keys their
  own way, the identity bugs return.
- **The key is not driven by Vast.** A Vast instance id exists only at
  Vast, after we ask; config.yaml, LiteLLM, RunPod and the commercial
  providers have none. As a key it would make every non-Vast record a
  special case. It is an **attribute** the live inventory attaches to a
  key, with the RunPod pod id beside it. A different instance id under the
  same key means a new rental at the same address: its history is reset
  and the box is checked afresh.

### Stamping the key into LiteLLM

llmao creates every self-hosted LiteLLM deployment, so it can record the
key in the deployment's `model_info` (for example `llmao_key`). LiteLLM's
deployments then say which deployment they are, with no translation from
public port to listen port. A deployment without the stamp is legacy or
not ours: an orphan by definition, whose listen port no longer has to be
recovered (65e22c8 notes that it cannot be).

This is small, works with today's code, and removes much of the port
matching in the skew checks. It could come first, before any other part of
this design.

## The dialable URL: one resolver at the edges

The URL that can actually be reached, `http://host:public_port/v1` on a
Vast box, comes from **one resolver**: given a key, it returns the dialable
URL from where the live inventory says the deployment is. For providers and
pinned ports, the dialable URL is the canonical one.

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
  `at_listen_port`, `same_deployment` and `on_listen_port`.

## Runners

- One fetcher per source, each on its own schedule (as the RunPod listing
  runner is today). Each one publishes a new snapshot.
- One reconcile loop, woken by an `asyncio.Event` when any snapshot
  changes, and by a slow periodic tick.

The metrics sampler and the capacity runners stay as they are. They read
the view, and through the resolver they dial.

## Where the code goes

No new swarm of modules. The sources stay where they are: `vast_client`,
`runpod_client`, the proxy client. The snapshots, key, view, plan,
`reconcile()` and the resolver replace most of `fleet.py` and the sync and
skew logic in `proxy/deployments.py`. That makes one focal module, or two if
the pure logic and the I/O shell read better apart.

## Testing

`reconcile()` is pure, so its tests are tables: these snapshots and this
history give this view and this plan. No httpx mocks are needed. Each
incident becomes a row: the provisioning gap in 65e22c8, an orphan after a
host is removed, a port remap, a new rental at an old address. The tables
are the readable statement of the state diagram.

## Migration: alongside, then instead

1. Stamp the key into LiteLLM deployments, and backfill the existing ones.
2. Build the snapshots, the key, the resolver and `reconcile()`, and run them
   **read only**, alongside the current code. Log every disagreement
   between the computed plan and what the current code did.
3. Once they agree, move the pages over to the view and the plan.
4. Move the actions over one kind at a time (adding and deleting LiteLLM
   deployments first), deleting each old path as it goes.

## Open questions

1. **Auto or human?** Which actions run unattended and which wait for an
   admin. This is a policy table worth writing down, including whether
   production LiteLLM writes ever run unattended.
2. **Is health its own source,** separate from the live inventory? It
   changes in seconds, and the listings in minutes.
3. **The key's name in `model_info`,** and how to backfill the existing
   deployments, including the legacy servers in `LEGACY_SERVERS`.
4. **Where history lives.** In memory, as today, so a restart forgets the
   hysteresis counters and starts cautious? Or in the memory store?
5. **The decision table itself:** worked out from today's real states and
   transitions, before any code.
