# Tenancy: who can see a session, artifacts, retention and deletion

The chat-session ACL, artifact visibility and uploaded HTML, rolling sessions, content retention and a person deleting their own session's content. Part of the [tenancy](tenancy.md) topic; the index there lists every part.

## The chat-session ACL (CI-enforced)

One module decides who may see and act on a chat session: `apps/canopy_sessions/access.py`. Its module docstring and `access.py::visible_session_q` state the tenant gate, every leg, reach, writing, sharing and pulling; read those, not a copy. Who sees what per channel: `access-sessions-and-people.md` § Who can see a session. Facts the code does not make obvious:

- **Tenant first, then a leg; never the whole workspace.** A runner-discovered laptop session is single-player until its box owner adds someone (`access.py::_box_owner_q`). A box owner's project share (`ProjectSessionShare`, `canopy_sessions/sharing.py`) is per person and project, revocable, no new share can be made (existing ones hold until their person stops them), never an agent's own login, never a sensitive session (`access.py::_project_share_q`).
- **Pulling from a machine is `access.may_pull`.** Everyone else who can read a session (a coach, a runner admin, a viewer, an agent login not explicitly added) reads the stored rows: their attach is a no-op and their backfill a 403.
- **A session review grant lets one agent's own login read one person's sessions** (`SessionReviewGrant`, `access.py::_review_grant_q`). Only the person makes or ends it, for an agent of a workspace they are in (`apps/contacts/session_reviewers.py`), each act on their memory audit. It is read-only (`access.py::role_for` gives a viewer), never a sensitive conversation (any runner requirement keeps it out, failing closed: `_not_sensitive_q`), never a session whose words were deleted, and never anyone but that agent's login: its admins, other agents and co-tenants gain nothing.
- **The read answers to the turn's asker** (`access.py::review_grantors`, the one rule; the asker is `harness/turn_asker.py::standing`, worked out from the server's own turn record, never from anything the agent sends). A stored token, a browser session, a socket, a finished or confined turn, and a contact's or unverified user's turn read nothing by grant. The roster the agent lists is cut by the same rule (`apps/contacts/session_reviewers.py::granted_to`; `of_user` only narrows).
- **One session asked from its page is no leg:** `SessionReviewAsk`, `access-sessions-and-people.md` § Asking a coach to review one session.
- **The one grant read a turn's caller token reaches:** `GET /api/people/me/session-reviewers/` shows it only whether THAT agent is among the asker's reviewers (`apps/contacts/people_api.py::list_my_session_reviewers`); POST and DELETE stay the person's own.
- **Opening a chat is never a grant:** the socket must not auto-join anyone. Participation comes only from creating, being shared, being in the Slack channel a session lives in, being on an email thread its proven sender addressed to you (`access-sessions-and-people.md` § Who is in a session), or being in the conversation a session was dispatched from.
- **A dispatched session inherits its parent's people** only from a parent its creator can read (or, for an agent's login, the live turn it is running in that parent), and only for a private parent when the creator is in it: `canopy_sessions/services.py::_inherit_participants`, `access.py::own_turn_session`. The runner-made session for a dispatched turn goes through the same rule (`harness/session_sync.py::_adopt_turn_parent`).
- `tests/test_session_acl.py` asserts every entry point gives the same answer for every (person, session shape), and fails on a `SessionParticipant` writer outside its allowlist.
- Where a session is filed and moving it: `roles.md` § Which workspace a session lives in — and moving it.

## Artifact visibility and uploaded HTML

