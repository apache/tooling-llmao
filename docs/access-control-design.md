# Access control: design (draft)

Status: thinking, not decided. Nothing here is built.

The question: some projects, or some people, should reach particular models
or deployments, and others should not. Some models should be invisible
except to the people allowed them; others visible to all but usable only
on request. And where a model is served both from our boxes and from a
provider we pay, who gets which matters: our boxes mean waiting in line
with everyone else, the provider means no wait, at a price.

## How access works today

Everything below is LiteLLM's doing, and has to stay so: requests go to
LiteLLM directly, so a rule kept only in llmao's pages stops nothing.

- **LiteLLM decides by model name, never by deployment.** A key may call a
  model name if its team allows it and the key allows it. Both lists can
  name models directly, or name an **access group**: a label carried in a
  deployment's `model_info.access_groups`. A group covers every model name
  that has at least one deployment carrying the label
  (`Router.get_model_access_groups`).
- **Every deployment of ours carries `llmao:self-hosted`.** The two
  pretend projects (free tier; service keys with no project) are limited
  to that group, so a projectless key cannot spend money
  (`TeamManager._PRETEND_MODELS`).
- **Real projects have no list,** which LiteLLM reads as any model, paid
  ones included.
- **Keys carry no list,** so a key gets whatever its team allows.
- **A person's own list is never consulted.** LiteLLM checks a user's
  `models` only for a key with no team (`can_user_call_model`, called only
  when `team_object is None`), and every key llmao mints has a team, a
  pretend one if nothing else.
- **A person's membership of a team, however, is.** Separately from the
  user's own `models`, LiteLLM enforces a per-member scope from the team
  membership (`_check_team_member_model_access`), when a membership row
  exists and carries a non-empty `allowed_models`. llmao creates no
  membership rows today, so nothing is enforced there yet; this is the
  mechanism point 3 below rests on.
- **Within a name, the router picks the deployment,** by health, then
  `order` (ours first, a provider second), then its load-balancing
  strategy. The one mechanism that lets a request, a key or a team narrow
  that choice is tag routing (`enable_tag_filtering`, off today); see
  "Tags" below. Nothing else about a key or team reaches the router.

## What follows from that

1. **Groups restrict names; tags restrict deployments.** A group is
   resolved to the set of model names, so a key granted a group gets every
   deployment of each name, whichever groups those deployments carry. A
   deployment to be reached by only some people, or avoided by some, is
   therefore not a groups question. Two mechanisms were looked at:

   LiteLLM's team-scoped deployments (`model_info.team_id` with a
   `team_public_model_name`, `Router.team_public_model_names`) do **not**
   do it. `Router.should_include_deployment` (`router.py:11618`) matches a
   team-scoped deployment for its own team *and* every public deployment
   of that name, because a public deployment has `team_id` unset and the
   code treats unset as "accessible to all teams". So a team-scoped
   deployment sharing a public name is added to the public pool for that
   team rather than substituted for it: the team would be load-balanced
   across its own deployment and everyone else's. Team-scoped deployments
   isolate a name only when no public deployment carries it, which is a
   second name by a longer route.

   Tag routing does. A deployment carries tags, a request carries tags,
   and the router keeps only the deployments the request's tags allow.
   The tags a team or key carries are applied to every request it sends
   and cannot be taken off by the caller. This is the tool for everything
   below that is about *which deployment*; a second model name is no
   longer needed for it. (An earlier draft of this document said it was.)

2. **A restricted model must leave `llmao:self-hosted`.** Otherwise every
   free-tier key reaches it through the group. Restricted models get
   groups of their own.

