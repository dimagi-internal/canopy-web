# Turn Context: one fan-in, one resolver, one fan-out

**Status:** design, requested by the owner on 2026-11-05 ("let's build this all out"). Follows
`2026-11-04-agent-settings-replace-the-interface-design.md`, whose P3 retired the interface routes.

## 1. In plain words

Canopy takes many facts (who asked, which agent, which channel, which site, whether the conversation is
sensitive) and works out what one turn may do. That work happens today, but it has no name and no single
home. It is spread over `access.decide`, `interface.profile`, `caller_context.build`, `turn_mode`,
`credentials.authority` and more, and its result has several names: Decision, stamp, profile, caller
envelope. Nobody can look at one thing and see what was resolved or why.

After this change:

```
FAN IN                                 RESOLVE            FAN OUT
Session Context ─┐                                        ┌─ the agent (caller envelope)
Asker           ─┼─→ resolve_turn_context() → Turn Context ┼─ the runner (session, confinement)
Agent settings  ─┘                                        ├─ tokens, credentials
                                                          ├─ the turn's refusal, the composer
                                                          └─ the session UI and IDE
```

Every entry point calls the one resolver. Everything downstream reads its result. A person can open a session
and see both contexts and the reason for each value.

**Why the owner wants it:** this is the thing that has been missing from the mental model. Showing both contexts
in the UI is also a test of the architecture: if the screen cannot be drawn from these two objects, the
design is not sound.

## 2. The words

| Word | Meaning |
|---|---|
| **Session Context** | The durable facts of a conversation, whoever is asking. |
| **Asker** | Who this turn is for, and whether this message proves it (`verified`). |
| **Standing** | What the asker holds on this agent, and why: permissions, role, source. |
| **Turn Context** | What one turn may do, resolved fresh for each message. Always produced, even when refused. |
| **Profile** | The complete definition of what a turn can do: tools, shell commands, read and write paths, how it starts. Every allowed turn has exactly one. |
| **Access** | `full`, `confined` or `refused`: how a profile is enforced. |

Admins never see "profile" as something to edit. They edit the **access list** (who), **Read mode** and
**Connected sites** (what), which define some profiles. In a UI, a profile reads as "what it may do".

**Profile kinds** (a closed set):

| Kind | Defined by | Access |
|---|---|---|
| `whole_agent` | The agent itself | `full` |
| `read_mode` | The agent's Read mode setting, else canopy's default | `confined` |
| `connected_site` | That row on the agent. A row with no site (`parse` still accepts one; production has none, per `shape`) keeps this kind with `site` null | `confined` |
| `session_review` | Canopy (review chats) | `confined` |
| `deny_all` | Canopy: a turn whose stored stamp no longer resolves to a published entry, or a sensitive turn stamped with read mode that has since gone (`turn_context.stamped_profile`), or a stamped turn whose agent has gone. Fail closed | `confined` |
| `no_agent` | A turn with no agent: a person's own repo or project session. Its enforcement and credentials are what they are today; this kind only names them | `full` |

There is no separate "limited session" path in resolution. A confined turn is an instance of the general result.

## 3. The objects

### 3.1 Session Context

A frozen read-model built from existing data by `session_context.build(session, turn=None)`. **No new table.** It does
not depend on the asker and writes nothing. The turn is an input because some facts only a turn holds: a sessionless
turn dispatched from a sensitive parent is sensitive through its ancestors (`sensitivity.py:79-91`), and its conversation
can be `canopy_session` rather than `chat_session`.

* `session_id`, `agent`, `workspace`, `channel`, `runner` requested and bound. `channel` is `turn.origin` for a turn, which
  is what turn mode reads. For a preview it is where the conversation lives, as `SessionInfoPanel` already derives it.
  It is NOT `Session.origin`, which only holds `web` or `runner`.
* `site`: the host site and page for this conversation, if any (`interface._conversation_site`, `_declared_resource`).
* `sensitive`: asked with the turn when there is one (`sensitivity.is_sensitive`), else `any_sensitive`, and the requirement it came from. Once true it does not revert.
* `send_blocks`: why this session takes no message from anyone or from some people, see 3.3 stage 0.
* `review_ask`: this is a review chat, and for which session.
* `audience`: the existing `apps/canopy_sessions/audience.py` output.
* `participants`: `(user, role)` rows.
* `session_grants`: a list, see 3.4.

