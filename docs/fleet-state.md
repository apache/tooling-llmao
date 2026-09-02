# Design: Fleet State — Ownership, Lifecycle, and Recovery

Companion to [`vllm-fleet-design.md`](vllm-fleet-design.md), which defines the
control-plane contract: what a host is, how a box fetches its assignment, and
what the JSON looks like. This document covers **where fleet membership lives,
how it changes, and what happens when it is lost**.

Amends §3.2 of that document, which places membership in `config.yaml` →
`fleet.hosts`.

---

## 1. Principle

**Configuration is what a human decides and reviews. Data is what the system
mutates during normal operation.**

Fleet membership is data. It changes when an instance is rented, migrates, or
dies — none of which warrants a code review. Placing it in a Puppet-rendered
`config.yaml` would make adding a GPU box a hiera edit, a pull request, and an
agent run, on a timescale of hours for instances that churn in minutes.

Puppet owns configuration. The app owns data. Puppet still guarantees the data
file exists with correct ownership; it stops enforcing the contents.

---

## 2. File Split

### 2.1 `config.yaml` — Puppet-owned

Rendered from hiera and eyaml on every agent run, overwritten each time.
Contains secrets, LiteLLM settings, budgets, `site_admins`, health intervals,
and `fleet.key` — everything a human decides deliberately.

It no longer contains `fleet.hosts`. It gains a pointer:

```yaml
fleet_path: /etc/llmao/fleet.yaml
```

### 2.2 `fleet.yaml` — app-owned

Membership only. Seeded once by Puppet, never overwritten:

```puppet
file { '/etc/llmao/fleet.yaml':
  ensure  => file,
  owner   => $user,
  group   => $group,
  mode    => '0640',
  replace => false,     # create if absent; contents never enforced
  content => "# Managed by llmao at runtime.\nhosts: {}\n",
}
```

`replace => false` applies the content only when the file does not exist.
**Ownership and mode remain enforced on every run**, so a stray `chmod` is
still corrected while the contents are left alone.

### 2.3 Why a file and not Postgres

§10 of the fleet design already records `YAML SoT; no STORE_MODEL_IN_DB`. The
same reasoning applies to membership, and adds a resilience argument:
`fleet.yaml` on disk means a GPU box rebooting during a database outage can
still fetch its assignment. Membership in Postgres would break that — the box
comes up, asks what to run, and gets nothing.

The database remains authoritative for keys, teams, budgets and spend.

---

## 3. Schema

```yaml
hosts:
  80.188.223.202:
    label: vast-a100-gemma
    provider: vast
    instance: "49080461"
    added: 2026-09-02T17:45:12Z
    added_by: akm
    last_config_fetch: 2026-09-02T18:03:44Z
    servers:
      - [gemma4-26b, 10100]

  100.105.28.100:
    label: asf-l40s-qwen
    provider: asf
    added: 2026-08-04T00:48:01Z
    added_by: akm
    last_config_fetch: 2026-09-02T18:03:51Z
    servers:
      - [qwen3-8b, 8003]

retired:
  - host: 159.48.242.29
    label: vast-a100-gemma
    provider: vast
    instance: "46213937"
    added: 2026-08-04T02:11:00Z
    retired: 2026-09-02T16:30:00Z
    retired_by: akm
    reason: host maintenance window, migrated to 80.188.223.202
```

`servers` keeps the existing `[model, port]` / `[model, port, name]` form from
§3.2, so `validate_fleet` is unchanged. The rest is additive.

| field | purpose |
|---|---|
| `label` | IPs are unreadable at a glance; a dozen hosts need names |
| `provider` | `vast` churns, `asf` does not — sets expectations for staleness |
| `instance` | the provider's own id, for finding the box in their console |
| `added` / `added_by` | audit trail |
| `last_config_fetch` | **liveness.** A rented box that has not fetched in a week is gone |

### 3.1 Host capacity is not declared here

VRAM and disk are **discoverable on the box** and must not be hand-entered.
A typed `vram_gb: 80` is wrong the first time a provider supplies a different
card than was ordered, and the resulting failure appears as an engine-init
error rather than a validation message.

The model's absolute requirement belongs in the catalog
(`model_info.vllm.vram_gb`, `disk_gb`); the box decides fit at install against
hardware it can see.

`expect_gpu` / `expect_vram_gb` may optionally be carried here as a **hint**,
so the UI can warn at add-time that a model will not fit. It is never
authoritative, and a mismatch between expected and observed is itself a useful
signal — the rental was not what was paid for.

### 3.2 Retirement is a soft delete

Entries move from `hosts` to `retired` with a required reason; they are not
removed. This answers "did this host ever exist, and what happened to it"
without a database table.

Retirement also has a security dimension. Providers recycle IP addresses, so a
stale entry may eventually match an instance rented by someone else.
`GET /vllm/config` requires the fleet key with a constant-time compare, so the
blast radius is bounded — but the fleet key is a single shared secret baked
into the box template (§3.1, §8), which makes "nobody else could hold it" an
assumption rather than a guarantee. Retire promptly.

---

## 4. Mutation