3. **Granting one person, not their whole project, uses per-member
   scope.** A key cannot exceed its team, so the team must allow the model.
   What narrows it to one person is not a list on every key but
   LiteLLM's per-member scope: `LiteLLM_TeamMembership.litellm_budget_table
   .allowed_models`, enforced on the ordinary request path in
   `common_checks` (`proxy/auth/auth_checks.py:1027`, step 2.2, calling
   `_check_team_member_model_access`). One record per person per project,
   no list on keys and no backfill.

   Its default is **inherit**: an absent or empty `allowed_models` means
   the team's list applies with no further restriction. Granting by
   widening the team and narrowing each existing member would therefore
   leak to the next person who joins, silently, with every record
   individually consistent and nothing for a skew check to notice. The
   mechanism that avoids this is the team's `default_team_member_models`,
   seeded onto each new member as their `allowed_models`
   (`team_endpoints.py:2879-2883`). See "Granting" below.

4. **Real projects get explicit lists,** made of groups:
   `llmao:self-hosted` plus whatever else they are allowed. Groups are
   resolved by LiteLLM at request time, so adding a new open model needs no
   team updates: its deployments just carry the group.

## Access groups as the main tool

Groups are LiteLLM's way to name a set of models once and grant the set.
llmao would define a small number, carried by deployments and granted to
teams and keys:

| Group | Holds | Granted to |
|---|---|---|
| `llmao:self-hosted` | every open model on our boxes | everyone, free tier included |
| `llmao:paid` | open models served by a provider we pay | real projects (or only some: see question 3) |
| `llmao:<name>` | a set defined for a purpose, e.g. a research preview, or paid models at will | the projects and people named for it |

A model can be in several groups. A deployment says which groups it is
in; a grant names a group, not a model, wherever a set is meant. So "these
three people and the security team may use the preview models" is one
group and one grant list, and adding a fourth preview model touches
neither.

## Tags: choosing among a name's deployments

Groups and tags answer different questions, and both are LiteLLM's, so
both hold against a client that never sees a llmao page:

| | Access groups | Tags |
|---|---|---|
| Live on | a deployment's `model_info.access_groups` | a deployment's `litellm_params.tags` |
| Decide | whether the key may call the model name at all | which deployments of that name this request may land on |
| Enforced | at auth, before routing, by name | in the router, after auth, per deployment |
| Set per | team list, key list, team membership | team metadata, key metadata, and the request itself |
| On refusal | 401 at the door | a router error: no deployment satisfies the tags |
| Hide a name from `/v1/models` | yes | no |

**How a request's tags are made** (`litellm_pre_call_utils.py`, 1.105.0):
the union of the key's `metadata.tags`, the team's `metadata.tags`, and
what the caller sent in the `x-litellm-tags` header or the body's
`metadata.tags`. The ones from the key and team are also recorded apart,
as `inherited_tags`, and that is what makes them policy: a caller can add
tags but never remove those.

**How the router reads them** (`router_strategy/tag_based_routing.py`),
once `enable_tag_filtering` is on in `router_settings`:

- `tag`: steer. Only deployments carrying the tag are eligible; with none
  healthy, the ones tagged `default` if any, else an error.
- `!tag`: exclude every deployment carrying it.
- `&tag`: require it.
- No tags at all: the deployments tagged `default` if any exist for the
  name, else every healthy one, ordered as now. So switching the filter
  on with nothing tagged changes nothing.

A `!` or `&` that came from the key or team is kept even when it leaves
nothing: the request fails rather than routing around it. A caller's own
constraint may be dropped only on a deployment that sets
`model_info.allow_fail_open`, which none of ours will. A caller sending a
steering tag that conflicts with an inherited exclusion gets the error,
not the deployment.

**What tags are for here.** Each is a choice among deployments of one
name that nothing else can make:

- **Paid or ours.** Provider deployments carry `paid`. A team that may
  never spend carries `!paid`: it lands on our boxes, and when they are
  all down it gets an error instead of a bill. A team that may spend
  carries nothing and falls back to the provider as now. A key or team
  allowed to skip the queue carries `paid` and goes to the provider
  first. See the next section.
- **A request that asks for Private.** The FAQ says a Mixed model "may
  take either path, and you cannot choose which". With `!paid` a request
  can: a security reviewer, or the prompt analysis's judge (issue #73),
  says so on the request, and a team whose work is always confidential
  says so once, in its team metadata. "Unpublished security work belongs
  on Private models" gains "or send `!paid`".
- **A canary.** A new vLLM build or a new rental under a shared name,
  tagged `canary` while the established deployments carry `default`:
  nobody reaches it except a request tagged `canary`, which is what the
  smoke test and a load run would send. Today a new box takes its share
  of everyone's traffic from the moment it is registered.
- **Dedicated capacity.** A box rented for one project, tagged with the
  project, the team's metadata carrying the same tag: the project's
  requests go to its box and nobody else's do. With `allow_fail_open` on
  that box they fall back to the shared pool when it is down; without, they
  wait. The fast-GPU-for-some case is the same shape with `fast`.
- **Background work on its own box.** A deployment tagged `batch` for the
  judge, benchmarks and anything that should queue behind people, which
  issue #72 wanted from vLLM's priority scheduler. Tags give a separate
  line rather than a priority within one, which is the safer of the two:
  nothing a user can set moves them up.
- **A variant under one name.** A quantised build with a shorter context,
  or one without tool calling, tagged for what it lacks and excluded by
  the requests that need it. Plausible, not wanted yet; the catalogue
  would rather such a build had its own name.

LiteLLM can also match a deployment's `tag_regex` against the request's
User-Agent, routing by client tool. Noted, not wanted: a tool name is not
a policy.

**What tags cannot do.** Deny or hide a name: a team under `!paid` still
sees the name and may call it, which is right, since it is served on our
boxes. Anything about who may call a model stays with groups and the team
lists. And tags carry nothing in `/v1/models` or any listing, so what a
request will get is not discoverable from the client; the /models page
has to say it.

**A hole in the groups design that tags close.** The free tier is kept
off paid models by `llmao:self-hosted`, but a group covers a *name*. The
moment a provider deployment shares a name with ours, which the next
section plans for, every projectless key reaches the provider as the
fallback, and nothing in the groups design stops it. Nothing paid is
deployed today. When it is, `!paid` on the two pretend projects' teams is
what keeps the promise the group was meant to keep.

**The work.** Tag provider deployments at `/model/new`
(`proxy/deployments.py`) and backfill the existing rows; write team
metadata tags from the team manager and add them to the skew check;
switch `enable_tag_filtering` on in p6's `litellm.yaml` template. The
request and approval flow of "Granting" below covers the tags a team
carries as it covers its groups.

## Three kinds of audience

Each model (set in models.yaml, reviewed in git) has one:

| Audience | Seen on /models | Usable by |
|---|---|---|
| `open` (default) | by everyone | everyone its groups reach |
| `request` | by everyone, marked as needing access, with a **Request access** button | those granted it, after a request is approved |
| `hidden` | only by those granted it; absent for everyone else, catalogue and API alike | those granted it |

`request` and `hidden` differ only in what llmao shows. In LiteLLM both
are a group that open lists do not include.

`hidden` also has to keep the model out of anything llmao serves that
lists models: the /models page, the public catalogue, and any model list
a key can read from LiteLLM (a key's `/v1/models` already lists only what
it may call, so LiteLLM covers that one).

That last part holds only while the model is in no group an open list
names. A hidden model that reaches `llmao:self-hosted` by accident is
callable by every free-tier key and appears in their `/v1/models`, with no
llmao page involved. Hiding is therefore a property of the deployment's
groups, not of the pages, and what the skew check must assert is that a
model is in *exactly* its intended groups -- a stronger statement than
"the deployment exists", which is what it checks for the pretend projects
today.

Usage reports (question 6) are the exception to this section's opening
rule. The spend log is llmao's own surface, LiteLLM has no notion of who
may see a model's name in a report, so filtering in llmao's pages is the
only mechanism there is -- and the only place where it is sufficient.

## Granting

A grant says: this project, or this person, is in this group. Grants are
made at runtime and kept in llmao's store, through the request and
approval flow that exists: a third kind of request beside capacity and
keys. A person asks, an admin approves, it shows in Recent work. llmao
then writes the LiteLLM lists. A skew check compares LiteLLM with the
grants and puts it right, as it does for the pretend projects today.

Three lists, and which ones a grant touches is what keeps it closed by
default:

| | holds | set by |
|---|---|---|
| team `models` | the ceiling: every group anyone in the project may reach | `/team/update` |
| team `default_team_member_models` | the project's own grants, seeded onto each new member | `/team/update` |
| membership `allowed_models` | one person's effective set | `/team/member_add`, `/team/member_update` |

- **A grant to a project** adds the group to `models` *and* to
  `default_team_member_models`, and to every current member's
  `allowed_models`. A new member then inherits it, which is right: the
  grant was to the project.
- **A grant to a person** adds the group to `models` (the ceiling must
  allow it) and to that one membership's `allowed_models`, leaving
  `default_team_member_models` alone. A new member inherits the project's
  grants and not this one.

So the invariant is: **never widen `default_team_member_models` to grant an
individual**, and for each person, `allowed_models` equals the project's
grants plus their own. The skew check gains that as a third thing to
assert, beside the deployment groups and the team lists.

Two consequences worth stating plainly.

**llmao has to start creating memberships.** Per-member scope fires only
when a `LiteLLM_TeamMembership` row exists for the pair: `get_team_membership`
returning `None` means no restriction, and setting `user_id` on a key does
not create one. So llmao calls `/team/member_add` when a person first gets
a key in a project, and `/team/member_update` when a grant changes. This is
new work, though less than the per-key alternative, and the endpoints are
admin-only, which llmao's master key satisfies.

**Automation keys cannot be person-scoped.** They are minted with no
`user_id` by design (only the minter ever saw the secret), and
`_check_team_member_model_access` returns early without one. A service key
is therefore bounded by the team's `models` and nothing finer. So a group
granted to one person is nonetheless reachable by that project's service
keys, because the ceiling had to allow it. Where that matters -- a group
that costs money, or a model whose existence is confidential -- the grant
belongs to a group-specific team rather than to a membership inside the
project's own team.

## Self-hosted or paid: which deployment someone gets

The same model can be served from our boxes and bought from a provider
(gemma4-26b on our boxes and through OpenRouter). Today both are
deployments of one name, our boxes first by `order`, the provider only
when ours cannot answer. That is right for most people: they wait in line
with everyone else on our boxes.

Some should be able to use the paid one at will, without waiting, and
some should never reach it at all. One name does both, with the provider
deployments tagged `paid`:

| team or key carries | gets |
|---|---|
| `!paid` | our boxes only; an error when none can answer |
| nothing | our boxes, the provider when they cannot answer (as now) |
| `paid` | the provider first |

The pretend projects carry `!paid` from the start, which is the group's
promise made good (see the hole above). Whether a real project starts at
"nothing" or at `!paid`, and who moves it, is question 3. A grant of
`paid` is a grant like any other: requested, approved, written to the
team's metadata, checked for skew. A second model name per paid route,
which an earlier draft proposed, is not needed.

## Open questions

1. ~~**Model or deployment?**~~ Answered twice: groups for names, tags
   for deployments. A name routes differently per project through the
   tags its team carries; team-scoped deployments do not do this (point 1
   above), and a second name per restricted deployment is withdrawn.
2. **Who grants?** Site admins only, or may a PMC grant its own project
   access to a group, or `paid` to itself?
3. **Paid models:** today any real project can call them, and anyone
   falls back to a provider when our boxes cannot answer. Does a real
   project start at "nothing" (fallback allowed) or at `!paid`, and is
   `paid` at will granted per project with a budget in mind? LiteLLM
   also keeps spend per tag (`tag_max_budget_check`), which may be the
   budget's natural home; not yet looked at.
4. **Grants to people:** per-member scope covers a person across all
   their keys at once, so "to one key" would need the per-key lists this
   design now avoids. Is per-key granularity wanted by anyone? And should
   a person-level grant of a paid or hidden group use a group-specific
   team instead, to keep it away from the project's service keys? (A tag
   on a key is per key by nature, so `paid` at will for one person is
   easier than a group for one person.)
5. ~~**Names for paid-at-will routes.**~~ Withdrawn: tags, one name.
6. **Hidden models in usage reports:** someone without access should not
   learn a hidden model's name from a usage page or a project report.
7. **Tags on the /models page:** a request cannot discover from LiteLLM
   which deployment it will get, so the page has to say what a team's
   tags mean for it, and the FAQ's "you cannot choose which" has to
   change.
