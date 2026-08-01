# The blocked-agent dialog comes from the transcript

**Date:** 2026-07-31
**Status:** shipped, then **partly corrected 2026-08-01** — see the addendum at
the foot of this file. The transcript is NOT the primary producer and cannot be;
the hook is.
**Supersedes:** the "read it on demand" half of `a973f40` (#510), which was never built

## The incident

On 2026-07-31 `ace`'s `spark` session called `AskUserQuestion` at 03:53:46Z and
sat there until 04:45:35Z — **52 minutes** — while the phone showed a chat that
had simply stopped. Opening the session showed no dialog, no buttons, and no
indication anybody was being waited on.

## Why nothing reached the phone

Three independent gaps, all on the same path:

1. **Nothing ever read the dialog.** #495 wired a screen read onto the
   `Notification` hook. #510 removed it — correctly: reading a session's screen
   means driving emdash over CDP, and `openTask` CLICKS the task and focuses its
   terminal, so every agent that asked for input yanked emdash away from
   whatever was being typed. It closed with *"a menu can still be read on
   demand"* and *"a phone shows 'needs you' without buttons until the read is
   triggered deliberately."* **No on-demand trigger was ever added.** The only
   remaining caller of `read_hook_menu_from` is `execute._blocking_dialog_note`
   — a chat send that has already failed. So the only way to see a menu was to
   send a message into a blocked session and have it bounce.

2. **A menu could not survive being looked at later.** `session.activity` frames
   carry `index = -1` (view-only, never persisted) and the connect snapshot
   (`session_state_dto`) had no menu field. A menu therefore existed only inside
   the WebSocket of a client that was *already connected* when it fired — which
   is precisely the case that fails, because you go and look BECAUSE it stopped.

3. **"Blocked" itself did not reach an unattended session.** `hooks._hook_sessions`
   is rebuilt each tick solely from `sync_streams`, which the server filters to
   `stream_desired=True`. With no viewer attached, a hook resolves to `""` and is
   dropped as unknown-cwd — 3310 of 3336 on the box that day.

## The decision: derive it from the transcript, not the screen

The fleet's blocking dialog is **not a permission prompt**. Sessions run
`⏵⏵ bypass permissions on`, so what actually stops an agent is
`AskUserQuestion` — and that is a **tool call**. The question, every option and
every option's description are already in the transcript the runner tails.

That single fact removes every constraint the screen read imposed:

| | screen read (CDP) | transcript |
|---|---|---|
| steals emdash's focus | yes — the bug #510 fixed | no |
| costs | a CDP round trip | 0.31 ms (64 KB tail, measured) |
| works with no viewer attached | no | yes |
| recomputable later | no | yes — it is a file |
| sees permission prompts / trust gates | yes | no |

So the transcript becomes the primary producer, on the **session report** the
runner already sends every ≤10s for every open session. The screen reader
(`canopy_runner.menu`) stays for the dialogs a transcript genuinely cannot see.
Both emit the **same dict**, so no client learns which found it.

## Shape

```
runner tick (≤10s, every open session)
  transcript tail (64 KB)
    -> canopy_transcript.pending_question   # last unanswered AskUserQuestion
      -> session report `question`
        -> RunnerBinding.pending_question
          |- GET /api/canopy-sessions/            waiting_on_you: bool
          |- GET /api/canopy-sessions/{id}        menu: {...}
          |- ws session.state (connect snapshot)  menu: {...}
          '- ws session.menu (on the EDGE only)   menu: {...} | null
                -> MenuPrompt -> POST answer-menu -> runner.menu_answer
                     -> re-read the REAL screen -> send_keys
```

## Load-bearing details

**Numbering.** The answer is delivered as a keystroke, so a wrong number
silently presses the wrong option. Claude Code renders the declared options
first, numbered from 1 in declared order, then appends its own ("Type
something", "Chat about this") — verified against the live capture in
`canopy_runner/tests/test_menu.py::ASK_USER_QUESTION`. We derive numbers for the
declared options and **do not invent the appended ones**. Nothing rests on that
alone: `answer_menu_with` still re-reads the real screen and refuses any option
not on it, so a divergence costs a dropped tap, never a wrong keypress.

**`None` is a real answer and is written through.** The report is a fresh
observation, so "no dialog" has to be able to retire one — otherwise a menu
answered at the laptop keeps live buttons on every phone, and a tap types a
number at what is now an ordinary prompt.

**Uncapped, unlike the message tail.** `attach_recent_tail` reads the top
`session_tail_count` because only those are shown. A waiting session is the
mirror image: it stops writing the instant it asks, so it **sinks** in a list
ordered by activity — the longer somebody is kept waiting, the further down it
goes. A top-K bound would hide exactly what this exists to surface.

**A bare `blocked` frame no longer retracts the menu.** The hook path reports
`blocked` without one by design (#510). The reducer previously cleared the menu
on any activity frame that did not carry one, which would now erase every menu
the snapshot and the report supply. Only a **non-blocked** state retracts.

**`session.menu` is its own frame.** Overloading `session.activity` would mean
inventing an activity state to carry a menu, reporting an agent as idle or
blocked on this path's much slower clock. Activity answers "is it producing";
the hook path owns it.

**The composer refuses while a dialog is up.** The TUI draws the menu where the
composer would be — this is exactly the `COMPOSER_NOT_VISIBLE` the runner
already refuses to blind-send against. The send would bounce, and the answer the
agent wants is one tap away in the banner.

## What is deliberately NOT changed

**`_hook_sessions` still covers only attached sessions.** Widening it would ship
live tool events for every session on the box to serve one badge, and the
`blocked` signal it was needed for now arrives on the report instead. The hook
path stays what it is good at: sub-second "is it producing" for a chat you are
watching.

Related: on this machine something unidentified periodically rewrites
`~/.claude/settings.json`'s canopy hook to a dead ephemeral port (self-healed
each tick since #541). The transcript path is unaffected by that class of
failure entirely, which is a second reason not to build this on hooks.


---

## Addendum, 2026-08-01: the transcript is structurally blind to a live dialog

This spec's central claim — that an `AskUserQuestion` is a tool call, so "the
question, every option and every option's description are already sitting in the
transcript the runner tails anyway" — is true about the *content* and wrong
about the *timing*.

**Claude Code writes the `tool_use` record only once the ask is ANSWERED.**

Measured on a live box on 2026-08-01, across the 60 most recently touched
transcripts: **39** `AskUserQuestion` `tool_use` records, **every one of them
already answered, and not a single pending one** — while two sessions sat
blocked on dialogs plainly visible on their terminals, with no such record in
their files at all. Both files simply ended on a `tool_result`, minutes (in one
case 1h20m) before.

So `pending_question` can report a dialog that is over, and can never report one
that is waiting. That is the inverse of the job, and it failed *silently*: the
function returns `None`, which is a legitimate answer meaning "nothing is
blocked". Nothing distinguishes it from working correctly, which is why this
shipped, was believed, and left the same 52-minute symptom the spec was written
to end. The incident above recurred on 2026-08-01 — twice at once, 15 minutes
and 1h20m, on a phone showing nothing.

### What actually fixes it

`PreToolUse` fires when the call **starts**, and its `tool_input` is the same
object the transcript would eventually carry. So the parse, the dict and every
property this spec argued for survive intact — no CDP, no stolen focus,
computable for every open session on the ordinary report cadence — while the
signal arrives at the only moment it is useful. `canopy_transcript.menu_from_hook`
is that path; `hook_retires_menu` drops the menu on the answer, on `Stop`, and on
a new prompt.

Ordering is now: **hook** (live), **transcript** (a session whose hooks were
never installed), **screen** (what neither can see — permission prompts, trust
gates). The transcript reader is kept, not deleted: it costs nothing, and it is
correct the day Claude Code flushes the record eagerly.

### A second, independent bug on the same path

Even with a menu discovered, no keystroke could land on either stuck session.
`menu.find_menu` requires a footer offering a way out — but a footer needs a row
to be drawn on, and a tall dialog (long question, six options, a description
under each) overflows a short emdash pane, taking the footer with it. Both live
frames were 41 rows ending mid-dialog on `6. Chat about this`. `answer_menu_with`
re-reads the screen before pressing anything, so it refused every tap; the API
had already answered `ok:true`, so the phone reported success and nothing
happened.

Fixed with a second acceptance of equal strictness — the selection cursor on an
option **and** the dialog running to the bottom of the frame. See the "screen
parser" bullet in `CLAUDE.md`.
