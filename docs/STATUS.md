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
| **PAT UX** | **My Keys** `/keys` (personal, one list call); **Other Keys** `/keys/other` (automation, PMC/admin); create/revoke split; secret once |
| Tests | Offline **`tests/mock_backend.py`** injected into Seam — not a config mode |

### Credential rules (as implemented)

| Kind | Who creates (today) | Binding |
|------|---------------------|---------|
| **Personal** | Committer (self), member of project | **project + user + purpose** |
| **Automation (team-scoped)** | **Any PMC member of project or site admin** (provisional) | **project + purpose** (user null) |

- Design names on `KeyInfo`: `project`, `user`, `purpose` (optional); `token_id` for list/revoke (not the secret). Automation ⇔ `user is None`.
- Automation keys store **`metadata.created_by`** (ASF uid who minted and saw the secret); surfaced as `KeyInfo.created_by` so other PMCs know who to ask.
- Seam/Backend product API speaks **project** (LDAP); LiteLLM `team_id` is resolved only inside `LiteLLMBackend`.
- In-process **project→team_id** cache only (immutable pair); warmed at startup via `before_serving` + `LiteLLMBackend.warm()` (**fail-fast** if LiteLLM is down). Spend always from live `team/info` when id is known.
- Wire map: purpose↔**`metadata.purpose`** (optional; not `key_alias` — LiteLLM aliases are globally unique), user↔`user_id`, project↔`metadata.project` + team_alias, token_id↔LiteLLM `token`, created_by↔`metadata.created_by`.
- No on-disk `StateStore` / `state_path`.
- Secret `sk-…` shown **once** (`CreatedKey.secret`); not kept on `KeyInfo`.

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

## Models page (v1)

| Item | Notes |
|------|--------|
| Route | `/models` (nav for signed-in users) |
| Data | `model_list.yaml` via `public_models` / `ux_models` — no secrets or `api_base` |
| UX | Overview table + Details modal; badges for modality / thinking / hosting |
| Supply path | **Site admins only** (`is_site_admin`): weights, provider, provenance fields. Redacted for others (partnerships / ops). |
| Not yet | Per-team/key model allow-list filtering (“available *to you*” hard filter) |

## Planned UX (top nav)

| Nav | Intent | Status |
|-----|--------|--------|
| **Models** | Inventory (+ later allow-list scoped) | **v1 done** |
| **Reports** | Usage, utilization, governance, Commons fairness | Not built |


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
6. Models: per-team/key allow-list scoping when configured.  
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
