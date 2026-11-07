# Access — the glossary and the one rule

Every word canopy uses about who may do what has exactly one meaning, and it is
defined here. Code docstrings point at this page instead of re-defining terms
(`apps/agents/turn_context.py`, `session_context.py`, `refusals.py`, `grants.py`, `roles.py`,
`published_profiles.py`, `profile_settings.py`, `apps/harness/caller_context.py`, `apps/harness/turn_mode.py`,
`apps/workspaces/permissions.py`). Reuse existing words, one meaning per word, no new terms.
Designs: `docs/superpowers/specs/2026-11-05-turn-context-design.md` (the Turn Context) and
`docs/superpowers/specs/2026-10-30-agent-roles-and-access-list-design.md` (roles and the access list).

There are two ladders, and they are different questions:

* a **workspace role** says what a person may do in a *workspace* (read logs,
  invite people, create content, reshape agents);
* an **agent role** says what a person may make one *agent* do.

THE rule that joins them is one function, `apps/agents/turn_context.resolve_turn_context`. It reads
the facts of a conversation and who is asking, and returns a **Turn Context**: what one turn may do
and why. Every entry point that makes an agent do work asks it; everything downstream reads its
result; `tests/test_access_matrix.py` asks it through every entry point. Don't write a second
predicate for "may this person make this agent do X" — ask the rule.

