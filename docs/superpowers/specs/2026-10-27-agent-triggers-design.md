# Agent triggers — design

**Date:** 2026-10-27
**Status:** Approved (Jonathan, 2026-10-27: "Yep do it all … triggers as first class thing")
**Tier:** Framework (`apps/harness`), retiring `apps/workspaces` system accounts

## Intent

An inbound alarm is not a member of a workspace. It is a **trigger**: something
that starts a turn on one agent, with the authority of the person who set it up.

Until now an automated sender (AWS CloudWatch alarm mail to Hal) was modelled as a
**system account**: a fake `User` with a `.invalid` email, a workspace membership
capped at editor, and `SystemSender` bindings. It showed up as a "person" in member
lists and the member-tree grid, needed a guard on every credential door (allauth,
PATs, OAuth, delegated tokens, passkeys, vault keys, Slack audiences, people
coverage, analytics…), and attached its standing to the workspace instead of to the
agent its mail wakes.

## Goals

- One **Triggers** concept on an agent, shown on one tab and served by one API:
  schedules are triggers of kind `cron`; alarm mail is a trigger of kind
  `inbound_mail`.
- A trigger's authority is its creator's on that agent — never more — and a trigger
  whose creator has left or lost the right stops firing **loudly** (an alert).
- Fired turns are ordinary harness `Turn`s: origin, `origin_ref` naming the trigger,
  idempotency per slot or per message.
