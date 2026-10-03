# Design: Unified Models Dashboard

**Status:** Draft for review
**Scope:** Merge the "Models" and "Fleet" tabs into a single model-first page that serves both users and admins.

---

## 1. Problem

The app has two tabs that describe the same underlying things at different levels without saying so.

- **Models** is a catalog of models users can call. It shows an "Available: Yes" flag and unexplained pills (`text+vision`, `thinking`), and hides context window, license and hosting behind Details. "self-hosted, fast" is stuffed into the model name.
- **Fleet** is a table of *deployments* (one row per vLLM server or commercial endpoint). It mixes admin plumbing (host, listen/public ports, KV cache) with health signals that are unlabeled or contradictory:
  - "Last ok" and "LiteLLM health" are two different clocks with no explanation.
  - A row can read "healthy 1m" and "LiteLLM reports this endpoint down" at once, because the two signals come from probes taken at different times.
  - "Skew" is a free-text column that is blank most of the time, though it is the most important signal on the page.
  - `qwen3-8b` appears five times with no grouping, so there is no way to tell at a glance whether the model is usable.

The overlap (name, context, "is it up?") is why both tabs feel wrong: neither owns the concept.

## 2. Goals and non-goals

**Goals**

1. One page, model-first. A *model* is what users call; a *deployment* is where it runs.
2. Users get plain-language answers: can I use it, what can it do, how much context, will my data leave our infrastructure.
3. Admins get per-deployment health, lifecycle state and actions, with problems surfaced first.
4. Make the deployment lifecycle (unknown box to healthy) explicit instead of inferred from columns.

**Non-goals**

- Changing how vLLM, LiteLLM or the control plane work.
- Designing RunPod support. The lifecycle is kept provider-agnostic so it can be added later.

## 3. Concepts

| Term | Meaning |
|---|---|
| **Model** | What a user calls via the `model` parameter (e.g. `qwen3-8b`). |
| **Deployment** | One serving endpoint for a model: a self-hosted vLLM server on a rented VM (Vast.ai today), or a commercial API endpoint (e.g. Anthropic, xAI). Corresponds to a LiteLLM deployment, or to an *intended* deployment in `config.yaml` not yet in LiteLLM. |
| **Privacy tier** | Whether a query can leave infrastructure we control (section 6). |

## 4. User view

Each model is one row or card:

```
Qwen3 8B   qwen3-8b                          ● Degraded
  Text · Thinking · 40,960 context · Apache-2.0
  🔒 Private: prompts stay on VMs we control
  Handles up to ~10 full-context requests at once
  [Request a key]  [Details]
```

- **Status** is one of three labels: **Available**, **Degraded**, **Unavailable** (replaces the old "mixed").
- **Capabilities** are labeled chips with tooltips (e.g. "Vision: accepts images", "Thinking: reasons before answering"), not bare pills.
- **Context window** is the minimum across healthy deployments, so the number is honest.
- **Concurrency** is shown to users as "up to N full-context requests" (KV cache tokens divided by context window, summed over healthy self-hosted deployments). This is an upper bound, so the label says "up to". A model with no live self-hosted capacity shows "—", not 0. Commercial deployments show no concurrency figure.
- **Privacy** is a plain statement (section 6), not a hosting jargon term.
- License, hosting and other long-form content live in Details.
- Provisioning, stalled and retired deployments are **never** shown to non-admins.

## 5. Admin view

Same page, with three additions. A role-based view with an optional "preview as user" toggle is proposed.

1. **Needs-attention strip** at the top, pulling out only actionable items:
   - Unknown boxes awaiting approval
   - Stalled deployments (loading or reboot timed out)
   - Unhealthy deployments
   - True skew (section 8)
2. **Replica summary** per model, e.g. "1 healthy / 3 provisioning / 1 unhealthy".
3. **Expandable Deployments table** per model (replaces the Fleet tab).

**Deployment row fields**

| Field | Self-hosted vLLM | Commercial |
|---|---|---|
| Type / provider | Vast.ai (RunPod later) | e.g. Anthropic, xAI |
| Lifecycle state | yes (section 7) | Configured / Healthy / Unhealthy |
| Host, listen port, public port | yes | n/a |
| Context window | yes | yes |
| KV cache (raw tokens) | yes | n/a |
| vLLM `/healthz` + age | yes | n/a |
| LiteLLM `/health` + age | yes | yes |
| Last lifecycle transition | "Loading since 12:04" | yes |
| Actions | Approve, Reboot, Check LiteLLM now, Retire | Check LiteLLM now, Retire |

Fields that don't apply are omitted for that type, so the table doesn't fill with "—" cells. The old highlighted-row convention (fleet key presented from a box not in `config.yaml`) becomes an explicit **Unknown** state badge.

## 6. Privacy tiers

Privacy is a first-class, user-facing field. The question it answers: *will my query leave the LLMs we control?* (For example, private email archives must not be sent to a commercial LLM.)