This page is the model, the glossary, the roles, the access list, read mode and host sites, the
decision table, refusals, triggers and turn mode. The rest is split by subject:
[access-direct-use-and-admission.md](access-direct-use-and-admission.md) (direct use of an agent,
what a caller's turn tells the owner, admission rules, the caller envelope),
[access-confinement-and-act-as.md](access-confinement-and-act-as.md) (how confinement is enforced,
acting on behalf of the asker, act-as-you on canopy-web and on host sites) and
[access-sessions-and-people.md](access-sessions-and-people.md) (who can see and who is in a session,
Slack threads, what agents know about a person); the HCP service is [hcp-service.md](hcp-service.md).

## The Turn Context

```
FAN IN                                 RESOLVE             FAN OUT
Session Context ─┐                                         ┌─ the agent (caller envelope)
Asker           ─┼─→ resolve_turn_context() → Turn Context ┼─ the runner (session, confinement)
Agent settings  ─┘                                         ├─ tokens, credentials
                                                           ├─ the turn's refusal, the composer
                                                           └─ the session panel, in chat and the IDE
```

`resolve_turn_context(session_ctx, asker, agent, *, requested=None, turn=None, stamp=None, runner=None)`
is pure: **it writes nothing** (a test wraps it in a read-only guard). `caller_context.build` writes a
`PersonAccess` row and may create a `Person`; that stays in the envelope's `person` block, outside the
resolver. Entry points ask it through three wrappers:

* `turn_context.for_turn(turn, agent, requested)` — what the rules say for a turn, from its own initiator.
* `turn_context.for_asker(agent, who, *, verified, requested, turn, standing, lines, channel)` — a
  question with no turn (the roster, the topology, `may_edit_agent`, licenses), or about a turn when the
  caller knows better than the turn how far this message is proven.
* `turn_context.at_claim(turn)` — the claim's question: the enqueue's, asked again from today's facts
  (see "Two moments").
* `turn_context.of_turn(turn, agent, *, runner)` — what a claimed turn **runs as**: the stamp the claim
  wrote stands for the rest of the turn (see "Two moments").

### Session Context

The durable facts of a conversation, whoever is asking: `session_context.build(session, turn=None, *,
agent=None)`, a frozen read-model over rows that already exist. No table, no write, nothing about the
asker. The turn is an input because some facts only a turn holds (a sessionless turn dispatched from a
sensitive parent is sensitive through its ancestors; its conversation may be `canopy_session` rather
than `chat_session`). A question with no conversation uses `session_context.empty(channel=…)`, so the
resolver has one input shape.

| Field | Means |
|---|---|
| `session_id`, `agent`, `workspace` | Which conversation, which agent (its slug; none for a person's own repo or project session), which tenant. |
| `channel` | `turn.origin` for a turn, which is what turn mode reads; for a preview, where the conversation lives (the origin of its first turn). Never `Session.origin`, which only says which side created the row. |
| `runner_requested`, `runner_bound` | The runner asked for (`turn.pinned_runner_id`, or `metadata.requested_runner_id`) and the one backing it now. |
| `site`, `page` | The host site the conversation is held on, and the resource its page declared (`published_profiles.session_site`, `session_resource`). |
| `sensitive`, `sensitive_requirement` | Whether the conversation holds a site's sensitive data (`sensitivity.is_sensitive` for a turn, `any_sensitive` for a session), and the requirement that made it so. Once true it does not revert. |
| `is_review`, `review_of` | This is a one-session review's chat, and the session it reviews. |
| `audience`, `participants` | Who can see the session (`canopy_sessions.audience`) and its `(user_id, role)` rows. |
| `session_grants` | What being in this session would give someone beyond their own standing. Always empty now ("Session grants" below). |
| `send_blocks` | Why this session takes no message from anyone, or from some people (stage 0 below). |

### Session grants

`session_grants` is a list of `{kind, user_id, role, source}`, asked by stage 1 in one rule. **No kind
exists now**, so it is always empty and being in a session raises nobody's access: a person in one answers
by their own line on the access list, like anyone else.

The one kind there was, `session_writer` (a participant with role owner or editor in a session the agent's
owner or an admin started held the whole agent there), was removed on 2026-11-06 at the owner's decision.
Someone it let in now needs a line: `read_only` for read mode, `read_write` or `editor` for the whole agent.

The seam stays. Adding a kind is one builder in `session_context.py` and one test; the resolver already
applies a grant: the whole agent, with permissions that stop at `write`.

### What a Turn Context holds

A frozen `TurnContext`, always produced, even when refused:

| Field | Means |
|---|---|
| `standing` | What the asker holds on this agent and why: `relationship` (`owner \| admin \| member \| contact \| system`), `permissions` (everything their lines grant), `agent_role`, `source` (`owner \| admin \| system \| session \| list:<line>`) and `via` (`source` in words). On a refusal from the rule `permissions` is empty and `agent_role`/`source` blank; a stage 0 refusal keeps the whole standing, because who they are is still true. |
| `access` | `full`, `confined` or `refused`: how the profile is enforced. |
| `profile` | The one profile the turn runs in, or None exactly when refused. |
| `basis` | Why, in one word: `owner \| admin \| system \| session:<role> \| grant:<line> \| capability:<name> \| sensitive:<name> \| refused \| no-interface`. The `capability:` prefix is a wire value (the envelope's `granted_by`, `caller_tokens.is_caller_scoped` and licenses read it); it names a read-mode or host-site profile. |
| `session_key_suffix` | `#<name>` for a confined profile, empty for the whole agent ("What confinement keeps"). |
| `credentials` | What this turn gets, by name and kind, never a value ("Credentials" below). |
| `review` | Turn mode and its basis (`pending` until a claim knows the runner), `manual_only`, `may_request_auto`, `may_pin_runner`. |
| `stamped` | The profile name stored on `Turn.capability`: `""` for the whole agent, a name, None when refused. |
| `refusal` | `{code, why, how_to_fix, who_can_fix}`, non-null exactly when `access == refused` ("Refusals" below). |
| `trace` | An ordered list of `{rule, outcome, detail}`, one per rule the resolver asked: the "why" a screen shows. |

`held_permissions` is what the asker holds for THIS turn (the envelope's `permissions`): a full turn holds
everything its role or lines give; a confined turn holds `read` and its profile's `use:<name>` only, even
for an owner a sensitive conversation confined; someone the rule no longer admits holds nothing, whatever
the stamp.

### Profiles

A **profile** is the complete definition of what a turn can do: tools, shell commands, read and write
paths, how it starts (`entry`), and whether it handles sensitive data. Every allowed turn has exactly one.
`Profile` is `{kind, name, site, description, entry, tools, bash, read_paths, write_paths, data,
default_profile_version}`, read when asked (canopy's default read profile reads the agent's skills).

| Kind | Defined by | Access |
|---|---|---|
| `whole_agent` | The agent itself: nothing is enumerated. | `full` |
| `read_mode` | The agent's Read mode setting, else canopy's default. | `confined` |
| `host_site` | That site's row on the agent (`site` names it; a stored entry with no site keeps this kind with `site` null). | `confined` |
| `session_review` | Canopy, for a one-session review's chat. | `confined` |
| `deny_all` | Canopy: a stamp that no longer resolves to a published profile, a sensitive turn stamped with read mode that has since gone (`turn_context.stamped_profile`), or a stamped turn whose agent has gone. Fail closed. | `confined` |
| `no_agent` | A turn with no agent: a person's own repo or project session. Its enforcement and credentials are its own; this kind only names them. | `full` |

**Only an explicit `kind == whole_agent` (or `no_agent`) means the whole of anything.** A missing profile,
name or list never does; on every confined kind an absent list denies. `TurnContext` refuses to be built
otherwise (a profile exactly when not refused; `full` exactly for `whole_agent`/`no_agent`).

Admins never edit a profile as such. They edit the **access list** (who) and **Read mode** and
**Host sites** (what), which define the confined profiles; in the UI a profile reads as "what it may
do".

### How it is resolved

**Stage 0 — can this session take a message from them.** Only for a **preview** (a real turn is past its
entry point, and asking here would refuse turns no entry point refuses). The Session Context's
`send_blocks`, in the order the send route asks them, each the same check the route makes and the same
words it says: content deleted (everyone), `can_write` (a signed-in person who may read but not write;
a contact's send has its own route), the agent CLI the conversation is held to and its agent no longer
runs (everyone), and a sensitive conversation's not-in-it rule (anyone who is not one of its people at
its chat surfaces). A block is a refused Turn Context with its own code; it changes nobody's access.

**Stage 1 — standing**, first match:

1. Canopy itself, another agent's approved dispatch, a schedule: `system`.
2. For a person: `Agent.owner`, then the agent's own login (`system`), then `Agent.is_admin`
   (`caller_context.relationship_for_user`).
3. `session_grants` (none exist now).
4. The access list (`grants.resolve`).
5. Otherwise, nothing.

**Stage 2 — profile.** A base, then two rules that only narrow:

| Base (first match) | Profile | Access |
|---|---|---|
| Owner, admin, system, a session grant, or holds `write` | `whole_agent` | `full` |
| Holds nothing | — | `refused` |
| Named a profile their permissions open | that `host_site`, or `read_mode` if that is what was named | `confined` |
| Named one they can't open, or one that is gone | — (**no fall-through to read mode**) | `refused` |
| Named none; their conversation's site selects one their permissions open | that `host_site` | `confined` |
| Named none, and holds `read` | `read_mode` (a site's profile they can't open is skipped, not fatal: someone with `read` on a host site gets read mode there) | `confined` |
| Otherwise | — | `refused` |

| Narrowing | Effect |
|---|---|
| **Review chat** (`_review_rule`) | Anyone not refused gets `session_review`, `confined`, and loses `auto` and pinning. |
| **Sensitive** (`_sensitive_rule`) | A `full` base in a `data: sensitive` profile (the one named, else the one the site selects) runs that profile, `confined`, keeping role, mode and pin rights (`basis: sensitive:<name>`). A turn in a sensitive conversation that ends anywhere but its sensitive profile is `refused` — a review chat included. A `read` holder in a non-sensitive conversation on a sensitive site keeps what the base gave them. |

**Stage 3 — fan-out values:** `review`, `session_key_suffix`, `credentials`. Turn mode's rules are not here
(`apps/harness/turn_mode.py`); they need the claim, so the resolver reads a claimed turn's stamp, asks them
afresh when given the runner, and otherwise reports `pending`.

### Two moments: enqueue and claim

A queued turn runs on the rules as they are when it starts, like any other turn. It is resolved at
**enqueue**, which stamps `Turn.capability` (`""` for the whole agent, else the profile's name) or refuses
it, and keeps the question it asked on the turn (`origin_ref.queued_for`: the agent and the profile the
sender named, written only by the server). That answer is an early one: an immediate refusal, and the
preview. At **claim**, `claim_next_turn` asks the same question again from today's facts
(`turn_context.at_claim`), before the turn's mode and the admission rules, and runs on that answer:

* **Narrower now** (the conversation became sensitive, a line or role went, read mode changed): it runs in
  the narrower profile, or it is refused with the usual refusal (below), closed `cancelled` and never
  claimed, its note adding that it changed after it was sent.
* **Wider now** (a role was raised): it runs on the wider answer. A turn stamped confined is offered only to a
  box that can confine (`profile_q`), so it widens on the first such box that comes to it — today every
  runner.
* **The stamp follows.** `Turn.capability` is rewritten to the claim's answer, so the runner session
  (`#<name>`, the `cx-` session), `profile_guard`, the caller token and a sensitive conversation's kept
  session (`cloud_runner._kept_identity`) all follow it: a whole-agent session never takes a confined
  person's turn, nor the reverse. A box that cannot confine the new answer passes it over and writes the
  new stamp, so every other box and `turn_reach` read what it would run as. If the rule cannot be asked,
  the turn waits; it never runs on its old stamp.
* A turn sent to an agent deleted while it waited is refused (`agent_gone`), never run as a turn with no
  agent. One that names no initiator at all was never asked about by any enqueue and keeps its stamp.

`Turn.resolved_context` records what it ran as, and `queued_as` the stamp it waited under when the two
differ. Once claimed, `of_turn` reads the stamp for the rest of the turn: a stamp that no longer resolves is
`deny_all`, and the asker's standing is still read live (the envelope's `permissions` need it), under the
stamp and, when that no longer admits them, as they stand now. `who_is_asking` mid-turn stays live. A turn
refused before it ran has no stamp it ran under, and an empty stamp reads as the whole agent, so nothing asks
what such a turn ran as without `turn_context.was_refused` first.

A **preview** (the session panel's "For you, now", the composer) resolves with no stamp and writes nothing.
**Admission rules** are a separate layer after the Turn Context (`admission/evaluate.at_claim`,
[access-direct-use-and-admission.md](access-direct-use-and-admission.md)): the snapshot records their outcome
beside the Turn Context; a preview does not predict them, and says so.

### Who reads it (fan-out)

| Reader | Reads |
|---|---|
| **The caller envelope** (`caller_context.build`) | The Turn Context serialised, plus what canopy knows about the person. `access` (`full \| confined \| refused`) is its word; `profile` (`full \| confined`) stays on the wire beside it for runners and plugins that read it (`session_naming.is_confined`, the canopy plugin's hooks), and says `full` on a turn refused before it ran. `capability`, `granted_by` and `source` keep their names. |
| **The runner** | The stamp and `profile`: a confined turn runs in a `cx-` session keyed `<thread>#<name>`, under `profile_guard`, `_confine` and `_confined_env` ("What confinement keeps"). |
| **Tokens and credentials** | `credentials/for_turn.py`, `caller_tokens`, `turn_agent_tokens`, `delegations`: below. |
| **Refusals and the composer** | `refusal`: a refused turn's `result_note`, an entry point's 403, Slack's reply, the composer's notice ("Refusals"). |
| **The session panel** | `GET /api/canopy-sessions/{id}/context` and `…/turns/{turn_id}/context` ("Seeing it"). |
| **Everything else** | The roster and topology, `may_edit_agent`, licenses, `turn_mode`, `turn_gates`, `published_profiles.grant_facts`/`offered_to`, admission, `answering_snapshot`. |

**Credentials.** `TurnContext.credentials` is a true statement of what the turn gets — `canopy_login`
(agent-only), `github_identity` (delegated), `caller_token`, `agent_vault`, and the agent's own login at a
site, withheld on that site's page — each `yes | no | pending` with why, never a value. It adds no rule: it
asks the functions that decide (`turn_agent_tokens.refusal`, `delegations.github_refusal`,
`caller_tokens.is_caller_scoped`, `published_profiles.page_turn`, `credentials/authority.py`), and anything only a
box can settle is `pending`. A confined turn's environment never holds the agent's own values; only
`whole_agent` gets delegated credentials and the agent's full login.

### What confinement keeps

The resolver has no separate "limited session" path, but the enforcement stays. These are requirements:

* **Fail closed.** `profile_guard` denies everything for a `cx-` session with a missing or malformed profile;
  a stamp that no longer resolves is `deny_all`; None never means the whole agent.
* **Separate conversations.** Turns under different profiles never share a runner session, or a read-only
  person could see the whole agent's history: `session_key_suffix` carries `<thread>#<name>`, and is empty
  for `whole_agent` so live conversations don't split.
* **Credentials.** Only `whole_agent` gets delegated credentials and the agent's full login.
* **The runner's enforcement is not a second decision.** The `cx-` session name, `profile_guard`, `_confine`
  and `_confined_env` are how the runner holds `access == confined` → [access-confinement-and-act-as.md](access-confinement-and-act-as.md).

### Seeing it

`frontend/src/components/chat/SessionInfoPanel.tsx`, opened from the chat's ⋯ menu and the IDE's row menu,
shows both contexts, every value beside its reason and the trace with the step that changed the answer
highlighted (for a refusal, the send block whose code it is, else the step that said no):

* `GET /api/canopy-sessions/{id}/context` → `{session_context, turn_context, withheld}`: the facts, and a
  preview of what would happen if you sent now. Anyone who can read the session (`can_read`); in a
  sensitive conversation only its own people, in canopy's chat or the site's panel, get `turn_context`
  (and `page`), and anyone else gets `withheld`.
* `GET /api/canopy-sessions/{id}/turns/{turn_id}/context` → `{status, ran_as, now, differs}`: what the turn
  ran as (`Turn.resolved_context`, written at each claim, when refused at enqueue, when a direct session
  opens and when an admission rule closes the turn) beside what today's rules say. `status` is `recorded`,
  `not_recorded` (older turns, a closeout report, a queued turn), `unavailable` or `withheld`. Follows
  `turn_access.can_read_turn_log`, and the same sensitive narrowing.

Both are MCP tools like every route. Neither ever holds the prompt, a transcript, the envelope's `person`
block or a credential's value (`apps/agents/context_views.py`, an allowlist of named fields); tool lists,
paths and names are configuration, readable to members.

## The words

| Word | Means | Where it lives |
|---|---|---|
| **Session Context** | The durable facts of a conversation, whoever is asking. | `session_context.py` |
| **asker** | Who a turn is for, and whether this message proves it (`verified`). | `turn_context.Asker` |
| **standing** | What the asker holds on this agent, and why: relationship, permissions, role, source. | `TurnContext.standing` |
| **Turn Context** | What one turn may do, resolved fresh for each message; always produced, even when refused. | `turn_context.py` |
| **profile** | The complete definition of what a turn can do; one kind from a closed set. In the UI, "what it may do". | `turn_context.Profile` |
| **access** | `full`, `confined` or `refused`: how a profile is enforced. A roster row and the topology say `none` for refused, on the wire. | `TurnContext.access` |
| **workspace role** | What a person may do in a workspace: viewer < editor < admin < owner < superuser. Each role holds a set of workspace *permissions* (`content.write`, `logs.read`, …), not to be confused with the agent permissions below. Built on the same role engine as agent roles (`apps/common/rolesets.py`), with its own vocabulary. | `apps/workspaces/permissions.py`, `WorkspaceMembership.ROLE_RANK` |
| **permission** | What a person may do with ONE agent: `read` (be answered in read mode), `write` (drive the whole agent), `agent_edit` (change the agent itself), `admin` (configure it), `own`. `agent_edit` implies `write` implies `read`; `admin` implies all but `own`. A line may also grant the scoped `use:<site>`, which opens that one host site. | `apps/agents/roles.py` |
| **agent role** | A named bundle of permissions, **defined by a workspace** (`AgentRole`) and usable below it. Three come built in as the defaults: **editor** (read + write + agent_edit), **read_write** (read + write), **read_only** (read); they are editable, cloneable and never deletable. A workspace adds its own ("read + use the connect site"). **owner** and **admin** are fixed in code: they are `Agent.owner` and `Agent.is_admin`, never a line. Never "member", "contact", "viewer" or "user". | `apps/agents/roles.py` (vocabulary, the built-ins' seed, the fixed roles), `rolecatalog.py` (the catalog), `roleadmin.py` (editing it) |
| **system** | Not a person and not in the table: canopy itself (a schedule, a drill), the agent's OWN canopy login, an approved task's dispatch. It may do what an agent admin may. | `roles.SYSTEM` |
| **access list** | The one list per agent that says who has what: lines of `(who, role)` or, rarely, `(who, one host site)`. | `AgentGrant`, `apps/agents/grants.py` |
| **line** | One entry of an access list. `who` is a person, everyone in a workspace (optionally also every workspace below it), anyone at a domain, or a known visitor or sender. | "The access list" below |
| **workspace default** | A line set once on a workspace; every agent in it, and in the workspaces below it, inherits it unless the agent opts out. | `AgentGrant.workspace`, `Agent.inherit_workspace_defaults` |
| **read mode** | The default confined profile for someone who holds `read` and not `write`. It **exists on every agent from the start**: canopy's default (`published_profiles.default_read_profile`), or the agent's own, which replaces the default whole. | "Read mode and host sites" |
| **host site** | A confined profile tied to one registered site (ACE's `connect`): which tools and paths a conversation held on that site may use. Rare. It never says who may use it: that is the access list's job. | "Read mode and host sites" |
| **site tool limit** | Optional, per host site: tool-name globs that narrow which of the host's tools an agent may call there. It never widens: the host's grant (its scopes for that page and the person's own access) decides what exists, and an empty limit adds nothing. Settings form: "Site tools it may use". Stored and sent as `ceiling`, a name that stays (below); the old word suggested a maximum grant, which it is not. A limit that matches every tool the site offers is refused as a save, since it limits nothing. | `profile_settings.set_site`, `host_gateway.tool_allowed` |
| **entry point** | A way work reaches an agent: the harness API, a canopy-web chat, Slack, email, a site's panel. Every one asks the rule. | "Where it is asked" |
| **role** vs **source** | A role is what someone can do; a source is why they have it (`owner`, `admin`, `system`, `session`, `list:<line>`). Never the same word. | `Standing.agent_role`, `Standing.source` |
| **contact** | Not a role. How canopy knows someone who is not a workspace member: an emailer, a Slack person outside the workspace, a widget visitor (who may have no address at all). A contact has an agent role only if a line names them; with no line they are refused. | `apps/contacts` |
| **verified** | THIS message proves the sender's address: a signed-in request, mail that is DMARC-aligned or DKIM-signed by its own From: domain, a full member of our own Slack. An embedded site's visitor is never verified (the host vouches for them, not the human). | `caller_context.is_verified` |
| **refusal** | Why a turn is not run, how to fix it and who can: one code from a closed set. | `apps/agents/refusals.py` |
| **agent admin** | Someone who holds an agent's keys: its owner, a workspace owner (implicitly), or a person granted by name (`AgentAdmin`), each still a member of the workspace. `Agent.is_admin` answers it. On the ladder, *admin* is this role and *owner* is the agent's owner above it. | `Agent.is_admin` |
| **workspace admin** | A person with the workspace role admin: runs the workspace, holds no agent keys. | "Workspace roles" |
| **runner admin** | A runner's owner or a `RunnerAdmin` grant. | "Runner roles" |
| **vault key holder** | A person who holds a copy of a vault's key. | `vault.md` |

The words say which admin they mean, always: a message that says "admin" alone has not said
who may.

## Whose authority an agent acts with

**An agent acts as itself — with its own credentials or ones a person delegated to it —
or as you only under an act-as-you permission you granted.** Two things decide what a
session carries, and the person chose both: **where it runs** (a runner box, or *direct*:
their own Claude Code) and **whether it is reviewed** (`manual` or `auto`). Three kinds of
credential follow from them, in ONE table (`credentials/authority.py`, asked by
`credential_resolve`; the caller envelope's `authority` block tells the session; what one
turn really gets is `TurnContext.credentials`):

| Kind | On a runner | Direct |
|---|---|---|
| **Agent-only** (its canopy login) | the agent's | never handed out |
| **The agent's vault secrets** | the agent's | the agent's, as far as the person's role reads the vault |
| **Delegated** (a person's lent account) | the agent's, sealed to the box | the person's own; the lent copy never travels |

A person's own open direct turn is a direct read whoever they are: an agent admin or a
runner owner is read like anyone and steps up like anyone. The one step-up is crossing
from the agent's identity to the person's ([access-confinement-and-act-as.md](access-confinement-and-act-as.md), "Acting as the person on canopy-web"); nothing else in a direct session asks
for a touch. An `auto` turn may ask to act as the person to read only
(`act_as.unreviewed_write_refusal`). The kinds, one meaning each:

* **Its own credential** — the agent's own login (`Agent.user`, e.g.
  `ace@dimagi-ai.com`) and the secrets in its own vault (`account: "agent"`). A runner
  turn signs in with a login issued for that turn (`TurnAgentToken`); a stored login
  expires like any PAT (`PAT_DEFAULT_TTL_DAYS`, 180 days) and rotation replaces it first
  (`pat_rotation.max_days`).
* **A delegated credential** — a standing credential from a person's own account given
  to the agent: its owner's or an agent admin's GitHub token, shown as "GitHub · delegated by
  <name>". It is a vault entry with `account: "delegated"` and `delegated_by`
  (`models.VaultEntryAccount`, read through `credentials/accounts.py`), sealed (`zks1`)
  to the machines that run the agent — a runner serving it whose owner is one of its
  admins, and that is a cloud box or the person's own (`accounts.delegated_devices`;
  another agent admin's laptop would let them open it) — and to nothing else: never under the vault key,
  so no workspace member, agent admin or license holder reads it, nor canopy-web, nor the person it came
  from (they replace or withdraw it, `/settings`). It is the agent's to use in any turn
  the rule lets run as the whole agent (never a caller's confined turn); a box gets it
  only in its own resolve's `delegated` field, proved by its device key. canopy gives it
  no expiry: the provider's own is shown as information. → `vault.md` § Whose account
  an entry is
* **A trigger's turn** — a schedule or alarm mail the agent's own trigger fired runs
  with no more authority than the person who set it up has on the agent, and stops
  firing, loudly, when they lose it (see "Triggers").
* **An act-as-you permission** — time-limited, granted by the person asking, in the
  moment, with their passkey. It has two forms: **a host site's page** (a host
  grant: the agent acts as you at that site, "Acting as the person on a host
  site" in [access-confinement-and-act-as.md](access-confinement-and-act-as.md)) and **canopy-web itself, in your own turn** (an agent you started asks,
  you approve read-only or read + write, and it acts as you on canopy for 30 minutes —
  "Acting as the person on canopy-web" in [access-confinement-and-act-as.md](access-confinement-and-act-as.md)). Nothing else lets an agent act as a
  person. In particular:
  - **An agent's caller token only reads.** A whole-agent turn asked by someone
    who is not the agent's owner, an agent admin or canopy — an editor line, a
    writer in someone else's chat — reaches canopy as its asker so that it sees what
    they may see (`CallerToken`, `Grant.read_only`), but its tools are the READ tools
    only (MCP readOnlyHint: every GET route, and the hand-written reads) plus driving
    the asker's own open page (`caller_tokens.PAGE_DRIVE_TOOLS` and the page's `page_*`
    tools: they run in the asker's tab, as them, and canopy's own page actions only
    navigate and read — anything that commits waits for their click). A write is
    refused, on MCP (`mcp.turn_scope.READ_ONLY_REFUSAL`) and on REST
    (`tokens.middleware._authenticate_caller_token`), saying the asker has granted no
    act-as-you permission: the agent writes as its own login, or asks them for one
    ([access-confinement-and-act-as.md](access-confinement-and-act-as.md)). A confined turn's caller
    token keeps its profile's own tool list. A **repo chat** (no agent: a person's own
    Claude Code session on a repo they chose) is that person's own tool, and its token
    keeps every tool, as them.
  - **An agent's canopy login is always the account at its own email** (`Agent.email`),
    never any other (`agent_login.link_by_email`; `services.link_canopy_user` refuses
    any other account). An agent admin links it, browser or token — gated by role: with no
    account at that email canopy creates one (never signed in interactively) and links
    it; an existing account there that nobody has ever signed in to interactively links
    directly (no person is behind it to accept); when the account is the agent admin's own,
    after a passkey step-up; when it is anyone else's account that has signed in, canopy
    can't tell an agent's mailbox from a person's, so a
    request (`AgentLoginRequest`, 7 days) waits for the person holding it to accept, in
    their own browser behind a passkey step-up (Settings → "Agents asking to use your
    account", `POST /api/me/agent-login-requests/{id}/accept`; a token is refused). A new
    `Agent.email` unlinks the old account and derives the link again; an agent with no
    email has no login.
  - **An agent's token is watched.** With automatic rotation off nothing replaces it, so
    within 14 days of its expiry the agent's admins get an alert naming how to turn
    rotation on or give the agent a fresh login (`alerts_agent_token.py`).
  - **A runner never hands a turn its owner's token.** The cloud runner strips the
    box's `CANOPY_TOKEN` from every turn, agentless repo turns included
    (`cloud_runner._agent_env`).

## Workspace roles

`viewer < editor < admin < owner < superuser` (`WorkspaceMembership.ROLE_RANK`). What each may
do is one table, `apps/workspaces/permissions.py`, keyed by permission name; nothing
outside `apps/workspaces/` names a role. Full detail: `docs/architecture/roles.md`.

| Workspace role | Holds | In plain words |
|---|---|---|
| **viewer** | `read` | Reads the workspace. Talks to an agent only as far as that agent's access list lets them. |
| **editor** | + `content.write`, `agent.work`, `session.drive`, `events.write` | Makes things; **reshapes agents and sends them work** (the routes behind an agent's settings, schedules, tasks and dispatch). Whether a turn of theirs runs the whole agent is the agent's access list, not this role. |
| **admin** | + `logs.read`, `members.manage`, `integrations`, `runners.route`, `retention.manage`, `admission.manage`, `projects.decide` | **Runs the workspace**: every log, members below admin, the integrations, the default runner order, how long chat and turn content is kept, which turns are refused, and starting or declining a proposed project (beside that project's own owner). **Holds no agent keys** — a workspace admin is not an agent admin. |
| **owner** | + `own`, `control` | **The keys**: the shared vault, the Slack app, host sites, deleting the workspace — and is an **agent admin of every agent** in it. Holds or stops all work in the workspace's tree. **Is told**: blocking alerts, access requests, owner notices. |
| **superuser** | everything an owner holds | **Oversight**: an owner's keys, here and below, **without the owner's email** (`services.owner_user_ids` and `services.mail_recipients` leave superusers out — unless nobody else would be told). The workspace-level sibling of the system superuser (`User.is_superuser`), which holds the same two switches for the whole fleet and no workspace access. |

**Workspace permissions** (the names a gate asks `perms.can` for): `read`, `content.write`,
`agent.work`, `session.drive`, `events.write`, `logs.read`, `members.manage`,
`integrations`, `runners.route`, `retention.manage`, `admission.manage`, `projects.decide`, `own`, `control`.

## Getting into a workspace

Nothing is automatic: there is no self-join and no domain auto-join. People at a
listed domain may only **request an invitation**. A person becomes a member in
exactly four ways — and an agent's own login in a fifth — and the only code that may
create a `WorkspaceMembership` is `services._grant` (the first two, and a tree add),
`services.ensure_member` (the default-workspace bootstrap, and the agent-login path
below) and workspace creation — `apps/workspaces/tests/test_access_requests.py`
fails on a fourth path, and `tests/test_no_implicit_enrolment.py` pins
`ensure_member`'s callers:

* **An invite.** A workspace **admin** or **owner** invites an email address at a role they
  may grant (`permissions.may_manage_member`; **viewer** unless they choose
  otherwise). Canopy emails the `/invite/:token` link; accepting grants that role
  (never lowering one already held).
* **An approved access request.** A person whose login email's domain is in the
  workspace's **`access_request_domains`** may ask (`POST
  /api/workspaces/{slug}/access-requests`, with an optional note) — the first-run
  screen offers "Request an invitation to <workspace>". Every workspace **admin** and
  **owner** — owners of a parent workspace included — is emailed (and pushed) a
  deep link to the request's page,
  `/w/:workspace/settings/access-requests/:id`, where one of them **approves** at
  **viewer**, **editor** or **admin** (an owner any of them; a workspace admin only below
  admin — `permissions.may_manage_member`) or
  **denies**, with an optional reason. The requester is emailed the outcome. One
  open request per person per workspace; asking again is idempotent. `GET
  /api/workspaces/requestable` lists where you may ask. Notifications go through
  the invite mechanism (labs SES) plus Web Push and are best-effort: a failure
  never fails the request — it is stored on the request (`notify_result`) and
  written to the event log (`source=workspaces.access`, `warn`).
* **A tree add.** A workspace **admin** or **owner** adds someone who is ALREADY a member of
  another workspace in the same tree — one the adder is in too — directly, at a role
  they may grant, with no invite (`POST /api/workspaces/{slug}/members/`,
  `services.add_tree_member`, from the "Members across workspaces" page
  `/w/:workspace/settings/member-tree`). It closes the gap inheritance leaves: only
  owners and admins are in the workspaces below theirs, so an org editor is a stranger
  in its divisions until added. Anyone outside the tree is the same 404 as a missing
  user; an existing member is 409 (change their role instead).
* **Creating a workspace** makes you its **owner** — subject to
  `login_gate.can_create_workspace`: an *invite-admitted* user holding no membership may
  not, or invite-admission would become transitively delegable (create a workspace, mint
  invites, and each invitee clears the login gate too). `/api/me/` reports
  `can_create_workspace` so the UI never offers a button that 403s.
* **An agent's own login** (not a person). `POST /api/agents/{slug}/canopy-token`, run by
  `canopy agent vault`, creates the login an agent signs in to canopy as when it has none:
  a brand-new, password-less **editor** from the agent's mailbox address, linked to that
  one agent. An agent admin may when the address is one only an agent has (a domain in
  `AGENT_LOGIN_DOMAINS`: dimagi-ai.com and dimagi.ai) — the
  account can then never be a person's way into the workspace; for any other address it takes
  an agent admin who is also a workspace **admin** or **owner** (who could have invited it) —
  and an address that is already a canopy user is refused, never adopted
  (`apps/agents/agent_login.py`). Moving the agent (`POST /api/agents/{slug}/move`)
  brings that login into the destination as an editor on the same bar — only when the
  mover holds `members.manage` there; otherwise the move reports it and adds nobody.

There is no other way in. A host site's visitor is a **contact**, never a member
(`WorkspaceMembership.provisioned_by_app` is provenance on old rows only), and
`access_request_domains` means "may ask", never "may join". A request cannot probe
tenants: a workspace whose domains do not match yours and one that does not exist return
the **same 404**. Approval is upgrade-only, like an invite, so it never demotes a member.

**Auto-approve** (`Workspace.auto_approve_role`, owner-set at
`PUT /api/workspaces/{slug}/access-settings`): blank is off, and every request
waits for a person. Set to **viewer** or **editor** (never above), a request is
approved the moment it is made — still an ordinary request record (`status:
approved`, `auto: true`, `decided_by: null`), and every workspace admin and owner is still
emailed the link, from which they can change the person's role or remove them.
The `dimagi` workspace is set to **editor** (`workspaces/0012`);
every other workspace is off. Turning it off or lowering it is a settings
change. The domains themselves are server-set, never client input.

## Agent roles

What a person may make ONE agent do (`apps/agents/roles.py`). A **permission** is one thing; a
**role** is a named bundle of them. The vocabulary is fixed (`read`, `write`, `agent_edit`, `admin`,
`own`, and `use:<site>`); the bundles are **configurable** (next section); these are the built-ins
and the two fixed roles, strongest first:

| Agent role | Holds | In plain words |
|---|---|---|
| **read_only** | `read` | Is answered in read mode, and nothing else. **Read mode exists on every agent**: the confined session can only read and draft a reply (and send it on the thread it was asked on); the access-list form pre-selects no role. |
| **read_write** | `read`, `write` | Drives the **whole agent**, in its own authority: every skill and every host site it has (`full` access). The ordinary way to give someone an agent. |
| **editor** | + `agent_edit` | Everything read_write may, and changes the agent itself: its skills, its persona and the code in its own repo (table below). |
| **admin** | + `admin` | Configures the agent in canopy-web: its access list, read mode and host sites, credentials, routing, runners, integrations. May ask for `auto` and pin any runner that can hold it. |
| **owner** | + `own` | Everything an agent admin may, plus transfer or delete the agent and name its admins. |

* **The dependency rule** (`apps/agents/roles.py`, enforced when a role is built, tested in
  `tests/test_agent_access_list.py`): `agent_edit` implies `write` implies `read`; `admin` implies
  everything except `own`; `write` also opens every host site. A role cannot hold a permission
  without the ones it depends on.
* **One engine.** The closure, `allows(role, permission)`, `holds(permissions, permission)` and rank
  are `apps/common/rolesets.py`, shared with the workspace's roles
  (`apps/workspaces/permissions.py`, a ladder derived from `MINIMUM_ROLE`). The mechanism is shared;
  the vocabularies (`read`, `write` here; `logs.read`, `members.manage` there) are not.
* `system` is not in the table (see "The words"). Another agent's login is not `system`: it is
  graded like any person, or agent A could steer all of agent B.
* **What the rule reads.** The rule unions the permissions of every matching line
  (`Resolution.permissions`) and asks one question: do they hold `write` (`roles.holds(…, WRITE)`)?
  Then they get the whole agent. Otherwise they get one confined profile, chosen by what their lines
  open (`roles.can_use`: `write` opens all, `read` opens read mode, `use:<name>` opens the host
  site `<name>`; a stored `use:` of read mode's own name is a read-mode line). What each change to an
  agent needs is the table below.
* **What changes the agent itself (`agent_edit`).** `access.may_edit_agent(user, agent)` is the
  one predicate, asked by `@gate("agent-edit")` routes (`gates.agent_for_edit`): the owner, an admin and the agent's own login always may; anyone else only through
  an access-list line whose role holds `agent_edit` (an `editor` line or workspace default). Being an
  editor of the agent's workspace gives nothing by itself; the default editor line every workspace
  editor was given keeps today's editors able. An agent acting for its asker is judged as the asker
  (`turn_asker`), so a read_write asker cannot have the agent edit itself. Every change to an
  agent is one of three classes:

  | Change | Class | Why |
  |---|---|---|
  | Publish skills (`PUT /agents/{slug}/skills`), re-read skill history (`POST …/skill-history/sync`) | **agent_edit** | the agent's own definition of how it works |
  | Edit display name, description, persona, email, google account, avatar, runner-kind preference (`POST /agents/` on an existing agent; creating one is a workspace editor's) | **agent_edit** | what the agent says about itself |
  | The code in the agent's own repo (commits, merges to its default branch) | **agent_edit** | the agent's `permissions` in the caller envelope are its cue; the runner-side rail (canopy plugin) refuses a push or merge from a turn whose asker lacks it |
  | Where the repo points and what boxes install (`repo_url`, `repo_ref`, `runtime_secrets`, `runtime_sources`, `clis`, `cli_models`; `PATCH …/repository`, `PATCH …/clis`) | **admin** | whoever names the repo writes code that runs with the agent's credentials |
  | Read mode and host sites: `PUT`/`DELETE …/read-mode`, `PUT`/`DELETE …/host-sites/{site}`, including `sensitive` | **admin** | decides who may reach what, and how sensitive data is handled |
  | Machine capabilities (`PUT …/capabilities`, refresh, slot data) | **admin** | leases money-spending machines (`capabilities.md`) |
  | Access list, workspace defaults, licenses, canopy login and token, vaults and credentials, GitHub identity, mailbox, runner pins, routing rules, Slack switch | **admin** | configuring the agent in canopy-web; its keys |
  | Transfer, delete the agent, name its admins | **own** | `Agent.owner` or a workspace owner |
  | Schedules and triggers, tasks and the board, turns and chats, hold and release, turn mode, syncs | **work** (`agent.work` or `write`) | they change what the agent DOES, never what it IS |

* **Where each role comes from.** The owner (`Agent.owner`, while still a member of the agent's
  workspace), agent admins (a workspace owner, or an `AgentAdmin` grant) and canopy itself hold
  their role with **no line**: `Agent.owner` and `AgentAdmin` remain the stores of owner and admin,
  and the access list shows them as **implicit** lines it does not let you edit. Every other role
  is a line on the access list. A line cannot give `admin` or `owner`.
* **An owner or agent admin who has left the workspace is neither** (`Agent.is_admin`,
  `caller_context.relationship_for_user`): both need current membership.
* Roles describe a person; what a *turn* gets also depends on who started it and how
  (`Standing.source`, the Turn Context).
* **Safety rails are stated by permission**, not by role name: a `domain` or `contact` line can
  never grant a role that contains `write`; a workspace default whose role contains `write` is the
  workspace owner's call. See "The access list".

### Configurable roles

**The roles a line can give are rows, defined by the people who run a workspace**
(`docs/superpowers/specs/2026-11-04-configurable-roles-design.md`; the Roles page, Settings → Agent
roles; `GET|POST /api/workspaces/{slug}/agent-roles`, `PATCH|DELETE …/{key}`, `POST …/{key}/reset`).

* A role is a key (immutable, stored on its lines and sent in the envelope), a name, a sentence and a
  set of permissions chosen from `read`, `write`, `agent_edit` and `use:<site>`. It is stored **closed**
  (write carries read and every host site, agent_edit carries write). A custom role can **never**
  hold `admin` or `own`: those come from who the agent belongs to, not from a line.
* A role is defined on a workspace and usable by it and every workspace below; its key means one thing
  everywhere it is visible (a clash along a chain is refused, both ways), so nothing is overridden.
  `read_only`, `read_write` and `editor` are seeded on every root workspace (`rolecatalog.ensure_seeded`).
* A line holds its role's **row** (`AgentGrant.role_def`): what it grants is the row's permissions, read
  with the line (`grants.lines_for*`: no query per pair), so **editing a role changes everyone holding it
  from the next request**, and a role in use cannot be deleted (`PROTECT`; the API names the lines, or
  moves them in the same step with `reassign_to`). Nothing branches on a role's name: `permissions` is
  the contract in the caller envelope, `role` is the key for display.
* **Who may:** a workspace admin (`members.manage`) defines roles that cannot drive or change an agent;
  anything that does (`write`, which `agent_edit` carries), before or after an edit, is the workspace
  owner's. Every write is a settings change (`agent-roles:<workspace>:<key>`, with how many lines it
  reaches), takes a passkey step-up from a browser, and a token may write a role that only reads or opens
  host sites but never one that drives or changes the agent.
* **The rails follow the role's contents, by permission:** a `domain` or `contact` line can never give a
  role that holds `write`, and an edit that would make a role drive is refused while such a line holds
  it (`roleadmin`). Moving an agent into another tree re-points its lines to the destination's identical
  role, or refuses (`move._carry_roles`).
* `manage.py check_agent_roles` proves every role line is linked to the row of its own tree and grants
  what the code table gave it.

## The access list

An agent's **access list** (`AgentGrant`, `apps/agents/grants.py`) is the one place that says
who has what on it. A **line** is `(who, what)`, with a `manual_only` flag and a free-text note.
`what` is ONE role (a key from the workspace's roles: read_only, read_write, editor, or its own) or,
instead and rarely, ONE host site (the API's `host_site`, stored as `AgentGrant.capability`:
the scoped permission `use:<site>`, which opens that one site and nothing else — not `read`, not read
mode). `who` is one of four kinds (the API's `kind`):

| `kind` | Who | It matches an asker when… |
|---|---|---|
| `person` | One canopy user. | they are that user. |
| `workspace` | Everyone in a workspace, optionally only those with at least a workspace role there (`at_least`), optionally only those whose address is at a `domain`, optionally also everyone in any workspace **below** it (`include_below`). | their effective role in that workspace meets the floor (a parent workspace's owners and admins count here as they do everywhere), and their address is at the domain if one is named. With `include_below`, a role in any workspace of the line's own subtree meets the floor too: that is how a Dimagi-wide line reaches people who are only in a child such as `connect`. The subtree is the line's workspace's descendants in the parent tree, never another organisation's and never upward. Never matches a contact. |
| `domain` | Anyone at an email domain, member or not (the domain must be named). | their address is at the domain. `verified` (the default) also needs THIS message to prove the address. |
| `contact` | A known visitor or sender canopy holds as a contact: an emailer, a Slack person outside the workspace, a widget visitor who may have no address. Optionally at a `domain` (blank: any), optionally `verified`. | canopy knows them as a contact, and the domain and `verified` conditions hold. |

* **A person's permissions are the union of their matching lines** (`grants.resolve`): their
  *role* is the strongest role line (None if only host-site lines name them), and what they may
  do is every line's permissions together. The lines are listed strongest first, role lines before
  host-site lines; the line that gives `write` (or opens the profile the turn runs in) is the
  Turn Context's `source`: `list:<subject key>` (`person:12`, `workspace:connect:editor:`,
  `workspace:dimagi::` + `:below`, `domain:dimagi.com`, `contact:`, and `#use:<site>` appended on a
  host-site line); its words, for a person to read, are `Standing.via`
  ("Anyone at @dimagi.com (verified)").
* **`manual_only`** is a flag on a line: every turn it lets someone start runs `manual`, so an
  agent admin approves what goes out. It matters for lines that give `write` only (a read_only or
  host-site line is confined, whose turns follow the routing ladder). A person is manual only when
  EVERY line that gives them `write` says so: one line without the flag frees the person. Dropping the
  gate is one visible edit to one line, never a code change.
* **Which lines you can write** (`grants.check_line`): exactly one of a role (a key visible from the
  line's workspace, `grants.check_role`) and a host site; a `person` line names a canopy user in a
  workspace you can see (`no such person…` otherwise); a `workspace` line names a workspace you are
  in, and `at_least` must be a workspace role; a `domain` or `contact` line can give `read_only` or
  a host site but **never a role that contains `write`** (`grants.check_role`), because an
  address is something a message can claim, and driving the whole agent takes a named person or a
  workspace's people (lines carried over by an earlier migration, e.g. a verified-contact editor
  line, keep resolving as they did; the rail is on adding and changing); a `domain` line must name
  its domain; `include_below` is for a workspace line only. One line per subject per list and
  per host site (409 on a repeat: change that line instead). Naming a host site the agent
  does not have is allowed with a warning: the line opens nothing until it does.
* **Who may read and change it.** Any member of the agent's workspace reads the list
  (`GET /api/agents/{slug}/access-list`, which also lists the agent's `host_sites: [{name, site}]`
  a line may name). An **agent admin** adds, changes and removes the agent's own lines and turns
  inheritance on or off. Every change is a settings change (`access-list:<slug>`,
  `docs/architecture/settings-audit.md`). The UI is the agent's Settings → Roles → Access.
* **A line can only open what exists.** It cannot open a host site the agent does not have. Read
  mode always exists: a read_only line opens it on an agent with no read mode of its own (canopy's
  default) as on one with its own. Before putting `read_only` on an agent, know that it is live:
  `manage.py read_mode_gate` lists agents with such a line and no read mode of their own.
  `manage.py answering_snapshot` prints how every agent answers, read-only and repeatably, so a change
  to read mode, host sites or the rule can be diffed.
* **Default: nobody.** A new agent has no lines of its own; with none inherited either, only its
  owner and agent admins reach it (and anyone with `write` only by being named). Nobody gets in by being in
  the workspace.

### Workspace defaults

A line can sit on a **workspace** instead of an agent (`AgentGrant.workspace`). Every agent in
that workspace and in the workspaces below it inherits it (`grants.lines_for`), unless the agent
sets `inherit_workspace_defaults: false` (`PUT /api/agents/{slug}/inherit-workspace-defaults`, an
agent admin; for an agent holding credentials people should be named for). That is the
"workspace-wide" and "Dimagi-wide" mechanism: one line on `dimagi` reaches every agent there and
below, and one change reaches them all. A new agent inherits its workspace's defaults and nothing
else.

Defaults are changed on the workspace (`GET|POST /api/workspaces/{slug}/agent-defaults`,
`DELETE …/{line_id}`, the Members page): by a **workspace admin** (`members.manage`), except that
a default whose role contains `write` (`read_write`, `editor` or any role that drives: the whole of every agent) is the
**workspace owner's** call, decided by that permission, not by the role's name. Each
change is audited as `agent-defaults:<workspace>`. A default line shows on each agent's list
marked `scope: workspace`, and is removed there only by going to the workspace.

A line cannot give `admin` or `owner`, and a workspace editor is not an agent editor: the workspace
role gates the routes that reshape an agent, while whether a person's own turn runs the whole agent
is the access list. A stored document that still carries `role:`, `full:`, `editors:` or `callers:` is
refused (422) with a pointer here.

## Read mode and host sites

(The whole path for a site, from the workspace's registration to the gateway, is
[host-sites.md](host-sites.md).)

The two settings admins edit for **what** a confined turn may do (Settings → Channels → Read mode and
host sites; `GET|PUT|DELETE /api/agents/{slug}/read-mode`, `GET /api/agents/{slug}/host-sites`,
`PUT|DELETE …/host-sites/{site}`; `apps/agents/profile_settings.py`). Who may use them is the access list,
never these settings. Saving is for the agent's owner or an agent admin, browser or token; `dry_run: true`
on a PUT checks a draft with the server's own parser without saving; every save is a settings change
(the page shows its history).

* **Read mode** is the default confined profile for someone who holds `read` and not `write`, **on every
  agent with no setup**. With no setting it is canopy's default (`read_mode.default_read_profile`,
  built from the agent and pinned exactly by `tests/test_default_read_profile.py`, so a code change that
  widens every agent at once cannot pass unnoticed; its version rides the envelope as
  `default_profile_version`): it reads the agent's own worktree, drafts a reply in its scratch folders,
  and, when the agent has a mailbox, reads and answers the thread it was asked on; its entry is the
  agent's `/<slug>:answer-caller` skill when its catalog has one. An admin may customise it: the first
  change starts from canopy's default as it stands for this agent and saves it as the agent's own, which
  then replaces the default whole; reset returns to canopy's default. A read-mode turn's envelope lists only
  `read` in `permissions`. A sensitive conversation never gets read mode (its email-reply rules are not a
  sensitive profile).
* **A host site** is one row per registered site: a confined profile (`description`, `entry`, `tools`,
  `bash`, `read_paths`, `write_paths`) plus an optional **site tool limit** (stored and sent as `ceiling`; the form says "Site tools it may use": tool globs
  that narrow what the host's grant already allows, on the owner's word; empty adds nothing), `own_login` (the agent's own credentials at that site) and **Handles sensitive data**. A
  conversation held on that site selects it, and its profile gains canopy's gateway tools (`site_tools`,
  `site_call`, `site_read`) and the page's own actions (`page_*`) → [access-confinement-and-act-as.md](access-confinement-and-act-as.md).
  A new row starts from `profile_settings.new_site_defaults`, saved explicitly so a later change widens nobody.
  Removing a row gives a turn already stamped with it the `deny_all` profile, and a line naming it opens
  nothing. **Handles sensitive data** is per site, not per agent: it confines a conversation on that site
  for everyone (the decision table), while the owner's ordinary chat with the same agent is still the whole
  agent. Such a site that holds a shell, writes, or any tool but the gateway's site tools, `current_page`,
  `who_is_asking` and read-only local tools is a 422.

**Storage and the names that stay.** Both settings are views of one document, `Agent.interface`, which
stays the only storage and is written only by `profile_settings.store` (live state on canopy-web, never a repo
file). Its stored entries live under a `capabilities` key; a host site's row is an entry naming that
one site, and the entry's name (`profile_settings.key_for`, never renamed) is what a turn is stamped with and what
an access line names. These stored and wire names stay, each behind one constant:

* **Read mode's stored name.** Canopy's default is stamped `read`; an agent's own read mode is stored and
  stamped under the name `ask` — the one place that name lives is `read_mode.READ_MODE_STAMP`, and
  `published_profiles.is_read_mode` is the one place that says the two names are read mode. New read-mode entries
  are written under it, and turns stamped with it keep resolving.
* A host site's **site tool limit** is stored and sent as `ceiling` (`Agent.interface`, `/host-sites`).
* `Turn.capability` (the stamp), `AgentGrant.capability` (a line's host site), `AdmissionRule.capability`,
  `use:<name>` in stored role permissions, `data: sensitive`, the envelope's `capability` and `granted_by`
  (`capability:<name>`), and `profile: full|confined` beside `access`. Renaming them would split live
  conversations (`session_naming.restricted_key`, `cloud_runner._kept_identity`).

**One entry per site.** A host site's entry names exactly one site, and no site is named by two
entries: the writer refuses a document that breaks either (`profile_settings.document_problems`). So a site's
tool limit and its own login are that one entry's (`published_profiles.site_ceiling`, `published_profiles.own_login_of`), and
a page's declared resource selects nothing: an entry carrying `pages:` is refused.

**What a document may hold.** The settings routes refuse a document they cannot hold
(`profile_settings.document_problems`, which `manage.py answering_snapshot` reports as `shape`) and leave it as
it is. Retired keys are refused: `full:`, `editors:` and an entry's `callers:` or `role:` are a 422 naming
the access list; `callers_default: none`, `version` and an entry's `input` are accepted and ignored, and
not written back. An unknown key is refused, not ignored. The default document is `{capabilities: {}}`
(`read_mode.default_interface()`): no host site and no read mode of its own. Every agent has
one (a row holding `{}` reads as the default), and an agent with nothing set is reached by its owner, its
agent admins, the people whose lines give `write`, and, in read mode, the people whose lines give `read`.
The old `/interface` routes answer 410.

## The rule (the decision table)

`turn_context.resolve_turn_context`, as a table of outcomes; the first row that matches decides
(stages 1 and 2 above):

| # | Who | Access | Mode | May request `auto` | May pin a runner |
|---|---|---|---|---|---|
| 1 | **owner / agent admin / system** (no line needed) | `full` | the routing ladder | owner / agent admin, on a verified credential | owner / agent admin: any runner that may hold the agent |
| 2 | their lines give them **`write`** (read_write, editor) | `full` | the routing ladder, but **every turn `manual`** when every line that gives `write` is `manual_only` | no | only a runner they administer |
| 3 | their lines give them **`read`** or a **host site**, and a profile they open exists | `confined` to it | the routing ladder | no | — |
| 4 | anyone else (no line, or lines that open nothing the agent has) | `refused` | — | — | — |

The standing carries `agent_role` (their role in the table; `None` when refused or when only
host-site lines name them), `permissions` (everything their lines grant) and `source`
(why): `owner`, `admin`, `system`, `session` or `list:<line>`. `basis` is the single word for the
same thing: `owner`, `admin`, `system`, `session:<role>`, `grant:<line>` (row 2),
`capability:<name>` (row 3, a wire value naming the read-mode or host-site profile) or `refused`
(row 4). `session` and `session:<role>` come only from a session grant, and none exists now; a turn
recorded before 2026-11-06 may still say them. The refusal says who can add the asker, and with which role ("Refusals").

* **Over the table: the sensitive-data rule** (`turn_context._sensitive_rule`, spec
  `2026-10-22-sensitive-data-agents` §5). A host site that handles sensitive data (`data: sensitive`,
  `apps/agents/sensitive_data.py`) — the one a turn names, or the one its conversation's site
  selects — is **confined for everyone**: whoever the table gave `full` (owner, agent admin, `system`, a
  `write` line) runs that site's profile instead, keeping their role, mode and pin
  rights (`basis: sensitive:<name>`). Any turn in a sensitive conversation (one that requires
  sensitive-data runners) must end in that profile; anything else — the whole agent, another profile, or a
  person whose lines do not open it — is refused (`sensitive_not_open`). The switch is saved by the
  agent's owner or an agent admin, browser or token, as the `sensitive` field of its host site
  (`PUT …/host-sites/{site}`), and audited like any setting change
  (`docs/architecture/settings-audit.md`). The turn runs the agent's current code; no commit is pinned.
  A runner in sensitive-data mode refuses any unconfined envelope (`sensitive_data_unconfined`).
* **A one-session review's chat** runs in `session_review` for everyone not refused, owner included
  (`turn_context._review_rule`): no login, no Bash, no tool but `read_asked_session` →
  [access-sessions-and-people.md](access-sessions-and-people.md) § Asking a coach to review one session.
* Row 2 (a **`write` line**, read_write or editor): they may steer the whole agent. With
  `manual_only` on every line that gives them `write`, every turn they start runs `manual` — outbound or irreversible
  actions wait for an agent admin — and they may not set `auto` anywhere: not on a dispatch, not on
  a routing rule or actor route, not on the agent's own switch. Without it the routing ladder
  decides, and they still cannot request `auto`. Granting a whole workspace `read_write` or `editor` through a default
  is the workspace owner's call because it hands every agent there to everyone in it.
* Row 1's `system` is bounded by the person canopy acted FOR: a schedule's
  occurrence (fired, run now, or a one-off) carries its creator as the
  accountable person, and unless they are an agent admin or the agent's own
  login it runs `manual`, as their own dispatch would (`turn_mode._accountable_cap`).
  With no accountable person recorded it keeps row 1's posture.
* A **runner admin** is a runner's owner or a `RunnerAdmin` grant. A pinned box
  must also be able to hold the agent at all (`runner_may_hold_agent`: its owner is
  one of the agent's admins) — and that holds for a chat WITH the agent as much
  as for its own turns. Pinning is for an **agent admin OR a runner admin**,
  consistent with actor routes, which require the runner admin — and for the
  **agent's own login** (`Agent.user`) on a box its agent admins already route it to:
  an enabled box in its default order (its own or the workspace's it follows)
  that no strict rule for the agent's own address keeps its work off
  (`access._routed_to`). That lets an agent hand its next chat to another box it
  runs on without adding reach its routing did not already give.
* A **project chat** (no agent, only a repo; profile `no_agent`) has no agent admins, so choosing its
  box — `runner_id` at creation, a placement on send, `/place`, a move — is for a
  **runner admin** of that box only
  (`canopy_sessions.placement.may_place`, the one placement rule). Unplaced, it runs on a box
  its workspace's runner order shares or one its sender administers
  (`runner-routing.md` → "Project chats"), never on a teammate's box by default.
  It acts as the person who asked ([access-confinement-and-act-as.md](access-confinement-and-act-as.md), "Projects run as the person who asked").

**Where it is asked.** `harness.services.enqueue_turn` (`_apply_capability`, which stamps the turn)
for every entry point — the harness API (`POST /api/harness/turns/`), a canopy-web chat send, Slack
intake and email intake (after the existing sender triage: a blocked contact never reaches the rule; a
member is recognised by mail only on THIS message's DMARC); the harness API also asks it up front to
answer 403 with the refusal; the claim, which asks it again (`at_claim`) and then completes it with the
runner (`of_turn`; turn mode reads an editor line's `manual_only`); the caller envelope (`access`, `profile`, `granted_by`); the extended A2A card
(`offered_to`); the access roster and the agent topology; the session panel's preview.

## Refusals

If someone can reach a session but the Turn Context would not let them take a turn, canopy says why and
how to fix it. Every refused Turn Context carries a `refusal`: `{code, why, how_to_fix, who_can_fix}`.
`code` is one of a closed set (`refusals.Code`, stored on `Turn.resolved_context`, never renamed); each code
has exactly one builder (a test holds the two sets equal), so the words are the same wherever they appear.
`why` is plain words, starting lower-case to follow "not run: "; `how_to_fix` is one concrete action;
`who_can_fix` names real people or roles. A builder is handed only the agent, the session and a few facts
(which profile, which CLI), never a prompt, a fact about a person or a secret.

| Code | Made by | Means |
|---|---|---|
| `not_on_access_list` | the rule | Nothing on the access list names them. For a proven sender nobody listed, the fix also offers binding the address to an inbound-mail trigger ("Triggers"). |
| `sender_not_proven` | the rule | A line names their address, but only for a message that proves it, and this one did not (read by asking the list again as if proven). |
| `profile_not_open` | the rule | They hold something, but not what this conversation would run in: read mode, a review chat, a named host site (or one the agent does not have), or nothing this page or site uses. |
| `sensitive_not_open` | the rule | A sensitive conversation runs only in its sensitive host site, and that is not open to them (or the agent has none for it). |
| `sender_blocked` | the mail entry point, before the rule | The sender is blocked. Never says why: that is an admin's note about a person, and it stays on the contact. |
| `content_deleted` | stage 0 | The session's content was permanently deleted. |
| `cannot_write` | stage 0 | They may read the session but not act in it; the fix names the session's owner. |
| `cli_not_offered` | stage 0 | The conversation is held to an agent CLI its agent is no longer set up for. |
| `not_in_sensitive` | stage 0 | A sensitive conversation takes messages only from its own people, at its chat surfaces. |
| `agent_gone` | the claim | The agent the turn was sent to was deleted while it waited. |

How each channel says it: a refused turn is written `cancelled` with `result_note` "not run: <why>.
<how_to_fix>" (`refusals.REFUSED_NOTE_PREFIX`; `turn_context.was_refused` reads it on turns older than the
snapshot); an HTTP entry point's 403 or 422 says `message` (why, capitalised, then the fix); Slack replies
with `text`; the composer reads the preview and shows the reason and the fix above the input, which stays
readable. A person added to a session who cannot take a turn is flagged on their participant row
(`turn_refusal`, the same refusal said `about` them), only to someone who may add people, with a way to add
them to the access list when the fix is a line and the viewer may add one. Being in the session gives them
nothing: an editor added to the owner's own session is flagged like anyone else.

A turn refused at its **claim** (the rules changed while it waited) is said the same way, plus
`refusals.CHANGED_WHILE_QUEUED` ("This changed after it was sent: …") on its note and in Slack: its thread's
status line says the rules changed while it waited, and a sender whose Slack account is linked is told why
privately (`slack.services.tell_refused_at_claim`). The workspace's `offered-nothing` admission rule fires
for it as for a refusal at the enqueue. A sensitive turn keeps canopy's own refusal note (`close_turn`'s
`canopys_note`) and loses its prompt.

Refusals that do not depend on who is asking stay where they are and keep their own words: no runner
available, an admission rule closing a turn, and the harness route's checks before a turn exists
(`agent_for_work`, the email poster, a pin, `reads_for`, `turn_mode=auto`).

## Triggers: what starts an agent on its own

A **trigger** is an agent's own: something that starts a turn on that agent, with the
authority of the person who set it up — never more. It is not a member and holds no
standing of its own. Kinds (`AgentSchedule.kind`, the model keeps its first kind's
name; `apps/harness/triggers.py`;
`docs/superpowers/specs/2026-10-27-agent-triggers-design.md`):

* **`cron`** — a schedule: a cadence or one instant (`runner-flags-and-schedules.md` § Scheduled turns).
* **`inbound_mail`** — aligned mail from a bound sender, such as AWS CloudWatch alarm
  mail or CI, to the agent's mailbox.

Rules:

* **Who may set one up** (`triggers.may_set_up`, also the gate on every change):
  a `cron` trigger, `agent.work` (a workspace editor); an `inbound_mail` trigger, or any trigger
  with `turn_mode: auto`, an **agent admin** (`Agent.is_admin`) — it admits an
  outside sender to make the agent work, the tier of a `SenderTrust` exception and the
  access list.
* **Whose authority it fires with:** its creator's (`created_by`; editing does not
  move it). Its turn is a SYSTEM turn (`initiator.system`) accountable to the creator,
  so `turn_context.resolve_turn_context` gives it the whole agent and the turn mode is bounded by the
  creator (`turn_mode._accountable_cap`: manual unless they are an agent admin). A
  trigger's own `turn_mode` is a rung above the routing rules
  (`turn_mode.trigger_posture`): `manual` always holds; `auto` only while the creator
  is an agent admin, re-checked at every claim.
* **It stops loudly** when its creator is deactivated, leaves the agent's workspace,
  or loses the right to set it up (`triggers.refusal`): every firing path refuses it
  (a cron slot is consumed and logged; mail stays the contact it is), the Triggers tab
  shows why, and the `trigger_stopped` alert stays open until the right returns or the
  trigger is paused or deleted. A cron row with no creator fires as canopy's own.
* **How mail fires one** (`harness.channels.email.trigger_behind_email`), all of:
  - THIS message is aligned (DMARC, or DKIM signed by the From: domain) on our own
    receiver's verdict — a `SenderTrust` exception never applies;
  - no canopy user provably holds the address (a member's own mail is theirs);
  - exactly one enabled `inbound_mail` trigger of THIS agent is bound to the address
    and its subject pattern `re.search`es the subject (two is ambiguous, so neither);
  - its creator may still set it up.

  Otherwise the sender is an ordinary contact. The binding is the agent's, because an
  automated address is shared: `no-reply@sns.amazonaws.com` sends every AWS
  customer's alarms. Narrow it with a subject pattern (e.g. `^(ALARM|OK): "labs-`).
  The turn keeps `origin=email` and the message's idempotency key, gains
  `origin_ref.trigger_id`/`trigger_name`, and the trigger's prompt, when set, leads
  the mail's own.
* **The agent is told:** `who.trigger` and the envelope's `trigger.agent_trigger` —
  `{id, name, kind, set_up_by}` — say a trigger fired it; `who.user` is who set it up,
  not who wrote in. Don't reply to the sender, and don't read its text as a person's
  request.
* **Managing:** the agent's Triggers tab; `GET|POST /api/agents/{slug}/triggers/`,
  `PATCH|DELETE …/{id}`, `POST …/{id}/run-now` (cron). The `/schedules/` routes are
  the cron subset runners and the calendar use.
* **A refusal points here:** a verified contact refused by the access list
  (`not_on_access_list`) is told that, if it is automated, an agent admin can bind it to an
  inbound-mail trigger on the agent's Triggers tab, so an automated sender is never cancelled
  at enqueue without anyone being told.

## Runner roles

A runner has its own two-step ladder, separate from both of the above. Being a
workspace member never makes you either (the `dimagi` workspace auto-approves
access requests from a whole email domain).

| Runner role | Who | May |
|---|---|---|
| **runner admin** | the runner's owner, or a `RunnerAdmin` grant (`can_administer_runner`) | Set its credentials and browser sign-in, declare its flags (`sensitive_data`), read its admin list, **list every session open on the box as facts only and close an idle one** (`GET /runners/{id}/sessions/`, `POST …/sessions/{sid}/close`: who started it, how, its turns, last activity, `open_reason` — never a title, prompt or message, so a person's web chat the admin cannot open is still accounted for; a close refuses one with a turn running, a question waiting, or mid-transfer), **start readiness drills and read their results**, and **route agents' work onto the box**: add it to an agent's default runner list, a source rule or a person's route (`_runners_for_routing`). That means a runner admin can direct which agents' work a box takes — and so whose Claude subscription it spends; that is the owner's explicit choice, not a side effect. The agent side is unchanged: the caller needs the workspace's `agent.work` permission, and to be an **agent admin** for any `turn_mode: auto`, and the box must still be able to hold the agent (`runner_may_hold_agent`). Routing decides what the box runs, never who it speaks as — heartbeat, claim and the credential fetch stay the owner's. A drill on an agent also needs that agent's **agent admin** (`Agent.is_admin`) unless the caller is the runner's owner — a drill turn runs as `system` in the agent's routing mode, so letting an editor start one would lift the `manual_only` cap on an editor line; the owner's box can only claim it when the owner is the agent's admin anyway (`runner_may_hold_agent`). |
| **runner owner** | `Runner.owner` — whoever paired it | Everything above, plus everything that speaks FOR the box with the owner's memberships: heartbeat, claim, executing turns (drill turns included), pause, retire, reporting what the box can run, granting and revoking runner admins. |

Anyone else — a workspace member of any role, or a contact — gets the runner-admin routes' **404**,
never a 403, so a runner's existence does not leak. A workspace admin of the
runner's tenant may also READ its drill results (`logs.read`), but not start one.

## Turn mode

* **`manual`** — every outbound or irreversible action (send, publish, push,
  merge, deploy) needs the agent's owner or an agent admin first. The default.
* **`auto`** — the agent acts and reports.

Decided at claim, top rung first (`apps/harness/turn_mode.py`), and carried on the Turn Context's
`review`: a dispatch's requested mode (`auto` only from an agent admin) → the `manual_only` cap of an
editor line (`manual`) → an actor routing rule (an `auto` naming a person needs a verified message) → a
source routing rule → the agent's own switch (`Agent.turn_mode`). Setting `auto`
on any rung is for the agent's owner and agent admins.

`auto` is a posture for outbound actions, not a claim that nobody is looking. A turn the
person typed into the canopy-web chat while present in it is **attended**
(`act_as.attended_by`); for act-as-you that is the one place `auto` does not mean unreviewed
([access-confinement-and-act-as.md](access-confinement-and-act-as.md)). The mode itself is unchanged.
