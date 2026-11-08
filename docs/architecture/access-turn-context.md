# Access: the Turn Context, the decision table and refusals

What one turn may do and why: the Turn Context and what it holds, profiles, how it is resolved, the two moments (enqueue and claim), the fan-out, what confinement keeps, the decision table and every refusal. Part of the [access](access.md) topic; the index there lists every part.

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

Being a participant in a session never lets someone in: they need a line, `read_only` for read mode,
`read_write` or `editor` for the whole agent.

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
(row 4). `session` and `session:<role>` come only from a session grant, and none exists, so only an
old recorded turn can carry them. The refusal says who can add the asker, and with which role ("Refusals").

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