A turn with no session (a bare API turn, a schedule's) gets a Session Context with `session_id=None` and the
turn's own channel and site, so the resolver has one input shape. A question asked with no turn at all (the
roster, topology, `may_edit_agent`, licenses) also goes through it, with an empty Session Context.

### 3.2 Turn Context

A frozen dataclass from `apps/agents/turn_context.py`:

* `standing`: `relationship`, `permissions`, `agent_role`, `source`, `via`. On a refusal from the rule these are what it
  always reported: `permissions` empty and `agent_role` and `source` blank, even for someone who holds `read` and named what
  it does not open (the `trace` says what they held). `via` is not cleared by a sensitive refusal. A stage 0 refusal keeps the
  whole standing: who they are is still true.
* `access`: `full | confined | refused`.
* `basis`: why, in one word (`owner | admin | system | session:<role> | grant:<line> | capability:<name> | sensitive:<name> | refused | no-interface`). The list above left it out, but `caller_tokens.is_caller_scoped`, `licenses`, and the envelope's `granted_by` read it.
* `profile`: a `Profile`, or `None` only when refused.
* `session_key_suffix`: `#<name>` for a confined profile, empty otherwise, see section 6.
* `credentials`: a true statement of what this turn gets, by name and kind, never a value (see stage 3).
* `review`: turn mode, its basis, and `manual_only`, `may_request_auto`, `may_pin_runner`.
* `stamped`: for a real turn, the profile name stored on `Turn.capability`, see "Two moments".
* `refusal`: `{code, why, how_to_fix, who_can_fix}`, or `None`. Non-null exactly when `access == refused`.
* `trace`: an ordered list of `{rule, outcome, detail}`, one per rule the resolver asked, ending at the deciding
  rule. This is the "why" the UI shows.

`Profile`: `{kind, name, site, description, entry, tools, bash, read_paths, write_paths, data, default_profile_version}`.
Its contents are read when asked, and so is `credentials`: canopy's default read profile queries the agent's skills, the roster
asks the rule once per member, and `credentials/for_turn.py` asks functions that ask the rule (`is_caller_scoped`).
For `whole_agent`, the allow-lists are absent, because nothing is enumerated: `kind` alone says it. An absent
list on any other kind means deny, as today.

### 3.3 The resolver

`resolve_turn_context(session_ctx, asker, agent, *, requested=None, turn=None, stamp=None, runner=None) -> TurnContext`

