# Blocked agents: how a dialog reaches a phone and an answer gets back

When an agent stops and waits on a human (an `AskUserQuestion`, a permission prompt, a trust gate), canopy turns that dialog into a **menu** a phone, the chat page or Slack can show and answer. This doc covers where the menu comes from, how it is stored and served, how a tap becomes a keystroke, and the fail-closed rules at each step. The full evidence chain behind these rules is `docs/superpowers/specs/2026-08-01-clicking-does-nothing-incident.md`; the transcript-reader design is `docs/superpowers/specs/2026-07-31-blocked-agent-dialog-from-the-transcript-design.md`.

## What you will actually meet

Fleet sessions run with permissions bypassed (`⏵⏵ bypass permissions on` in emdash; the cloud runner passes `--dangerously-skip-permissions` to `claude -p` and puts every ACP session in `bypassPermissions` before its prompt — `ACP_PERMISSION_MODE`, sent by `canopy_acp.drive_turn`'s `mode` as `session/set_mode`, since the adapter starts each session in `default`, `runner/ec2/cloud_runner.py`), so permission dialogs basically never appear. The menu that matters is **`AskUserQuestion`**: part of the conversation, independent of permission mode, and shaped differently from a permission prompt (a description line under each option, a box rule *between* options, no `?` in the question, plus two options Claude Code appends itself). Test against that shape, not against a permission prompt.

## One payload, several producers

Every producer emits the **same dict**; `source` says which one made it, and every one is stamped with `observed_at` (`packages/canopy_transcript/canopy_transcript/questions.py`) so staleness is visible rather than discovered by tapping. The client never learns which producer it came from.

In precedence order:

1. **The hook — primary.** `PreToolUse` carries the `AskUserQuestion` `tool_input` the moment the ask starts; `canopy_transcript.menu_from_hook` turns it into a menu (`source: "hook"`). The transcript cannot be primary: Claude Code writes the `tool_use` record only once the ask is ANSWERED, so a transcript reader sees dialogs that are over and never one that is waiting — and fails silently, since `None` also means "nothing is blocked".
2. **The transcript — second.** `canopy_transcript.pending_question` (runner: `transcript.pending_question_for`) wins only where hooks were never installed.
3. **The screen — what neither can see** (permission prompts, trust gates). On the laptop such a dialog exists only on emdash's terminal, so the runner reads the rendered screen over CDP and parses it (`runner/canopy_runner/canopy_runner/menu.py`). On the cloud, ACP's `session/request_permission` carries the whole dialog and `runner/canopy_acp/canopy_acp/menus.py` maps it onto the same dict; `runner/canopy_acp/tests/test_menus.py` asserts the two are interchangeable, so the screen parser is deletable once emdash exposes its own ACP stream.
   **A cloud chat's ask** arrives over ACP whole: AskUserQuestion as an `elicitation/create` form (`canopy_acp.menus.menu_from_elicitation` turns it back into the tool input and parses it with the hook's own parser, `source: "acp"`), and a bypass-immune permission as `session/request_permission`. claude-agent-acp turns AskUserQuestion ON only when the client advertises `elicitation.form`, so `AcpAgent` advertises it only when given `ask_human` — which the cloud runner does for a chat turn alone (`run_acp` → `_ask_a_person`); a turn nobody watches keeps the tool off and the allow-first policy. The ask waits off the reader thread, is posted with `POST /runners/{id}/session-menu`, and is answered from `/menu-answers` (the frame only rings the bell; a waiting ask also polls), retired with its outcome, then cleared. A stop ends the wait; an ask that cannot be shown is answered unattended. A finished or lease-swept chat turn clears an `acp` menu its box left (`harness.services.end_turns_ask`). Tests: `runner/ec2/tests/test_acp_asks_a_person.py`, `tests/test_cloud_session_menu.py`.
4. **An option-less `Notification` marker — words without buttons.** Reading the screen means CDP, which clicks the task and steals focus, so it is done on demand only. The `Notification` hook needs neither: `canopy_transcript.marker_from_hook` records a menu with **no options** carrying the hook's `message` verbatim. It is deliberately not parsed to guess which kind of dialog it is.