- Inbound mail keeps every trust check it has today (this message's own DMARC /
  domain-DKIM alignment on our receiver's verdict; subject pattern; one match only).
- System accounts are gone: models, users, memberships, routes, MCP tools, UI, docs,
  credential-door guards that existed only for them.

## Non-goals

- New trigger kinds (webhooks, Slack events). `kind` leaves room; nothing else does.
- Changing how a schedule's cron is evaluated, superseded, released or nagged.
- Changing the email channel for anyone who is not a trigger's bound sender.

## Decisions (Jonathan's)

1. Triggers are first-class and agent-owned; they extend the existing schedule
   concept rather than a parallel system. Fields: agent, name, kind
   (`cron` | `inbound_mail`), prompt, turn mode, routing / cli / notify,
   `created_by` / `updated_by`, enabled. An `inbound_mail` trigger carries the
   sender binding: address, optional subject pattern, and the alignment requirement.
2. Authority comes from the person who set it up. Changing a trigger is gated (below);
   its turn runs with no more authority than its creator has on the agent; a creator
   who leaves or loses the right stops it firing, loudly.
3. Aligned mail from a trigger's bound sender to the agent's mailbox fires the
   trigger instead of resolving to a system-account user.
4. The agent page shows Triggers with create / edit / pause / delete; the
   system-accounts section and its member rows go.
5. System accounts are retired completely, failing loudly; a data migration converts
   each one into equivalent `inbound_mail` triggers, failing loudly when it cannot
   tell which agent the mail woke.

## My calls (Claude's), and why

**C1 — Generalize `AgentSchedule`, keep its name.** A trigger is an `AgentSchedule`
row with a `kind`. A sibling model would have made "one Triggers list" a union of two
tables with colliding ids, and duplicated created_by/updated_by, enabled, notify,
routing, cli, turn-mode and the authority rule. Renaming the model (and table) would
have churned ~40 files, the runner protocol and the fleet's MCP tools
(`create_schedule`, `list_schedules`, …) for a word. So: the concept, the UI and the
new API say *trigger*; the model keeps its historical name, and its docstring says so.

**C2 — `/agents/{slug}/triggers/` is the agent's one list; `/schedules/` stays as the
cron subset.** `/triggers/` lists and edits every kind. The `/schedules/` routes, the
runner's `/api/harness/schedules/` sync + fire, and the week calendar serve
`kind=cron` only — they are how runners and existing skills drive cron, not a
compatibility layer for a retired feature. Both call the same service functions.

**C3 — The gate follows what the trigger opens.**
- `cron`: `agent.work` (editor), as schedules required before. A cron trigger only
  times its creator's own request, and its turns are already bounded by the creator
  (an editor's run manual).
- `inbound_mail`: **agent admin** (`Agent.is_admin`). It admits an outside sender to
  make the agent work — the same tier as a `SenderTrust` exception and the agent's
  interface. (System accounts needed workspace admin; agent admin is the narrower,
  agent-owned equivalent.)
- `turn_mode = auto` on any trigger: agent admin, as for a dispatch that asks for auto.

**C4 — Authority is checked at every fire and by an alert source.** `triggers.refusal`
says why a trigger may not fire: no recorded creator, a deactivated creator, a creator
no longer a member of the agent's workspace, or one who lost the trigger's gate (agent
admin for `inbound_mail`; `agent.work` for `cron`; agent admin for an `auto` trigger).
The agent's own login stands for itself. A refused cron slot is consumed (not retried
every tick) and recorded; refused mail falls back to being the contact it is. The alert
source `trigger_stopped` raises one alert per stopped trigger on the agent's Triggers
tab, resolved when the creator's right returns or the trigger is paused or deleted.
Editing does not move a trigger's authority to the editor (`created_by` stays); to
take one over, re-create it.

A cron row created before attribution existed (`created_by` NULL) keeps firing as it
always has — canopy's own turn, bounded by nobody: stopping every pre-attribution
schedule (four of them live on Ada's and Eva's agents alone) would have been a
regression, not a safety gain. An `inbound_mail` trigger always has a creator (the
migration records one) and is shut without one.

**C5 — A trigger turn is a SYSTEM turn accountable to its creator.** The initiator is
`system`, `via = schedule:<id>` (cron, unchanged) or `trigger:<id>` (mail), its
accountable user the creator — the shape a schedule already used, so `access.decide`
and `turn_mode._accountable_cap` bound it by the creator with no new rung. The email
contact rides along on a mail trigger turn. `initiator.describe` and the caller
envelope carry `trigger: {id, name, kind, set_up_by}`, so the agent reads "CloudWatch
alarm trigger, set up by ada@", not a person.

**C6 — Trigger turn mode.** `turn_mode` "" lets the agent's routing decide (bounded by
the creator, as before); `manual` forces manual; `auto` runs auto only while the
creator is an agent admin (re-checked at every claim, like a dispatch's request).
Mail turns become session turns, so this is a rung in `turn_mode._own_posture`, read
from `origin_ref.trigger_id` / `schedule_id`, not `requested_turn_mode` (agent turns
only).

**C7 — Mail keeps its own prompt and routing.** An `inbound_mail` trigger's prompt is
optional; when set it leads the mail turn's own prompt. The turn keeps `origin=email`
(routing rules key on it) and the message's own idempotency key, and gains
`origin_ref.trigger_id` / `trigger_name`. A member's own mail still wins over a
trigger; two triggers on one agent matching one message is ambiguous and fires
neither, as before.

**C8 — The migration.** For each `SystemAccount`:
- agents = every agent whose turns it initiated (`Turn.agent`, or the session's agent).
  None → the migration raises, naming the account. Several → one trigger per agent:
  that is where its mail actually routed.
- one trigger per (agent, sender): name = the account's name (suffixed by the sender
  when there are several), `kind=inbound_mail`, address + subject pattern, prompt "",
  `turn_mode = manual` (a system account was at most an editor, whose turns ran manual
  — so this preserves today's posture exactly), enabled unless the account was disabled,
  `created_by` = the account's creator, else the workspace's first owner.
- its past turns are rewritten to the trigger's shape (system initiator, `trigger:<id>`,
  accountable creator, `origin_ref.trigger_id`), so they stay attributed once the user
  is deleted; then the users (and with them memberships, accounts, senders) are deleted
  and the two models dropped.

Live: `AWS CloudWatch alarms` (user 41, workspace `connect`, created by ada@) → one
trigger on **hal**, sender `no-reply@sns.amazonaws.com`, subject `^(ALARM|OK): "`.

**C9 — Guards.** Guards that existed only for system accounts go (allauth `pre_login`
refusal, the PAT / OAuth / delegated-token filters, vault and passkey "not a person"
legs, Slack audience, people coverage, member kind `system`, analytics
`actor_kind=system` classification). Guards that also protect agent logins stay, with
the system-account leg removed. `Activity.SYSTEM` stays as a stored value for history.

## Surfaces

- API: `GET|POST /api/agents/{slug}/triggers/`, `PATCH|DELETE
  /api/agents/{slug}/triggers/{id}`, `POST /api/agents/{slug}/triggers/{id}/run-now`
  (cron only). `TriggerOut` adds `kind`, `sender_address`, `subject_pattern`,
  `turn_mode`, `stopped` (why it may not fire, or null). MCP tools follow the routes.
- Removed: `/api/workspaces/{slug}/system-accounts/…` and their MCP tools; `system` on
  member rows.
- UI: the agent's **Triggers** tab (`/w/:ws/agents/:slug/triggers`) replaces its
  Schedules tab; one table of every trigger, an editor with a kind switch. The members
  page loses its System accounts section.

## Risks

- A runner that syncs schedules only ever sees `kind=cron` rows — inbound rows have no
  cron to evaluate.
- The migration fails the deploy if a system account's agent cannot be determined —
  intended: it names the row instead of guessing.
