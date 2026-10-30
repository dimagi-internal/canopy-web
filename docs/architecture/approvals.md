# Approval cards — a proposal waiting on your yes, decidable from where it reaches you

A **card** is a proposal that waits on a person's yes or no, delivered with everything needed
to decide it, so the decision is one tap and no hunting. It is not a new kind of record: the card's
content is the board task itself (`agents.AgentTask`). `apps/approvals` adds delivery, one decision
door, and the memory of both.

Code: `apps/approvals` (framework tier, L2 features), `apps/slack/approvals.py` (the Slack DM and
its buttons). Page: `/w/:workspace/approvals/:taskKey` (`frontend/src/pages/ApprovalPage.tsx`).

## What is a card

`cards.needs_decision(task)`: a **suggested** task whose approval starts work (`on_approve` is not
empty) and that is not snoozed. This is deliberately narrower than "every open ask": a plain question
or review on the board does not push (owner decision, 2026-10-05 — those were noise); a card is what an
agent files when a person's yes is what lets it act.

`cards.build(task)` renders the one card every channel shows — the ask, the person's own words
(`human_why`, verbatim), the agent's reason, the exact plan, **what approving starts** (the
`on_approve` turns, or the card's own turn), **what declining does**, links, who it was sent to and,
once decided, who decided it, through which door and what they said. Nothing is invented.

An agent files a card by filing the task: `create_task` (or `propose_task`) with `status:
suggested`, `human_why`, `plan`, `rationale` and `on_approve`. Nothing else to call.

## Who gets it (`deciders.for_task`)

The first rule that names anyone wins: the person the task is parked on or assigned to → the
project's owner and check-in lead → the owner of the agent that raised it → the workspace's owners.
An agent's own login is never a decider. At most 5. Anyone else in the workspace can still open a
card and decide it (the same viewer tier as the board's Approve button); they are just not pushed.

## How it reaches them (`delivery.deliver`)

Once per (task, person, channel), on commit of the save that made it a card; a 60-second backstop
(`approvals.backstop`, tick) delivers any card from the last 6 hours nobody was told about.

| Channel | What | One tap |
|---|---|---|
| **Slack DM** (`apps/slack/approvals.py`) | The whole card as blocks — ask, quotes, plan, what starts — and **Approve / Decline** buttons. | Yes: the click decides. |
| **Web Push** (`channel_push.py`) | Title = what is asked; body = who and what; opens the card. High urgency. | Opens the card. |
| **Email** (`channel_email.py`) | The same facts, and Approve / Decline buttons that open the card with the choice made. | Opens the card to confirm — a link never decides by itself (mail scanners open every link). |

A person with a push or a Slack DM is not also emailed; email is for someone neither reaches.
`NotificationPreference.approval_cards` (Settings → Account → Approval cards) turns the sending off
for a person; the card stays on the board and Home.

## The one door (`decide.decide`)

The web page, a Slack button and an MCP client all call `decide`, which calls the board's own
`services.act` — so approving starts exactly the turns `on_approve` names, once, atomically — and
records an `ApprovalDecision` (who, `via` web | slack | email | push | api, comment). A second
decision is a **409 naming who got there first**; the work is never run twice. A project decision
card is still the project owner's (`proposals.before_act` → 403).

A Slack click is accepted only from the DM canopy sent **that person for that card** (matched on the
message ts), by the person Slack signs the click as; anything else is refused privately.

## Every copy stays honest (`delivery.settle`)

Any action on the task — from the card, the board, or the API — triggers `settle` on commit, which
rewrites each Slack DM to drop the buttons and say "Approved by X via Slack / the web / the board",
and stamps the delivery settled. Push and email cannot be recalled; opening them shows the outcome.

## API

`GET /api/approvals/` (cards waiting on you) · `GET /api/approvals/{key}` · `POST
/api/approvals/{key}/decide {decision, comment}`; each also an MCP tool. 404 for a task in a
workspace you are not in.

## Not here (on purpose)

No passkey step-up on a decision yet: a card whose action is dangerous enough to need one is a
follow-up (`act-as` and sign-offs already have the pattern). No snooze or reminder: a card waits on
the board like any open ask.