### Notification rules

- **A `Notification` counts only MID-TURN.** It fires both when an agent stops to ask and when a prompt sits idle after a turn ends — the second is every idle session on the box. The discriminator is turn STATE (`UserPromptSubmit` seen, `Stop` not yet; `HookListener._in_turn` in `runner/canopy_runner/canopy_runner/hook_listener.py`), never the message wording, which Claude Code may change.
- **It fails closed:** a runner that restarted mid-turn misses a real prompt rather than raising a false one. Silence costs a menu; noise costs the signal.
- **An API error ends a turn without a `Stop` hook**, so on a `Notification` the listener reads the transcript tail (`canopy_transcript.turn_ended_in_api_error`) and clears the turn state.
- **Precedence is one-way:** a marker never overwrites a real menu; a real menu always replaces a marker.
- **Escape stays real on a marker** — the runner re-reads the screen before pressing anything, and a permission prompt parses there, so the refuse button is an action.

## The screen parser fails closed

`menu.find_menu` returns a menu only when it sees consecutively-numbered options **and** either:

- a footer offering a way out (prose never offers one), **or**
- the TUI's selection cursor (`❯`) on an option, with the dialog running to the bottom of the frame — a tall dialog overflows a short emdash pane and the footer is the line that falls off.

Numbered lines are the most common shape in an agent's own output, so both halves matter: prose does not draw `❯` at the head of a numbered line, and a live dialog is always the last thing on screen, so a menu an agent merely *quoted* is rejected. A grid-wrapped question is rejoined only on the wrap signature, because a dialog's SUBJECT sits directly above its question.

Read the **DOM, not the PTY**: Claude Code draws spaces as `ESC[nC`, so stripping ANSI from the raw stream welds words together.

## Storage: one authority, many copies

The dialog drawn on the terminal is the **authority**. Everything else is a copy: `HookListener._pending_menus`, `~/.canopy/pending-menus.json`, `RunnerBinding.pending_question` (`apps/canopy_sessions/models.py`) and the client's `menu`.

### Persisted on the runner, pruned from both ends

- `~/.canopy/pending-menus.json` (`runner/canopy_runner/canopy_runner/menu_store.py`, loaded by `HookListener`) persists pending menus: the hook fires once, so without it a restart would ship `question: null` and retire a live dialog nothing can rediscover.
- A notification marker is NOT restored — it claims a turn was in flight, and turn state is deliberately not persisted.
- Both ends prune on the wholesale session report so a menu cannot outlive its session: the runner drops menus whose task is not in the open set (`hooks.prune_menus`), and `apps/harness/services.py::replace_reported_sessions` clears `pending_question` for that runner's bindings absent from `now_keys`. The server half is load-bearing: the runner cannot report on a session it can no longer see.

### Served so it survives being looked at later

People look *because* a session went quiet, so a menu must not exist only as a live frame:

- `RunnerBinding.pending_question` holds it, and one reader serves it — `apps/canopy_sessions/serializers.py::pending_menu` — to both the WS connect snapshot (`session_state_dto`) and `GET /api/canopy-sessions/{id}`.
- The session list carries `waiting_on_you`; `session.menu` frames push the EDGES (appear/clear) to an open chat (`apps/harness/services.py`, `apps/canopy_sessions/consumers.py`).
- A bare `blocked` activity frame does not retract the menu (the hook path sends none); only a non-blocked state does.

### The composer lock

The composer refuses to send while a **parsed dialog** is up — the TUI draws the menu where the composer would be, so a send would bounce as `COMPOSER_NOT_VISIBLE`. An **option-less marker does NOT lock it** (`menuBlocksComposer`, `frontend/src/pages/chatPageLogic.ts`): nothing was parsed, so "a dialog is up" is a guess, and a wrong guess would leave a lock with no way out. If the marker is real, the send bounces loudly and the runner ships back the dialog it actually found.

## Answering: a tap becomes a keystroke