Two visibilities: `private` (signed-in members of the artifact's workspace) and `link`.

- **Walkthroughs, `link`:** anonymous read needs `?t=<share_token>`; a missing or wrong token 404s exactly like private, so existence never leaks (`apps/walkthroughs/models.py::Walkthrough.token_matches`, `readable_by`). A row with no workspace is readable only by its token. Rotating the token kills shared links without touching the artifact.
- **Reviews, `link`:** tokenless read, the URL is enough (`apps/reviews/api.py::_can_read`). Submitting a decision needs an editor in the review's workspace (`_can_write`); the share token only lets an unauthenticated reviewer submit suggestions (`_token_ok`, `suggest_review`).
- **`private`** 404s to anonymous for both.
- `PATCH /api/runs/narratives/{slug}/visibility/` cascades to every artifact and review under a narrative (`apps/runs/api.py::set_narrative_visibility`).
- **Login middleware allowlist:** anonymous visitors reach only the scoped viewers, the per-artifact read APIs and the flat-artifact 404; every other `/w/` path is the authenticated tenant shell. Enforcement: [roles.md § The first tier has no account at all](roles.md#the-first-tier-has-no-account-at-all).
- **Uploaded HTML is always sandboxed (opaque origin)** for every non-video walkthrough, even when its URL is opened directly (`apps/walkthroughs/streaming.py::SANDBOX_CSP`). Every iframe of uploaded content uses `UPLOADED_CONTENT_SANDBOX` (`frontend/src/lib/uploadedContentSandbox.ts`). **Never add `allow-same-origin`**: beside `allow-scripts` on a same-origin URL it is no sandbox, and a deck's script would run as the viewer against the cookie-authenticated API.

## Rolling sessions

`SESSION_SAVE_EVERY_REQUEST = True` (`config/settings/base.py`) so the two-week expiry rolls with use. So every request writes the session, and **any view that catches `IntegrityError` must wrap the failing write in its own `transaction.atomic()` savepoint**, or `SessionMiddleware`'s write on the way out hits a poisoned transaction instead of the intended 409/handled error. Examples: the idempotency-key create in `apps/harness/services.py::enqueue_turn` and the blocked-contact turn write in `apps/harness/channels/email.py`.

## Content retention (`apps/retention`)

Rule resolution is `apps/retention/policy.py` (module docstring); what each kind drops and keeps, and the sensitive clock, is `apps/retention/services.py` (module docstring); rule fields are `RetentionRule` in `models.py`. Per-workspace rules are managed at `/api/workspaces/{slug}/retention…` (`apps/retention/api.py`, gate `retention.manage`). Enforcement is off unless `CANOPY_RETENTION_ENFORCE` is true; `manage.py retention_sweep` dry-runs and `--apply` purges either way. Gotchas:

- A turn is **scrubbed, never deleted**: the row, `origin_ref` and `report_title` stay because `/activity`, KPIs and the schedule lookup read them.
- A chat is one unit: its turns resolve by the SESSION's attributes and its expired messages go as a prefix. **`Session.retention_floor_index` stops a runner re-ship (backfill, reset) writing purged history back**; `persist_transcript_rows` and `_next_index` (`apps/canopy_sessions/transcript_ingest.py`) enforce it, so any new `Message` writer must go through them.
- Shared transcripts are deleted. Deployment-wide rules (no workspace) are Django-admin only and are the only rules that govern shared transcripts.
- **A sensitive conversation is held to its own clock** whatever `CANOPY_RETENTION_ENFORCE` says (`retention/services.py::_sweep_sensitive`, a forced rule tallied as `#0`); a shorter workspace rule still wins when enforcement is on. A turn is sensitive through any ancestor, so forced retention reaches a session-less sensitive turn.

## Deleting your own session's content

`DELETE /api/canopy-sessions/{id}/content` (`apps/canopy_sessions/api_lifecycle.py`; what goes, what stays and why it is a tombstone: `canopy_sessions/content_deletion.py` module docstring). Retention is a workspace's clock; this is one person's act, and it does not depend on a rule, on enforcement being on, or on being coached.

- **Who: the person whose session it is, nobody else** (`access.py::owns_words`). No role overrides it: not a workspace owner, an agent admin or a runner admin. A contact's conversation is never anyone's to delete this way.
- **As themselves** (`harness/acting_person.py::refusal`, `embedding/delegation.py::acting_app`): 403 for an agent's login, any token issued to a turn (a turn's login, a caller token, an act-as-you token, also listed in `harness/act_as.py::EXCLUDED_PATTERNS`), a runner, and a site's token for its visitor. It does not ask for a passkey.
- **Only while it is theirs alone** (`access.py::others_in_it`), else 409, including people since removed.
- **Only while it is quiet** (`content_deletion.py::blocker`), else 409; rechecked under the row locks.
- **It leaves a metadata row**, archived and empty. A coach stops reading it at all (`access.py::_review_grant_q`; the asked read in `api_read.py` refuses it; a live `SessionReviewAsk` is revoked) and a grant or ask given later does not bring it back. What honours the tombstone: `chat-sessions-recording-and-transfer.md` § A person deleting their own session: the tombstone.
- A failed delete of a file's stored object answers 503: objects already removed stay removed, no row has changed, and a retry finishes the rest.
- Recorded as `session.content_deleted` (`canopy_sessions/lifecycle.py`) in the workspace's event log and `session.contentDeleted` (`contacts/session_reviewers.py`) on the person's own audit, ids only. It is not a setting and not an analytics fact.
