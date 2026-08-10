# "Clicking does nothing": the blocked-agent dialog, end to end

**Date:** 2026-08-01
**Status:** shipped — historical record of an incident, plus the invariants it left behind
**PRs:** #560, #561, #562, #564, #565, #566, #568, #569, #572, #575, #577
**Supersedes the "primary producer" claim in:** `2026-07-31-blocked-agent-dialog-from-the-transcript-design.md`

## The report

> "It's stuck on a menu and I can't unstick it, clicking on a menu option doesn't
> fire. Also, this might be related — my phone knows something is pending but
> also doesn't show any one session as pending."

Two sessions were sitting on `AskUserQuestion` dialogs, one for 15 minutes and
one for 1h20m, with the phone showing nothing.

## Why it took eleven PRs

It was not one bug. It was a chain in which **every layer failed in a way that
was indistinguishable from success** — which is why it survived four separate
fixes, each of which shipped green.

| # | Failure | Why it looked fine |
|---|---|---|
| 1 | The transcript cannot see a live dialog | Claude Code writes the `AskUserQuestion` `tool_use` record only once the ask is **answered**. Measured: 39 records across 60 transcripts, every one already answered, **zero pending**, while two dialogs were plainly on screen. `pending_question` returns `None` — which also means "nothing is blocked". |
| 2 | A tall dialog has no footer | `find_menu` required a footer offering a way out; a long question with six described options overflows a short emdash pane and the footer is the line that falls off. Both live frames were 41 rows ending mid-dialog. |
| 3 | The hook map was scoped to watched sessions | `_hook_sessions` is rebuilt from `sync_streams`, i.e. sessions a **viewer is attached to** — so a menu was captured exactly when somebody already had the chat open. Measured right after deploy: 5539 hook events received, 36 forwarded. |
| 4 | A refusal was silent | The API returns `sent` the instant it relays; the runner's refusals were logged and dropped. A correct refusal and a working press were identical from a thumb. |
| 5 | A restart *retired* menus | The hook fires once. Held only in memory, a restart made the next report ship `question: null` — which **clears** the menu server-side, with nothing able to rediscover it. |
| 6 | A menu outlived its session | Persisting fixed 5 and created this: a session whose emdash task was deleted still served six buttons. Both halves needed fixing — the runner prunes its copy, and the server clears `pending_question` for bindings absent from the wholesale report. |
| 7 | An idle prompt counted as blocked | `Notification` fires both mid-turn *and* when a prompt merely sits idle. Counting the second put four idle sessions on the fleet's waiting list within minutes. |
| 8 | A notification marker could never die | It asserts "a turn was in flight", and turn state is deliberately not persisted — so a restored one was a claim nothing could justify and nothing could retire. |
| 9 | **The answer vanished with the control channel** | The root cause. See below. |

## The root cause (#575)

`answer-menu` published a `runner.menu_answer` WS frame and returned `"sent"`.
When that channel is down the frame lands in a Channels group with **no
consumer** and is discarded — while the runner keeps heartbeating over REST, so
it reads `ONLINE`, `is_reachable` is true, and the API answers `ok:true`.

Nothing on the runner runs. So there is no keystroke — **and no refusal either**,
because no code executed to produce one. Every fix above made refusals visible;
this failure has no refusal to make visible.

```
wake listener connected  10:16:17
  … answer sent 10:50 — no keystroke, no refusal, no log line …
wake listener connected  10:58:49
```

`2026-07-31-blocked-agent-dialog-from-the-transcript-design.md` had hypothesised
exactly this window and could not confirm it. It was right.

The same hole existed for `close_session` (#577), found the same way.

## Invariants this left behind

These are the parts that matter going forward; they live in `CLAUDE.md` in short
form and are argued for here.

1. **One authority, and a tap reconciles to it.** The dialog is drawn on a
   terminal; the runner's memory, its disk store, `RunnerBinding.pending_question`
   and the client are all copies. At the tap: *could we read the screen?* → the
   screen wins (clear if nothing, **replace** if a different dialog); *could we
   not?* → keep the cache and say why. Never keep a cache the authority has just
   contradicted.
2. **A control frame is a doorbell, never a mechanism.** Anything that must
   happen gets a durable record the runner drains on its poll tick —
   `pending_answer`, `close_requested` — beside `/streams` and `/backfills`. A
   dropped channel then costs one tick, not the action. Same shape as inbound
   mail and runner updates.
3. **Every menu is stamped `observed_at`**, so staleness is visible rather than
   discovered by tapping.
4. **A refusal is a value, not a log line**, and only ever rides a menu we are
   *keeping*.
5. Some refusals are **irreducible**: the dialog is on a laptop and you are on a
   phone, so it can be answered at the keyboard while your thumb travels.
   Re-reading before pressing is what stops a stale number landing in a prompt,
   where a bare "2" is read as an instruction.

## The testing lesson

Every one of these passed its unit tests. Six were found only by driving the real
deployment, which is why `scripts/e2e_session_chat.py` exists.

Two traps it walked into itself, both worth knowing:

- **Vacuous green.** Steps asserted against the runner's synthetic *tail*
  (negative `turn_index`, `tail:` pks) rather than durable rows. The tail does not
  page and a reset has nothing to drop, so `scroll_back` and
  `rebuild_from_transcript` passed while testing nothing. A later version
  reported *"dropped 8 rows; 0 came back"* and still passed, because "no row
  predates the reset" is trivially true on an empty set.
- **One green proves nothing.** `answer_from_the_web` passed three times before
  failing — the control channel simply happened to be up. Run it twice.

## Two bugs found alongside, unrelated to menus

- **#574** — agent sessions never had a transcript at all: both descriptors sent
  a blank `project`, so the runner could not resolve one and skipped them
  silently, forever. This is the *"983 of 6119 rows (16%), 8 sessions at exactly
  zero"* that `list_streams` already recorded.
- **#577** — a failed turn wrote `~/.canopy/not-ready`, which latched **forever**:
  `mark_ok` only runs after a turn executes, and routing will not give a
  not-ready runner a turn. One transient failure exiled a box from the fleet
  permanently, escapable only by deleting a file by hand, with no signal.
