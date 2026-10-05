# Content retention: drop AI session and transcript content after N days

**Status:** built, **not enforced**. Off until `CANOPY_RETENTION_ENFORCE=true`
is set and at least one `RetentionRule` exists. Both switches are off as of
this change.

## Why

Two reasons, in order:

1. **Blast radius.** canopy-web holds the full text of every agent conversation:
   chat messages, every turn's prompt, its event ledger, and the raw Claude
   JSONL. That includes email bodies, contacts' messages and whatever a tool
   printed. If the server is compromised, all of it leaks, going back forever.
   Content nobody will read again is a liability that buys nothing.
2. **Cruft.** `TurnTranscript`'s own docstring records the gap: "no cap, TTL,
   or archival tier exists on this TABLE — it grows monotonically… forever."

## What counts as content

| Kind (`RetentionRule.kind`) | What is dropped | What is kept |
|---|---|---|
| `chat`: a conversation (`canopy_sessions.Session`) **and every turn on it** | `Message` rows, `Draft`s, `Attachment`s (S3 bytes first), `PageAction` args/results, transfer-request briefs. Once the whole session is idle past the cutoff: its title, `page_state`, the binding's `tail`/`summary`/`pending_question`, and the session is archived | The `Session` row (id, workspace, agent, who, when), participants, metadata linkage |
| `turn`: agent/project work that is not a chat (email, schedule, API, dispatch) | `Turn.prompt`, `result_note`, `report_summary`, every `TurnEvent`, the `TurnTranscript` blob, the Slack status line's rendered text, `CallerToken`s | The `Turn` row: who, when, origin, status, runner, mode, `report_title`, `origin_ref` |
| `shared`: uploaded `/canopy:share-session` transcripts (`session_sharing.Session`) | The whole row (messages, share tokens and arc memberships cascade) | Nothing. A shared transcript IS content; its share link stops working |

**Turns are scrubbed, not deleted.** `/activity`, the agent KPIs, the board's
`dispatched_turns` and the schedule occurrence lookup all read turn rows. That
a turn happened (when, for whom, on which box) is operational metadata. What
it said is the content. `origin_ref` stays because routing, idempotency and
the schedule lookup key on it. For email it holds the sender's address, not
the body (the body is in `prompt`).

**A chat is dropped as one unit.** A turn on a chat session is resolved with
the *session's* attributes, not its own, so a conversation's messages and the
prompts that produced them expire together.

**Out of scope, deliberately:** the board (`AgentTask`: tasks and asks are work
records a person acts on, not transcripts), the event log, the MCP audit log,
feedback, contacts, and anything on a runner's disk. canopy-web cannot reach
a laptop's `~/.claude/projects`, and that is the runner owner's retention. Each
of these could get a kind later, using the same rule resolution.

## Rules

```
RetentionRule(workspace?, kind?, source?, principal?, agent?, keep_days?)
```

Every field except `keep_days` is a **filter**; blank/null means "any".

- `workspace`: null means deployment-wide.
- `kind`: `chat` | `turn` | `shared`.
- `source`: the `Turn.origin` vocabulary (`email`, `slack`, `ace_web`,
  `canopy_web_chat`, `canopy_scheduler`, `api`), plus `emdash` for a chat
  discovered on a runner rather than started in an app. A chat's source is
  its creator's marker (`metadata.source`: `ace-web` → `ace_web`, `email`,
  `slack`), else `emdash` for a runner-origin session, else `canopy_web_chat`.
- `principal`: who the content is about. `member` (a canopy user), `contact`
  (an outside person with no account), `agent` (another agent dispatched
  it), `system` (a schedule, a drill, or nobody established). Turns read
  `initiator_kind`. Chats read `contact` → `contact`, `created_by` → `member`,
  else `system`.
- `agent`: one agent.
- `keep_days`: null means **keep forever**, an explicit exemption that beats a
  broader rule. Minimum 1.

### Which rule wins

1. **Nearest workspace first.** Rules on the item's own workspace, then its
   parent, up the tree, then deployment-wide rules. The first level with any
   matching rule decides. A division can loosen or tighten its org's policy,
   which is the same nearest-wins shape as `WorkspaceRunnerOrder` and the
   shared vault.
2. **Most specific within that level**: the most non-blank filters.
3. **Ties go to the shorter retention.** When two equally specific rules
   disagree, privacy wins.

No matching rule means **keep forever**, today's behaviour. A deployment with
no rules purges nothing even when enforced.

Worked example, workspace `connect` under `dimagi`:

| Rule | Where |
|---|---|
| `kind=turn keep 90` | deployment-wide |
| `principal=contact keep 7` | `dimagi` |
| `kind=chat keep 30` | `connect` |
| `kind=chat agent=hal keep ∞` | `connect` |

- A contact's chat with ace in `connect` → `connect` has a matching rule
  (`kind=chat`), so **30 days**. `dimagi`'s contact rule is not consulted.
  To keep contacts at 7 days in `connect` too, add
  `kind=chat principal=contact keep 7` on `connect`.
- A member's chat with hal in `connect` → two `connect` rules match, and the
  one naming the agent is more specific → **kept forever**.
- An email turn for eva in `dimagi` → `dimagi`'s only rule is about contacts
  and this sender is a member, so it falls through to deployment-wide →
  **90 days**.

## The age of an item

- Turn: `finished_at`, else `created_at`. Only TERMINAL turns are touched; a
  turn still queued or running is never scrubbed, however old.
- Chat message: purged as a **prefix**. Find the newest message with
  `created_at < cutoff`, then drop every message at or below its `turn_index`.
  The transcript is chronological by index, so this never leaves holes.
- Shared transcript: `created_at` (upload time).

## Purged content must stay purged

A transcript-sourced chat's `Message` rows are a **cache of a file on the
runner's disk** (`transcript_sourced`), and the runner re-ships them: on
backfill ("load full session"), on `reset`, on reconnect overlap. A plain
`DELETE` would be undone the next time anyone opened the chat.

So purging raises `Session.retention_floor_index`, and
`persist_transcript_rows` (the one funnel for live stream, backfill and reset)
drops any row whose final `turn_index` is below it. The floor only ever rises.
A legacy (ordinal-less) backfill into a purged session is refused outright,
because it would renumber old history from the floor up. Rows assigned
server-side (`index < 0`) start at or above the floor.

The ledger projection needs no guard, because purging deletes the
`TurnEvent`s it projects from.

## Running it

- `uv run python manage.py retention_sweep` prints a **dry run**: per rule,
  what would go. It never writes. Use it to see what a rule will do before
  enforcing.
- `… retention_sweep --apply` purges now, as a deliberate human act, whether
  or not enforcement is on.
- **Automatic**: the fleet's heartbeat is canopy's only clock (no celery), so
  `harness.services.heartbeat` calls `retention.services.maybe_sweep()`,
  beside the shared-secret purge. It returns at once unless
  `CANOPY_RETENTION_ENFORCE` is true. When on, it runs at most once an hour
  (a cache lock; Redis in labs) and is bounded per run
  (`BATCH_LIMITS`), so one heartbeat never carries a long purge. A big
  backlog drains over successive hours. Run the command once with
  `--apply` to clear it in one go.
- Every run, dry or real, writes a `RetentionSweep` row with counts only, no
  content. It records what was purged and when, which is the record you want
  after deleting things.

## Who sets the rules

Rules are a **workspace setting**: `/w/:ws/settings/retention`, backed by
`GET /api/workspaces/{slug}/retention` (the policy: own rules, inherited
rules, whether the deployment enforces), `…/retention/preview` (counts per
rule for this workspace's own chats and turns, deleting nothing), and
`POST …/retention/rules`, `PUT|DELETE …/retention/rules/{id}`. Like every
route, these are MCP tools too.

- **Any member reads the policy.** It decides when their own conversations
  go, so it is not hidden from them.
- **`retention.manage` (admin and above) changes it**, by owner decision. That
  is the tier that already reads every turn's content (`logs.read`), and
  setting how long that content lives is part of running the workspace, not
  holding its keys.
- **Every change is written to the workspace event log** (`source=retention`)
  with who made it and the rule before and after. A rule decides what gets
  deleted, so who set it has to be on record.
- A workspace edits only its own rules. Inherited ones show read-only, labelled
  with where they come from. Because the nearest level wins, a division's
  admin can override the org's rule for their division; the org's rule still
  governs every division that sets none.
- **Deployment-wide rules stay in Django admin.** They are the only rules that
  reach shared transcripts (which have no workspace), and no workspace role
  should set policy for every other tenant.

## Turning it on

1. Add rules under Settings → Retention in each workspace, plus a
   deployment-wide rule in Django admin if shared transcripts should expire.
2. Use the page's Preview (or `retention_sweep` for the whole deployment) and
   read the counts.
3. Either `retention_sweep --apply` once, or set
   `CANOPY_RETENTION_ENFORCE=true` in the task definition and let the
   heartbeat drain it.
