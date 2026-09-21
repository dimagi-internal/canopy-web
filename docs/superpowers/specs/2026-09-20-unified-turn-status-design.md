# Where the ask stands: one turn status, every channel

**Date:** 2026-09-20
**Status:** shipped
**Touches:** `apps/harness/turn_status.py`, `apps/canopy_sessions/status_feed.py`,
`apps/slack/status.py`, `frontend/packages/canopy-ui/src/chat/turnStatus.ts`,
`frontend/src/embed/EmbedApp.tsx`

## The report

> "I go to the agent history to ask about a specific skill, it triggers the
> session correctly. However, the session widget doesn't have the same UI
> feedback as normal canopy-web to tell it's working / processing."

Correct, and the cause was not the widget's chat component. The widget mounts
the **same `ChatPanel`** from `canopy-ui/chat`, over the **same `ag-ui`
protocol**, as `/w/:ws/chat/:id`. What differed was the seams the host wired:
canopy's chat page wires six, the widget wired none.

## Two problems, and only one of them was the widget's

### 1. The widget did not use the kit it was built on

| Seam | ChatPage | Widget (before) | What the person saw |
|---|---|---|---|
| `renderMarkdown` | `Markdown` | — | replies as literal `**bold**`, `- `, unrendered fences |
| `awaitingReply` | `socket.awaitingReply` | broken, two ways | no "Queued…" / "Thinking…" |
| `banner` → `MenuPrompt` | yes | — | **silent and dead exactly when the agent asks a question** |
| `banner` → `PlacementBanner` | yes | — | never learns its runner is offline |
| `disabledReason` | yes | — | sends that bounce as `COMPOSER_NOT_VISIBLE` |
| `emptyState` | yes | — | — |

`awaitingReply` was broken differently on each path, and both reduce to *the
flag is set by `sendChat()` and nothing else*:

* **Member, first message.** `startConversation` sends over **REST** (the
  session does not exist until it is sent, so there is no socket yet), then
  `EmbedChat` mounts **fresh** with `awaitingReply=false`. The user's own
  question lived in `EmbedStart`'s textarea, which just unmounted, and is not a
  server row until the agent's transcript ships it back. **You typed, pressed
  send, and watched your question disappear into an empty box.**
* **Contact, every message.** `awaitingReply={sending}`, cleared in the POST's
  `.finally()` — it tracked the **HTTP request**, not the turn. An indicator
  for ~200ms, then silence for the entire real wait.

The `MenuPrompt` gap was the worst of them, because the panel was behaving
*correctly*: `activity` flips to `blocked`, `agentHasFloor` goes false, the
working bubble is withdrawn — and nothing took its place. The one state with a
button to press rendered as a finished conversation.

### 2. "What is happening with my ask" was implemented three times

This is the architectural half, and it long predates the widget.

