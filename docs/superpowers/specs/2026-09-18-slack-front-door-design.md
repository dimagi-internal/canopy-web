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

Jonathan's ask: "read everything from the last 10 minutes", across threads,
without handing the bot the ability to ingest whole channels.

### The constraint Slack imposes

**Slack has no time-bounded or thread-bounded read scope.** Reading messages is
`channels:history` / `groups:history` / `im:history` / `mpim:history`, and each
grants the full history of every conversation of that type *the bot is a member
of*. And a useful Slack agent needs `channels:history` + `groups:history` anyway:
an `app_mention` event carries only the one message, so even reading the thread
the agent was mentioned in requires them. So "can technically read a whole
channel it is in" arrives with basic usefulness, not with this feature.

What can be made true is that canopy **never does** it, enforced in one place:

1. **No passive ingestion.** Subscribe to `app_mention` and `message.im` only —
   never `message.channels` / `message.groups`. Nothing is read unless a linked
   human asks, at that moment.
2. **Only where invited.** The bot reads only conversations it is a member of.
   It cannot see human-to-human DMs at all (`im:history` covers DMs *with the
   bot*).
3. **Window capped server-side.** The window is a Slack `oldest` computed by
   canopy (default 10 min, hard max 60), not a parameter the agent or the prompt
   can widen. Pagination stops at the cap.
4. **Requester must be a member.** Every channel read is checked against
   `conversations.members` for the requesting Slack user, so the agent cannot be
   used to read a channel you cannot see.
5. **The agent never holds a Slack token.** canopy-web fetches and hands text
   into the turn's prompt. The agent cannot call Slack itself, so a
   prompt-injected "now read #finance" has nothing to call.
6. **Audited.** Each read writes an `events` row: requester, channels, window,
   message count.
7. **Private by default.** The resulting session is the requester's alone
   (above); content from a private channel lands nowhere wider than that.

### Two ways to pick the context

**A. Time window — `@canopy hal read the last 10 min` (optionally `in #a #b`).**
Default scope: threads the *requester* posted in during the window, across
channels both they and the bot are in, each thread fetched whole
(`conversations.replies`) so the context is not cut mid-conversation. Named
channels narrow it. This is the "10 minutes" ask. Internal (non-Marketplace) apps
are not subject to the 2025 `conversations.history` throttle on distributed
apps; confirm on the first live run.

**B. Basket — a message shortcut "Add to canopy context".** Click `⋯` on any
message, in any conversation; the interaction payload carries that message's text,
so this needs **no history scope** and works in human-to-human DMs and channels
the bot is not in. Items collect per user for 30 min; the next `@canopy <agent>`
from that user attaches them. Most precise and least privileged; more clicks.

Ship **A** with the first real version (it is the ask) and **B** right after
(it covers DMs, which A structurally cannot).

### Option C: the Real-time Search API — viable for an internal app, spike it

`assistant.search.context`, called with the bot token plus the `action_token`
Slack puts on the mention event. It is the closest thing Slack has to the ask:
results are bound to what the *requesting user* can see, the call is only
possible in response to that user's message, and it takes `after`/`before`
timestamps. With a bot token it covers public channels the user is in — **no
need to invite the bot** into each one, which A requires.

**The storage question.** The developer page says flatly *"You must not store or
copy any of the data retrieved from this API"*, and a canopy session persists
by design (Turn.prompt, Message rows, and the runner's Claude Code transcript,
which is never deleted). The binding text is the API Terms of Service, and it is
narrower: in its "Data Access API and Real-Time Search API" section the
prohibition is on persistent copies of **other organizations'** API Data, aimed
at third-party providers, and the Commercial Distribution restrictions exempt an
app built for a single organization. canopy's Slack app is internal to Dimagi's
own workspace, so this is Dimagi's own data. Read that way, persisting it in a
private Dimagi session is permitted. Because the docs page and the terms
disagree, **get a one-line confirmation from whoever owns the Slack admin
relationship before shipping C** — it is not a design blocker.

**It stops being true the day canopy serves another organization's Slack.** At
that point canopy is the third party and the restriction binds: Slack-sourced
context would need an ephemeral path (fetched per turn, never written to
Turn.prompt or Message rows, transcripts purged). So `SlackInstallation` records
whether the team is the deploying org's own, and C is refused for any other team
until that ephemeral path exists. Ties to the deferred org layer.

**Also required by the same page, and already true here:** "don't expose
messages to anyone who would not have access to them in Slack" — satisfied by
the session being private to the requester. **Adding a participant to a session
holding Slack-sourced context must warn** (or refuse, for private-channel
content), because that is exactly the exposure the rule forbids.

**Unknowns for the spike:** whether `query` can be empty or wildcarded (a time
window has no search terms), keyword vs semantic behaviour on Dimagi's plan
(semantic needs Slack AI on Business+), and the method's rate limit. If a
windowed query is not expressible, C does not replace A.

If the spike is clean, C is preferable to A: no channel invitations to manage,
and "what the user can see" is enforced by Slack itself rather than by canopy's
`conversations.members` check.

### Considered and not chosen

- **User tokens** (`search:read`, per-user history, or the Real-time Search API
  with user scopes for private channels and DMs). Reaches everything the user
  can see, including DMs, but means holding a broad token per user at rest.
  Broader than the problem; the basket (B) covers DMs without it.

## Delivery

1. **PR 1 — inbound.** `apps/slack` models + migrations, verify, install, link,
   `/events` + `/commands`, per-agent flag + owner UI toggle, thread→session,
   `send_message(origin="slack")`, ack-and-dedupe. Tests through the real
   views with a signed body — no mocking of the starter (the ace-web #697
   failure).
2. **PR 2 — relay back** + a live e2e script (`scripts/e2e_slack.py`) that posts
   a real mention and asserts the reply lands in the thread.
3. **PR 3 — context window.** Spike C first (half a day against the live
   workspace); ship C if a windowed query works, else A. Either way the
   guarantees above are pinned by tests (cap, no token in turn, audit row, and
   the membership check for A).
4. **PR 4 — basket (B).**
5. **ace-web:** retire `/ace` or keep it only for tracked-run progress cards.

**Manual, needs a human:** create the Canopy Slack app, approve the install in
the Dimagi Slack workspace (bot scopes: `app_mentions:read`, `chat:write`,
`commands`, `im:history`, `im:write`, `users:read`, `channels:history`,
`groups:history`, `channels:read`, `groups:read`), put the secrets in Secrets
Manager.