### A control frame is a doorbell, never a mechanism

A WS frame published while the control channel is down lands in a Channels group with no consumer and is discarded, while the runner keeps heartbeating over REST and reads `ONLINE` — no keystroke and no refusal. So anything that must happen gets a **durable record the runner drains on its poll tick**, beside `/streams` and `/backfills`:

- `RunnerBinding.pending_answer` → `runner/canopy_runner/canopy_runner/streams.py::drain_menu_answers`
- `RunnerBinding.close_requested` → `streams.drain_closes`; a close that fails (here or from the menu bar's "Close sessions") is reported back (`POST …/close-results` → `RunnerBinding.close_error`) and shown in Supervisor as "Close failed: <reason>" with a Retry. The drain skips a task the frame is closing right now or has closed and not yet reported (`close.settled_or_in_flight`), and `close.close_session` refuses a second concurrent close of one task — two deletes at once made the loser report a failure for a close that succeeded

The WS frame is only the fast path; a dropped channel costs one tick, not the action. `pending_answer` carries an `id` the runner echoes back when it reports the outcome (`apps/harness/api_session_sync.py` retires it only on a matching id), because applying an answer twice is a second keystroke into a session that has moved on. If the report fails, the answer stays set: pressing late is safe (see re-read below), pressing twice is not.

### At the tap, reconcile every copy to the authority

The runner **re-reads the screen before pressing anything**: a stale number typed at a session with no dialog lands in its PROMPT, where the agent reads a bare "2" as an instruction. Then:

- **Could we read the screen? The screen wins.** Clear the menu if it showed nothing; REPLACE it if it showed a different dialog; never keep a cache the authority just contradicted. Tapping an already-answered dialog removes it rather than leaving dead buttons.
- **Could we not? Keep the cache and say why.** So an error only ever rides a menu we are KEEPING.

### The outcome is a value, not a log line

The API returns `sent` the instant it relays; only the runner knows whether the key landed. Refusals — `no_dialog` / `not_on_menu` / `unmodelled` / `wrong_pane` / `unreachable` / `no_session` / `ambiguous_session` (`runner/canopy_runner/canopy_runner/hooks.py`; success is `answered`) — ride the next session report back inside the menu itself as `answer_error` plus a human sentence in `answer_note` (the column is JSON). The report is forced (`sessions.request_report_now`) because a blocked session writes nothing to trigger the change-driven tick. Some refusals are irreducible (the dialog is on a laptop and you are on a phone).

### `send-keys` selects the Claude tab before refusing

A task can hold several terminals (the Claude pane plus shells a human opened) and only the selected one is rendered. Pressing "3" then Enter in a shell RUNS something, so a non-Claude pane is refused (`NOT_A_CLAUDE_PANE` → `wrong_pane`). Before refusing, `send-keys` tries to select the Claude tab (`runner/canopy_runner/canopy_runner/cdp/emdash_control.mjs`) — the human asked for this keystroke, so moving emdash's UI is allowed here. Rules that make it safe blind:

- Match the tab by `title` starting `Claude (` — never by position, and **never by an `aria-label` starting `Close`**: that is the button that KILLS the session, and it sits inside the same tab.
- Dispatch a direct `.click()`, not a real mouse click, so no overlay can intercept it at the coordinates.
- Zero or several visible matches is ambiguous and falls through.
- The positive identification has the final say: if the pane is still not a Claude session, it refuses and says the switch was tried.

## A blocked turn is not a wedged one

The chat bridge (`runner/canopy_runner/canopy_runner/chat_bridge.py`) has a ~15-minute idle backstop (`IDLE_TICKS`) for a writer that never stamps a `stop_reason`; an agent waiting on a human is silent with the same signature. `LiveBridge.step(..., blocked=True)` suppresses the idle finish while a dialog is up on that session — and only that; `MAX_TICKS` still bounds the bridge.

## Checking it live

`scripts/e2e_session_chat.py` drives the real deployment; its `answer_from_the_web` step asserts the agent ACTED on the answer, not merely that a menu appeared or a tap returned `ok`.