All writes go through a single atomic helper: serialise, write to a temp file
in the same directory, `fsync`, `rename`. A crash mid-write must never leave a
half-parsed fleet. Validation runs **before** the write, not after.

`Fleet.reload()` re-reads the file and rebuilds `Server` objects, **preserving
health state for hosts whose entry did not change**. With a dozen hosts and an
1800s health grace, resetting every probe because one box was added would put
nineteen healthy servers back into `starting` for twenty minutes.

### 4.1 UI actions

The `/fleet` page becomes the management surface:

- **Add host** — IP, label, provider, instance id, `[model, port]` rows.
  Validates the model exists in the catalog and the port is unclaimed on that
  IP; stamps `added` / `added_by` from the session.
- **Retire host** — moves the entry to `retired` with a required reason. A box
  still running then receives 404 from `/vllm/config` on its next fetch, which
  is the correct signal that it is no longer ours.
- **Edit servers** — change the model/port rows for a box being repurposed.

Hand-editing the file remains supported when the app is stopped.

### 4.2 Surfacing staleness

`last_config_fetch` is rendered as an age and coloured: green under an hour,
amber over a day, red over a week.

**Entries are never auto-retired.** A box down for maintenance is
indistinguishable from one that is dead, and silently removing it during an
outage is the wrong default. The colour prompts a human; the human decides.

---

## 5. Loss and Recovery

### 5.1 What survives what

| event | `fleet.yaml` | LiteLLM DB | in-memory health |
|---|---|---|---|
| Puppet run | untouched | untouched | untouched |
| app restart | untouched | untouched | rebuilt in ~45s from probes |
| Postgres outage | untouched | unavailable; calls fail at auth | untouched |
| host rebuild | **lost** | **lost** | lost |

Health state is correctly ephemeral — it is a live measurement, not a record.

### 5.2 Host rebuild is the real exposure

A rebuilt gateway seeds `fleet.yaml` with `hosts: {}`. Every GPU box then
receives 404 on its next config fetch and stops being served. This is the cost
of taking membership out of version control and is accepted deliberately.

Partial mitigation exists for free: **the GPU boxes are still running.** They
hold their assignment locally and continue serving; they simply cannot
re-fetch. Reconstructing a lost `fleet.yaml` from a dozen live boxes is tedious
but possible — a better failure mode than a lost database.

### 5.3 Backup

`fleet.yaml` and the LiteLLM database are lost together and should be captured
together:

```bash
sudo -u postgres pg_dump litellm | gzip > "$dest/litellm-$stamp.sql.gz"
tar czf "$dest/etc-llmao-$stamp.tar.gz" -C /etc llmao
```

**`$dest` is unresolved and belongs to the p6 work.** Three constraints:

- The destination must be captured **off-host**. `bpc_client_asf` pulls via
  rsync from a central BackupPC server, so the client has no local daemon and
  its coverage cannot be confirmed from the box. The share list lives on the
  server — which paths are pulled is a question for Infra, not an assumption.
  A default share of `/etc` and `/home` would miss `/var/backups` entirely.
- Confirm the host is not in `bpc_client_asf::excludelist`.
- A file-level copy of a live Postgres data directory is **not** a valid
  backup. The `pg_dump` is what makes it restorable, whatever captures it.

Until that is settled, a local dump protects against a bad edit or an
accidental delete and nothing else. Worth having; not worth calling a backup.

Restore is a file copy and a reload:

```bash
tar xzf /var/backups/llmao/etc-llmao-<stamp>.tar.gz -C /
chown llmao:llmao /etc/llmao/fleet.yaml
systemctl restart llmao
```

---

## 6. Behaviour at Scale

Assumptions: roughly a dozen GPU boxes, split across ASF-provisioned and
rented; vast and runpod instances that vanish, migrate, and are reassigned new
addresses.

- **`retired` becomes the longest section.** That is correct — it is the
  record. Prune by hand if it becomes unwieldy; never automatically.
- **`hosts` must stay clean** or the skew check (§`check_config_skew`) fires
  continuously against machines that no longer exist, and the signal is
  ignored.
- **Group by provider in the UI.** With a dozen boxes the useful question is
  "how many rented instances are still alive", not an alphabetical list of IPs.
- **A `notes` field is worth considering.** "Rented for the superset scan, kill
  after" is obvious for a week and unrecoverable after a month.

---

## 7. Implementation

1. Read `fleet_path`, falling back to `config.fleet.hosts` for compatibility.
2. Accept the extended per-host schema (additive; `servers` unchanged).
3. Atomic write helper.
4. `Fleet.reload()` preserving unchanged hosts' health state.
5. `/fleet` add / retire / edit routes.

Items 1–3 are worth doing even if the UI is deferred: they are what make the
file safely hand-editable, which is the current practice.

---

## 8. Open Points

- `$dest` for backups, pending the p6 manifest and confirmation of BackupPC
  coverage.
- Whether `expect_gpu` / `expect_vram_gb` are worth carrying, or whether
  observed-only reporting is sufficient.
- Whether `notes` is a field or belongs in `label`.

**Resolved:** membership is a file, not a database; `replace => false` is the
Puppet mechanism; retirement is a soft delete; entries are never auto-retired;
host capacity is discovered, not declared.

---

*End of design document.*