For this purpose, "self-hosted" means **we control the virtual machine**, not that we own the hardware (the ASF owns none). A rented Vast.ai or RunPod VM running our vLLM counts as self-hosted.

| Tier | Meaning | User-facing wording (draft) |
|---|---|---|
| **Private** | Served by vLLM on VMs we control. | "Prompts stay on infrastructure we control." |
| **External** | Sent to a third-party model provider. | "Prompts are sent to an external provider." |

- Admins also see the provider name in the detailed spec.
- Wording should not overclaim: a GPU host operator can in principle observe a rented VM's memory. The Private wording should be reviewed with that in mind.
- A model served by **mixed** deployments (some private, some external) takes the **least private** tier, so a Private badge is never shown for a model that could route externally. Open question O5.

**Transport and authentication:** vLLM *public* ports are TLS endpoints; *listen* (private) ports are plain HTTP on the vLLM server. The port mappings are queried dynamically. Every vLLM server is configured with an API key, which is stored in LiteLLM and used when it proxies a request to the backend. The path from LiteLLM to a Private model is therefore both encrypted and authenticated, which the Private tier relies on.

## 7. Deployment lifecycle

The central change: replace several loosely related columns with one explicit state per deployment.

### 7.1 Self-hosted (Vast.ai)

A Vast.ai instance is created from a template, then asks our control plane (LLM.apache.org) which models to launch and on which private ports, authenticating with the shared `FLEET_KEY`. Today an admin adds the caller's IP and model/port config to `config.yaml`. The new flow is:

1. Admin says "this IP is good; use these models" (**Approve**).
2. The system immediately creates the LiteLLM deployments and records the approval as the source of intent. `config.yaml` is backfilled afterwards.
3. The admin (or the system via the Vast.ai API) reboots the instance so it asks for config again and receives an answer.
4. The box loads the model; vLLM starts answering `/healthz`.

| State | Set by | Meaning |
|---|---|---|
| **Unknown** | observed | Fleet key presented from an unrecognized IP. |
| **Approved** | admin | Approved; LiteLLM deployments created; config backfill pending or done. |
| **Reboot requested** | system/admin | Reboot attempted via API; awaiting a config request. |
| **Config served** | observed | The box fetched its config after approval. |
| **Loading** | observed | Config served, vLLM not yet answering `/healthz`. |
| **Healthy** | observed | `/healthz` passing. |
| **Unhealthy** | observed | Was healthy since last config answer, now failing. |
| **Stalled** | timeout | Reboot or Loading exceeded its timeout (an alert, not a quiet "down"). |
| **Retired** | admin | Instance destroyed or removed; hidden from default views. |

Notes:

- **Loading vs. Unhealthy** is the same observation (vLLM not answering), distinguished by whether the deployment has been healthy since its last config answer.
- **Timeouts** apply to *Reboot requested* and *Loading*. Proposed default: ~20 minutes, configurable (big models load slowly). Value is open (O3).
- **Reboot is observed, not assumed.** An earlier attempt at rebooting via the Vast.ai API had unclear results. The design therefore treats the API call as a request, and the UI reports only what is observed (a config request arriving, or a timeout).
- A restarted instance may come back with different mapped public ports, which is why ports are queried dynamically.

### 7.2 Commercial

Shorter lifecycle: **Configured** then **Healthy** or **Unhealthy**, driven solely by the LiteLLM probe.

### 7.3 RunPod

Unknown shape. The state machine is provider-agnostic. Provider-specific parts (e.g. reboot) are optional capabilities of a provider adapter.

## 8. Health signals and staleness

Two signals, shown side by side, each with its own age:

| Signal | Applies to | Cost | Interval |
|---|---|---|---|
| vLLM `/healthz` | self-hosted | free | 45 s |
| LiteLLM `/health` | all | costs tokens | 4 h (config variable) |

- A signal is **stale** after ~3 missed intervals (about 2.5 minutes for vLLM, about 12 hours for LiteLLM). Stale displays as **Unknown**, never as Down.
- This resolves the "healthy 1m" vs. "endpoint down" confusion: each signal carries its own timestamp, so differences read as different snapshots, not a contradiction.
- Admins get a **Check now** action to force a LiteLLM probe when a problem is reported. Because it costs tokens it has a per-deployment cooldown (proposed: 5 minutes). Forcing a vLLM check isn't necessary at 45 s.

### Skew, redefined

Much of today's "skew" is just lifecycle ("vLLM isn't up yet, so there's no LiteLLM route" is simply *Loading*). Skew is reserved for genuine inconsistencies between sources, each a named badge with a one-line explanation and suggested action:

