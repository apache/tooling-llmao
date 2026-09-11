# Build status and backlog

**As of:** 2026-09-10  
**Repo:** `apache/tooling-llmao`  
**Product design (concepts/policy):** `apache/rai-private` → `services/llmao/README.md`  
**How to run/use this software:** repo [`README.md`](../README.md)  
**Ops:** Infra `p6/modules/llmao/README.md`

**Doc split**

| Kind | Where |
|------|--------|
| Product concepts (project-centered, budgets, credentials) | **rai-private design** |
| What the code does *today* (short) | **Done** below — detail is the code |
| **Planned UX** (layouts, workflows, phases) | **UX backlog** below |
| How to run locally / PATs against proxy | **README** (how-to) |

---

## Done (today — thin)

Implemented enough for local production-shaped use: asfquart OAuth; LiteLLMBackend + fail-fast team cache warm; `model_list.yaml` inventory; PAT UX (**My Keys** / **Other Keys**); **Models** catalog (supply-path redaction for non–site-admins); secrets as dual YAML / eyaml intent; system Postgres + prisma setup; offline `tests/mock_backend.py`.

**Models page:** catalog-first table (name, id, Available Yes/No, Request a key → `/keys/new?model=`). Context, license, and hosting live in Details; supply-path fields stay site-admin in the modal. Available is policy (always true until P5) **and** in service **and** in LiteLLM. Self-hosted with nothing serving or no deployment is No. Sort puts No last. Per-process state is on `/fleet`.

**Catalog vs LiteLLM:** `model_list.yaml` is not included by the proxy.
`litellm.yaml` `general_settings.store_model_in_db: true` (Puppet may also set the env). `self_hosted` is a required boolean. Commercial rows need a static `api_base` (fail-fast). Standalone watches `config.yaml`; Puppet restarts on change.

**GPU fleet:** `fleet.hosts` (IP → listen port); box JSON listen only; Vast HostPort → `public_port`; `VllmServer` + `FleetDeployment`. `/fleet` is signed-in (host:port admin). Green serving = vLLM up **and** LiteLLM has that `api_base`. Self-host: `/model/new` after serving, `/model/delete` on down (catalog YAML). Commercial: `/model/new` at llmao startup. LiteLLM `/health` cached on the deployment by the skew runner. Remaining: long vLLM boot, config revision, pending-assignment table, box smoke.

Open policy still: **who creates automation PATs** (A RAI / B Chair-VP / C any PMC — code provisional C). See design §5.1.1.

Edge cases that bite operators (LiteLLM down, pagination, master key drift, prisma path, etc.) are tracked as work items, not product design.

---

## UX backlog (planned — do these)

Product intent for budgets, roles, and reports: **design §6**. Below is **what to build in the UI**, in order.

### IA (target)

```text
My Keys | Other Keys (PMC+) | Models | Projects | Reports?
Home = role-aware launchpad (not keys-only)
```

**Projects** is the second pillar (envelopes + people). Budgets are not a floating top-level “Budgets” app without project context.

### P0 — Project list + read-only project overview

- **Projects** list: projects I’m in / I administer; mini envelope % for steward projects.
- **Project overview** (`/projects/<name>` or equivalent):
  - Money meters: **People** vs **Automation** spend split (display split OK if one team budget under the hood)
  - Period label (e.g. monthly · reset date)
  - Grantor of the dollar ceiling (v1: Free Tier on first cfg default; later RAI / Security / …)
  - By-person spend this period (**transparent to project members**)
  - Export CSV (steward+)
  - Automation summary + link toward Other Keys for that project
- Empty: “Envelope appears when first key is minted” / trial copy when RAI defines defaults
- Flashes on any POST

### P1 — Home pressure + key ↔ project links

- Home: purpose line; primary CTA keys
- **≥ ~90%** near-limit callouts (keys or projects)
- **Top ~3** personal key (or project) usages this period
- Steward strip: 2–3 administered projects with % used
- My Keys: **project column → project overview**; optional near-limit badge
- Collapsed “For scripts” base URL hint (not hero)

### P2 — Member caps (steward write)

- Members table: cap / used / **Edit cap** dialog (empty = no cap; cannot exceed envelope)
- Optional: lower people/automation sub-caps if policy allows
- **Cannot raise** outer ceiling (no fake Increase button — Request/raise is RAI)
- Quart **flashes** on save/error; later PMC email (design audit)

### P3 — RAI allocations

- Superuser-only: set/raise project envelope(s), period, **trial/free/allocated** type
- Dual hard people vs automation limits when RAI/LiteLLM support exists
- Flashes; PMC notification when email exists

### P4 — Reports (open product)

- Foundation roll-up (RAI)
- Commons fairness / high-usage distributions (careful labels)
- Self-hosted utilization (capacity; Infra signals as needed)
- My usage (committer)
- **Not in open product:** donated **credit burn-down** (RAI-private vendor deals)

### P5 — Capacity + models allow-list

- Capacity meters (TPM/RPM/parallel) separate from $
- Models: wire Available (and key `models` allow-list) when team / envelope policy exists; column already stubbed

### Cross-cutting UX rules (when building)

- ASF vocabulary only (project, person, purpose, envelope)
- Script-first; secret once
- Quart flashes for mutations
- Proxy-down = loud banner, not silent zeros
- Supply-path model fields: site admin only (already in Models v1)

### Open RAI (block precise numbers, not UX scaffolding)

1. Trial/free default amounts and duration  
2. Hard dual budgets vs display-only people/automation split  
3. Narrow stewards later (Chair vs any PMC)?  
4. Capacity fair-share defaults from Infra  

---

## Engineering backlog (non-UX or infra)

1. Harden PAT against LiteLLM pagination / delete ids  
2. Automation creator policy after RAI decides §5.1.1  
3. Site admin via `rai` PMC (optional keep cfg list)  
4. PMC notification email on key/budget lifecycle  
5. p6 Puppet: Postgres, eyaml→YAML, systemd, restart LiteLLM on config change  
6. Advisor / richer routing  

---

## Multi-repo

| Tree | Role |
|------|------|
| rai-private `services/llmao/` | Product design |
| tooling-llmao | Software + STATUS (this file) |
| p6 `modules/llmao` | Production deploy (planned) |