* **Slack** (`apps/slack/status.py`, shipped days earlier) answered it
  properly: server-side, durable (`SlackTurnPost`), self-healing (`sweep()` on
  other runners' reports), distinguishing eight states — including the three no
  client can derive for itself: *its runner is offline*, *no runner is set up
  for this agent*, *its runner died mid-turn*.
* **The web** answered it with one client-side boolean plus `state.activity`,
  which can say Queued or Thinking and nothing else, and does not survive a
  reload.
* **The widget** did not answer it at all.

Slack's own docstring — *"The failure this exists for is silence"* — is a
statement about **turns**, not about Slack.

## The shape

The primitive was already in the right tier: `harness.turn_reach(turn)` returns
`Reach(LIVE|OFFLINE|UNROUTED, runners)` and accounts for tenancy and
`RunnerAssignment` coverage. Slack was simply its only consumer. So this is not
a new subsystem — it is **moving a derivation one module left and giving it a
second and third renderer**.

```
        harness.turn_reach ─┐
        Turn.status ────────┼──► harness.turn_status.derive() ──► TurnStatus
        runner heartbeat ───┘                                        │
                                          ┌──────────────────────────┼─────────────────────┐
                                          ▼                          ▼                     ▼
                              slack/status.render()      canopy_sessions/status_feed   (next channel)
                              mrkdwn + a button          → session socket frame
                                                         → connect snapshot
                                                         → REST detail
                                                              │
                                                              ▼
                                                    canopy-ui/chat/turnStatus.ts
                                                    ├─ ChatPage  (free)
                                                    └─ widget    (free)
```

**Eleven states**, because they are the ones a *person* distinguishes. "Queued"
is three of them — `picking_up`, `waiting_runner`, `unrouted` — since the wait
looks identical in all three but what you should do about it does not: wait, go
open your laptop, or go fix the agent's routing.

Two derived booleans travel with it so four clients cannot disagree about them:
`settled` (nothing more will happen) and `stuck` (nothing is moving and only a
person can change that). Slack's native three-level indicator collapses from
`stuck`/`pending` rather than re-deriving; the chat kit uses the same two.

### Decisions worth keeping

**Not a model or a column.** The answer is a function of rows that already
exist and change on their own clock. Persisting it would create a second source
of truth that goes stale exactly when the runner dies — the one case it has to
get right.

**The frame carries the whole status, never a delta.** It is small, it is
re-derived on every transition anyway, and a delta needs the client to hold a
correct prior — which a client that just connected does not have.

**In the connect snapshot, not only in live frames.** Same lesson
`session.menu` learned: you open the page *because* it went quiet, so the
client that most needs the status is the one that was not connected when it
changed.

**An explicit AG-UI passthrough.** `agui.project` returns `[]` for an
unrecognised frame — deliberately, so a new canopy event cannot take down a
third-party stream. That means a new frame is dropped **silently**, and both
consumers here are on `ag-ui`, so forgetting it would have disabled the status
on the chat page and the widget at once. This is the page-action bug of
2026-09-18 exactly; `test_the_frame_reaches_an_ag_ui_client` pins it.

**Enqueue needed its own signal.** Claim/run/done/fail already append a
`status` TurnEvent and ride `turn_events_appended`; enqueue writes no event and
is the single most important moment to report — the one where somebody has just
pressed send and is looking at the screen. Hence `turn_status_changed`, fired
post-commit from `enqueue_turn`, so all five send paths (chat socket, REST,
contact, Slack, email) are covered at one site rather than five that rot.

**The kit's notice yields to a host banner.** canopy's chat page raises
`PlacementBanner` (wait / continue elsewhere / resume) for an offline runner
and `MenuPrompt` for a dialog — richer, actionable answers to the same
question. Rendering the kit's sentence underneath would state the fact twice
and put a dead-end restatement directly below the thing you can press. So
`turnNotice` is suppressed whenever the host supplies a banner; it is the
fallback for hosts that have none, which is exactly the widget.

**`noteLocalSend` rather than a new reducer case.** A REST send needs to show
the line and start waiting. It routes through the ordinary `chat.user_message`
case so the optimistic row gets the **existing** dedupe: when the agent reads
the message and the runner ships it back at its durable composite ordinal, the
reducer matches on recent identical text and merges instead of double-rendering
(the failure of 2026-08-27).

## What was deliberately NOT changed

* **`PlacementBanner` and `runnerEligibility.ts` stay.** They answer a
  different question — *this session is stuck on a box that is offline; do you
  want to continue elsewhere, losing the conversation's context?* — which is
  session stickiness plus an interactive choice, and the no-auto-failover
  policy behind it is deliberate (spec 2026-07-24). Its by-name fleet match was
  already fixed to treat a loaded-but-missing runner as evidence rather than
  absence; it is not fail-open today.
* ~~**Slack still posts from `services.py` on enqueue** rather than subscribing
  to `turn_status_changed`. It works, and the duplication is a trigger, not
  logic.~~ **Wrong, and fixed 2026-09-21** — see "Addendum" below.
* **Contacts get no `MenuPrompt`.** `/api/contact/` is their entire surface and
  `answer-menu` is not on it (`test_contact_surface_is_bounded`), so a dialog
  they cannot answer is shown as words rather than as buttons that would 403.

## Verification

* `tests/test_turn_status.py` — all eleven states, pure, no fleet needed.
* `tests/test_session_turn_status_feed.py` — the chain: enqueue → frame →
  snapshot → AG-UI survival.
* `tests/test_slack.py` — **unchanged, 113 passing**, which is what proves the
  extraction from Slack was faithful rather than approximately faithful.
* `turnStatus.test.ts`, `ChatPanel.pending.test.tsx` — the kit's wording and the
  spinner-vs-statement decision.
* `EmbedApp.test.tsx` — the three widget feedback tests were **confirmed to fail**
  with the echo removed, then restored. A green test nobody has seen fail proves
  nothing.

## Addendum (2026-09-21): the trigger was not harmless

The original of this spec left Slack posting its first line from an explicit
call in `services._send`, and called that duplication harmless: "a trigger,
not logic". It hid a real gap.

**The case.** Someone continues a Slack-born conversation from canopy-web or a
phone, and the agent's runner is offline. Slack posted a line for a turn that
did not come from Slack only in `on_status`, which fires when a `status` event
lands — i.e. when a runner claims the turn. An offline runner never claims, so
no event lands, and `sweep` only refreshes lines that already exist. The thread
heard nothing: not the question, not that it was stuck. The chat page, driven
by the enqueue signal, said "queued, runner offline". The two channels
disagreed in precisely the case the status line exists for.

**Why the explicit call existed.** A slash command posts an anchor message, and
the status line ADOPTS it rather than posting a second one. The ts to adopt
lived only on the in-memory `Inbound`, so only `_send` could post a Slack turn's
line — and `on_status` had to skip Slack turns, or it would win the race, claim
the `SlackTurnPost` row without the anchor, and post a duplicate.

**The fix: make the anchor a fact about the turn.** `_send` writes it into
`origin_ref["slack"]` via a new `send_message(origin_ref=...)` passthrough;
`status.adoption(turn)` reads it back; `post(turn)` takes no anchor arguments.
With the anchor on the turn, ANY path may post first, so:

* Slack subscribes to `turn_status_changed`, exactly as the chat feed does;
* `on_status` is origin-agnostic;
* both explicit `status.post` calls are gone (`_send`, and the re-ask in
  `route_to_cloud`, which enqueues through `send_message` and so fires the
  signal itself).

Slack and the chat page now learn about an ask from the same three events:
enqueue (`turn_status_changed`), transition (a `status` row), and a runner
dying (`sessions_reported` → the throttled sweep).

`send_message` merges the caller's keys UNDER the harness's own
(`thread_key`, `chat_session_id`), so a channel can never overwrite what a
turn is routed by.

**Test fidelity.** Every Slack request in `test_slack.py` goes through one
helper, which now executes post-commit callbacks, matching production (no
`ATOMIC_REQUESTS`, so the callback runs inside the request that enqueued).
Disconnecting the receiver fails 18 existing tests; not writing the anchor
fails the pre-existing "keeps the ask on every later edit" test. The gap itself
is `test_continuing_from_the_web_while_the_runner_is_offline_still_tells_the_thread`,
confirmed to fail on the old wiring.
