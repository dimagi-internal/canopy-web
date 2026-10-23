# Tenancy, identity and visibility

How canopy-web decides **which tenant a row belongs to, who is asking, and what an anonymous
reader may see**: the workspace as tenant anchor, the sole workspace authorizer, how writes
pick a workspace, agent rows as instances, the two kinds of principal (user, contact), the
chat-session ACL, artifact visibility and the uploaded-HTML sandbox, sign-in and sessions,
and content retention.

What each *role* may do is not here: the glossary and the agent-turn rule are in
[`access.md`](access.md), and enforcement per surface (the permission table, nesting, read
scope, runner enrollment, session filing/moving, product ACL shapes) is in
[`roles.md`](roles.md). The MCP surface's auth is in [`mcp-surface.md`](mcp-surface.md);
the host-grant chain is in [`sdk/python/README.md` § Host grant contract v1](../../sdk/python/README.md#host-grant-contract-v1).

## The tenant: `Workspace`

- `Workspace` (`apps/workspaces`) anchors every surface that owns tenant data. Members hold
  `viewer < editor < admin < owner`, plus email invites and access requests (`access.md` →
  "Getting into a workspace").
- Tenant surfaces live under `/w/:workspace/` (browser) and `/api/w/{ws}/` (API).
  `apps/api/tenancy.py::WorkspaceResolveMiddleware` gates membership on the pinned form,
  then strips the prefix and reroutes to the flat mount, so the OpenAPI schema stays single.
  The flat `/api/…` routes remain a compat shim; how an unpinned read or write is scoped is
  below and in `roles.md` → "A read that names no workspace stays inside one organisation".
- Rows carrying a tenant FK: `agents`, `agent_runs`, and the product roots `walkthroughs`,
  `reviews`, `shareouts` (plus storyboards, issues, sessions, events …). Authenticated lists
  filter by `wsvc.request_workspace_slugs(request)` (the read scope).
- **`Agent.workspace` is NOT NULL** (`agents/0013`). This is a security invariant: a nullable
  tenant FK invites `if row.workspace_id and <membership check>`, which reads a NULL tenant as
  *allow*. Don't re-nullable it, and never write a NULL-means-allow leg in any tenancy
  predicate — a row with no workspace is visible to nobody (`roles.md`).
- `Turn` has **no** workspace FK; it derives its tenant one hop away (`turn.agent.workspace`,
  or its session's). `InboundMailbox` does the same through its agent.
- `Runner.workspace` (nullable, `PROTECT`) records where a box LIVES, not whom it works for —
  see "Runners and tenancy" below.

Design record: `docs/superpowers/specs/2026-06-30-workspace-multi-tenancy-design.md`.

## One authorizer for workspace access (CI-enforced)

`apps/workspaces/services.py` is the only module that decides "is this user allowed in this
workspace?": `member_role`, `has_role_at_least`, `is_member`, `membership` (the row, for the
admin surface that acts on it; it also resolves inherited ownership). Every other gate goes
through these or through `perms.can(...)` (`roles.md`).

`tests/test_workspace_authorizer_is_sole_gate.py` walks the AST of every production module
and fails on a `WorkspaceMembership` **read** filtered by **both** a workspace key and a user
key outside that file — that pair is the signature of an access decision.

- Escape hatch: an inline `# authz-exempt: <why>`, for a question about someone ELSE (e.g. is
  the TARGET already a member — `apps/agents/api.py`, `apps/workspaces/system_accounts.py`).
- Deliberately narrow: user-only filters (my workspaces) and workspace-only filters (who is in
  this) are listings; writes are not decisions. Membership creation has its own guards
  (`tests/test_no_implicit_enrolment.py`, `apps/workspaces/tests/test_access_requests.py`).

## A write names its workspace

- `wsvc.creation_workspace(request)` resolves a create's target: the pinned `/api/w/{ws}/`
  workspace, else the caller's SOLE workspace, else nothing. On nothing, every create 422s
  with `wsvc.creation_refusal(request)`, which names the caller's workspaces. There is no
  fallback to an org default — a multi-workspace caller posting flat (a PAT script, the canopy
  CLI without `CANOPY_WEB_WORKSPACE`) is refused on purpose.
- Resolving is not permission: each create still checks `CONTENT_WRITE` on what came back,
  including a defaulted target.
- Absolute links are built with `links.scoped_url(workspace, path)` on
  `CANOPY_PUBLIC_BASE_URL`, never `request.build_absolute_uri` — an in-process MCP call has no
  real Host and would mint `https://localhost/…`. Every URL the API returns is under
  `/w/<workspace>/`; a row with no workspace has no address.
- Pinned by `tests/test_nothing_outside_a_workspace.py`.

## Runners and tenancy

A runner's tenant is its pairing owner's workspaces it is enrolled in, never
`Runner.workspace` (`apps/harness/routing.py::runner_tenant_slugs`; a NULL owner fails closed) —
the rule, its claim/schedule parity test and the routing hint:
[runners.md § Which workspaces a runner serves](runners.md#which-workspaces-a-runner-serves-the-tenant-gate).

- **One cloud runner, one tree.** A `kind=cloud` runner enrolls in one root's
  tree only (422 otherwise); a box serving two trees runs a runner (an isolation
  unit) per tree. A laptop may span trees and says so on its runner row.
  → [runners.md § Isolation units](runners.md#isolation-units-one-runner-per-unit-on-a-cloud-box)
- **On the box, a unit is a Unix user.** Each unit's secrets, logins, mail tokens,
  transcripts and worktrees are its user's (home 0700); IMDS is root's, so a unit
  cannot act as the box; its device key and its KMS ECDH key are its own, so one
  unit's token or proof never reads another unit's agents, and an agent's vault key
  is wrapped only to the units that run it. Inside a unit, agents share a user: the
  unit, not a rail, is the boundary. Root on the box is trusted.
  → [runners.md § Units on the box](runners.md#units-on-the-box-runnerunits)
- **Sessions a runner reports are filed inside its tenant.** A new agent-less
  session names its agent only among agents in `runner_tenant_slugs(runner)`
  (`harness.session_sync._agent_for_project`: `Agent.slug` is global). A session whose
  project names an agent OUTSIDE that tenant is filed nowhere — not in the agent's
  tree, and not in the runner's as a plain project (`session_home` → None; the
  report's `skipped` counts it, missing-sessions omits it) — and the owner's default session
  workspace applies only when it is in that tenant, else the runner's workspace
  (`canopy_sessions.filing.filing_workspace`). Tests: `tests/test_session_filing.py`.

## An agent row is an instance; its definition is the repo

- An `Agent` row is an INSTANCE in one workspace: board, credentials, turns, runner
  assignments and mailbox hang off it. Persona, skills and config live in the git repo at
  `repo_url` @ `repo_ref` (`apps/agents/definition.py`). There is no `AgentInstance` model and
  the definition is not a database concept — `AgentSkill` is replaced wholesale on each
  publish so it mirrors the repo.
- Sharing = two instances pointing at the same repo; forking = pointing elsewhere.
  `definition_key()` normalises the URL (`.git`, scp-style, case, trailing slash, non-default
  ref) — a normalisation miss silently reports related agents as unrelated. `siblings()` is
  the one deliberately cross-tenant query and returns instances only, never their contents.
- **`Agent.slug` is globally unique** (`SlugField(unique=True)`): the slug is the agent's
  Slack slash command (`/hal`), which canopy registers in the Slack app itself
  (`apps/slack/commands.py`). Don't relax it to `UniqueConstraint(["workspace", "slug"])`.
  Because it is global, `POST /api/agents/` answers a non-member of an existing agent's
  workspace with 404, never 403 (`roles.md`).
- Planned direction (shared definition, owned instance): see
  `docs/superpowers/specs/2026-09-12-agent-instances-and-the-acl-design.md`.
- **Moving an agent is one operation** (`apps/agents/move.py`, `POST /api/agents/{slug}/move`;
  the upsert's `workspace` goes through it too). Several agent-scoped tables carry their own
  `workspace` FK, so changing `Agent.workspace` alone strands the agent's history in the old
  tenant. A table that gains a `workspace` FK and belongs to an agent must add a move
  participant (`move.register`; a product app registers from its `AppConfig.ready()`, as
  `apps/mail/apps.py` does) — otherwise moved agents leave those rows behind. People records,
  a person's grants and audit stay where they are, by design.

## Two principals: users and contacts

They are two **types**, not one shape with a flag. A contact has no membership, so one
arriving through the user lookup would be an *unspecified* permission — the NULL-means-allow
failure again.

### Users

- **Sign-in:** Google OAuth via django-allauth, restricted to `AUTH_ALLOWED_EMAIL_DOMAIN`
  (comma-separated; default `dimagi.com` in `config/settings/base.py`, deployed as
  `dimagi.com,dimagi-ai.com,dimagi-associate.com` in `deploy/aws/canopy-web.cfn.yaml`).
  A new login domain also needs the Google consent screen to allow it.
- **Personal access tokens** (`apps/tokens/`) authenticate machine callers with
  `Authorization: Bearer <raw>`; `apps.tokens.middleware.BearerTokenAuthMiddleware` resolves
  them after `AuthenticationMiddleware` and before `LoginRequiredMiddleware`. An MCP OAuth
  access token is a `PersonalToken` too. A `DelegatedToken` (a connected site acting for a
  visitor who has an account) resolves to a `User`; every surface applies that user's ACL.
  A token acts with exactly its person's role (`roles.md` → "A token can do whatever its
  person can").
- **One resolver, and a prefix per kind.** REST, MCP and the WebSocket handshake all resolve a
  bearer with `apps/tokens/principal.py::resolve_bearer`. Tokens name their kind: `cpat_`
  (personal access, incl. MCP OAuth access tokens), `cdt_` (delegated), `ccon_` (contact),
  `cct_` (caller), `crt_` (runner), `chk_` (chat key), `hcpat_`/`hcprt_` (HCP). A prefixed
  token is looked up in its own table only, and refused by a door that does not take its
  kind. Tokens minted before the `cpat_`/`cdt_`/`ccon_` prefixes are 43 bare characters and
  still resolve by trying each kind. The prefixes are what `.gitleaks.toml` (`canopy-token`)
  and the transcript scrubber (`apps/session_sharing/redact.py`) recognise.
- `POST /api/debug/mint-session/` (`apps/common/views_debug.py`) trades a PAT for a
  short-lived browser session (the macOS menubar's web view). A minted session is still a
  machine (`views_debug.is_machine`), and the MCP OAuth consent page refuses it, so a token
  cannot sign an MCP client in on its own.
- `apps/common/middleware.py::LoginRequiredMiddleware` is **default-deny** with an allowlist;
  the public surfaces and their two-halves rule are in `roles.md` → "The first tier has no
  account at all".

### Contacts

- `ContactToken` (`apps/tokens/models.py`) is its own model. `BearerTokenAuthMiddleware` sets
  `request.contact` and leaves the request **anonymous**, so `LoginRequiredMiddleware` refuses
  everything outside the one prefix opened to it.
- **`/api/contact/` is the entire contact surface** — one tuple entry in
  `apps/common/middleware.py`, not a per-view decision. The trailing slash is load-bearing:
  `/api/contacts/` is the tenant-admin list and must not match.
- `visible_session_q` (users) and `contact_session_q` (contacts) in
  `apps/canopy_sessions/access.py` are disjoint predicates.
- A host vouches for a visitor with a **signed assertion** (`apps/embedding/assertions.py`:
  asymmetric algorithms only, `aud` enforced, `exp` ≤ 120s, single-use `jti` failing closed).
  canopy holds only the host's public key. Arrival resolves to an account the person already
  has that is a member of the site's workspace, or to a contact — it never creates a user or a
  membership. **Don't add a shared-secret-plus-email door**; a signature is the only way a
  site speaks for somebody.
- The other direction — canopy calling the site's tools as the visitor — is the host grant
  chain: the host issues an ID-JAG at arrival, canopy redeems it (`apps/host_grants/host_grants.py`,
  stored DPoP-bound in `HostGrant`) and attaches it server-side in `site_tools`/`site_call`
  (`apps/host_grants/host_gateway.py`). No runner holds it; an expired grant is a refusal, never a
  fallback to the agent's credential. canopy's client identity is the CIMD at
  `/oauth/client.json` + `/oauth/jwks.json` (keys `CANOPY_OAUTH_CLIENT_KEY` /
  `CANOPY_OAUTH_DPOP_KEY`). Contract: [`sdk/python/README.md` § Host grant contract v1](../../sdk/python/README.md#host-grant-contract-v1).
- Guarded by `tests/test_contact_surface_is_bounded.py`.

## Contacts (`apps/contacts/`): a person canopy knows who is not a member

- A `Contact` lets an agent remember an outside correspondent (email sender, Slack guest,
  widget visitor) and lets routing depend on who they are, without a `WorkspaceMembership`.
- **Recording a contact grants nothing** — not the workspace, not the agent. `promote_to_user`
  (`apps/contacts/services.py`) links a contact to a user and still grants no membership.
  Asserted by `tests/test_contacts.py`.
- Per-tenant by design: the same human known to two workspaces is two contacts; merging would
  leak one tenant's dealings into another.
- **Email identity is an assertion.** `From:` is forgeable, so `apps/contacts/email_auth.py`
  grades the receiver's `Authentication-Results`: `dmarc` (or `dkim_aligned`, a DKIM
  signature by the `From:` domain itself) > `dkim` > `spf` > `none` (graded, not boolean).
  Only OUR receiver's header is read, selected by `authserv-id`; an unknown or
  absent one fails closed. `dmarc=pass` proves the domain, not the human.
- Grades are a high-water mark (a forwarded message must not downgrade a correspondent), with
  the latest grade kept beside it so a drop stays visible.
- Recorded best-effort from the email door (`apps/harness/channels/email.py`) — mail
  is answered even when the bookkeeping fails.
- What agents learn about a person (`PersonFact`, HCP): `access.md` → "What agents know about
  a person".

## The chat-session ACL (CI-enforced)

One module decides who may see and act on a chat session: `apps/canopy_sessions/access.py`.

- **Tenant first** (member of `Session.workspace`), then one of four legs: creator ·
  explicit participant (`SessionParticipant`) · runner-discovered emdash session · the own
  thread of an agent you administer (`Agent.is_admin`: its owner, a workspace owner, or an
  `AgentAdmin`).
- Acting needs participant role owner/editor; a `viewer` reads; sharing is the owner's
  (`/participants`, the chat header's People control).
- Every door calls it: REST, the chat socket, attachments, bulk reset, MCP, Slack share, push
  recipients. **Opening a chat is never a grant** — the socket must not auto-join anyone;
  participation comes only from creating, being shared, joining a Slack thread you can
  already read, or being in the conversation a session was dispatched from.
- **A dispatched session inherits its parent's people.** A session created with a parent
  (`X-Canopy-Parent-Session`/`-Turn`, or a payload `parent`) that its creator can read gets
  the parent's participants who are members of its workspace — owner becomes editor, a
  viewer stays a viewer (`services._inherit_participants`) — and the parent turn's
  initiator joins as editor when that turn belongs to the parent, since a session started
  in emdash has no participant rows to copy. An agent starting work for the
  person it is talking to therefore never hides that work from them.
- `tests/test_session_acl.py` asserts every door gives the same answer for every (person,
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
- **A sensitive conversation is held to its own clock.** One that requires ZDR runners
  (`apps.harness.sensitivity.is_sensitive`) is purged after `CANOPY_SENSITIVE_KEEP_DAYS`
  (30) by the same heartbeat sweep **whatever `CANOPY_RETENTION_ENFORCE` says**
  (`retention.services._sweep_sensitive`, a forced rule tallied as `#0`); a shorter
  workspace rule still wins when enforcement is on.
- Spec: `docs/superpowers/specs/2026-10-05-content-retention-design.md`.

## Sensitive conversations (`apps/harness/sensitivity.py`)

`is_sensitive(session | turn)` — the requirements (`runner_requirements.requirements_of`,
so a child, dispatched or forked turn inherits it) include `zdr`, or cannot be read. It is
THE predicate; every surface below asks it, one test each in
`tests/test_sensitive_storage.py` (spec `2026-10-22-phi-agents-on-zdr-runners` §4):

- **Stored less:** no `TurnTranscript` (`ledger.append_transcript` answers the runner with
  an empty success); a `tool_end`/`tool_result` event, a `TOOL_RESULT` `Message` and its
  live frame keep `{tool_use_id, is_error, withheld}`, never the result
  (`sensitivity.strip_result`, in `ledger.append_events`, `persist_transcript_rows` and
  `post_session_stream`).
- **Metadata in every readable column:** every stored event and every `Message`'s
  `plaintext`/`content` (the person's, the agent's, a tool call's) keeps kind, tool name,
  argument names, ids, error flags, size and hash only (`sensitivity.redact_payload`). The
  prompt is wiped at the turn's end (`closing.close_turn`), the result note withheld, and
  there is no owner-ask line, live tail (`RunnerBinding.tail`) or autotitle.
- **The conversation, sealed for its own people:** the words of a `Message` (not a tool's
  result) are kept in `Message.sealed`, encrypted (`sensitivity.seal`, from `send.py` and
  `transcript_ingest`; `apps/common/encryption.py`, so a database read, a backup or a shell
  sees ciphertext), so a person can come back to it and a participant can join it. ONE
  gate reads them, `sensitivity.reads_content`: the conversation's own people (its creator,
  its participants, its contact) on its own chat surfaces (a browser session, the widget or
  SDK's contact or site token; never MCP, a PAT, OAuth or a minted session). It is asked by
  the chat REST reads (`GET /{id}`, `/messages`), the contact's `/messages`, the socket
  snapshot and an MCP Apps View's `tool_input`, through `sensitivity.readable`. Every other
  reader — search, human inputs, a session review, an agent's or the fleet's reads, an
  admin who is not in it — reads the columns, so a new reader is closed until it asks.
  The reply reaches the asker the one way every turn's does: its transcript rows (a ZDR box
  ships a confined turn's before deleting it).
- **Live and log doors ask the same gate:** the chat socket (`SessionConsumer`), whose live
  frames carry the words, admits only `reads_content`; the turn socket's tail gets the stored
  metadata; and `turn_access.can_read_turn_content` is False for a sensitive turn, so no turn
  read (REST, MCP, the board, the unclaimable report, the caller envelope) returns its
  prompt, note or report while it runs. The `TURN_CREATED` log line keeps no prompt head.
- **Only its people hand it on:** adding a participant and forking (which copies the
  sealed words) need `reads_content` too, so an agent's admin who may open a thread they
  are not in cannot add themselves or copy it.
- **The agent's memory:** every turn of a sensitive conversation runs confined, is never
  resumed and is deleted on the box, so the caller envelope carries the conversation so far
  (`conversation.history`, `sensitivity.history`: the person's lines and the agent's
  replies, never a tool's) to the box that runs the next turn; the canopy plugin's
  `UserPromptSubmit` hook puts it in context. It is the one reader of the sealed words
  besides the conversation's people.
- **The chat page says so:** `SessionOut.sensitive` draws a banner and turns off Share to
  Slack.
- **MCP Apps:** a View gets `tool_result: null` and fetches its data as the viewer from
  `tool_input`, which it reads back from the sealed call; a receipt keeps no excerpt;
  `ui/update-model-context` is accepted and not kept (`apps/mcp_apps_host/mcp_apps_views.py`).
- **Never re-exposed:** share-link upload (403), Slack share / new thread (403) and
  mirroring (`slack.relay`), export (403), the usage scan's title.
- **Never re-processed:** auto-debug starts nothing for a turn with any requirement; the
  fleet's transcript readers find none to read.
