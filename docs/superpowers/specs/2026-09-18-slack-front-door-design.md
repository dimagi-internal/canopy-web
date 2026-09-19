# Slack as a front door to any canopy agent

**Status:** DESIGN — not built. 2026-09-18.
**Replaces:** the `slack` origin's "reserved; no producer yet" note in
`2026-07-27-source-aware-runner-routing-design.md` and
`2026-09-05-actor-aware-runner-routing-design.md`.

## Where we are

There is no Slack integration in canopy-web. There is one in **ace-web**
(`apps/slack/`, May 2026), and it is ACE-shaped end to end:

- `/ace run|new|track|untrack|status|list|link|help`, an OAuth install
  (`SlackInstallation`), signing-secret verification (`verify.py`), a
  Slack-user → Dimagi-login link by DM (`SlackUserLink`), an App Home tab, and
  an ASGI worker that mirrors a run's progress card into a thread.
- It is still deployed: `POST /ace/api/slack/{commands,events}` answers 401
  (signature enforced). Whether its secrets are populated and the app is still
  installed was not checked (needs AWS + Slack admin).
- `/ace run` **never executed anything** until ace-web #697 (2026-07-26) — every
  test mocked the starter. #697 wired it, "ships dark", and was never run against
  live Slack.
- Nothing in it talks to an *agent*, and nothing relays an agent's words back to
  Slack.

canopy-web has only the vocabulary: `Turn.ORIGIN_SLACK = "slack"` is postable and
routable (`apps/harness/models.py`), with no producer.

## Goal

Any canopy agent whose owner has enabled it can be talked to from Slack — a
mention, a DM, or `/canopy` — and answers in the same Slack thread. No per-agent
code. ACE becomes reachable the same way as every other agent.

## Shape: a Slack thread is a chat session

This is the email path again. `enqueue_turn` already binds an email thread to a
`canopy_sessions.Session` via `email_thread_session()`, because "a Turn is a fine
unit of execution and a poor unit of conversation". A Slack thread is the same
object:

```
Slack event ──verify──► apps/slack ──► thread_session(agent, team, channel, thread_ts)
                                          │
                                          ▼
                    canopy_sessions.send_message(origin="slack", user=<linked user>)
                                          │   (routing, actor rules, session UI: unchanged)
                                          ▼
                              runner executes the session Turn
                                          │
                              turn finishes ──► apps/slack relay ──► chat.postMessage(thread_ts)
```

- **Key:** `slack:<team_id>:<channel_id>:<thread_ts>` in `Session.metadata`. A
  reply in the thread continues the session; a new thread opens a new one.
- **Origin:** `slack`, so a per-agent routing rule on the `slack` source (already
  in the Runners-tab UI) works the day this ships.
- **Actor:** `enqueued_by` = the linked canopy user, which is what actor-aware
  routing keys on for `slack` (spec 2026-09-05 table).
- **Session visibility:** `created_by` = the linked user, `origin=web`, so
  `visible_session_q` makes it private to that person (+ participants they add).
  Deliberately NOT `origin=runner`: leg 3 of `visible_session_q` would widen it.
- **Ack within 3s:** Slack retries an unacknowledged event. Verify, dedupe on
  `event_id`, enqueue, return 200; everything else happens after the response.

## New app: `apps/slack` (framework tier)

A transport like `inbound`, agent-agnostic, so it goes in `FRAMEWORK` in
`tests/test_architecture_boundary.py` and may not import product code.

**Ported from ace-web (ACE-free pieces only):** `verify.py`, the OAuth install
view, `SlackInstallation`, `SlackUserLink` + the link-by-DM nonce flow.
**Not ported:** opps, runs, phase tiles, the progress worker, App Home.

**Models**

- `SlackInstallation(team_id unique, team_name, bot_user_id, bot_token_enc, workspace FK NOT NULL, installed_by)`
  — one Slack team maps to one canopy Workspace. Token Fernet-encrypted like
  `GitHubConnection`.
- `SlackUserLink(installation FK, slack_user_id, user FK)` unique on
  (installation, slack_user_id).
- Per-agent enablement: `Agent.slack_enabled` (bool, default False) and
  `Agent.slack_handle` (optional; defaults to slug). Owner-only to change.

**Endpoints** (bare Django views — they are signed form/JSON posts, not the Ninja
contract; allowlisted as `/api/slack/` in `apps/common/middleware.py` and
self-enforcing via the signature, exactly like `/api/inbound/`):

| Route | Purpose |
|---|---|
| `POST /api/slack/events` | `app_mention`, `message.im`, `url_verification` |
| `POST /api/slack/commands` | `/canopy <agent> <text>`, `/canopy link`, `/canopy agents` |
| `POST /api/slack/interactions` | message shortcut (context basket — below) |
| `GET /auth/slack/install/`, `/auth/slack/callback/` | OAuth install (workspace owner) |
| `GET /auth/slack/link/` | link a Slack user to the signed-in canopy user |

