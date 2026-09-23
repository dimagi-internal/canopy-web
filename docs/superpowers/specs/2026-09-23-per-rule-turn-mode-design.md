# A routing rule sets the mode as well as the box

**Status:** shipped with this change. Layers onto
`2026-07-27-source-aware-runner-routing-design.md` and
`2026-09-05-actor-aware-runner-routing-design.md`.

## The ask

> Route the combination of user + agent + channel to a runner **and a mode**.
> If Beth emails Eva, send that to the cloud runner in auto mode.

Routing already covered agent + channel + user → runner (an actor rule:
`RunnerAssignment(agent, source, actor)`). Mode did not exist below the agent:
`Agent.turn_mode` was one switch, read by the turn procedure with
`canopy agent mode --slug <slug>`, and nothing about a single turn could change
it.

## Decisions

1. **Mode lives on the rule, not in a second table.** "Beth's email → cloud,
   auto" is one decision about one kind of work; splitting it across two tables
   keyed on the same triple would be two writers of one fact.
   `RunnerAssignment.turn_mode` is rule-level (written to every row, like
   `strict`). `""` means the rule says nothing about mode.

2. **Same ladder as routing.** (source, actor) rule → (source, anyone) rule →
   `Agent.turn_mode`. A rung whose mode is `""` defers down. So a rule that only
   moves work to another box leaves its posture alone.

3. **Decided at claim, stamped on the turn** (`Turn.turn_mode`,
   `Turn.turn_mode_basis`). The agent reads it mid-turn, and a rule edited while
   a turn runs must not flip the posture of work already under way. A re-claim
   (lease lost) decides again.

4. **An `auto` that names a person requires a verified message.** Routing on a
   forged `From:` only picks a box. Auto on one would hand a forger an agent that
   sends without review. So an actor rule's `auto` applies only when
   `caller_context._verified(turn)` holds for this message, the same test the
   caller envelope's `verified` uses. Otherwise the turn runs **manual**, not
   "whatever the agent says": the rule was written about Beth, and a message
   that may not be Beth is the one to slow down on. `manual` is always honoured,
   because lowering autonomy is safe whoever asks. An anyone rule makes no claim
   about the sender, so it needs no verification.

5. **Delivered in the caller envelope** (`turn_mode: {mode, basis}`), which the
   runner already writes to `~/.canopy/caller/<turn_id>.json` and passes as
   `--caller`. The runner needs no change. `canopy agent mode --slug X --caller
   <path>` prefers the envelope's mode over the agent-wide switch (canopy plugin).

## The UI

Turn mode, the default runner order and the "Except when the work comes from"
list were three controls answering parts of one question. They are now one
**Routing** table (`frontend/src/components/agents/AgentRouting.tsx`):

| Work | From | Runs on, in order | If all are down | Mode |
|---|---|---|---|---|
| email | beth@dimagi.com | cloud-ec2-1 | Wait | Auto |
| email | anyone | cloud-ec2-1 | Fall back | As below (Manual) |
| **Everything else** | | the default order | Waits | Manual / Auto (the agent's switch) |

- Rows are in evaluation order, so reading top to bottom *is* the precedence;
  the footnote explaining precedence shrank to the two cases the table cannot
  show (pins, and live chats).
- "strict" is worded as its consequence: **Fall back** / **Wait**.
- A rule's mode is a select whose default option names what it inherits
  ("As below (Manual)").
- A named sender set to Auto shows the verification caveat inline.
- Adding a rule is a form (work, sender, runner, mode) rather than a
  source-only menu. The old menu could not add Beth's email rule once an
  "email from anyone" rule existed, because it hid a source as soon as any
  catch-all rule took it.
- Scheduler rows offer no sender field (a schedule has no sender).
- It is a CSS grid under a container query, so the same component works on the
  Settings page and in the supervisor's phone-width runner drill-down.

## Not done

- The Activity log does not show a turn's mode yet; `TurnOut.turn_mode` carries
  it for when it does.
