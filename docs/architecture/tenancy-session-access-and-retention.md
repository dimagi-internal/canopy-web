# Tenancy: who can see a session, artifacts, retention and deletion

The chat-session ACL, artifact visibility and uploaded HTML, rolling sessions, content retention and a person deleting their own session's content. Part of the [tenancy](tenancy.md) topic; the index there lists every part.

## The chat-session ACL (CI-enforced)

One module decides who may see and act on a chat session: `apps/canopy_sessions/access.py`.

- **Tenant first** (member of `Session.workspace`), then one of seven legs: creator ·
  explicit participant (`SessionParticipant`) · a runner-discovered session on a box you
  OWN (`Runner.owner`; never the whole tenant — a laptop session is single-player until
  its owner adds someone) · the own thread of an agent you administer (`Agent.is_admin`:
  its owner, a workspace owner, or an `AgentAdmin`) · a **session review grant** · an
  admin of the box a discovered session is on (`RunnerAdmin`; read-only, may move it off
  the box — `access.may_move`) · a **project share**: the box owner shared their laptop
  sessions in a project's repo with that project's team, and you are on it (its
  `owner_user` or a `ProjectParticipant`; never an agent's own login, never a sensitive
  session; editor, may pull). Per person × project, opt-in, revocable, each change an
  audited setting (`ProjectSessionShare`, `canopy_sessions/sharing.py`,
  `/api/me/session-sharing/`, Settings → Account). Who sees what per
  channel: `access-sessions-and-people.md` § Who can see a session.
