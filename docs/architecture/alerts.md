# Alerts: system problems a person can fix

An **alert** is one record per problem in the fleet that a person can resolve —
a runner's Claude credential signed out, turns that failed and nobody could
handle, turns no runner can claim, a device key waiting for approval, an agent
with no runner it can use. Every screen, email, push and MCP client reads the
same record, so a problem never shows on one screen and not another.

Tasks, chats and access requests are not alerts: they stay attention rows on
Home (`apps/attention/services.py`) with their own per-person hide.

## The model (`apps/attention`)

Start at the `apps/attention/alerts.py` module docstring (the source contract) and
the `Alert` / `AlertEvent` docstrings in `models.py`.

- Identity is `(kind, subject_key)`, so four failed turns from one cause are one
  alert, not four. A reconcile that still reports it bumps `last_seen_at`;
  `occurrences` counts only `changed` and `reopened` (`alerts._upsert`).
- `workspace` is NOT NULL; `scopes` (`workspace:<slug>`, `runner:<pk>`,
  `agent:<slug>`) say which screens it belongs on. `title`/`body` use the names
  people gave things (a token's label), never a slot name like `subscription-1`.
- `handler_agent` / `handler_user` — who has it: an agent working it (shown,
  **silent**: nobody is notified), a person it was handed to (told once, on the
  hand-off), or neither (the people holding a closing action).
- `AlertEvent` is the audit trail, one row per change, written in the same
  transaction; it is never purged by retention (`models.py`).
- `alerts.py` is the framework half (the source registry `SOURCES`, dotted paths so
  the framework never imports a product app; reconcile, resolve, dismiss, `act`,
  `notify_users`, the `tick` on `apps/common/ticks.py`). One source's `reconcile`
  raising skips that source entirely, so a source we could not read resolves
  nothing as "gone" (`alerts.tick`).

## Sources

A source is a module in the app that owns the subject; the kinds are the `SOURCES`
list in `apps/attention/alerts.py`, and each module's docstring says what it
raises, its fingerprint (its "new fact" rule), its tenancy and its actions. Add a
kind by adding a module and a `SOURCES` entry; the registry test in
`tests/test_alerts_core.py` holds the contract.

- **Actions are the fixes, nothing else.** An action is `Action(id, label, closes,
  href)`. An `href` action is a link the browser follows; the server only logs
  `acted` (`alerts.act`). A link tagged "closes" means the destination is where the
  fix is made: the alert closes on the next reconcile, once the source no longer
  reports the condition. `act` asks the source what is offered before it takes the
  row lock and re-checks only the status under it; a handler that raises
  `ValueError` rolls back and the route answers 409 `action-failed`.
- **Dismiss is opt-in** per source (include `alerts.DISMISS` in `actions()`; grep it
  for the kinds that do). Kinds whose fix is not optional offer none.
- **`runner_credential` offers only the two actions that fix a broken credential**
  (replace it, or remove it when removing is allowed — never the runner's last token
  or API key), and no dismiss (`apps/harness/alerts/runner_credential.py`).
- **`turn_failure` lives in the investigation's workspace (the debugger's), not
  the failing agent's**: `auto_debug.can_handle`, the gate on every action, is asked
  about that workspace. The agent and runners are in its scopes. One alert per
  investigation from failure to fix; who holds it follows the investigation
  (`apps/harness/alerts/turn_failure.py` docstring, `apps/harness/auto_debug.py`,
  [`api-turns-and-routing.md`](api-turns-and-routing.md)).
- **A turn that cannot do its work is aborted, not finished.**
  `apps/harness/turn_abort.py` ends it FAILED with the reason, which enters the
  `turn_failure` lifecycle, and stops the box still running it. The canopy plugin's
  credential guard calls it when a tool fails on credentials; agents use
  `canopy turn abort`.
- **`unclaimable_turns`'s diagnosis is fleet-wide** and `actions()` runs per viewer
  per read, so it is memoised per process (`MEMO_TTL_S` in `unclaimable.py`);
  `perform` re-checks on a fresh read.

## Rules against a wasteland

Home must not become a list people learn to ignore. Each rule is code or a test:

- **Every kind offers someone a closing action** (`CLOSING_ACTION_IDS`
  non-empty; registry test in `tests/test_alerts_core.py`).
- **Self-healing.** A reconciled source resolves the alert the moment its
  condition clears; one not re-confirmed within `CONFIRM_WITHIN` is resolved as
  stale (`alerts._expire_stale`, `tests/test_alerts_tick.py`).
- **One alert per problem**, not per occurrence (identity `(kind, subject)`).
- **Raise bar.** Only what a person can resolve; transient, self-recovering
  conditions (a runner dark for an hour, a capped credential) stay facts on
  their own pages. Configuration is a raise condition, liveness is not.
- **Shared dismiss sticks** until the source's fingerprint for that subject
  changes (a new fact, not a repeat).
- **Visibility** = member of the alert workspace's organisation tree AND at
  least one action offered (`alerts_api.list_alerts`, `alerts.visible_actions`).
- **Notifications** (`alerts.notify_new_blocking`): only `blocking`, on raise and
  on reopen, once per alert (`notified_at`, claimed by a conditional update BEFORE
  sending, so overlapping ticks mail once; given back if every delivery fails),
  email + Web Push to the people holding a closing action — and never while an agent
  or a person holds it. Handing any alert to a person notifies that person once,
  whatever its severity (`alerts._assign`).

## Read API

`apps/attention/alerts_api.py` (`list_alerts`, `act_on_alert`, `undismiss_alert`,
`alert_history`) →
[`api-people-and-attention.md`](api-people-and-attention.md).

## Screens

Every screen renders alerts with one component,
`frontend/src/components/alerts/AlertList.tsx` (API: `frontend/src/api/alerts.ts`),
and never decides what to show beyond naming a scope. Its callers say where each
scope appears: Home (all), the Topology side panel (`workspace:<slug>`, which also
matches every workspace below it — the one place a runner or agent problem shows,
with no separate can't-run list), the agent page (`agent:<slug>`), the runner
detail (`runner:<pk>`) and the Supervisor Runners tab (every open alert you can
act on; its count is the menu badge). Home has no personal hide: dismiss is
shared ("Dismiss for everyone").
