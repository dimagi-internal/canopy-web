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
- **`ConnectionApp` (`apps/connections`) belongs to a workspace**: the OAuth client a workspace admin sets up for a provider (one per workspace and provider; its secret is encrypted in the DB). A `Connection` goes through one app, and a person's connection is usable only while they are a member (read) of that app's workspace — leaving the workspace ends its use (`not_connected`), it is not deleted. An agent uses its own workspace's apps. → [`vault.md`](vault.md) § OAuth connections are server-held.
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
  the TARGET already a member — `apps/agents/api.py`).
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
  tree only (422 otherwise); a machine serving two trees runs a runner per tree.
  A laptop may span trees and says so on its runner row.
  → [runners.md § Several runners on one machine](runners.md#several-runners-on-one-machine)
- **On a machine, each runner is a Unix user.** Each runner's secrets, logins, mail
  tokens, transcripts and worktrees are its user's (home 0700); IMDS is root's, so a
  runner cannot act as the machine; its device key and its KMS ECDH key are its own, so
  one runner's token or proof never reads another runner's agents, and an agent's vault
  key is wrapped only to the runners that run it. Inside a runner, agents share a user:
  the runner, not a rail, is the boundary. Root on the machine is trusted.
  → [runners.md § Setting up the runners on a machine](runners.md#setting-up-the-runners-on-a-machine-runners)
- **Sessions a runner reports are filed inside its tenant.** A new agent-less
  session names its agent only among agents in `runner_tenant_slugs(runner)`
  (`harness.session_sync._agent_for_project`: `Agent.slug` is global). A session whose
  project names an agent OUTSIDE that tenant is filed nowhere — not in the agent's
  tree, and not in the runner's as a plain project (`session_home` → None; the
  report's `skipped` counts it) — and the owner's default session
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
- **What each kind may do is one table**, `apps/tokens/kinds.py`: every credential type a
  door records (`request.auth_credential["type"]`; none = `session`), what it reaches, whether
  it may mint another credential (a session or a PAT only), whether it is a turn speaking for
  its asker (a caller token: refused where only the person may act), and whether it may carry
  `X-Canopy-On-Behalf-Of` (a PAT). Ask the table (`kinds.speaks_for_a_turn(request)`,
  `kinds.kind_of(cred).may_mint`); `tests/test_token_kinds.py` fails a comparison against a
  kind's name written out anywhere else. A type the table does not know is never the person
  and never mints.
- **An agent's own login (`Agent.user`) is an account the agent IS** — its token signs in
  as that account — and it is ALWAYS the account at the agent's own email. canopy creates it
  when there is none, and links one nobody has ever signed in to; an account there that has
  signed in is linked only when its person accepts, in
  their browser behind a passkey step-up (`AgentLoginRequest`, `apps/agents/agent_login.py`). Its stored token expires (`PAT_DEFAULT_TTL_DAYS`); an agent turn's caller token
  reads as its asker and never writes (`access.md` → "Whose authority an agent acts with").
- `POST /api/debug/mint-session/` (`apps/common/views_debug.py`) trades a PAT for a
  short-lived browser session (the macOS menubar's web view). A minted session is still a
  machine (`views_debug.is_machine`), and the MCP OAuth consent page refuses it, so a token
  cannot sign an MCP client in on its own.
- **Browser or machine** is one question, `views_debug.is_machine` (`stepup.is_browser` asks
  it): a machine is a request a token authenticated (`request.auth_method`, stamped by every
  bearer door in `apps/tokens/middleware.py`), an in-process MCP call, or a session minted
  from a token. An `Authorization` header alone proves nothing: a signed-in cookie user
  skips the token lookup, so a junk `Authorization: Bearer x` sent with a stolen cookie
  leaves `auth_method` unset and the request is still a browser, which every passkey
  step-up and browser-only route applies to. Never read the header to decide this.
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
  fallback to the agent's credential. **A grant is one page's arrival, not the visitor's:** each
  arrival is its own `HostGrant` row, the token minted with it points at it, and a conversation
  reads through the grant of the page that last acted in it (a send, a page-state declaration —
  `canopy_sessions.services.note_host_grant`, read by `host_grants.grant_for(session=)`), so a
  host that holds a page's grant to what that page shows sees each tab's own. canopy's client identity is the CIMD at
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
  `/api/me/session-sharing/`, the Settings → Opted-in repos panel). Who sees what per
  channel: `access.md` § Who can see a session.
- **Pulling from a machine is `access.may_pull`.** Asking a runner to stream a session
  (`POST /{id}/attach`, and the chat socket's connect) or to ship its history
  (`POST /{id}/backfill`) is for the box owner and anyone the rule lets write in it.
  Everyone else who can read it — a coach, a runner admin, a viewer, an agent login not
  explicitly added — reads the stored rows: their attach is a no-op and their backfill a 403.
- **A session review grant lets one agent's own login read one person's sessions** — a
  coach (`SessionReviewGrant`; leg 5, `access._review_grant_q`). Only the person makes
  or ends it, for an agent of a workspace they are in (`/api/people/me/session-reviewers/`,
  `apps/contacts/session_reviewers.py`), each act on their memory audit
  (`sessionReviewer.granted` / `.revoked`); revoking drops the agent's next read. "Their
  sessions" are the chats they started and the runner-discovered sessions on a box they
  own that are not an agent's own work (`access.sessions_of_q`). Read-only (`role_for`
  gives the agent a viewer), never a sensitive conversation (any runner requirement keeps
  it out, failing closed), and never anyone but that agent's login — its admins, other
  agents and co-tenants gain nothing. The agent lists who let it in
  (`GET /api/people/session-reviewers/granted-to-me/`) and one person's sessions with
  `GET /api/canopy-sessions/?of_user=<id>`. Which agent canopy offers as the coach (the
  first-run checkbox, the Settings switch) is `settings.COACHING_AGENT_SLUG`, offered only
  where that agent is in a workspace the person is in or may ask to join.
- Acting needs participant role owner/editor; a `viewer` reads; sharing is the owner's
  (`/participants`, the chat header's People control).
- Every door calls it: REST, the chat socket, attachments, bulk reset, MCP, Slack share, push
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
- **A sensitive conversation is held to its own clock.** One that requires sensitive-data runners
  (`apps.harness.sensitivity.is_sensitive`) is purged after `CANOPY_SENSITIVE_KEEP_DAYS`
  (30) by the same heartbeat sweep **whatever `CANOPY_RETENTION_ENFORCE` says**
  (`retention.services._sweep_sensitive`, a forced rule tallied as `#0`); a shorter
  workspace rule still wins when enforcement is on.
- Spec: `docs/superpowers/specs/2026-10-05-content-retention-design.md`.

## Sensitive conversations (`apps/harness/sensitivity.py`)

`is_sensitive(session | turn)` — the requirements (`runner_requirements.requirements_of`,
so a child, dispatched or forked turn inherits it) include `sensitive_data`, or cannot be read. It is
THE predicate; every surface below asks it, one test each in
`tests/test_sensitive_storage.py` (spec `2026-10-22-sensitive-data-agents` §4):

- **Stored less:** no `TurnTranscript` (`ledger.append_transcript` answers the runner with
  an empty success); a `tool_end`/`tool_result` event, a `TOOL_RESULT` `Message` and its
  live frame keep `{tool_use_id, is_error, status, withheld}`, never the result
  (`sensitivity.strip_result`, in `ledger.append_events`, `persist_transcript_rows` and
  `post_session_stream`; an allowlist, so a new field stays withheld, and each kept
  string in its shape: ids and names as identifiers, `status` from `TOOL_STATUSES`) —
  plus the host's **resource links** (`resource_links`: `<scheme>://<url-safe id>`, a
  mimeType from `canopy_sdk.resources.MIME_TYPES`, and the scheme as the name — addresses,
  not data; spec `2026-10-23-host-held-results`). A runner pulls links out of the raw
  result before cutting its text (`canopy_transcript.tool_result_links`). A link is kept
  only when the result is the **site's own tool's** — `site_call`, or a site tool canopy
  indexed reached directly (`site_needs.is_site_tool`, registered as
  `sensitivity.register_site_tools`; the call is placed from its batch, `calls_in`, or the
  stored call, `producing_tool`) — so link-shaped output of any other tool is dropped,
  and when it is a valid MCP `resource_link` (at most 10, never a reserved scheme such as
  `http(s)`/`data`/`ui`) under a URI scheme the conversation's site declares in its
  signed `site.json` (`site.resource_schemes`, with the exact id length from
  `site.resource_id_lengths` where declared; read live through
  `site_needs.resource_schemes_for`, registered as `sensitivity.register_resource_schemes`;
  unreadable → none kept). The host keeps the artifact; a View
  (`mcp_apps_views.withheld_result`) and the agent (`site_read`) read it back from the host
  as the person. Nothing else of the result survives, which
  `tests/test_host_held_results.py`'s canary checks across every table.
- **Metadata in every readable column:** every stored event and every `Message`'s
  `plaintext`/`content` (the person's, the agent's, a tool call's) keeps kind, tool name,
  argument names, ids, error flags, size and hash only (`sensitivity.redact_payload`). The
  prompt is wiped at the turn's end (`closing.close_turn`), the result note withheld, and
  there is no owner-ask line, live tail (`RunnerBinding.tail`) or autotitle.
- **The conversation, sealed to its own people** (spec
  `2026-10-23-sensitive-conversations-sealed-to-their-people` §2,
  `apps/canopy_sessions/conversation_keys.py`): canopy-web can WRITE a sensitive conversation
  and cannot read it back.
  - *The key.* A chat started from canopy's own chat surfaces (the chat page, the embedded
    panel) carries a P-256 key pair its starter's BROWSER made (`SessionCreateIn.conversation_key`,
    `sealedChat.ts::newConversationKey`): the public half, a fresh conversation id, and the
    private half wrapped (ECDH-ES + HKDF-SHA256 + AES-256-GCM, the vault's `wrapEcdhEs`, info
    `canopy-chat/v1/wrap|<conversation>|<user>`) to the starter's vault key
    (`GET /my-vault-key`) — its PUBLIC half, so starting a chat or sending in one, private
    or not, never asks for a passkey (`sealedChat.offerForNewChat`). A key pinned in this
    browser (`trust.checkSelf`) is used silently. A key CHANGED since this browser pinned it
    stops the new chat with a sentence (a substituted key looks like that); the panel and
    the chat page show it and keep what was typed. A key never seen in this browser (a fresh
    browser, the panel's frame, storage the browser will not keep) is **trusted on first
    use**: the chat starts wrapped to the key canopy names, pinned here as unconfirmed with
    the chat's id (`trust.trustSelfOnFirstUse`). The first time the person opens their vault
    key in that browser for any reason (`unlockLocally`, `useVaultUnlock` → `trust.rememberSelf`),
    the key they actually hold — derived from its private scalar — is checked against it;
    a mismatch is a warning naming those chats on the sealed-conversation banner
    (`useSealedConversation.warning`), and the pin becomes the key they hold, so the
    substituted key is refused from then on. **The trade-off:** a key substituted on a
    browser's very first chat is DETECTED at the first unlock there, not prevented; with
    storage blocked nothing is remembered, so each start trusts anew and nothing is checked.
    canopy-web keeps it (`SessionKey.public_jwk`, a `SessionKeyWrap`) even
    while the chat is ordinary, so if it becomes sensitive its words are sealed to a key
    canopy-web never saw the private half of (`conversation_keys.check_offer`, `ensure_key`).
    **The limit:** a session that becomes sensitive with no browser there — made from Slack,
    MCP, a contact or a host's grant, or a chat that came with no key — gets a pair made on
    the server, its private half wrapped to the starter and dropped at once (the `zk_seal`
    stance: canopy-web saw it once). A fork, successor or branch (`parent_session`) carries
    the same key and every wrap (`copy_key`) — only when its creator is one of the
    parent's own people (`conversation_keys._carries_on`, `is_conversation_person`):
    naming a private chat as `parent` gives anyone else a key of their own, no copy in
    their `/my-keys`, and none of its people (`services._inherit_participants` asks the
    same of a sensitive parent).
  - *Sealing.* Each stored message's words (`{plaintext, content}`; never a tool's result)
    go into `Message.sealed` as a `cc1:` ECIES envelope to the conversation's public key,
    info and AAD `canopy-chat/v1/message|<conversation>|<turn_index>|<role>`, the role also
    named as `r` (`sensitivity.seal`, from `send.py` and `transcript_ingest`; an envelope
    without `r` predates the role binding). The browser opens a row only where its envelope
    says it belongs: the conversation, its index (`i == turn_index + d`, d in
    [0, …`sealed_key.index_offsets`] — a fork's copied rows sit the recorded offset below the
    index they were sealed at) and its speaker. No key canopy-web holds — the field key, the
    legacy `SECRET_KEY` key — opens it, and no private key is stored.
    `docs/vault/chat-v1/chat_v1_vectors.json` pins the bytes for both languages.
  - *Reading.* ONE gate hands the ciphertext out, `sensitivity.reads_content`: the
    conversation's own people (its creator, its participants) on its own chat surfaces (a
    browser session, the widget's site token; never MCP, a PAT, OAuth or a minted session).
    The chat REST reads (`GET /{id}`, `/messages`) and the socket snapshot return each row's
    `sealed` (`sensitivity.readable`) and `GET /{id}` the reader's own wrap
    (`sealed_key`); the chat page and the embedded panel unlock the person's vault key
    (passkey or passphrase), unwrap, and decrypt in the browser (`frontend/src/vault/sealedChat.ts`,
    `useSealedConversation`). Live frames to the admitted sockets still carry the words in
    transit; only what is stored is unreadable to canopy-web. Which socket is a chat surface
    is read from the session STORE at the handshake (`channels_auth`): a cookie minted from a
    token is `auth_method="minted"`, never "session", and `scope_is_chat_surface` refuses it. Every other reader — search,
    human inputs, a session review, an agent's or the fleet's reads, an admin who is not in
    it — reads the metadata columns. The reply reaches the asker the one way every turn's
    does: its transcript rows. A confined turn has no binding, so its box tails the turn's
    own transcript while it runs (`_TurnTail`, every ~2 s, one row per message or tool call as
    Claude writes it — never token by token) and ships through the unbound door
    (`session_sync.ingest_turn_end_rows`), then ships the whole transcript once more at the
    end, before deleting it. Every ship of one transcript lands in the same epoch, and a row
    already held is neither stored nor fanned out again, so the final ship adds nothing the
    live tail sent. A sensitive-data box still runs no `/streams` tail or backfill.
  - *An agent's own login.* It holds no vault key, so it cannot start or read one —
    unless an admin of the agent turned on its TEST vault key, which receives copies only
    of the conversations that login is itself in (`docs/architecture/vault.md` § An agent
    login's test vault key). A harness signed in as the agent then reads them exactly as a
    person does: the passphrase unlock in its browser.
  - *Who may send into one.* Its own people on its chat surfaces, the read gate
    (`reads_content`, asked in `doors.send_from` for every door: REST, socket, contact,
    app view, MCP, Slack) — 403 `NOT_IN_IT_SEND` otherwise, `forbidden` on the socket.
    Whoever may not read the answer may not steer the agent either. A leg that only lets
    someone see one exists (an agent's admins, a co-tenant of a runner-discovered one)
    gives them VIEWER, never owner or editor (`access.role_for`), and nobody becomes its
    key's holder by sending: the key is its creator's (`require_key`), so a conversation
    nobody started stays keyless and keeps no words.
  - *Who may hold one.* A canopy user with a vault key. A contact, or a starter with none,
    is refused in a sentence ("a sensitive conversation needs a canopy account with a vault
    key; set yours up at …") at create (422) and at every send door (`CannotHold`: 422 on
    REST, `session.error` `cannot_hold` on the socket). A send that would MAKE an older
    conversation sensitive is refused before the switch, in its transaction
    (`add_runner_requirements(..., refuse_keyless=True)` from `doors.send_from`): all or
    nothing, so a refused send never leaves it private with its history blanked. Rows an agent ships into a keyless
    sensitive conversation keep no words at all.
  - *Multiplayer.* Adding someone is a browser act by someone in it: their browser unwraps
    its copy and wraps it to the newcomer's current vault key (`POST …/participants` with
    `wrap`; the server checks the shape and `recipient_jkt`). The key the server names for
    them is never taken on its word: the browser wraps only to a key it already trusts for
    that person (pinned in this browser) or one the adder accepts now, having seen its
    fingerprint (`trust.trustPerson`, `PersonTrustPrompt`; then pinned) — the ZK vault's
    rule. A key that does not match its thumbprint stops the add with a sentence. A key
    that changed under a pinned person is a STOP: the prompt shows no new fingerprint and
    no button that continues until the adder types the fingerprint the person read them
    by another channel (`fingerprintMatches`). So does an adder's own copy gone stale ("ask someone else in it
    to give you a fresh copy"). Removing someone deletes their wrap; the key is not rotated
    (they could have kept what they read). Nothing else makes
    anyone one of its people: `participants.ensure_participant` adds nobody to a sensitive
    conversation who is not its creator and holds no wrap of its key, so no door — a Slack
    reply, a dispatch's asker — can. Asking to join is once an hour per conversation
    (`join_requests.TooSoon`, 429; `CANOPY_JOIN_REQUEST_REASK_SECONDS`).
  - *Given it before it became sensitive.* A participant with no copy for their current
    vault key is listed `needs_key` (People); an editor or the owner in it holding a current
    copy gives them one in a click (`POST …/key-copies`, the same trust rule; 403 for a
    viewer). A copy is written only where the person holds none for their CURRENT vault key
    (`conversation_keys.add_wrap` → `CopyExists`, 409): the server cannot tell a good wrap
    from random bytes, so an overwrite could lock anyone out, the owner included. A copy
    for an older vault key is provably stale (its `recipient_jkt` names a key they no
    longer have) and is replaced; a current one that does not open is set aside by its
    holder alone (`DELETE …/key-copies/mine`, the banner's "Set my copy aside"), after
    which they are `needs_key` again. A re-add or role change keeps a current copy. Until they hold a copy the
    live socket, whose frames carry the words, turns them away (`sensitivity.hears_live`,
    asked at connect and on every re-check; `session.error` `needs_key`).
  - *A new vault key.* Replacing one (Settings → Vault key → Start over) names the private
    conversations at stake, unlocks the OLD key, opens every copy the person holds
    (`GET /my-keys`), sets the new key, and stores each copy re-wrapped to it
    (`PUT /my-keys`; `vault/conversationCopies.ts`). A copy the old key no longer opened, or
    a starter who replaced their key elsewhere, is refreshed by an editor in it
    (`POST …/key-copies` takes the starter too, since their copy is provably stale).
  - *Rows from before it became sensitive.* Sealed in the transaction that makes it
    sensitive (`services.add_runner_requirements` → `conversation_keys.seal_existing`): the
    server holds the conversation's PUBLIC key, so it seals what it can no longer read back,
    clears the columns, and drops a tool's result; an ended turn's prompt is wiped and its
    note withheld, its events reduced to metadata, its raw transcript deleted, and the
    binding's tail, summary and dialog words cleared (`_withhold_what_it_kept`). A live
    turn keeps its prompt until it ends. "Its turns" are every turn whose conversation it
    is (`sensitivity.conversation_of`, `turns_of_q`): a chat turn's `chat_session`, and an
    agent, board or email turn's `canopy_session` — which is also in what a turn requires
    of its runner (`runner_requirements.requirements_of`), so such a turn routes only to a
    sensitive-data box, stores metadata and loses its prompt at its end. Those turn rows
    are locked by the switch, and `ledger.append_events` reads sensitivity under the same
    lock from fresh rows, so an event written as it switches is reduced or stored as
    metadata, never kept in clear; a keystroke racing it (`drafts.update_draft`) asks again
    under the session row's lock. Every file attached to it, sent or not, is deleted in
    the same transaction (rows now, stored bytes on commit, `services._drop_attachments`):
    it was kept in clear with its name, and sealing it would need a browser holding the key
    at the switch, which a send the server handles has not got — so it is not carried in.
    A send there claims only a sealed file (see *Files*). A FORK made while it was ordinary holds a copy of the
    same words, so it becomes sensitive with it, recursively (its rows sealed to the key
    its forker carries on); any other child keeps its own state.
  - *Every row is a `cc1:` envelope.* No sealed row is under canopy's field key, and canopy-web
    has no code that would open one; `readable` still hands out only `cc1:` envelopes.
  - *In the browser.* A quiet "Private" line under the header says so on the chat page and
    the panel. The unlock banner shows only when sealed rows are in view (history, scroll-back),
    never for a conversation whose messages all arrived live; its one click goes straight to
    the passkey (`unlockLocally`, `useVaultUnlock`), and a passphrase is asked only when the
    passkey is absent or failed. What arrived live is kept in the tab by place (turn index +
    role), so a reconnect's snapshot — the same rows, sealed — still reads without an unlock
    (`useSealedConversation`); a row with only its metadata reads "Sealing…". An MCP Apps View
    loads again, in a fresh frame, once the key opens (`AppHost.inputRevision`), so its input
    is the decrypted one.
  - *The keyset and the host.* `sealed_key.keyset` (the reader's vault public key and its
    seals) goes only to the person's browser: canopy's own signed-in page, or the panel's
    frame, whose fetches are `Sec-Fetch-Site: same-origin` (`sensitivity.request_is_panel_browser`).
    A site's backend calling canopy with the same delegated token gets `keyset: null`. The
    trust boundary: a connected site vouches for who its visitor is, so it can always act
    as them and read what canopy hands them — ciphertext and their wrap, never anything that
    opens it. Their seals would let a site that forged the browser's header try passphrases
    offline (a passkey seal cannot be attacked so); the header keeps them out of every
    ordinary server-to-server call, and is not a proof against a site that forges it.
    **A connected site's backend hears live words.** It holds the visitor's delegated
    token, so it can open the chat socket or send as them, and the socket's live frames
    carry the words in transit: sealing protects what canopy-web STORES, not a site the
    person chose to use canopy through. Only connect a site you would trust with the
    conversation.
  - *Drafts stay in the browser.* A sensitive conversation stores no draft: the keystroke
    path stores nothing there (`drafts.update_draft` → None), the composer never mirrors
    keystrokes (canopy-ui `localDrafts`), the text reaches canopy-web only inside the send,
    and peers see no typing preview. Becoming sensitive empties every draft
    (`drafts.forget_words`); a body kept under the field key before reads as nothing.
  - *Files: the agent's copy for one turn, the people's sealed* (`private_attachments`).
    The agent reads a file on its box, which holds no key to the conversation, so a file is
    treated like the prompt. Its people's browser — canopy's chat page, or the embedded
    panel's frame (`api._from_its_peoples_browser`: a delegated token only with
    `Sec-Fetch-Site: same-origin`, the keyset's rule, `request_is_panel_browser`); a token,
    MCP, a minted session, a contact or a site's backend gets 422 — uploads it in clear for one turn, under
    a key that names no file (`plain_key`), the row keeping a size bucket and a type family;
    then seals the name, type and size (`cf1:`) and the padded bytes (`cfb1`) to the
    conversation's public key with the message construction, bound to the conversation, the
    attachment and its uploader (`vault/sealedFiles.ts`, `POST …/attachments/{id}/sealed`;
    canopy-web checks the shape, once, from the uploader, before the send). A send claims
    only a file whose sealed copy is stored, and the turn's `origin_ref` names it by id
    alone (`send.claim_pending_attachments`, `bind_turn`). The runner's download route serves
    it ONCE, to the box holding that turn, deleting object, name and key as it does
    (`take_for_runner`); the turn's end (`close_turn` → `forget_turn`, so the unrun deadline
    too) and the sensitive sweep (`private_attachments.sweep`, its own tick and the retention
    sweep) delete any copy not fetched, and an unsent file whole. The plaintext route
    (`/attachments/{id}/content`) serves it to nobody. Its people read `GET
    …/sealed-attachments` (no name; each placed at the line it was sent with) and the sealed
    bytes (`GET …/attachments/{id}/sealed`), through `reads_content`, and open both in the
    browser: "Attachment" under the line until the key opens, then the name, a preview and a
    download (`SealedAttachments`, canopy-ui `AttachmentHost`). On the sensitive-data box the
    file lands in the turn's own directory (`<cwd>/.canopy-attachments/<turn>/`, owner-only),
    readable by that turn alone (`sensitive_data_settings(attachment_dir=…)`, and the
    profile's `attachment_dirs`, which profile_guard ≥ 4 — canopy 0.2.781 — lets Read open
    and nothing else; the capability's own tools and read_paths are unchanged, and an older
    guard gets no file, named as not available), and goes when the turn ends
    (`_sensitive_data_forget_turn`); what the agent read of it survives only in the kept
    session's transcript, which lives no longer than the kept conversation. The transcript
    the box ships back names it only inside sealed words (the user row's note, a Read
    call's path), never in a readable column.
- **Memory is off, both ways:** nothing is written (`hcp.refuse_if_sensitive`), and nothing is
  read into one — `hcp.memory_state` reads every feature as not effective in a sensitive
  session, `hcp.effective` / `hcp.require` refuse a sensitive turn (so the envelope's `person`
  runs no search, whose query would keep the turn's words, and `hcp_searchPreferences` is a
  403), and the person cannot switch it on or grant it there. The Learn/Use switches are not
  offered in one (`tests/test_hcp.py`).
- **Live and log doors ask the same gate:** the chat socket (`SessionConsumer`), whose live
  frames carry the words, admits only `reads_content`; the turn socket's tail gets the stored
  metadata; and `turn_access.can_read_turn_content` is False for a sensitive turn, so no turn
  read (REST, MCP, the board, the unclaimable report, the caller envelope) returns its
  prompt, note or report while it runs. The `TURN_CREATED` log line keeps no prompt head.
  A sensitive turn no runner has started (QUEUED or CLAIMED) is closed MISSED through
  `close_turn`, prompt wiped, `CANOPY_SENSITIVE_TURN_DEADLINE_SECONDS` (1 h) after it could
  first RUN (`harness.sweeps.expire_unrun_sensitive_turns`, its own tick, and the
  sensitive sweep): never while another turn of its conversation is running, and then
  from when the last turn before it that ran ended, or when it was asked if later. Session info (`GET /{id}/info`) shows an allowlist of metadata
  (`api.INFO_METADATA_KEYS`) and, for a sensitive one, no box Claude session or summary.
  Its "done" push says "Reply ready" or "The turn failed", nothing more.
- **Only its people hand it on:** adding a participant (which needs their wrap, made in a
  browser already in it) and forking (which copies the sealed words, the key and the wraps)
  need `reads_content` too, so an agent's admin who may open a thread they
  are not in cannot add themselves or copy it. A fork is the OWNER's act as well
  (`access.can_share`): it carries the key, the wraps and the agent's memory into a
  conversation its forker owns, so an editor in it cannot. A fork is canopy's to make:
  `metadata.fork` is server-owned (create takes only an allowlist of host keys,
  `services.HOST_METADATA_KEYS` and `x_…`), and what the runner is told
  (`placement.fork_plan`) is read from canopy's own columns — `worktree_of` only within
  the fork's lineage up `parent_session` (`fork_lineage`, which the cloud runner checks
  before running in another chat's directory), the cut only from the recorded
  `parent_claude_session`. No member can aim their turn at someone else's worktree,
  where their transcripts sit.
- **Nothing from outside its people:** Slack takes no message into one — a reply in its
  thread, a mention, a branch, a channel read, even from its owner — and says so with the
  link (`slack.services.private_refusal`); an email in its thread runs as the agent's own
  turn beside it (`channels.email.arrive`). Nor is it moved from Slack: "Run on cloud" and
  `/canopy cloud` leave a private conversation as it is and say so (`_move_to_cloud`),
  since its stranded turn could only be re-asked by Slack sending into it.
- **The agent's memory:** every turn of a sensitive conversation runs confined to the same
  capability, so it resumes the Claude session of the conversation's previous turn when it
  runs on the same sensitive-data box, in the same capability for the same agent, and the
  box still holds the transcript. The box records which session belongs to which
  conversation itself (`~/.canopy/kept-conversations/<session>.json`, runner/ec2
  `kept_resume` / `keep_conversation`), told by the claim that the turn is in one
  (`ClaimedTurnOut.sensitive_conversation`); it keeps only that session's transcript and
  sidecar, until the conversation has been idle 24 hours or is closed (the box asks
  `POST /runners/{id}/kept-conversations`; a 401, 403 or 404 there, a retired box among
  them, forgets them all, and so does the box's own shutdown). Otherwise the turn starts fresh and is told so
  (`conversation.starts_fresh`, which the runner adds to the envelope and the canopy
  plugin's hook renders): canopy-web never reads the sealed words back to hand them on,
  so the envelope carries no history. The resumed turn's ship sends only the records after
  the resume point (and after what its live tail already landed), in chunks, under the same
  id, retrying a failed post (`session_sync.ingest_turn_end_rows` keeps each row once). No
  turn's read tools reach another conversation's kept words: they are scoped to the turn's
  own directory and envelope, and every transcript, the kept-conversation records and the
  other per-turn state under `~/.canopy` are denied by name (`sensitive_data_settings`). A
  member's `who_is_asking` is gated like its REST twin (`can_read_turn_content`), so a
  sensitive turn is "turn not found".
- **No door around the gate** (`tests/test_sensitive_read_doors.py`,
  `tests/test_sealed_server_doors.py`): a session minted from a
  token is refused by the app-view routes (`app_views_api.browser_only`) and the panel's
  own token (`embed_api.embed_self_token`), both via `is_machine`, and by the chat socket
  (`auth_method="minted"`); every page route — its state and actions, declared, listed,
  invoked and answered (`/{id}/page-state`, `/run-input`, `/page-actions…`; MCP
  `read_page_state`, `list_page_actions`, `invoke_page_action`) — asks `reads_content`
  (`api._page_in_it_or_403`), `current_page` and the `page_*` tools over MCP leave a
  sensitive one out (the turn in it still reads and acts on its own, by its caller token
  or chat key, asked again at the call), an action's arguments and answer are not kept on
  its row (`page_actions._forget`), and the page is forgotten when its
  last live turn ends (`closing._forget_the_page`), or after
  `CANOPY_SENSITIVE_PAGE_TTL_SECONDS` (15 min) with no live turn
  (`page_state.forget_idle_sensitive_pages`, a tick); the unbound stream door
  (`api_session_sync._turn_end_session`) takes only the confined turn's own transcript
  (the first it ships, stamped on `Turn.session_key`) from a box that may still hold the
  agent (`runner_may_hold_agent`); a list reads who is in which once per page
  (`sensitivity.people_in`); a View's `tool_input`
  asks the request's real surface (`Viewer.chat_surface`); attachment bytes ask
  `reads_content` (404 otherwise).
- **A blocked agent's dialog is metadata:** `RunnerBinding.pending_question` keeps source,
  stamp, numbered options and a digest, never the question or option words
  (`sensitivity.menu_metadata`, both producers in `session_sync`); its push says only that
  it "needs your answer", and it is not relayed to Slack.
- **The socket re-asks:** `access.recheck` also asks `reads_content`, and is sent to every
  socket on a session that becomes sensitive (`services.add_runner_requirements`) and to a
  removed participant's (`services.recheck_sockets`).
- **No title:** create drops one, becoming sensitive clears one, `PUT …/title` is 422
  (`sensitivity.NO_TITLE`; clearing is allowed), and the `SESSION_CREATED` line keeps none.
- **Who a site may say a visitor is takes a passkey:** a signed `email_verified` claim
  resolves to a member, whose token reads their sensitive chats, so registering a connected
  site or changing its public keys, JWKS URL or issuer needs a fresh step-up; a token is
  refused (`embedding/connected_apps_api.py`).
- **The chat page says so:** `SessionOut.sensitive` draws a banner and turns off Share to
  Slack.
- **MCP Apps:** which View a call has is decided from its metadata columns alone (call id,
  tool name, the host tool a gateway call reached: `sensitivity.call_of`); a View gets
  `tool_result: null` and fetches its data as the viewer from its input, which the server
  sends sealed (`tool_input_sealed`, `sensitivity.sealed_call_for`) and the browser decrypts
  and hands it; a receipt keeps no excerpt;
  `ui/update-model-context` is accepted and not kept (`apps/mcp_apps_host/mcp_apps_views.py`).
- **Never re-exposed:** share-link upload (403), Slack share / new thread (403) and
  mirroring (`slack.relay`), export (403).
- **Never re-processed:** auto-debug starts nothing for a turn with any requirement; the
  fleet's transcript readers find none to read.
- **A sensitive turn's token reaches a fixed few tools:** its caller token's tools are the
  capability's globs ∩ `sensitive_data.SENSITIVE_CANOPY_TOOLS` (`current_page`,
  `who_is_asking`, `site_tools`, `site_call`, `site_read`) plus its own page's actions
  (`page_*`, `sensitive_data.sensitive_tool`: each runs in its person's tab and answers
  only the turn, so nothing leaves its people), whatever the interface lists; it resolves to
  nothing once the capability stops saying `data: sensitive` (re-read on every call) or the
  turn is not confined (`caller_tokens._sensitive_grant`, `mcp/turn_scope.py`). So a
  sensitive turn creates no schedule, task or turn of its own.
- **Lineage is never dropped:** a caller token's own turn is the parent of anything its
  request starts, whatever a header or payload names (`request_context.from_request`,
  `provenance.requested_parent`); a payload `parent` that names nothing is a 422
  (`provenance.UnknownParentError`). A session started from a sensitive turn or
  conversation — a `parent`, a Slack branch, a transcript successor — takes its
  requirements at birth (`provenance.inherit_requirements`); a turn started from one makes
  the conversation it joins sensitive (`sensitivity.take_on`, at enqueue). A queued turn
  whose recorded parent has since been deleted is unsatisfiable
  (`runner_requirements.lost_lineage`).
- **One sealing decision:** every writer of `Message` rows (`send.py`, `transcript_ingest`)
  asks `sensitivity.seals(session, turn)` on the locked session, which first raises it to
  the turn's requirements — so what is sealed and what `reads_content` admits agree.
- **Elsewhere:** a turn refused at creation keeps no prompt (`services._apply_capability`);
  forced retention reaches a session-less turn sensitive through any ancestor
  (`retention.services._sweep_sensitive`); a draft's body is stored encrypted
  (`drafts.SEALED_PREFIX`) and reaches only a socket `reads_content` admits. Tests:
  `tests/test_sensitive_lineage.py`.
