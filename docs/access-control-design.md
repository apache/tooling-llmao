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

## What follows from that

1. **Restricting one deployment means giving it its own model name.** If
   a project pays for a box, its deployment becomes a model such as
   `qwen3-8b-tooling`, granted to that project, while `qwen3-8b` stays
   open on the other boxes.

   LiteLLM also has team-scoped deployments: `model_info.team_id` with a
   `team_public_model_name`, visible only to that team under a name of its
   choosing (`Router.team_public_model_names`). That might let a project's
   box serve plain `qwen3-8b` for that project alone. How it routes when
   public deployments share the name is **untested**; test before relying
   on it.

2. **A restricted model must leave `llmao:self-hosted`.** Otherwise every
   free-tier key reaches it through the group. Restricted models get
   groups of their own.

3. **Granting one person, not their whole team, needs a list on every
   key.** A key cannot exceed its team, so the team must allow the model;
   then every other key in that team must carry its own list without it,
   or they all inherit it. So llmao would set an explicit `models` list on
   every key it mints, and backfill existing keys (as it once backfilled
   token caps).

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
| `llmao:<name>` | a set defined for a purpose, e.g. a research preview, a project's own box | the projects and people named for it |

A model can be in several groups. A deployment says which groups it is
in; a grant names a group, not a model, wherever a set is meant. So "these
three people and the security team may use the preview models" is one
group and one grant list, and adding a fourth preview model touches
neither.

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

## Grants

A grant says: this project, or this person, is in this group. Grants are
made at runtime and kept in llmao's store, through the request and
approval flow that exists: a third kind of request beside capacity and
keys. A person asks, an admin approves, it shows in Recent work. llmao
then writes the LiteLLM lists: groups on deployments, team lists, key
lists. A skew check compares LiteLLM with the grants and puts it right, as
it does for the pretend projects today.

## Self-hosted or paid: which deployment someone gets

The same model can be served from our boxes and bought from a provider
(gemma4-26b on our boxes and through OpenRouter). Today both are
deployments of one name, our boxes first by `order`, the provider only
when ours cannot answer. That is right for most people: they wait in line
with everyone else on our boxes.

Some should be able to use the paid one at will, without waiting. Because
LiteLLM chooses by model name, that means a second name for the paid
route, in a group only they are granted, for example:

- `gemma4-26b`: our boxes, the provider as fallback (as now), open.
- `gemma4-26b-now` (name to be chosen): the provider only, in a group
  such as `llmao:priority`, granted to the projects or people allowed to
  spend for speed.

Whether the fallback to the provider should stay open to everyone, or
itself be limited, is part of question 3.

## Open questions

1. **Model or deployment?** Is restricting whole models enough, with a
   dedicated box handled as its own model name? Or must the same public
   name route differently per project (the untested team-scoped
   deployments)?
2. **Who grants?** Site admins only, or may a PMC grant its own project
   access to a group?
3. **Paid models:** today any real project can call them, and anyone
   falls back to a provider when our boxes cannot answer. Restricted by
   default, granted per project with a budget in mind? Should fallback be
   limited too?
4. **Grants to people:** to all of a person's keys, or to one key?
5. **Names for paid-at-will routes,** if that design holds.
6. **Hidden models in usage reports:** someone without access should not
   learn a hidden model's name from a usage page or a project report.