`stamp` and `runner` are the claim's inputs ("Two moments" below). `for_turn(turn, agent, requested)` and `for_asker(agent, who, *,
verified, requested, turn, standing, lines, channel)` are how an entry point asks it. `Asker` carries `standing` and `lines` when
the caller read them in bulk, so a view judging many pairs still reads once. `session_context.build(session, turn=None, *, agent=None)`:
the agent is who a session's grants are about, and a caller may ask about an agent the turn does not name.

Pure: **it must not write**. `caller_context.build` writes a `PersonAccess` row and creates a `Person`; those stay
in the envelope's `person` block, outside the resolver. A test wraps the resolver in a read-only guard.

**Stage 0, can this session take a message from them.** Blocks that sit outside the access rule today and already stop a
send: deleted content (`canopy_sessions/api.py:84`), `can_write` ("you can read this session but not act in it",
`canopy_sessions/api.py:86`), the CLI refusal and the sensitive not-in-it rule (`canopy_sessions/doors.py:97-106`, the module this
spec first placed in `harness`). The owner's principle is that reaching a session without being
able to take a turn must be explained, so these are `refused` Turn Contexts with their own codes, and the preview tells the
truth. **They change nobody's access:** the routes keep refusing as they do, using the same check. Skipped for a question
with no session, **and for a real turn**: it is already past its door, and these blocks are only asked at some doors. Asking them
at enqueue would newly refuse an email, a schedule or a dispatch into a session no door refuses it from today. So stage 0 runs
only for a preview. `can_write` is asked only of a signed-in person, since a contact's send has its own door. The equivalence test compares stages 1 and 2 against `decide`, and separately pins each block to its route.

**Stage 1, standing** (first match, same order as `_decide` + `_from_access_list` today):

1. Canopy itself, another agent, an approved dispatch: `system`.
2. `Agent.owner`, then the agent's own login (also `system`), then `Agent.is_admin`, in that order (`relationship_for_user`).
3. `session_grants` (3.4).
4. The access list (`grants.resolve`).
5. Otherwise, nothing.

**Stage 2, profile.** First a base, then two rules that only narrow, in this order, exactly as `decide` does:

| Base (first match) | Profile | Access |
|---|---|---|
| Owner, admin, system, a session grant, or holds `write` | `whole_agent` | `full` |
| Holds nothing | none | `refused` |
| Named a profile and their permissions open it | that `connected_site`, or `read_mode` if that is what was named | `confined` |
| Named one they can't open, or one retired | none. **No fall-through to read mode** | `refused` |
| Named none, and their page or site selects one their permissions open (page first) | that `connected_site` | `confined` |
| Named none, and holds `read` | `read_mode`. **A page's or site's entry they can't open is skipped, not fatal**: someone with `read` on a connected site gets read mode there, today and after this | `confined` |
| Otherwise | none | `refused` |

| Narrowing | Effect |
|---|---|
| **Review chat** (`_review_rule`) | Anyone not refused gets `session_review`, `confined` |
| **Sensitive** (`_sensitive_rule`) | A `full` base in a `data: sensitive` entry gets that entry, `confined`. A turn in a sensitive conversation that ends up anywhere but its sensitive entry is `refused`, with no profile, including a review chat. A read-holder in a NON-sensitive conversation on a sensitive page keeps what the base gave them |

This is today's behaviour, stated once. `decide` is the reference: any difference the equivalence test finds is a bug
in this table, not a reason to change behaviour. **This project changes no one's access.**

**Stage 3, fan-out values:** `review`, `session_key_suffix`, `credentials`. Turn mode's rules are not rewritten. Its evaluation
needs data only known at claim (the runner): the resolver takes that as an input, and when absent reports it `pending`.

`credentials` must tell the truth, and `credentials/authority.py` alone can't: it is a static table by kind. What a turn really
gets also depends on `turn_agent_tokens.refusal`, `delegations` (`reads_for` gets no GitHub identity), `caller_tokens.is_caller_scoped`
(an editor's whole-agent turn gets a read-only token that reads as the asker) and `own_login` withheld on site pages. So a small
`credentials/for_turn.py` asks those same functions and states the result. It adds no rule, and any answer needing the runner
is `pending`.

**Two moments, and the stamp wins.** A turn is resolved at **enqueue**, which stamps `Turn.capability` and may refuse. At
**claim** the resolver completes the Turn Context with the runner and final turn mode, and **does not re-decide the profile**:
the stamp stands, as today (`claim.py:500-514`, `interface.py:627-633`), so a turn queued under one access level runs as it was
admitted. A stamp that no longer resolves is `deny_all`. `who_is_asking` mid-turn stays live, as today.

A **preview** and the "today it would be" comparison call the resolver with no stamp and no turn writes.

**Admission rules stay a separate layer** that runs after the Turn Context (`admission/evaluate.at_claim`): they need the
runner, close the turn themselves, and aren't changed here. The snapshot records their outcome beside the Turn Context, so
"allowed, then rejected by rule X" is visible. A preview doesn't predict them, and says so.

### 3.4 Session grants: the seam for the future

The owner's direction: being added to a session *could* pass additional information that raises someone's access, but
it is not yet known how or if that will be used. The design must allow it without a redesign.

So `SessionContext.session_grants` is a list of `{kind, user_id, role, source}`, and stage 1 asks it in one rule.

**Exactly one kind exists today:** `session_writer`, the existing rule (`interface.session_writer`): a participant
with role owner or editor in a session created by the agent's owner or an admin holds the whole agent. It is read from a turn's
`chat_session` only, never `canopy_session`. `interface.session_writers(session, agent)` lists them, replacing the per-asker
question. **It stays, unchanged**, because this project changes no one's access. It becomes visible in the trace as a named rule.

`granted_by` is computed live and not stored, so Stage 1's report on how much this rule is used is an **approximation**: turns in
the last 90 days whose initiator was a non-admin participant (owner or editor role) in an admin-created session and who holds
no `write` line today. It's labelled as such. Stage 2's snapshot then records the real count going forward. Adding a grant
kind later means adding one builder function and one test, with no resolver change.

A turn with no agent has no grants and no standing to compute: `no_agent`, from the existing `_relationship_without_agent`.

## 4. Fan-out: who reads what

Every consumer moves off `Decision` and onto `TurnContext`, and `Decision` and `access.decide` are deleted in the same
pass, with no alias left behind. `apps/agents/access.py` keeps the rules, or they move, as the builder prefers. Consumers,
from `git grep decide\(` and `.capability`: `harness/services._apply_capability`, `harness/claim*`,
`harness/caller_tokens`, `harness/turn_agent_tokens`, `harness/turn_mode`, `harness/act_as`, `harness/owner_ask`,
`harness/routing_orders`, `harness/clis`, `agents/interface` (`grant_facts`, `offered_to`), `agents/topology`,
`agents/credentials/{licenses,delegations}`, `agents/agent_edit_check`, `agents/answering_snapshot`,
`admission/evaluate`, `host_grants/{host_gateway,execution}`, `slack/{services,status}`,
`contacts/session_review_asks`, `connections/api_turns`. The builder re-derives this list.

Stage 1's grep found that list too wide and too narrow. What called `decide`, `decide_for_turn` or read a `Decision`, and moved:
`agents/{access (the roster and may_edit_agent), agent_edit_check, answering_snapshot, credentials/licenses, interface (grant_facts,
offered_to), topology}`, `contacts/session_review_asks`, `dynamic_views/declared_tasks`, `harness/{caller_tokens, services, turn_gates,
turn_mode}`, `local_apps/{api, services}`, `mail/api`, `meetings/services`, `slack/services`. The rest of the original list
(`harness/claim*`, `act_as`, `owner_ask`, `routing_orders`, `clis`, `turn_agent_tokens`, `admission/evaluate`, `host_grants/*`,
`slack/status`, `connections/api_turns`, `credentials/delegations`) reads the stamp `Turn.capability`, not a `Decision`, and needed no
change. `admission/evaluate.decide` is a different function, of its own rules.

**`access.md` remains THE rule's documentation,** rewritten around this model in Stage 5.

**The caller envelope stays the wire format to the agent and runner.** It is the Turn Context's serialised
form plus what it adds about the person. `caller_context.build` reads from the Turn Context rather than recomputing.
An older runner keeps working: the existing fields `profile`, `capability`, `granted_by` and `source` keep their
values. New additive field: `access`.

## 5. Showing it: session UI and IDE

`frontend/src/components/chat/SessionInfoPanel.tsx` is already opened from both the chat's ⋯ menu and the IDE row menu
(`pages/ide/IdePage.tsx`), so **one component change reaches both**. Add two sections there.

**Session Context:** every field in 3.1, plain labels.

**Turn Context:**
* **"For you, now":** what would happen if you sent a message now, from a preview (below).
* **Per turn:** each turn in the existing turn list opens what it was resolved to, from the stored snapshot. If that
  differs from what it would resolve to now, say so: "this turn ran as X; today it would be Y". That is the debugging value.
* Every value sits beside its reason. The `trace` renders as a readable list, deciding rule highlighted.
* The existing "Raw" toggle shows and copies the JSON.

**Routes:**
* `GET /api/canopy-sessions/{id}/context` → `{session_context, turn_context}`, where `turn_context` is the preview for the requester. Anyone who can read the session. Read-only. An MCP tool, since agents debug too.
* The snapshot: a sibling route, `GET /api/canopy-sessions/{id}/turns/{turn_id}/context` → `{status, ran_as, now, differs}`. Not on `/info`'s turns: "what the rules say now" is a full resolve per turn, and `TurnOut` is served by every turn route, none of which asks this rule. A turn in the panel opens it when opened.

**Stored snapshot:** new nullable `Turn.resolved_context` JSONField, written at each claim (a lapsed lease requeues and
re-claims a turn, so it overwrites and holds the run that last happened), and at enqueue for a turn refused there. Holds what it
**ran as**; "what the rules say now" is computed on read. Additive, no backfill: older turns show "not recorded".

**What is never in these views or the snapshot:** the prompt or any transcript content; the envelope's `person`
block (what canopy knows about the person; it has its own access rules); credential values; token values. Tool lists,
paths and names are fine. They are configuration, already readable to members.

**Who sees it:** Session Context and your own preview follow `canopy_sessions.access.can_read`. A turn's snapshot follows
`turn_access.can_read_turn_log`, the rule for what holds no words, because `can_read_caller_context` is false for every
sensitive turn, for everyone. In a **sensitive** conversation the snapshot and preview go only to the conversation's own people,
with a test pinning that they hold no words and no reader outside the conversation gets them. (Builder call, recorded for the
owner: the tightest rule that still lets those people debug their own session.)

## 6. What confinement keeps

The resolver stops treating confined as a separate path. **The enforcement does not go away.** At the runner a whole-agent
turn has nothing to enumerate, and a confined one must be held to its allow-list. These protections are requirements:

* **Fail closed.** `profile_guard` denies everything for a `cx-` session with a missing or malformed profile. A stored
  stamp that no longer resolves is `deny_all`. `None` must never come to mean "whole agent": only an explicit
  `kind == whole_agent` does. A test pins it at resolver and envelope level.
* **Separate conversations.** Turns under different profiles do not share a runner session, or a read-only person
  could see the whole agent's history. `session_key_suffix` carries today's `<thread>#<name>`, and is empty for
  `whole_agent` so live conversations don't split.
* **Credentials.** Only `whole_agent` gets delegated credentials and the agent's full login.
* **The `cx-` session name, `profile_guard`, `_confine` and `_confined_env` stay** as they are. They are how the runner
  enforces `access == confined`, not a second decision. Changing them is out of scope.

## 7. Refusals

**Principle (owner):** if someone can reach a session but the Turn Context would not let them take a turn, canopy says
why and how to fix it.

* Every `refused` carries `{code, why, how_to_fix, who_can_fix}`. Codes are a closed enum in one place. `why` is plain words, and `who_can_fix` names real people or roles.
* `Turn.result_note` for a refused turn is built from it: "not run: <why>. <how_to_fix>".
* **Before they type:** the composer reads the preview. When refused, it shows the reason and the fix in place of (or above) the input, and stays readable.
* **Sent anyway** (email, Slack, a stale page): same text, delivered the way that channel delivers refusals today.
* The builder audits every path that refuses or cancels a turn before it runs, beyond `_apply_capability`: `turn_gates`, sensitive-runner routing, unverified email. Those that depend on who is asking move to `refusal`. Those that don't (no runner available, an admission rule per 3.3) stay where they are and are listed in the PR, with a note on what the user sees.

**A person added to a session who cannot take a turn:** whoever adds them sees "they can read this but can't take a
turn", with a way to add them to the access list if that person can edit it.

## 8. Build stages

Each stage is one or more PRs, deployable alone. **The gate for every stage:** `manage.py answering_snapshot` hash unchanged
on a fixture and, after deploy, in production; `tests/test_access_matrix.py` green; no change in who gets what.

The snapshot hashes `dataclasses.asdict(Decision)`, so when `Decision` goes, `answering_snapshot.py` keeps a small explicit
projection of the Turn Context to that same shape (`answer_of`: `access: "none"`, `capability`, `reason`, `basis`, `stamp`), so the hash
means the same thing. A roster row keeps saying `none` too: the access API, the vault and the frontend read that word. It is the
one other place, and a Turn Context's own word is `refused`. Trellis's live edit and #2559 already moved the baseline: Stage 1 takes a new production baseline **before** it deploys.
**Stage 4 is the one named re-baseline:** refusal wording changes `reason`, and that PR shows the diff is only `reason`.

| Stage | Work | Check |
|---|---|---|
| 1 | Types, `session_context.build`, `resolve_turn_context`, trace. Move every consumer. Delete `Decision`/`decide`. Report the `session:*` count. | Equivalence over the whole access matrix between old and new, run before old is deleted. Read-only guard on the resolver. |
| 2 | `Turn.resolved_context`, the context route and MCP tool, envelope reads from Turn Context, additive `access`. | Snapshot never holds `person`, prompt, or a value: seeded-secret test. |
| 3 | The two sections in `SessionInfoPanel`. | **Rendered in a browser, in chat and IDE**, for: owner whole-agent, read-mode, connected-site, sensitive, refused. |
| 4 | Refusal structure, audit, composer preview, the add-participant notice. | Each code has a test. Rendered in a browser. |
| 5 | Vocabulary (section 9). | Guard test: the banned words don't appear in UI strings, or in new API field names. |

## 9. Vocabulary: door, ask, capability

**The owner wants `door` and `ask` gone, after the core design.**

* **Gone from:** every UI string, doc, API field, tool description, CLI output, code comment and identifier the builder can rename safely. Includes `doors`/`door_sites` on the access list API (replaced by `connected_sites`), a line's `capability` field (→ `connected_site`), and "Read mode and doors". In the canopy plugin and agent repos too. Note the code has modules with these words, e.g. `apps/harness/doors.py`.
* **`capability`, agent sense only.** Leased **machine capabilities** (`apps/capability*`, `acquire_capability_lease`) are a different concept and stay.
* **What stays, because it is wire or stored:** the stamp value `ask` on existing turns and agents, `Turn.capability`, `AgentGrant.capability`, `AdmissionRule.capability`, `use:<name>` in stored role permissions, and `data: sensitive` in the document. Renaming them would split live conversations (`session_naming.restricted_key`, `cloud_runner._kept_identity`). They are confined to storage and the wire, behind one named constant. New read-mode entries are still written under their existing name.
* **The envelope field `profile: full|confined`** stays on the wire beside `access` because deployed runners and plugins read it (`session_naming.is_confined`, `plugins/canopy/hooks`). Removing it is a later cleanup once every runner has moved. The word `profile` then means only section 2's meaning.

## 10. Non-goals

* No change to anyone's access. Not to site narrowing: **someone with `write` on a non-sensitive site keeps the whole agent, as today** (builder call recorded for the owner; the question is open and can be changed later by moving one rule).
* No change to `session_writer`'s behaviour (3.4).
* No new tables. No moving `Agent.interface`'s storage.
* No removal of `pages`, the site-ceiling union, or `interface_source` (separate, already planned).
* No change to turn-mode rules, admission rules or `credentials.authority` logic, only where their results are read.
* No change to `profile_guard`, `_confine`, or the `cx-` naming.

## 11. Left to the builder

Module layout, naming below the public objects, how `Decision` consumers migrate in order, whether the context route's
snapshot is inline, and the exact UI layout within the existing panel's style.

## 12. What building Stage 1 corrected

The code was the reference, and these are the differences it found. Each is folded into the section named.

| Section | The spec said | The code does |
|---|---|---|
| 3.3, stage 2 | A selected profile they can't open is refused, with no fall-through | Selected entries they can't open are skipped: `read` on a connected site gets read mode. Only a *named* one refuses |
| 3.3, stage 0 | Skipped only for a question with no session | Also skipped for a real turn, or it would refuse turns no door refuses today |
| 3.3, stage 0 | `harness/doors.py` | `canopy_sessions/doors.py`, and the CLI check comes before the sensitive one |
| 3.3, stage 1 | System first, then owner and admin, with the agent's own login under system | For a person: owner, then own login, then admin |
| 3.2 | No `basis` on the Turn Context | It needs one: `granted_by`, `licenses` and `is_caller_scoped` read it |
| 3.2 | `standing` is what the asker holds | On a refusal from the rule it is blank, as `Decision` was |
| 3.3 | Signature with `requested` and `turn` only | Also `stamp` and `runner`, the claim's inputs |
| 3.4 | `session_writer` is asked of a turn | It is a list per session now, from `chat_session` only |
| 4 | The consumer list | See the corrected list in section 4 |
| 8 | Only the snapshot keeps `none` | A roster row does as well |

### What building Stage 2 corrected

Stage 1's fan-out was tested and unread. These are what real callers found.

| Section | The spec or Stage 1 said | What it has to be |
|---|---|---|
| 3.3, "Two moments" | The stamp decides, and that is all a claim adds | The stamped answer lost what the rule itself said. The envelope's `permissions` need it: someone no longer admitted holds none whatever the stamp, and someone stamped whole-agent and now only reading holds `read`. So the result keeps the rule's answer beside the stamp (`held_permissions`) |
| 3.3 | A refused turn is a Turn Context with no profile | A written turn refused at enqueue has an empty stamp, and an empty stamp reads as the whole agent. Nothing may ask what such a turn ran as without `was_refused` first. The envelope's `profile` stays `full` for it, for older runners; `access` says `refused` |
| 3.1, 6 | A turn with no agent is `no_agent`, `full` | `Session.agent` is SET_NULL, so a turn stamped confined can lose its agent. It is `deny_all`, never `no_agent` |
| 3.3, stage 3 | `credentials` is true at claim | On a turn with no agent it left the agent's vault `pending`, in a claim and a preview, though there is no agent's vault to get. It says `no` now, as it does for the login and the GitHub identity |
| 3.1 | `runner` requested and bound | A preview read neither: the requested runner is `metadata.requested_runner_id` |
| 3.2 | `standing` | A preview of a repo chat said `contact` to its owner. It now asks the session ACL, as a turn does |
| 5, "Who sees it" | Session Context follows `can_read` | Whole, except `page`: a sensitive conversation's page is its own people's (`sensitivity.NOT_IN_IT_PAGE`), so another reader gets it blank |
| 5, "Who sees it" | "The conversation's own people" | Builder call: its people on its chat surfaces, as that page's rule says. A person's own PAT or an MCP call does not get a sensitive turn's context, which holds no words but is still read by an AI through those doors. To open it to them, drop `request_is_chat_surface` from `_is_its_own_person` |
| 5, stored snapshot | Written at each claim and when refused at enqueue | Also when a direct session opens, which is never claimed, and when an admission rule closes the turn (3.3 asks for that), at enqueue or claim. A blocked sender's mail is refused before the rule, so it has its own code, `sender_blocked`, and never stores the reason |
| 5 | Older turns show "not recorded" | So do turns canopy never ran: a closeout report (`agent_report`), and a queued turn. A snapshot that could not be built is `unavailable`, and overwrites the last run's |
| 4 | The envelope reads the Turn Context | Its `turn_mode` and `authority` still read `turn_mode.for_turn`: a queued turn's Turn Context says `pending`, and the envelope has always resolved it live |

### What building Stage 3 corrected

The panel was drawn from the two routes and nothing else. These are what drawing it found.

| Section | The spec said | What it is |
|---|---|---|
| 3.2, 5 | The `trace` ends at the deciding rule | The narrowing rules (review chat, sensitive) always run after it and usually change nothing. The panel highlights the last step that changed the answer; for a refusal, the send block whose code is the refusal's, else the step that said no |
| 3.2 | `basis` says why | At claim every confined turn's `basis` is `capability:<stamp>`, an owner's sensitive turn too, so it does not mean "from the access list". The panel reads it beside `standing.source` |
| 3.1, 5 | Session Context, every field | `participants`, `audience` and `session_grants` hold user ids only. The panel shows counts and "you", and the ids stay under Raw. Showing names needs a field on the route |
| 5, "Who sees it" | An outsider to a sensitive conversation is told it is withheld | In chat they never reach the panel: the page stops at "Private conversation" first. Only the IDE's row menu opens it for them, and it says withheld there |
| 9 | No `door`, `ask` or `capability` in UI text | The panel's own words have none. Text the server writes still does, shown as it is: a refusal's and a credential line's `why`, and a read-mode profile's `description`. Stages 4 and 5 |

### What building Stage 4 corrected

Every refusal now says why, how to fix it and who can, from one closed set of codes with one builder each
(`apps/agents/refusals.py`). These are what writing those words found.

| Section | The spec said | What it is |
|---|---|---|
| 7 | `why` says why | `why` is plain words starting lower-case, to follow "not run: ". A door's 403 says `message` (why, capitalised, then the fix), a turn's note `result_note`, Slack `text`. So a stale page, Slack and the composer say the same words |
| 7 | Refusals the rule makes | A sender whose address a line admits only from proven mail, writing unproven mail, was told "not on the access list". It now has its own code, `sender_not_proven`, read by asking the list again as if proven. Nobody's outcome changes |
| 7 | `result_note` from the refusal | A blocked sender's note used to carry why they were blocked, an admin's note about a person. It no longer does, as the snapshot already did not |
| 7, stage 0 | Stage 0's blocks keep their door's words | The send doors now say the refusal's `message`. Content deleted is byte-identical; "can read but not act" gains its fix and the session owner; a dropped CLI says ". Start a new conversation…" where it said ": start a new conversation" |
| 7 | Whoever adds someone sees the notice | The same refusal said `about` them, on each participant row, only to someone who may add people (`turn_refusal`), and `access_list_agent` only when the fix is a line and they may add one. An editor added to a session the agent's owner or an admin started is a session writer and is never flagged; a viewer is told "can read but not act", whose fix is their role |
| 8 | The equivalence test compares `reason` | It compares outcomes: a reason exactly when refused, never its words. `test_answering_settings_equivalence` against `legacy_interface` likewise |
| 3.3 | Turn gates that depend on who is asking move to `refusal` | `check_access` says the rule's refusal. The harness route's other gates (`agent_for_work`, email poster, pin, `reads_for`, `turn_mode=auto`) refuse a request before a turn exists, not a turn in a session someone reached; they keep their messages. Listed in the Stage 4 PR |

### What building Stage 5 corrected

The words went, and these are what removing them found.

| Section | The spec said | What it is |
|---|---|---|
| 9 | Agent `ask` skills are renamed | Canopy starts an agent's read mode by the entry stored on the agent, and production stores `/ace:ask`, `/hal:ask`, `/eva:ask` and `/echo:ask`. A migration (`agents/0131`) moves an entry to `/<slug>:answer-caller`, only for an agent whose catalog has that skill. The agent repos delete `commands/ask.md` only after it deploys: draft PRs until then |
| 9 | What stays: the stamp, the stored fields, `use:<name>`, `data: sensitive` | Also stored: the read-mode scratch folder `.<slug>-ask/` (production `write_paths` and `bash`), the document's `capabilities` key, and `interface_source`. Also on the wire: the A2A card's `capabilities` (the protocol's), a skill's `id` (`ask`), and two deployed fields agents read through MCP, the roster's and the topology's `capabilities` (and the topology `basis` value `capabilities`). Renaming those two is a separate change |
| 9 | One named constant per stored value | `interface.ASK` and `interface_default.ASK_MODE` were two constants for one value. Now `READ_MODE_STAMP`, only |
| 9 | `ask` goes | Only as read mode's name. "An ask" meaning a request (a task's, a review's, an investigation's, act-as) is a different noun, with deployed fields (`ask_kind`, `ask`), and stays |
| 9 | `capability` goes, except leased machine capabilities | Also not the agent sense: a runner's capabilities, a storyboard link's, a workspace app action's, and the plugin catalog at `/system` (whose screen now says "plugin catalog") |
| 3.1 | `review_ask` | `review_of`: the session a review chat reviews |
| 3.3 | `canopy_sessions/doors.py` | `canopy_sessions/send_paths.py` (`SEND_PATHS`) |
| 8 | Wording outside refusals leaves the snapshot hash alone | The default read profile's description is in the snapshot, and so are its version and the A2A card's label for read mode. Rewording them changes 137 leaves, none of them an access, profile, reason, basis or stamp |
| 9 | Guard: no banned word in UI strings | Two guards: `tests/test_turn_context_words.py` (every server string that is prose, the OpenAPI text, and no new field name) and `frontend/src/turnContextWords.test.ts` (JSX text, attributes and prose literals). The verb "ask" passes; read mode's name does not |
