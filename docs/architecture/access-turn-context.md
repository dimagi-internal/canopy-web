# Access: the Turn Context, the decision table and refusals

What one turn may do and why: the Turn Context, profiles, how it is resolved, the two moments (enqueue and claim), what confinement keeps, the decision table and every refusal. Part of the [access](access.md) topic; the index there lists every part. Start reading at the module docstring of `apps/agents/turn_context.py`: it states the stages, and the dataclasses (`Asker`, `Standing`, `Review`, `Profile`, `TurnContext`) carry the field meanings.

## The Turn Context

```
FAN IN                                 RESOLVE             FAN OUT
Session Context ─┐                                         ┌─ the agent (caller envelope)
Asker           ─┼─→ resolve_turn_context() → Turn Context ┼─ the runner (session, confinement)
Agent settings  ─┘                                         ├─ tokens, credentials
                                                           ├─ the turn's refusal, the composer
                                                           └─ the session panel, in chat and the IDE
```

`turn_context.resolve_turn_context` is pure: **it writes nothing** (a test wraps it in a read-only guard).
`caller_context.build` writes a `PersonAccess` row and may create a `Person`; that stays in the envelope's `person`
block, outside the resolver. Entry points ask it through `for_turn` (a turn, from its own initiator), `for_asker` (a
question with no turn: roster, topology, `may_edit_agent`, licenses), `at_claim` (the claim's question, asked again
from today's facts) and `of_turn` (what a claimed turn **runs as**: the stamp the claim wrote). Each has a docstring.

* **Session Context** (`apps/agents/session_context.py::SessionContext`) is a frozen read-model over rows that already
  exist: no table, no write, nothing about the asker. Its `channel` is `turn.origin` for a turn, never
  `Session.origin`, which only says which side created the row. `sensitive` never reverts once true.
* **`session_grants`** (`SessionContext.session_grants`, applied in `turn_context._no_list_stage`) is a seam: no kind
  exists, so it is empty and being in a session raises nobody's access. A person in a session answers by their own line
  on the access list; they need `read_only` for read mode, `read_write` or `editor` for the whole agent.
* **What a Turn Context holds** (`turn_context.TurnContext`, always produced, even when refused): on a refusal from the
  rule `standing.permissions` is empty and role/source blank, but a stage 0 refusal keeps the whole standing, because who
  they are is still true. `basis` takes the form `capability:<name>` for a read-mode or host-site profile; the prefix is
  a wire value (the envelope's `granted_by`, `caller_tokens.is_caller_scoped` and licenses read it). `held_permissions`
  is what the asker holds for THIS turn (the envelope's `permissions`): a confined turn holds `read` and its profile's
  `use:<name>` only, even for an owner a sensitive conversation confined.
* **Profiles** (`turn_context.PROFILE_KINDS`, `Profile`): **only an explicit `kind == whole_agent` (or `no_agent`)
  means the whole of anything.** A missing profile, name or list never does; on every confined kind an absent list
  denies. `TurnContext.__post_init__` refuses to build otherwise (a profile exactly when not refused; `full` exactly
  for `whole_agent`/`no_agent`). `deny_all` fails closed: a stamp that no longer resolves to a published profile
  (`turn_context.stamped_profile`). Admins never edit a profile as such; they edit the **access list** (who) and **Read
  mode** and **Host sites** (what), which define the confined profiles.

### How it is resolved

The stages are in the module docstring of `turn_context.py`; the code is `_send_refusal` (0), `_no_list_stage` and
`_base_stage` (1 and base of 2), `_review_rule` and `_sensitive_rule` (the narrowing of 2), `_finish` (3). What the
code does not make obvious:

* **Stage 0 is for a preview only.** A real turn is past its entry point, and asking here would refuse turns no entry
  point refuses. A block is a refused Turn Context with its own code and changes nobody's access
  (`session_context._send_blocks`, each the same check the send route makes).
* **A named profile they cannot open, or one that is gone, is `refused`: no fall-through to read mode**
  (`_entry_for`). Naming none, a site's profile they cannot open is skipped, not fatal: someone with `read` on a host
  site gets read mode there.
* **The two narrowing rules only narrow.** A review chat gets `session_review`, `confined`, loses `auto` and pinning,
  owner included. Sensitive data confines a `full` base to the sensitive profile (`basis: sensitive:<name>`), keeping
  role, mode and pin rights; a turn in a sensitive conversation that ends anywhere but its sensitive profile is
  `refused`, a review chat included. A `read` holder in a non-sensitive conversation on a sensitive site keeps what
  the base gave them.
* **Turn mode is not decided here** (`apps/harness/turn_mode.py`): it needs the claim, so the resolver reads a claimed
  turn's stamp, asks afresh when given the runner, and otherwise reports `pending`.

### Two moments: enqueue and claim

A queued turn runs on the rules as they are when it starts. It is resolved at **enqueue**, which stamps
`Turn.capability` (`""` for the whole agent, else the profile's name) or refuses it, and keeps the question it asked
(`origin_ref.queued_for`, written only by the server: `harness.services.enqueue_turn` drops a caller's). That answer is
an early one: an immediate refusal, and the preview. At **claim**, `harness/claim.py` asks the same question again
(`turn_context.at_claim`, via `_resolve_now`), before the turn's mode and the admission rules, and runs on that answer:

* **Narrower now** (the conversation became sensitive, a line or role went, read mode changed): it runs in the narrower
  profile, or it is refused with the usual refusal, closed `cancelled` and never claimed (`claim._refuse_at_claim`),
  its note adding that it changed after it was sent.
* **Wider now** (a role was raised): it runs on the wider answer. A turn stamped confined is offered only to a box that
  can confine (`routing_orders.profile_q`).
* **The stamp follows.** `Turn.capability` is rewritten to the claim's answer (`claim._restamp`), so the runner session
  (`#<name>`, the `cx-` session), `profile_guard`, the caller token and a sensitive conversation's kept session
  (`runner/ec2/cloud_runner.py::_kept_identity`) all follow it: a whole-agent session never takes a confined person's
  turn, nor the reverse. A box that cannot confine the new answer passes it over and writes the new stamp, so every
  other box and `turn_reach` read what it would run as. **If the rule cannot be asked, the turn waits; it never runs on
  its old stamp.**
* A turn sent to an agent deleted while it waited is refused (`agent_gone`), never run as a turn with no agent. One that
  names no initiator at all was never asked about by any enqueue and keeps its stamp (`turn_context.at_claim`).

`Turn.resolved_context` records what it ran as, and `queued_as` the stamp it waited under when the two differ. Once
claimed, `of_turn` reads the stamp for the rest of the turn: a stamp that no longer resolves is `deny_all`, and the
asker's standing is still read live, under the stamp and, when that no longer admits them, as they stand now. **A turn
refused before it ran has no stamp it ran under, and an empty stamp reads as the whole agent, so nothing asks what
such a turn ran as without `turn_context.was_refused` first.** A **preview** resolves with no stamp and writes
nothing. **Admission rules** are a separate layer after the Turn Context (`apps/admission/evaluate.py::at_claim`,
[access-direct-use-and-admission.md](access-direct-use-and-admission.md)): the snapshot records their outcome beside
the Turn Context; a preview does not predict them, and says so.

### Who reads it (fan-out)

The caller envelope (`caller_context.build`) serialises the Turn Context plus what canopy knows about the person.
`access` (`full | confined | refused`) is its word; `profile` (`full | confined`) stays on the wire beside it for
runners and plugins that read it (`session_naming.is_confined`, the canopy plugin's hooks) and says `full` on a turn
refused before it ran; `capability`, `granted_by` and `source` keep their names. The runner reads the stamp and
`profile`: a confined turn runs in a `cx-` session keyed `<thread>#<name>`. Everything else (roster, topology,
`may_edit_agent`, licenses, `turn_mode`, `turn_gates`, `published_profiles.grant_facts`/`offered_to`, admission,
`answering_snapshot`) asks the rule, never a predicate of its own.

**Credentials.** `TurnContext.credentials` is a true statement of what the turn gets, by name and kind, never a value.
It adds no rule: it asks the functions that decide (`turn_agent_tokens.refusal`, `delegations.github_refusal`,
`caller_tokens.is_caller_scoped`, `published_profiles.page_turn`, `credentials/authority.py`), and anything only a box
can settle is `pending` (`apps/agents/credentials/for_turn.py`).

### What confinement keeps

The resolver has no separate "limited session" path, but the enforcement stays. These are requirements:

* **Fail closed.** `profile_guard` denies everything for a `cx-` session with a missing or malformed profile; a stamp
  that no longer resolves is `deny_all`; None never means the whole agent.
* **Separate conversations.** Turns under different profiles never share a runner session, or a read-only person could
  see the whole agent's history: `session_key_suffix` carries `<thread>#<name>` and is empty for `whole_agent` so live
  conversations don't split.
* **Credentials.** Only `whole_agent` gets delegated credentials and the agent's full login; a confined turn's
  environment never holds the agent's own values (`runner/ec2/cloud_runner.py::_confined_env`).
* **The runner's enforcement is not a second decision.** The `cx-` session name, `profile_guard`, `_confine` and
  `_confined_env` are how the runner holds `access == confined` → [access-confinement-and-act-as.md](access-confinement-and-act-as.md).

### Seeing it

The session panel (`frontend/src/components/chat/SessionInfoPanel.tsx`) shows both contexts and the trace from two
routes: `GET /api/canopy-sessions/{id}/context` and `…/turns/{turn_id}/context` (`apps/canopy_sessions/api_read.py`,
whose docstrings state the shapes). In a sensitive conversation only its own people, in canopy's chat or the site's
panel, get `turn_context` (the rest get `withheld`); a turn's view follows `turn_access.can_read_turn_log` and the same
narrowing. Neither ever holds the prompt, a transcript, the envelope's `person` block or a credential's value:
`apps/agents/context_views.py` is an allowlist of named fields, so nothing there can carry them.

## The rule (the decision table)

`turn_context.resolve_turn_context`, as a table of outcomes; the first row that matches decides:

| # | Who | Access | Mode | May request `auto` | May pin a runner |
|---|---|---|---|---|---|
| 1 | **owner / agent admin / system** (no line needed) | `full` | the routing ladder | owner / agent admin, on a verified credential | owner / agent admin: any runner that may hold the agent |
| 2 | their lines give them **`write`** (read_write, editor) | `full` | the routing ladder, but **every turn `manual`** when every line that gives `write` is `manual_only` | no | only a runner they administer (`access.may_pin_runner`; the Turn Context's own flag is for row 1) |
| 3 | their lines give them **`read`** or a **host site**, and a profile they open exists | `confined` to it | the routing ladder | no | — |
| 4 | anyone else (no line, or lines that open nothing the agent has) | `refused` | — | — | — |

`basis` is `owner`, `admin`, `system`, `session:<role>`, `grant:<line>` (row 2), `capability:<name>` (row 3) or
`refused` (row 4); `session:<role>` comes only from a session grant. The refusal says who can add the asker, and with
which role.

* **Over the table: the sensitive-data rule** (`turn_context._sensitive_rule`, `apps/agents/sensitive_data.py`). A host
  site that handles sensitive data (`data: sensitive`) is **confined for everyone**: whoever the table gave `full`
  runs that site's profile instead, keeping role, mode and pin rights. Any turn in a sensitive conversation must end
  in that profile; anything else is refused (`sensitive_not_open`). A runner in sensitive-data mode refuses any
  unconfined envelope (`runner/ec2/cloud_runner.py`, `sensitive_data_unconfined`). Saving the switch is audited like any
  setting change (`settings-audit.md`).
* **A one-session review's chat** runs in `session_review` for everyone not refused, owner included
  (`turn_context._review_rule`): no login, no Bash, no tool but `read_asked_session` → [access-sessions-and-people.md](access-sessions-and-people.md)
  § Asking a coach to review one session.
* **Row 2** may steer the whole agent. With `manual_only` on every line that gives them `write`, every turn they start
  runs `manual` and they may not set `auto` anywhere: not on a dispatch, a routing rule or actor route, or the agent's
  own switch. Granting a whole workspace `read_write` or `editor` through a default is the workspace owner's call
  because it hands every agent there to everyone in it.
* **Row 1's `system`** is bounded by the person canopy acted FOR: a schedule's occurrence carries its creator as the
  accountable person, and unless they are an agent admin or the agent's own login it runs `manual`
  (`turn_mode._accountable_cap`). With no accountable person recorded it keeps row 1's posture.
* **Pinning** is for an agent admin or a runner admin, and for the **agent's own login** on a box its admins already
  route it to (`apps/agents/access.py::may_pin_runner`, `_routed_to`). A pinned box must also be able to hold the agent
  at all (`apps/agents/holding.py::runner_may_hold_agent`), for a chat WITH the agent as much as for its own turns.
* **A project chat** (no agent, only a repo; profile `no_agent`) has no agent admins, so choosing its box is for a
  **runner admin** of that box only (`apps/canopy_sessions/placement.py::may_place`, the one placement rule). Unplaced,
  it runs on a box its workspace's runner order shares or one its sender administers (`runner-routing.md` → "Project
  chats"), never on a teammate's box by default. It acts as the person who asked ([access-confinement-and-act-as.md](access-confinement-and-act-as.md), "Projects run as the person who asked").

**Where it is asked:** `harness.services.enqueue_turn` (`_apply_capability`, which stamps the turn) for every entry
point (the harness API, a canopy-web chat send, Slack intake, email intake after sender triage: a blocked contact never
reaches the rule, and a member is recognised by mail only on THIS message's DMARC); the harness API also asks it up
front to answer 403 with the refusal; the claim (`at_claim`, then `of_turn` with the runner); the caller envelope; the
extended A2A card (`offered_to`); the access roster and the agent topology; the session panel's preview.
`tests/test_access_matrix.py` asks it through every entry point.

## Refusals

If someone can reach a session but the Turn Context would not let them take a turn, canopy says why and how to fix it.
Every refused Turn Context carries a `refusal`: `{code, why, how_to_fix, who_can_fix}`
(`apps/agents/refusals.py`). `code` is one of the closed set `refusals.Code`, stored on `Turn.resolved_context`, so a
value is never renamed; each code has exactly one builder in `_WORDS` (a test holds the two sets equal), so the words are
the same wherever they appear. The module docstring has the word rules. Invariants:

* **A builder is handed only the agent, the session and a few facts, never a prompt, a fact about a person or a
  secret.** `sender_blocked` never says why: that is an admin's note about a person, and it stays on the contact.
* A refused turn is written `cancelled` with `result_note` `Refusal.result_note` (`refusals.REFUSED_NOTE_PREFIX`;
  `turn_context.was_refused` reads it on turns older than the snapshot); an HTTP entry point's 403 or 422 says
  `Refusal.message`; Slack replies with `text`; the composer shows the reason and the fix above the input.
* A person added to a session who cannot take a turn is flagged on their participant row (`turn_refusal`,
  `apps/canopy_sessions/api_people.py`, the same refusal said `about` them), only to someone who may add people. Being
  in the session gives them nothing: an editor added to the owner's own session is flagged like anyone else.
* A verified automated sender refused with `not_on_access_list` is told an agent admin can bind it to an inbound-mail
  trigger ([access.md](access.md) § Triggers), so it is never cancelled at enqueue without anyone being told.
* A turn refused at its **claim** is said the same way plus `refusals.CHANGED_WHILE_QUEUED` on its note and in Slack; a
  sender whose Slack account is linked is told why privately (`slack/services.py::tell_refused_at_claim`). The
  workspace's `offered-nothing` admission rule fires for it as for a refusal at the enqueue
  (`apps/admission/evaluate.py::at_refusal`). A sensitive turn keeps canopy's own refusal note (`turn_close.close_turn`'s
  `canopys_note`) and loses its prompt.
* Refusals that do not depend on who is asking stay where they are and keep their own words: no runner available, an
  admission rule closing a turn, and the harness route's checks before a turn exists (`agent_for_work`, the email
  poster, a pin, `reads_for`, `turn_mode=auto`).