- **Pulling from a machine is `access.may_pull`.** Asking a runner to stream a session
  (`POST /{id}/attach`, and the chat socket's connect) or to ship its history
  (`POST /{id}/backfill`) is for the box owner and anyone the rule lets write in it.
  Everyone else who can read it — a coach, a runner admin, a viewer, an agent login not
  explicitly added — reads the stored rows: their attach is a no-op and their backfill a 403.
- **A session review grant lets one agent's own login read one person's sessions** — a
  coach (`SessionReviewGrant`; leg 5, `access._review_grant_q`; one session asked from its page is no leg: `SessionReviewAsk`, `access-sessions-and-people.md`). Only the person makes
  or ends it, for an agent of a workspace they are in (`/api/people/me/session-reviewers/`,
  `apps/contacts/session_reviewers.py`), each act on their memory audit
  (`sessionReviewer.granted` / `.revoked`); revoking drops the agent's next read. "Their
  sessions" are the chats they started and the runner-discovered sessions on a box they
  own that are not an agent's own work (`access.sessions_of_q`). Read-only (`role_for`
  gives the agent a viewer), never a sensitive conversation (any runner requirement keeps
  it out, failing closed), and never anyone but that agent's login — its admins, other
  agents and co-tenants gain nothing. **The read answers to the turn's asker**
  (`access.review_grantors`, the one rule; the asker is `harness.turn_asker.standing`,
  worked out from the server's `Turn.initiator_*` and its stamped `on_behalf_of`, never
  from anything the agent sends): the agent's login reads only inside a running turn of
  that agent (its per-turn or session login, over REST, MCP alike). A turn a verified
  person asked reads that person's grant alone ("coach me" is me only); a turn nobody
  asked (a schedule or trigger no person owns, or a dispatch from such a turn, which the
  server stamps `unattended_dispatch`) or one an administrator of the agent asked reads
  every grantor's, the coaching sweep; a turn canopy started on a person's account (a
  schedule an editor made, an agent task's follow-up) answers to that person; a contact's
  or unverified user's turn and anything dispatched from one, a stored token, a browser
  session, a socket, a finished or confined turn reads nothing by grant. `of_user` only
  narrows what this allows, and the roster below is cut by the same rule. The agent lists who let it in
  (`GET /api/people/session-reviewers/granted-to-me/`) and one person's sessions with
  `GET /api/canopy-sessions/?of_user=<id>`. Which agent canopy offers as the coach (the
  first-run checkbox, the Settings switch) is `settings.COACHING_AGENT_SLUG`, offered only
  where that agent is in a workspace the person is in or may ask to join.
  The one other reader of the list is the agent's own turn: the caller token of a verified user who asked that turn may `GET /api/people/me/session-reviewers/` for their own record and sees only whether THAT agent is among their reviewers (no pickable agents, no coach offer); POST and DELETE stay the person's own (browser session or PAT), and a contact's, unverified, or another user's token reads nothing.
- Acting needs participant role owner/editor; a `viewer` reads; sharing is the owner's
  (`/participants`, the chat header's People control).
- Every entry point calls it: REST, the chat socket, attachments, bulk reset, MCP, Slack share, push
  recipients. **Opening a chat is never a grant** — the socket must not auto-join anyone;
  participation comes only from creating, being shared, being in the Slack channel a
  session lives in, being on an email thread its proven sender addressed to you (`access.md` §
  Who is in a session), or being in the conversation a session was dispatched from.
- **A dispatched session inherits its parent's people.** A session created with a parent
  (`X-Canopy-Parent-Session`/`-Turn`, or a payload `parent`) that its creator can read gets
  the parent's participants who are members of its workspace — owner becomes editor, a
  viewer stays a viewer (`services._inherit_participants`) — and the parent turn's
  initiator joins as editor when that turn belongs to the parent, since a session started
  in emdash has no participant rows to copy. An agent starting work for the
  person it is talking to therefore never hides that work from them. An agent's own
  login cannot read a web chat a person opened with it, so the parent also counts as
  reachable when that login is running a live turn in it (`access.own_turn_session`
  on the parent turn) — the same people join, nothing else; a finished or queued
  turn, a turn in another session or another agent's turn brings no one. The
  runner-made session for a dispatched turn (`session_sync._adopt_turn_parent`)
  goes through the same rule.
- `tests/test_session_acl.py` asserts every entry point gives the same answer for every (person,
  session shape), and fails on a `SessionParticipant` writer outside its allowlist.
- Where a session is filed and moving it: `roles.md` → "Which workspace a session lives in".

## Artifact visibility and uploaded HTML

Two visibilities: `private` (signed-in workspace members) and `link`.

- **Walkthroughs, `link`:** anonymous read (detail GET + content stream) requires
  `?t=<share_token>`; a missing or wrong token 404s exactly like private, so existence never
  leaks. `share_url` is returned to owners only; `POST /api/walkthroughs/{wid}/rotate-token`
  re-mints the token, killing shared links without touching the artifact.
- **Reviews, `link`:** tokenless — the URL is enough to read. Submitting a decision and every
  mutation require a login.
- **`private`** 404s to anonymous for both. A signed-out visitor on a private walkthrough's
  VIEW url is routed to sign-in rather than shown a bare error.
- `PATCH /api/ddd/narratives/{slug}/visibility/` cascades to every artifact and review under a
  narrative.
- **Login middleware allowlist:** anonymous visitors reach only the scoped viewers, the
  per-artifact read APIs and the flat-artifact 404; every other `/w/` path is the
  authenticated tenant shell. Enforcement: [roles.md § The first tier has no account at all](roles.md#the-first-tier-has-no-account-at-all).
- **Uploaded HTML is always sandboxed (opaque origin).** `/w/<ws>/walkthrough/<id>/content`
  sends `Content-Security-Policy: sandbox allow-scripts allow-popups
  allow-popups-to-escape-sandbox allow-downloads` + `nosniff` for every non-video walkthrough
  (`apps/walkthroughs/streaming.py::SANDBOX_CSP`), which holds even when the URL is opened
  directly. Every iframe of uploaded content uses `UPLOADED_CONTENT_SANDBOX`
  (`frontend/src/lib/uploadedContentSandbox.ts`). **Never add `allow-same-origin`**: beside
  `allow-scripts` on a same-origin URL it is no sandbox, and a deck's script would run as the
  viewer against the cookie-authenticated API.
- Specs: `docs/superpowers/specs/2026-07-13-walkthrough-share-token-revival-design.md`
  (walkthroughs), `docs/superpowers/specs/2026-06-08-tokenless-narrative-visibility-design.md`
  (reviews).

## Rolling sessions

`SESSION_SAVE_EVERY_REQUEST = True` (`config/settings/base.py`) so the two-week expiry rolls
with use (an installed PWA would otherwise log out every fortnight). Consequences:

- Every request writes the session.
- **Any view that catches `IntegrityError` must wrap the failing write in its own
  `transaction.atomic()` savepoint**, or `SessionMiddleware`'s write on the way out hits a
  poisoned transaction instead of the intended 409/handled error. Pattern: the
  idempotency-key creates in `apps/harness/services.py` (`enqueue_turn`) and `channels/email.py` (the blocked-contact
  turn write).

## Content retention (`apps/retention`)

Built and **off** unless `CANOPY_RETENTION_ENFORCE=true`.

- `RetentionRule`: workspace / kind (`chat` · `turn` · `shared`) / source (the `Turn.origin`
  vocabulary + `emdash`) / principal (`member` · `contact` · `agent` · `system`) / agent →
  `keep_days` (blank = forever). Resolution: nearest workspace up the tree, then the most
  specific rule, then the shorter retention; no match = keep forever.
- A turn is **scrubbed, never deleted**: prompt, result note, report summary, ledger, raw
  transcript and Slack status text go; the row, `origin_ref` and `report_title` stay
  (`/activity`, KPIs and the schedule lookup read them).
- A chat is one unit: its turns resolve by the SESSION's attributes and its expired messages
  go as a prefix. **`Session.retention_floor_index` stops a runner re-ship (backfill, reset)
  writing purged history back**; `persist_transcript_rows` and `_next_index`
  (`apps/canopy_sessions/services.py`) enforce it, so any new `Message` writer must go through
  them.
- Shared transcripts are deleted. Deployment-wide rules (no workspace — the only rules that
  govern shared transcripts) are Django-admin only.
- `manage.py retention_sweep` dry-runs; `--apply` purges. With enforcement on, the off-request
  clock (`apps/common/ticks.py`) sweeps at most hourly.
- Per-workspace rules: `/w/:ws/settings/retention`, `GET|POST|PUT|DELETE
  /api/workspaces/{slug}/retention…`. Any member reads; `retention.manage` (admin+) changes;
  every change goes to the event log; a preview counts what the rules would delete.
- **A sensitive conversation is held to its own clock.** One that requires sensitive-data runners
  (`apps.harness.sensitivity.is_sensitive`) is purged after `CANOPY_SENSITIVE_KEEP_DAYS`
  (30) by the same heartbeat sweep **whatever `CANOPY_RETENTION_ENFORCE` says**
  (`retention.services._sweep_sensitive`, a forced rule tallied as `#0`); a shorter
  workspace rule still wins when enforcement is on.
- Spec: `docs/superpowers/specs/2026-10-05-content-retention-design.md`.

## Deleting your own session's content

`DELETE /api/canopy-sessions/{id}/content`, the "Delete this session's content" item in a session's
menu (`canopy_sessions/content_deletion.py`). Retention is a workspace's clock; this is one person's
act, and it does not depend on a rule, on enforcement being on, or on being coached.

- **Who: the person whose session it is, nobody else** (`access.owns_words`): the chat they started,
  or a session discovered on a box they own that is not an agent's own work, also once it has fallen
  off that box's report. No role overrides it: not a workspace owner, an agent admin or a runner admin.
- **As themselves** (`harness.acting_person`, and `delegation.acting_app`): 403 for an agent's login,
  any token issued to a turn (a turn's login, a caller token, an act-as-you token, which also lists it
  in `act_as.EXCLUDED_PATTERNS`), a runner, and a site's token for its visitor. Their own browser
  session, PAT or OAuth token may. It does not ask for a passkey.
- **Only while it is theirs alone** (`access.others_in_it`), else 409: nobody else added, no channel
  thread it lives in, no line anyone else wrote and no turn anyone else asked for, including people
  since removed. A contact's conversation is never anyone's to delete this way.
- **Only while it is quiet** (`content_deletion.blocker`), else 409: no open turn, its engine not
  working, no move under way, no fork still holding a copy. Rechecked under the row locks.
- **It leaves a metadata row.** The session is archived and empty, stays readable by those who could
  read it (its row, not its words), and takes nothing more. A coach's legs stop reading it at all
  (`_review_grant_q`, `_review_ask_q`; a live ask is revoked), and a grant or ask given later does
  not bring it back. The chat a review ask opened is a conversation of its own, deleted the same way. The tombstone and what honours
  it: chat-sessions-recording-and-transfer.md § A person deleting their own session.
- **What it cannot reach**, and the confirm dialog and route docs say so: the transcript on the person's
  computer; what a coach already read into its notes, logs and mailbox; any chat a review ask opened (a
  conversation of its own, deleted the same way); words posted to Slack; a `/canopy:share-session`
  link (`session_sharing`, its own delete); facts on their memory page; artifacts made from the session
  (a walkthrough, a review, a storyboard); and sessions or turns this conversation dispatched to, which
  are other conversations with their own records.
- A failed delete of a file's stored object answers 503: objects already removed stay removed, no row
  has changed, and a retry finishes the rest.
- Recorded as `session.content_deleted` in the workspace's event log and `session.contentDeleted` on the
  person's own audit, ids only. It is not a setting and not an analytics fact.
