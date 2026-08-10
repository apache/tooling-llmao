# Build status and backlog

**As of:** 2026-08-09  
**Repo:** `apache/tooling-llmao`  
**Product design:** `apache/rai-private` → `services/llmao/README.md`  
**Ops plans:** Infra `p6/modules/llmao/README.md`

Detailed “where we are.” Top-level README stays short and links here.

---

## Done

| Area | Notes |
|------|--------|
| asfquart always | OAuth + LDAP session; no auth-mode split |
| Run | `main.py` (TLS/`certs`) + ASGI `llmao_app` (Hypercorn) |
| Config | `APP.cfg` EasyDict from `config.yaml` (dotted access) |
| LiteLLM admin client | Async **httpx** → **LiteLLMBackend** always (no app mock mode) |
| Model inventory SoT | **`model_list.yaml`** + LiteLLM `include`; flat `model_info`; **no** `STORE_MODEL_IN_DB` for inventory |
| Secrets | On-disk YAML; prod **hiera/eyaml**; same `master_key` in `config.yaml` + `litellm.yaml`; provider **API keys** in `model_list.yaml`; **api_base** cleartext |
| Postgres / Prisma | `litellm[proxy,extra-proxy]`; system PostgreSQL; `make db` |
| **PAT UX** | `/keys` list/create/revoke; personal + admin automation; secret once; revoke modal |
| Tests | Offline **`tests/mock_backend.py`** injected into Seam — not a config mode |

### Credential rules (as implemented)

| Kind | Who creates (today) | Binding |
|------|---------------------|---------|
| **Personal** | Committer (self), member of project | `user_id` + `team_id` + purpose (`key_alias`) |
| **Automation (team-scoped)** | **Any PMC member of project or site admin** (provisional) | `team_id` only; purpose required |

- Team ensure → LiteLLM **team** (`team_id`) only; **not** a shared product team PAT.
- Secret `sk-…` shown **once**; not kept in llmao for product PATs.
- Project names = LDAP session names (no rename map). Team.`team_alias` and Key.`metadata.project` both hold that name; UI uses **`metadata.project`** (required on every key we create).

### Open policy: who may create automation PATs?

**RAI decision pending.** Options: (A) RAI / proxy admins only, (B) PMC Chair/VP (or nominated role), (C) any PMC member (**current code**). Documented in design; tooling may tighten after RAI decides.

### Local path (production-shaped)

Copy three YAMLs from `*.example` → system Postgres + `make db` → matching master keys → `make proxy` + `make run`. There is **no** mock app mode; LiteLLM must be up for PAT UX (else `BackendUnavailable`).

---

## Edge cases and gaps

| Issue | Notes |
|-------|--------|
| LiteLLM down | PAT / admin API pages surface `BackendUnavailable` |
| Last used | Best-effort; may be `—` or `updated_at` |
| Model cost $0 | LiteLLM default without pricing metadata |
| Site admin = `rai` PMC | Not automatic; `site_admins` + isRoot |
| Automation list | PMC committees / site admin project scan, not global all-teams |
| List pagination | First page (~100 keys) |
| Team alias / revoke id | Depends on LiteLLM list payload |
| Master key drift | Two files must match (eyaml) |
| Prisma path | site-packages schema; re-generate after upgrades |
| Containers | Not the supported DB story |
| OAuth HTTP tests | Not automated |
| PMC email audit | Design §6.4; **not built** |
| Member budgets / TPM | Design; **not built** |

---

## Planned UX (top nav)

Committed product sketch for signed-in top nav (beside **API keys**). **Not built yet.**

| Nav | Intent |
|-----|--------|
| **Models** | Browse **models available to this user** (identity / project / allow-list scoped—not necessarily the full global inventory). |
| **Reports** | Usage, utilization, governance, and Commons-fairness views (see list below). |

### Reports (examples)

| Report | Audience / purpose |
|--------|--------------------|
| **PMC usage by user** | Project leads: spend/usage broken down by committer |
| **Self-hosted utilization** | Capacity/load on ASF or Infra-hosted open models (not only $) |
| **Board roll-ups** | Foundation / Board reporting material |
| **Donated credit burn-down** | Track consumption of donated provider credit pools over time |
| **Abuse / fairness histograms** | Spot **abusive committers** relative to **the Commons** (outliers vs peers / fair-share) |

Data sources: LiteLLM spend/usage APIs where they apply; Infra / host observability for self-hosted capacity. Product design: rai-private `services/llmao/README.md` §6.3.

---

## Next steps (detail)

1. Harden PAT against live LiteLLM edge responses (pagination, team_alias, delete ids).  
2. **Decide automation creator role** (RAI) and tighten code if not (C).  
3. Site admin via **`rai` PMC** (optional keep cfg list).  
4. PMC notification email on key lifecycle.  
5. Budgets / capacity limits UX.  
6. **Models** page + top nav: models available *to the user*.  
7. **Reports** page + top nav: PMC by-user, self-hosted utilization, Board roll-ups, donated credit burn-down, Commons abuse histograms.  
8. **p6** Puppet: Postgres, eyaml→YAML, systemd, restart LiteLLM on config change.  
9. Cleanup container paths; optional un-package.  
10. Advisor / richer routing UX.  

---

## Multi-repo

| Tree | Role |
|------|------|
| rai-private `services/llmao/` | Product design |
| tooling-llmao | Software (this repo) |
| p6 `modules/llmao` | Production deploy (planned) |
