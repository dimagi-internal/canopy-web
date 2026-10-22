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
  `(kind, subject)`, so four failed turns from one cause are one alert, with
  `occurrences` and `last_seen_at` bumped), `severity` (`blocking` | `action` |
  `setup`, ordered that way), `status` (`open` | `resolved` | `dismissed`),
  `workspace` (NOT NULL), `scopes` (`workspace:<slug>`, `runner:<pk>`,
  `agent:<slug>` — which screens it belongs on), `title`/`body` in the names
  people gave things (a token's label, never a slot name like `subscription-1`).
- `AlertEvent` — the audit trail, one row per change (`raised`, `changed`,
  `acted`, `notified`, `dismissed`, `undismissed`, `resolved`, `reopened`),
  written in the same transaction; never purged by retention.
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
| `turn_failure` | `apps/harness/alerts_turn_failure.py` (investigations `escalated`/`debugger_failed`/`held` only) | action | Failed turns |
| `unclaimable_turns` | `apps/harness/alerts_unclaimable.py` | blocking | Stuck turns |
| `device_key_pending` | `apps/harness/alerts_device_key.py` | blocking | Device key |
| `agent_cant_run` | `apps/harness/alerts_agent_routing.py` (configuration only: no routes, or no routed runner declares the repo — never liveness) | setup | Agent routing |

Actions: a source returns `Action(id, label, closes, href)`. An `href` action is
a link followed in the browser; the page also posts it to `POST
/api/me/alerts/{id}/act` (fire-and-forget), which only logs `acted` — every
action is audited, but a link runs nothing server-side. Others run through that
route. `dismiss` is the framework's shared dismiss
(`alerts.DISMISS`), which a source opts into.

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
  (`notified_at`), email + Web Push to the people holding a closing action.

## Read API

`GET /api/me/alerts/?scope=&status=open|dismissed` (user-level, once per
organisation), `POST /api/me/alerts/{id}/act` `{action}` (409
`action-not-offered`), `POST /api/me/alerts/{id}/undismiss`, `GET
/api/me/alerts/{id}/history`. MCP gets them as `list_alerts`, `act_on_alert`, …
→ [`api-surface.md`](api-surface.md).

## Screens

Every screen renders alerts with one component,
`frontend/src/components/alerts/AlertList.tsx` (API: `frontend/src/api/alerts.ts`),
and never decides what to show beyond naming a scope. One line per alert:
severity dot (blocking red, action orange, setup grey) · source chip · title
(and the last notify, when there is one) · its actions inline, each tagged
"closes" or "stays open" · × ("Dismiss for everyone"). It polls every 30 s.

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
- **Runner detail** (`/supervisor`, and Topology's runner panel):
  `runner:<pk>` above the health list, which stays the raw facts.