| Skew type | Description |
|---|---|
| Intended, not in LiteLLM | Present in `config.yaml`, no LiteLLM deployment. The runner emits this only for Healthy, or for a commercial deployment that is Configured. Loading with no route is lifecycle, not this badge. |
| vLLM up, LiteLLM down | The vLLM probe passes but LiteLLM reports the endpoint unhealthy. |
| vLLM down, LiteLLM up | The vLLM signal is down and LiteLLM still reports the endpoint healthy. |
| In LiteLLM, not in config | A LiteLLM `model/info` route whose `api_base` matches no deployment. Stored beside the fleet table as host, port, and model name (one host can serve several ports; one host:port can report more than one model name). The runner flags every such route on each config pass. Flagging only past a grace period after Approve is still future behavior. |
| Config/LiteLLM mismatch | The deployment's `api_base` is in `model/info` with a different model name ("config and LiteLLM disagree"). A matching name clears it. The flag is kept across the health pass. A different port is a different route, not this badge. |

The runner emits these five badges and nothing else (O4).

## 9. Roll-up: user-facing status

Provisioning deployments must not make a working model look broken (today `qwen3-8b` has 1 serving and 4 others in various states).

- **Counted:** Healthy and Unhealthy deployments.
- **Not counted:** Unknown, Approved, Reboot requested, Config served, Loading, Stalled, Retired, and stale (Unknown-signal) deployments.

| Label | Rule |
|---|---|
| **Available** | At least one counted deployment, and all are Healthy. |
| **Degraded** | At least one Healthy and at least one Unhealthy. |
| **Unavailable** | No Healthy deployments (including none counted). |

Whether a stale commercial deployment (>12 h without a probe) counts as healthy, unhealthy or excluded needs a decision (O2). The text above assumes excluded. A planned-maintenance "drained" state is a possible later addition so maintenance isn't read as degradation.

## 10. Security considerations

- **Approval is keyed to an IP.** Vast.ai recycles IPs between tenants, but a new tenant on a previously approved IP would not know the `FLEET_KEY` (a shared secret between the Vast.ai template and our server), and admins know the IPs of the boxes they expect. The residual risks are a leaked or compromised `FLEET_KEY`, and a stale approval outliving its instance. Proposed hardening, in order of value: expire an approval when the deployment is Retired (cheap, and keeps the approved-IP list accurate); optionally also bind approval to the Vast instance ID if the box can report it at bootstrap, so a known IP presenting a new instance ID returns to **Unknown**. See O1.
- Rotating the `FLEET_KEY` should be a documented procedure, since it is shared by every box launched from the template.
- Fleet-key bootstrap, Approve, Reboot, Retire and Check-now are admin-visible, audited actions (see event history, section 11).
- The public vLLM hop is TLS; listen ports are plain HTTP and must stay on the private side.

## 11. Additional behaviors

- **Event history:** record every lifecycle transition with a timestamp; the row shows the latest ("Loading since 12:04"). Debugging a stalled box otherwise means guessing.
- **Retire:** a destroyed instance needs an explicit action so dead rows don't accumulate in Stalled or Unhealthy and drown the attention strip.
- **Context across replicas:** user-facing context is the minimum across healthy deployments. This will matter once boxes with different VRAM exist.
- **Single source of intent:** the approval is the recorded intent; LiteLLM deployments and `config.yaml` are both derived from it, and a pending or failed `config.yaml` backfill is shown as a visible state, not as mystery skew.

## 12. Implementation plan

**Pass 1: status logic (no UI).** A pure, testable module computing, per deployment: lifecycle state, signal staleness, skew badges; and per model: roll-up label, privacy tier, context and concurrency. Tested against the current fleet as the first fixture (3 serving, 3 pending, 1 down, one Unknown/skewed row).

**Pass 2: template.** The combined Models page: user view, admin view with attention strip and Deployments drill-down, actions wired to the existing endpoints. The Fleet tab is removed.

Open items for Pass 1 inputs: how deployment and state data is currently stored (`config.yaml` parsing, skew runner output, LiteLLM health data).

## 13. Open questions

| # | Question | Current assumption |
|---|---|---|
| O1 | Beyond expiring approvals on Retire, also bind approval to the Vast instance ID? Is the ID available at bootstrap? | Expire on Retire; instance-ID binding is optional hardening. |
| O2 | Do stale commercial deployments count as healthy, unhealthy, or excluded? | Excluded. |
| O3 | Loading and Reboot timeout values? | ~20 minutes, configurable. |
| O4 | What other skew types does the skew runner detect? | The five in section 8. The one the earlier draft omitted is vLLM down, LiteLLM up. |
| O5 | How is a model with both private and external deployments labeled? | Least-private tier wins. |
| O6 | ~~Does the public hop authenticate the caller?~~ | Resolved: every vLLM server has an API key, stored in LiteLLM and used when proxying. |
| O7 | Final user-facing privacy wording, including the rented-VM caveat. | Draft in section 6. |
| O8 | Role-based admin view only, or add a "preview as user" toggle? | Add the toggle. |
| O9 | Is a "drained/disabled" state needed for planned maintenance? | Later addition. |
