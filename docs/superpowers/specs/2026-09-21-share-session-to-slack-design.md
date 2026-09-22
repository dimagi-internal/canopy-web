# Share a session to Slack

**Status:** approved and built 2026-09-21. The skill ships in the canopy plugin (`plugins/canopy/skills/share-to-slack`).

## Why

Every Slack thread canopy knows about is *born in Slack*: a mention, a DM or a
`/<agent>` command creates the `Session` and the thread adopts it. The reverse
does not exist. When you are working in a session and want teammates to see what
you are doing, the only path is to copy-paste into Slack by hand, and the thread
that results is invisible to the session.

The ask, in the session: *"summarize and share what we are doing"*.

## Two modes

- **broadcast** — one top-level post: `@you shared what they're working on`, the
  summary, and the canopy link. Nothing is recorded against the thread; replies
  stay human-to-human and never reach the session. Sharing again is a new post.
- **bind** — the same post, and then the session takes that thread: its
  `metadata` gets the same `slack_thread` / `slack_team` / `slack_channel` /
  `slack_thread_ts` keys a Slack-born session carries. From then on it is
  indistinguishable from one: the agent's output is relayed into the thread
  (`relay.py`, ledger + after-turn transcript), a plain reply in the thread
  continues the session, questions and buttons appear there, and a member who
  replies becomes a participant. **Replying in the thread steers the session** —
  that is the point, and why bind is a deliberate choice rather than the default.

A session holds one thread; there is no unbind in this change. **Sharing a bound
session again posts an update** into that thread (a reply headed `*Update* from
@you`), whatever mode or channel was asked for. Once bound, work done in the
session itself never reaches the thread (the thread hears only what it asked),
so re-sharing is how the people watching it stay current. *(Amended 2026-09-21:
this was first built as a refusal, which left no way to post an update where
people were watching.)*

Turns asked **before** the bind are not the thread's business. The bind stamps
`slack_bound_at`, and the relay and status line skip earlier turns — otherwise a
share from the chat page, which is itself a turn, would open its own new thread
with "Shared to <link>" and a done-line.

## One service, two entry points

`apps/slack/share.py::share_session(...)` is the only implementation. It is
reached by:

1. **The canopy plugin skill** `/canopy:share-to-slack #channel [bind]`, run
   inside the Claude Code session. The agent writes the summary — it has the
   context — and calls the MCP tool `share_session_to_slack` (runs as the caller's
   PAT, rate-limited and audited like every write tool).
2. **The chat page** (`/w/:ws/chat/:id`), a *Share to Slack* action that asks for
   channel + mode and sends `/canopy:share-to-slack #channel [bind]` into the
   session as an ordinary message — so the summary is always written by the
   session, never by a second summarizer that lacks its context, and the web path
   has no backend of its own.

The channel is **named every time**; there is no default. A name (`#x`) or an id
is passed to `chat.postMessage`, and the id Slack answers with is what gets
stored, since a thread key must survive a rename.

## Which session is "this one"

The tool is called from inside a Claude Code process, which knows its own
identity but not canopy's session id. It passes what it has:

- `claude_session_id` (`$CLAUDE_CODE_SESSION_ID`) → `RunnerBinding.transcript_id`;
- else `emdash_task` + `emdash_project` (`$EMDASH_TASK_NAME`, basename of
  `$EMDASH_ROOT_PATH`) → `RunnerBinding(session_key, emdash_project)`;
- or an explicit canopy `session_id` (the web path could, but does not need to).

Every candidate is filtered through the caller's workspaces and
`visible_session_q`, so the lookup can never reach a session the caller could not
open. More than one match is ambiguous and refused, not guessed.

**An unresolved session still broadcasts** — a broadcast needs no session (the
post simply carries no canopy link). **Bind refuses** — a thread with no session
behind it has nowhere to send replies.

## Guards

- Slack installed for the workspace, and the caller linked to a Slack user there
  (`SlackUserLink`, or auto-linked by email exactly as the front door does). The
  post names that Slack user.
- The caller can see the session (`visible_session_q`) and belongs to its
  workspace.
- **bind, agent session:** the agent must be `slack_enabled`. That switch is the
  owner's consent to the agent being reachable from Slack; bind must not be a way
  around it.
- **bind, repo session (no agent):** replies in the thread are accepted from
  workspace **members only**. A Slack-born thread always has an agent whose owner
  opted in to answering anyone; a repo session is someone's laptop session running
  with permissions bypassed, and a Slack guest must not be able to type into it.
  This is the one place a bound thread differs from a Slack-born one.
- The bot must be in the channel. `not_in_channel` / `channel_not_found` come back
  as a sentence ("invite @canopy to #x"), not a stack trace.

Posted as the agent (name + avatar, `relay.persona`) for an agent session; as the
plain bot for a repo session or an unresolved one.

## Reply routing change

`services.handle_message` finds a thread's session through its agent
(`resolve_agent` → `thread_session(agent=…)`), which cannot find an agent-less
session. So: when the first word does not name an enabled agent and the thread
belongs to an agent-less session, that session is continued directly (members
only). The agent case needs no change — the existing lookup already finds any
session carrying the thread key.

## Observability

Every share writes an `apps/events` row: `slack.shared` (info) on success, and
`slack.share_refused` (warn) with the reason on a refusal, keyed so retries
coalesce.

## Tests

Through the real Slack fake in `tests/test_slack.py`:

- broadcast posts once, top-level, stores nothing, and a reply is not a turn;
- bind stamps the metadata and a plain reply in that thread becomes a turn on the
  SAME session, and the agent's reply is relayed into it;
- bind on an agent-less session: a member's reply continues it, a contact's does
  not;
- refusals: not installed, not linked, not visible, agent not Slack-enabled,
  unresolvable session (bind), ambiguous match, `not_in_channel`;
- re-sharing a bound session posts an update in its thread; a turn asked before the bind
  is neither relayed nor given a status line;
- the MCP tool resolves by `claude_session_id` and by emdash task, and cannot
  reach another workspace's session.

## Not in this change

Channel autocomplete, a default channel, unbinding, and posting later updates as
anything other than the session's normal relayed output (broadcast again for a
fresh top-level post).