**Which agent?** In order: `/canopy <agent> …`; `@canopy <agent> …` (first word
matches an enabled agent in the installation's workspace); an existing thread
session's agent; else reply with the enabled-agent list. One bot identity for all
agents — not one Slack app per agent, which would be N installs and N secrets.

**Unlinked user:** ephemeral reply with the link URL and nothing else. Being in
the Slack workspace grants nothing in canopy — the same rule `apps/contacts`
holds for an inbound email.

**Relay back (the piece that has never existed anywhere):** a receiver on
`harness.signals.turn_events_appended` (already consumed by
`canopy_sessions/signals.py`) — when a turn on a Slack-keyed session reaches a
terminal status, post its final assistant text to the thread with a link to
`/w/<ws>/chat/<id>`. **Open question to settle in PR 2:** transcript-sourced
sessions persist rows through `persist_transcript_rows`, not the ledger
projection; confirm which path carries the final assistant text for them before
relying on the signal. A dropped relay must be visible (an `events` row), never
silent.

**Config:** `SLACK_CLIENT_ID` (public env), `SLACK_CLIENT_SECRET` +
`SLACK_SIGNING_SECRET` in Secrets Manager under `canopy-web/*`, declared in
`deploy/aws/canopy-web.cfn.yaml`. Unconfigured = a real state; every endpoint
answers 503 and `/settings` says so. A new "Canopy" Slack app is created — not
ace-web's, whose URLs point at `/ace/`.

## Feeding it context from several threads

Jonathan's ask: "read everything from the last 10 minutes" **in the channel the
bot is already in** — a conversation spread across several threads there —
without the bot ingesting whole channels. Decided 2026-09-18: channel history,
scoped to the channel the request came from. The Real-time Search API is
deferred (below).

### What the request does

`@canopy hal read the last 10 min <ask>` in channel C:

1. `conversations.history(channel=C, oldest=now-window)` — top-level messages
   posted in the window.
2. `conversations.replies` for each of those that has replies, limited to the
   same window, plus the thread the mention itself is in (fetched whole — it is
   the conversation the human is asking from).
3. Rendered as plain text — author, time, grouped by thread — and put into the
   turn's prompt fenced as quoted Slack content, so the agent reads it as
   material, not instructions.

**Known gap, accepted for v1:** a reply posted in the window to a thread whose
*parent* is older than the window is missed (Slack's history call returns
parents only, by parent time). Closing it means scanning parents further back
than the window, which is the opposite of the bound. The basket covers it.

### The constraint Slack imposes

**Slack has no time-bounded or thread-bounded read scope.** Reading a channel is
`channels:history` (public) / `groups:history` (private), each granting the full
history of every channel of that type the bot is a member of. A useful Slack
agent needs them anyway: an `app_mention` carries only the one message, so even
reading the thread the agent was mentioned in requires them. "The app could
technically read the whole channel" arrives with basic usefulness, not with this
feature.

What canopy makes true is that it **never does**, enforced in one module:

1. **No passive ingestion.** Nothing is read or stored unless someone is
   talking to an agent. *Amended 2026-09-19:* to let a thread continue without
   re-mentioning the bot, canopy now subscribes to `message.channels` /
   `message.groups`, so Slack delivers every message in the bot's channels —
   and every one that is not a reply inside a thread canopy already has a
   session for is dropped before anything is looked up or recorded. The
   channel-window read (below) is still only ever done on request.
2. **Only the channel the request came from.** The channel id comes from the
   verified event, never from message text, so "read #finance" cannot redirect
   it. No cross-channel reads — so no membership check is needed either: the
   requester just posted there.
3. **Window capped server-side.** `oldest` is computed by canopy (default 10
   min, hard max 60); a number in the message is clamped to it. Pagination stops
   at the window and at a message-count ceiling.
4. **The agent never holds a Slack token.** canopy-web fetches and hands text
   into the turn's prompt. The agent cannot call Slack, so a prompt-injected
   "now read the whole channel" has nothing to call.
5. **Audited.** Each read writes an `events` row: requester, channel, window,
   message count.
6. **Private by default.** The resulting session is the requester's alone
   (above). Adding a participant to a session holding private-channel content
   warns first — Slack's guidance is not to expose messages to anyone who could
   not see them in Slack.

Internal (non-Marketplace) apps are not subject to the 2025
`conversations.history` throttle on distributed apps; confirm on the first live
run.

### Later: the basket

A message shortcut "Add to canopy context" — `⋯` on any message, in any
conversation. The interaction payload carries the message text, so it needs **no
history scope** and works in human-to-human DMs and channels the bot is not in.
Items collect per user for 30 min and attach to that user's next `@canopy`.
Covers what the channel window cannot: DMs, other channels, the old-parent gap.

### Deferred

- **Slack Real-time Search API** (`assistant.search.context` with the mention's
  `action_token`). Bounded by what the requesting user can see and reaches
  channels the bot is not in — but the ask is the bot's own channel, which
  channel history covers. Its docs page forbids storing results; the binding API
  Terms scope that to *other organizations'* data, which an internal Dimagi app
  is not. Get that confirmed if it is ever picked up.
- **User tokens.** Everything the user can see including DMs, held at rest per
  user. Broader than the problem.

## Delivery

1. **PR 1 — inbound.** `apps/slack` models + migrations, verify, install, link,
   `/events` + `/commands`, per-agent flag + owner UI toggle, thread→session,
   `send_message(origin="slack")`, ack-and-dedupe. Tests through the real
   views with a signed body — no mocking of the starter (the ace-web #697
   failure).
2. **PR 2 — relay back** + a live e2e script (`scripts/e2e_slack.py`) that posts
   a real mention and asserts the reply lands in the thread.
3. **PR 3 — channel window**, with the guarantees above pinned by tests
   (channel from the event only, window cap, no token in the turn, audit row).
4. **PR 4 — basket.**
5. **ace-web:** retire `/ace` or keep it only for tracked-run progress cards.

**Manual, needs a human:** create the Canopy Slack app, approve the install in
the Dimagi Slack workspace (bot scopes: `app_mentions:read`, `chat:write`,
`commands`, `im:history`, `im:write`, `users:read`, `channels:history`,
`groups:history`, `channels:read`, `groups:read`), put the secrets in Secrets
Manager.
