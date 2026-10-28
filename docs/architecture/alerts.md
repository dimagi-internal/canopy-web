# Alerts: system problems a person can fix

An **alert** is one record per problem in the fleet that a person can resolve —
a runner's Claude credential signed out, turns that failed and nobody could
handle, turns no runner can claim, a device key waiting for approval, an agent
with no runner it can use. Every screen, email, push and MCP client reads the
same record, so a problem never shows on one screen and not another.

Tasks, chats and access requests are not alerts: they stay attention rows on
Home (`apps/attention/services.py`) with their own per-person hide.

## The model (`apps/attention`)

- `Alert` (`apps/attention/models.py`): `kind`, `subject_key` (identity is
  `(kind, subject)`, so four failed turns from one cause are one alert, not
  four). Every reconcile that still reports it bumps `last_seen_at`;
  `occurrences` counts only `changed` and `reopened` (its fingerprint moved, or
  the condition came back after resolving) — a plain repeat bumps nothing else.
  `severity` (`blocking` | `action` |
  `setup`, ordered that way), `status` (`open` | `resolved` | `dismissed`),
  `workspace` (NOT NULL), `scopes` (`workspace:<slug>`, `runner:<pk>`,
  `agent:<slug>` — which screens it belongs on), `title`/`body` in the names
  people gave things (a token's label, never a slot name like `subscription-1`).
  `handler_agent` / `handler_user` — who has it: an agent working it (shown,
  **silent**: nobody is notified), a person it was handed to (told once, on the
  hand-off), or neither (the people holding a closing action). A source sets
  them on its `Raise`; `alerts.raise_now` raises one immediately instead of on
  the tick.
- `AlertEvent` — the audit trail, one row per change (`raised`, `changed`,
  `acted`, `notified`, `dismissed`, `undismissed`, `resolved`, `reopened`,
  `assigned` — the handler changed, with the ask), written in the same
  transaction; never purged by retention. A `resolved` event carries the
  source's account of the fix when it gives one.
- `apps/attention/alerts.py` — the framework half: the source registry
  (`SOURCES`, dotted paths, so the framework never imports a product app),
  reconcile, resolve, dismiss/undismiss, `act` (re-checks the action is still
  offered), `notify_users`, and the 60-second `tick` (`apps/common/ticks.py`).

## Sources

A source is a module in the app that owns the subject; it declares `KIND`,
`SEVERITY`, `CONFIRM_WITHIN`, `CLOSING_ACTION_IDS`, and implements
`reconcile(now)` (the complete set of currently-true conditions), `actions(alert,
user)` (what this viewer may do — no actions, no alert) and `perform`.

| Kind | Source | Severity | Chip |
|---|---|---|---|
| `runner_credential` | `apps/harness/alerts_runner_credential.py` | blocking | Runner credential |
| `turn_failure` | `apps/harness/alerts_turn_failure.py` (every unresolved investigation, raised the moment the failure is recorded; held by the debugger agent until handed to a person; lives in the investigation's — the debugger's — workspace, see below) | action | Failed turns |
| `unclaimable_turns` | `apps/harness/alerts_unclaimable.py` | blocking | Stuck turns |
| `device_key_pending` | `apps/harness/alerts_device_key.py` | blocking | Device key |
| `runner_code` | `apps/harness/alerts_runner_code.py` (a heartbeating runner on a non-`main` branch, or still older than what shipped `OUTDATED_GRACE` (2 h) after the deploy — its updater's window; a quiet box and one ahead of the deploy raise nothing) | action | Runner code |
| `runner_refresh` | `apps/harness/alerts_runner_refresh.py` (a Refresh still pending 30 min after it was asked; handed to the person who asked, so they are told once; says what it waits on — offline, turns in flight, or the box's floor between self-restarts; resolves when the box reports the bootstrap) | action | Runner refresh |
| `canopy_projects_off` | `apps/harness/alerts_project_opt_in.py` (one per runner owner whose live runners hold repos while they opted into none, `ProjectOptIn`; only that owner sees it) | setup | Canopy projects |
| `transfer_left_open` | `apps/canopy_sessions/alerts_handoff.py` (a transferred session's task still open on the box it left: its close keeps failing, or it was kept because it holds the only copy of the work; 14 days) | action | Transfers |
| `agent_token_expiring` | `apps/agents/alerts_agent_token.py` (an agent's newest stored canopy token expires within 14 days and nothing rotates it — automatic rotation off, typically a token kept in 1Password; says how to re-mint it; only the agent's admins see it) | action | Agent login |
| `trigger_stopped` | `apps/harness/alerts_trigger.py` (one per enabled agent trigger whose creator may no longer set it up — left the workspace, deactivated, or lost agent admin / `agent.work` — so it no longer fires, `triggers.refusal`; seen by whoever may set such a trigger up on the agent) | action | Triggers |
| `agent_cant_run` | `apps/harness/alerts_agent_routing.py` (configuration only, never liveness; default routes only — reasons: no routes at all, the routed runners lack its repo, their owners may not hold its turns (`cannot_hold`), every routed runner is directed-only, or none of its own listed runners would take its turns by the setup checklist's `runner_takers` — another tenant's box, or one whose owner may not hold it) | setup | Agent routing |

Actions: a source returns `Action(id, label, closes, href)`. An `href` action is
a link followed in the browser; the page also posts it to `POST
/api/me/alerts/{id}/act` (fire-and-forget), which only logs `acted` — every
action is audited, but a link runs nothing server-side. Others run through that
route. `dismiss` is the framework's shared dismiss
(`alerts.DISMISS`), which a source opts into. A link action tagged "closes"
means the destination page is where the fix is made; following the link closes
nothing by itself — the alert closes on the next reconcile, once the source no
longer reports the condition. `act` asks the source what is offered before it
takes the row lock (a source's `actions()` may be slow) and re-checks only the
status under it; a handler that refuses (`perform` raises `ValueError`) rolls
back and the route answers 409 `action-failed` with the reason.

Per-source notes:

- **Fingerprints** are each source's "new fact" rule. `turn_failure`: the
  investigation's status and who has it (a repeat of the same failure is not a
  new fact).
  `unclaimable_turns`: the sorted distinct reasons the turns are stuck (the
  cause — not the turn ids, so a turn cancelled or another stuck for the same
  reason leaves a dismissed alert dismissed). `agent_cant_run`: the computed
  reason. `runner_credential`: the slot and its probe detail.
- **Actions are the fixes, nothing else.** Each alert
  offers only the buttons that fix it, and a fix is marked "closes" even when
  it is a link (the alert closes when the next reconcile sees it fixed):
  `turn_failure` See failed turns · Mark resolved; `unclaimable_turns` Fix
  routing · Cancel turns; `device_key_pending` Approve key · Reject key;
  `runner_code` Open runner (the fix is the command in its body, run on that
  machine); `agent_cant_run` Fix routing (· Dismiss, the one kind that keeps it — an
  agent nobody means to run yet); `canopy_projects_off` Choose repos (·
  Dismiss, for a person who runs only agents and means canopy to be off in
  their repos); `trigger_stopped` Open triggers (restore the creator's access, or
  re-create it as yourself); `transfer_left_open` Retry close / Close it anyway (· Dismiss,
  for a task someone already dealt with by hand); `runner_refresh` Open runner (·
  Dismiss, for a box someone means to leave as it is). No other kind offers Dismiss.
- **`runner_credential` actions** are the only two that fix a broken
  credential: **Replace token** (a link to the runner's credentials; "Sign in
  again" for a login) and **Remove token**, offered only when removing is
  allowed — never the runner's last token or API key. No dismiss. Title:
  `The "hal" token on cloud-ec2-2 no longer works`.
- **`turn_failure` tenancy.** The alert lives in the investigation's
  workspace (the debugger's), not the failing agent's — a deliberate deviation
  from the spec's "subject's workspace": `auto_debug.can_handle`, the gate on
  every action, is asked about that workspace. The agent and runners are in
  its scopes. `mark_resolved` refuses once the investigation is resolved.
- **`turn_failure` lifecycle.** One alert per investigation, from failure to
  fix: the debugger's (silent) while it works it, `pending_deploy` (still the
  debugger's, quiet) while a fix commit is not yet on every runner it hit, the
  handed-to person's after `hand_off` or a recurrence where the fix runs.
  Details in `apps/harness/auto_debug.py` and [`api-surface.md`](api-surface.md).
- **A turn that cannot do its work is aborted, not finished.** `POST
  /api/harness/turns/{id}/abort` (`apps/harness/turn_abort.py`) ends it FAILED
  with the reason, which enters this same lifecycle, and stops the box still
  running it. The canopy plugin's credential guard calls it when a tool fails on
  credentials; agents call it with `canopy turn abort`.
- **`unclaimable_turns` cost.** Its diagnosis is fleet-wide, and `actions()`
  runs per viewer per read, so it is memoised per process for 30 s (the
  reconcile refreshes it; `perform` re-checks on a fresh read). Agent editors
  keep "Fix routing" and dismiss after it resolves, so its history stays
  readable.

## Rules against a wasteland

Home must not become a list people learn to ignore. Each rule is code or a test:

- **Every kind offers someone a closing action** (`CLOSING_ACTION_IDS`
  non-empty; registry test in `tests/test_alerts_core.py`).
- **Self-healing.** A reconciled source resolves the alert the moment its
  condition clears; one not re-confirmed within `CONFIRM_WITHIN` is resolved as
  stale (`tests/test_alerts_tick.py`).
- **One alert per problem**, not per occurrence (identity `(kind, subject)`).
- **Raise bar.** Only what a person can resolve; transient, self-recovering
  conditions (a runner dark for an hour, a capped credential) stay facts on
  their own pages.
- **Shared dismiss sticks** until the source's fingerprint for that subject
  changes (a new fact, not a repeat).
- **Visibility** = member of the alert workspace's organisation tree AND at
  least one action offered.
- **Notifications**: only `blocking`, on raise and on reopen, once per alert
  (`notified_at`, claimed by a conditional update BEFORE sending, so
  overlapping ticks mail once; given back if every delivery fails), email + Web
  Push to the people holding a closing action — and never while an agent or a
  person holds it. Handing any alert to a person notifies that person once,
  whatever its severity.

## Read API

`GET /api/me/alerts/?scope=&status=open|dismissed` (user-level, once per
organisation), `POST /api/me/alerts/{id}/act` `{action}` (409
`action-not-offered`, or `action-failed` when the handler refused), `POST /api/me/alerts/{id}/undismiss`, `GET
/api/me/alerts/{id}/history`. MCP gets them as `list_alerts`, `act_on_alert`, …
→ [`api-surface.md`](api-surface.md).

## Screens

Every screen renders alerts with one component,
`frontend/src/components/alerts/AlertList.tsx` (API: `frontend/src/api/alerts.ts`),
and never decides what to show beyond naming a scope. One line per alert:
severity dot (blocking red, action orange, setup grey) · source chip · title ·
how long ago it was first seen (and `N×` when it recurred) · its actions inline,
each tagged "closes" or "stays open" · × ("Dismiss for everyone"). Clicking the
title expands the alert in place: first/last seen, count, last notify, handler,
the body (its first paragraph is the diagnosis; the rest shows as the raw error,
folded when long) and its history (`/history`, read on first open). It polls
every 30 s.

- **Home** (`/home`): its own section at the top, above sessions — severity
  count chips that filter it, no "quiet" fold, no personal hide, and a collapsed
  "Dismissed (n)" fold with who dismissed each and when, and Undo. Absent when
  nothing is open or dismissed.
- **Topology** (`/w/:ws/settings/topology`): the side panel's "Alerts here"
  (`workspace:<slug>`, which also matches every workspace below it) replaces the old can't-run list, badge and "lacks its
  repo" line; "held to some capabilities" and "runners serving nobody" stay as
  plain facts.
- **Agent page** (`/w/:ws/agents/:slug`): `agent:<slug>` above every section,
  nothing when empty.
- **Supervisor Runners tab** (`/supervisor?tab=runners`): every open alert you
  can act on, below the fleet hold; its count (plus a fleet hold) is the menu
  badge and the feed's one line. The queued-turns list below it is facts, not
  an alert (`QueuedTurns`, with per-turn Cancel).
- **Runner detail** (`/supervisor`, and Topology's runner panel):
  `runner:<pk>` above the health list, which stays the raw facts (a quiet
  box's last-reported code and branch included).
