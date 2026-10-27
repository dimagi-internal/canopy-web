# The HCP service

canopy holds each person's **Human Context Protocol** instance (HCP v1, draft 4:
`apps/contacts/hcp/` — `core`, `consent`, `sensitive`, `entries`, `grants`,
`relevance`, `conformance` — and `hcp_api.py`). canopy's own agents reach it under grants
the person gives each agent in a session — see "canopy's own agents" below. The
**HCP service** lets an application canopy does not operate ask a person, through
OAuth 2.0, for access to parts of their instance. That makes the profile `oauth2`
and the service eligible for HCP v1 Interop (spec 5.2.1).

The model is implemented end to end and designed for outside use, but is used only
internally for now — the owner's explicit choice ("Internal only — the one gate").

This page records the design and every judgment call made building it. The
self-audit against the spec is `hcp-conformance.md`.

## The pieces

| Piece | Where |
|---|---|
| A person's own instance, with or without a workspace | `PersonFact.workspace` NULL = a **personal** entry (`contacts/0020`); `/people/me` |
| Client registry (admins) | `HcpClient`; `/api/hcp-admin/clients` (`hcp_clients_api.py`); page `/hcp-clients` |
| Authorization server | `hcp_oauth.py`, `hcp_oauth_views.py`; issuer `<origin>/api/hcp`; PKCE, token-endpoint redirect_uri and one-time redemption shared with the MCP sign-in in `apps/common/oauth_core.py` |
| Consent screen | `templates/contacts/hcp_consent.html` (act 1), `hcp_keep.html` (act 2) |
| Tokens | `HcpToken` (access 1 h; refresh rotates), `HcpAuthorizationCode` (10 min, single use) |
| Bearer access | `apps/tokens/middleware.py` → `request.hcp_access`; `hcp_api._principal` |
| Revocation notices | `HcpRevocationDelivery`; signed, retried from the off-request clock |
| Discovery | `/api/hcp/.well-known/hcp-configuration` (also `/.well-known/hcp-configuration`) |
| RFC 8414 metadata | `/api/hcp/.well-known/oauth-authorization-server` and `/.well-known/oauth-authorization-server/api/hcp` |
| MCP manifest (Appendix B) | `/api/hcp/.well-known/mcp-manifest` |
| The internal-only gate | `settings.HCP_SERVICE_AUDIENCE` (env), read only by `hcp_oauth.audience()` |

## The flow

1. **The client sends the person to the authorize endpoint.** It goes to
   `/api/hcp/oauth/authorize?response_type=code&client_id&redirect_uri&scope&state&code_challenge&code_challenge_method=S256`.
2. **canopy checks the client and its return address before anything else.** It
   must be registered and enabled, and the `redirect_uri` must exactly equal one it
   registered. If either check fails, the error is **shown**, never redirected to.
   That avoids an open redirect.
3. **canopy checks the rest of the request.** PKCE S256 is required. Every scope
   must be a fully named `hcp:{category}:{read|write}` for a category canopy holds,
   and must be within the client's `allowed_scopes`. Errors from here on are
   redirected back to the client, as RFC 6749 says.
4. **Sign-in and the audience gate.** The person signs in if they aren't already.
   A session minted from a token is refused (`is_machine`). Then the audience gate
   applies.
5. **Act 1: the consent screen.** It shows:
   - the client's name and operator;
   - a row per category with read / save-or-change;
   - what the person's own record/use switches mean for this app, said plainly,
     including "which it is NOT now, so any read will be refused";
   - which sources it may reach, personal always plus any workspace the person
     ticks (unticked by default);
   - the expiry: 1 h, 4 h (the default) or 24 h.

   Allow or Deny. Denying creates nothing (4.1.7).
6. **Act 2, separately.** "Do you want *X* to keep this access after that, until
   you turn it off yourself?" The page has two buttons, neither styled as the
   default, and nothing is pre-selected.
7. **Only after act 2** is the `PersonGrant` written (temporary or persistent,
   `hcp_client` set) and `grant.issued` audited. The audit records the client,
   operator, type, expiry, modality, scopes and restrictions. A code is issued and
   the person is redirected.
8. **Token exchange.** `/api/hcp/oauth/token` with `authorization_code` checks the
   code, the client, the same redirect URI and the PKCE verifier. It returns an
   access token (1 h, never past a temporary grant's expiry) and a refresh token
   (30 days for persistent; the grant's expiry for temporary).
9. **The client calls `/api/hcp/v1/…`** with `Authorization: Bearer hcpat_…`.

## Judgment calls

**Its own issuer, `<origin>/api/hcp`.** canopy-web already runs an OAuth server
at `/oauth/*`, for MCP clients signing a person in to act *as* that person.
That is a different thing: those tokens are the whole canopy account. An HCP grant
is one client's category-scoped access to one person's context. One issuer for both
would put two meanings behind one token endpoint, so the HCP service has its own,
nested where the discovery document already lived.

**Registered clients only, public clients, PKCE always.** No dynamic registration
while the service is internal. An admin registering a client is the vetting step.
Clients hold no secret: native and browser apps can't keep one, and PKCE S256 on
every code is what binds the code to its requester. Confidential clients and the
Client Credentials and Device grants (4.1.2) are optional in the spec and not built.

**A grant reaches personal entries, plus only the workspaces the person ticks.**
Entries an agent wrote in a workspace were written under that tenant. Sending them
to an outside app by default would leak a tenant's dealings. So the consent screen
offers each workspace that holds entries about the person, **unticked**, and the
choice is recorded on the grant as a 4.1.5.1 restriction:
`narrowedTo: {"sources": ["personal", "workspace:<slug>"]}`. `narrowedTo` is
implementation-defined at v1. The person's grant list shows the reach (Rule 3), and
every read path filters by it (Rule 4, `hcp.within_grant`).

**A client's writes are personal entries.** No workspace's agent is told an entry
an outside app wrote.

**Workspace agents never read personal entries by default.** A first-party agent's
grant reaches its own organisation's workspaces (`hcp.reach_of`). A person's personal
entries reach only the person, the clients they authorize, and an agent the person
has pointed at a memory collection that includes them, after confirming
(`hcp-conformance.md`, 4.1.5.1). Reaching them by default would change what agents
are told without the person asking.

**The person's switches bound every client.** A client is served with no session,
so `hcp.require(person, need, None)` applies the person's *defaults*: a read
needs "use" available and on by default, and a write needs "record". A grant never
overrides them. The consent screen says so at the moment of asking. A session
override can't reach a client, because a client has no canopy session.

**Temporary expiry choices: 1 h, 4 h (pre-selected), 24 h.** 4.1.4 forbids
pre-selecting *persistence*. It does not forbid a default expiry for the temporary
grant, and 4 hours is the spec's own default (4.1.3). The concrete time is shown
on act 2 in UTC.

**Pending approvals live in the cache, bound to the user.** Between act 1 and
act 2 the request is held server-side for 15 minutes under a random nonce, keyed
to the person who approved it. Another session cannot complete it. Nothing is
written until act 2.

**Replays revoke.** A code presented twice, or a rotated refresh token presented
again, means someone else may hold it. The grant is revoked, every token dies, and
the revocation is committed even though the exchange itself is refused.

**A client revoking its own token ends the grant (RFC 7009).** It has said it no
longer wants access. The person sees `grant.revoked` with the client as actor.

**Webhooks retry from the off-request clock** (`apps/common/ticks.py`). canopy-web
has no job queue; the retention sweep runs on the same clock.
- **First attempt:** a short background thread fires right after commit.
- **Retries:** from the off-request clock, at most once a minute across the fleet.
- **Backoff:** 1, 5, 15, 30 and 60 minutes between attempts, so 6 attempts spanning about 1.85 h before it is abandoned,
  which meets "at least 5 attempts over at least 1 hour".
- **Signing:** the same payload every time, a fresh `HCP-Delivery-Id` each attempt,
  and `HCP-Signature` = hex HMAC-SHA256 over `<timestamp>.<body>`, keyed by a secret
  generated at registration. The secret is stored Fernet-encrypted and shown to the
  admin once.
- **Audit:** `revocation.notified` on first success and on abandonment.
- **Independence:** revocation never waits for any of it.

**An OAuth client is an `agent` in the audit log.** 4.3.2 allows only `user`,
`agent` or `system` as the actor type. The `actorId` is `client:<client_id>`, and
an entry it captures carries `capturedBy` = `client:<client_id>`. A `capturedBy` the
client asserts on `POST /v1/preferences` is kept only *under* its own id
(`client:<id>/<claim>`), so a client cannot record an entry as captured by canopy or
one of its agents (4.1.8).

**What a token opens.** An `hcpat_` token is refused by the bearer middleware on
any path outside `/api/hcp/v1/`. Within that path, the person's own routes (audit,
export, revoke) refuse it, and `GET /v1/grants` returns only its own grant.

**Rate limit: 120 requests a minute per client** on `/v1/`, with 429 and
`Retry-After`, published in discovery (3.4.5). The person is never limited, least
of all on revocation.

**MCP stays first-party.** The five HCP MCP tools (search, add, get, update, delete)
are canopy's agents'; the manifest lists four of them, leaving out get. An OAuth
client uses the REST transport. The spec requires one transport fully implemented,
not both per client. The manifest says `authorizationScheme: first-party`, and
discovery lists `transports_for_oauth_clients: ["rest"]`. Opening MCP to OAuth
clients would need the MCP endpoint to accept `hcpat_` tokens confined to those
tools.

**Discovery claims `HCP-v1-Core`, not Interop.** The OAuth half of Interop is
met. Known Core MUST gaps remain, listed in the document's
`canopy.conformance.known_gaps` and in `hcp-conformance.md`. Per 5.2.1 the
discovery document is authoritative, so it must not overclaim.

**Admins are superusers.** Registering a client puts its name before every person
it asks, which is a fleet-wide decision, the same tier as the fleet hold. The
registry is never on MCP: an agent registering an app that then asks people for
their context is exactly the escalation to rule out.

**Account without a workspace.** Any account that can sign in can already reach
`/people/me`, and can add, correct and export entries there without being
in any workspace. First run points a workspace-less person to it.

## The person's record/use switches

Two features, **record** ("Agents may learn about me") and **use** ("Agents may use
what they've learned"), each with a canopy-level `available` + `default`
(`Person.hcp_*_available` / `hcp_*_default`, `PUT /api/people/me/agent-memory/`
`{record?: {available?, default?}, use?: {…}}`) and a per-session override
(`SessionAgentMemory`, `PUT /api/people/me/sessions/{id}/agent-memory/`
`{record?, use?}` = `on|off|inherit`). Effective for a turn = available AND (that
turn's session's override, else default); a turn with no chat session uses the
defaults; a session never turns on what is not available (403). Everything starts
unavailable; Jonathan is record available + on by default, use not available
(migration `contacts/0019`).

- **record gates WRITES:** HCP add/create/update/delete, the legacy
  `POST /api/people/{id}/facts/` by an agent's login, and the envelope's `record` hint.
- **use gates READS:** HCP search/get, and the facts and `recall` in the envelope's
  `person` block.
- **Switch, then grant.** A refused operation is `scope-denied`: first the switch
  (`hcp_api._principal(need=)` → `hcp.require(person, need, turn)`), then the agent's
  grant (`hcp.NOT_GRANTED`, next section). With nothing granted for what is on, the
  block has `grant: null`.
- **The envelope's block** always carries `hcp: {record, use, available: {record, use},
  session, agent, granted: {record, use}, awaiting_grant}` — `record`/`use` are the
  switch AND this agent's grant; `awaiting_grant` lists features on in this session the
  person has not granted this agent yet (the UI asks, never the agent). An envelope read
  is logged only when a read is actually granted.
- **Record-only** builds a model of the person that no agent sees yet. Without `use` an
  agent cannot search before adding, so duplicates and contradictions are left to HCP's
  conflict quarantine.
- **Nothing is deleted when anything goes off.** The person's own access (page, export,
  audit) never changes.
- **Only the person changes them** — the canopy level as themself, a session's override
  only for a session they created (`created_by`; anyone else 404) — never an agent's
  login, a caller token or an admin; the routes are off MCP.
- **Each change is its own audit event:** `agentRecord|agentUse.enabled|disabled`
  (available), `.defaultOn|defaultOff` (default), and `sessionRecord|sessionUse.
  on|off|inherit` (a session's override) — canopy's own types, beyond the spec's
  required set.
- **Coverage** (`access.md` § "Project participants, coverage, and contact notes")
  counts only people who make record available; a session may still turn it off, and
  coverage does not see that.

## canopy's own agents: grants per agent, only by the person's act

The rule, the owner's explicit choice: *"each agent session / entry should obtain
the grant explicitly or due to previous granting to this agent."*

| Piece | Where |
|---|---|
| The act (domain) | `hcp.issue_agent_grant`, `hcp._record_grant` |
| The act (route) | `POST /api/people/me/sessions/{id}/agent-grants/` — person only, off MCP |
| What the UI asks from | `GET /api/people/me/sessions/{id}/agent-memory/` (+ the widget's read-only `/api/embed/…`): per feature `granted`/`grant`, plus `agent`, `categories`, `session_grant_hours` |
| The prompt | `SessionMemoryToggles.tsx` (chat page and canopy's own widget) |
| What the agent is told | envelope `person.hcp`: `record`/`use` = switch AND grant; `granted`; `awaiting_grant` |
| Retiring presumed grants | migration `contacts/0022_retire_presumed_grants` |

**The client is the agent.** A first-party grant's `client_key` names the agent
only, not how the person reached it (channel) or from where (host). "Previous
granting to this agent" is what is reused; asking again per channel would be
noise for the same agent. A "for this session" grant adds the session to
`attributes`, so it never reaches another session of the same agent.

**Settings are policy, never a grant.** `Person.hcp_*_available/default` bound
what a grant may carry (`available`) and what a session offers (`default`). They
issue nothing. Access for an operation = the session's switch (policy plus the
session's choice) AND a live grant to this agent carrying the scope.

**Two acts, never one (4.1.4).** The prompt shows the agent, the categories, the
actions (read / write), and "for this session only, until <time> or when it
ends". **Allow for this session** is the authorizing act and is temporary, the
default outcome. Only after it does a separate question ask **Keep allowing
<agent>?**, saying first that it carries over to every session and lasts until
revoked. Nothing is preselected, and "Just this session" / "Not now" grant
nothing more. The server enforces the order: `duration=always` is refused unless
this session's temporary grant already carries those features.

**Default on = offered at session start; default off = offered when turned on.**
A feature on by default but not granted to this agent shows the prompt as soon
as the session opens. A default-off feature is offered only after the person
turns it on in that session. Allowing a default-off feature also turns it on for
that session.

**A session grant ends with the session or after 24 hours.** The absolute expiry
(4.1.4's 24-hour ceiling) is fixed when it is issued. An archived session's grant
is marked `expired` the next time it is presented, with `grant.expired` on the
person's log. Unarchiving does not revive it.

**Widening is a new act and a new grant.** Allowing a feature the agent's grant
lacks replaces that grant with one carrying the union: the old one is
`grant.revoked` ("replaced by a wider grant the person gave") and the new one is
its own `grant.issued`. Asking for what is already held changes nothing.

**Until granted, the agent is served as if the feature were off.** The envelope
says which features await a grant, so the hook tells the agent the person is
being asked in the UI. The agent never asks the person to grant or flip anything.

**The widget.** canopy's own widget, signed in as the person, gets the real
prompt (`surface=widget`, `modality=canopy-widget`). A site's widget acting for
its visitor is not the person: it shows "Not granted to <agent> yet —
grant it in canopy" with a link, and its token cannot reach the grant route, so
a host can never grant on a visitor's behalf.

**Contacts can't grant.** A person who reaches an agent only as an email contact
has no session UI and no account, so they have no way to act, and no agent is
granted for them. That is the conformant result. It changes nothing in practice
today: everyone but Jonathan has nothing available.

**No grant is presumed.** A `canopy-control-plane` grant is not the person's act,
so none is active: migration `0022` revokes each, logging a `grant.revoked` on the
person's log saying why. Policy is untouched: a person with record available and on
by default is offered "record" by the next agent they talk to in a session.

## Contacts, from a site canopy trusts for email

Contacts may build up HCP interactions when they opt in from a trusted system, such
as labs, whose email identity canopy trusts. Code: `apps/contacts/contact_hcp_api.py` (`/api/contact/hcp/…`), the widget's
`frontend/src/embed/ContactMemory.tsx`; tests `tests/test_hcp_trusted_contacts.py`.

**Who.** A contact (a widget visitor with no canopy account) on a connected site
with `AppCredential.asserts_verified_email` set, whose arrival assertion signed
`email_verified: true`. The address is recorded on that contact token
(`ContactToken.verified_email`) at the exchange. Nothing else changes: an untrusted
site's address stays recorded-but-not-an-identity (`record_embed_visitor`), and an
anonymous or unverified visitor sees nothing.

**What labs sends.** connect-labs signs `email` + `email_verified: true` when the
address came from Connect's OAuth identity (`connect_labs/labs/canopy.py`). The live
`connect-labs` registration whose JWKS is served from `https://labs.connect.dimagi.com/`
is trusted (migration `tokens/0031`, matched on both, so a tenant that merely named a site "connect-labs" is not trusted).

**Judgment calls.**

- **Superusers decide trust, not workspace owners.** The person a trusted address
  keys is canopy-wide (`Person` keyed on email), not the tenant's. A tenant owner
  trusting their own site could have it claim any address — and read what another
  site's visitors built up under it. Set with `PATCH …/connected-apps/{id}`
  `{asserts_verified_email}` (403 for anyone but a superuser); shown read-only on
  the connected-sites page. **Changing the site's keys clears it**: trust is in the
  signer, and new keys are a new signer.
- **Identity / join rule.** Opting in re-points the contact at the email-keyed
  `Person` (creating it if needed). If the address belongs to a canopy ACCOUNT —
  `user_for_verified_email`, or any `EmailAddress`/`User.email` match at all — the
  contact is refused ("sign in to canopy to manage this"), and a person already
  joined to an account (`Person.user`) is refused too. A contact must never act on,
  or merge into, an account's settings; that person has `/people/me`. Conversely,
  when someone later signs into canopy with that address, `_person_for_user` joins
  the account to the correspondent row for its verified address, so everything the
  contact built up is there.
- **The act is the contact's, in canopy's frame — the frame proof.** The site minted
  the contact token, so the token alone cannot tell the visitor's act from the
  site's call. Every `/api/contact/hcp/` route (except minting one) spends a
  single-use proof (`HcpFrameProof`, 5 minutes, hashed at rest) that is minted only
  to a request that (1) carries canopy's frame cookie `canopy_hcp_frame` — set on the
  embed shell document, `HttpOnly; Secure; SameSite=None; Partitioned`, path-scoped
  to `/api/contact/hcp/` — and (2) the browser marks `Sec-Fetch-Site: same-origin`
  (plus `Origin` = canopy's own on writes). The proof is bound to that cookie and
  that contact. The host page's scripts get none of it: they cannot read an HttpOnly
  cookie, their calls are cross-site and carry no canopy cookie (CORS never allows
  credentials, `embedding/cors.py`), and they cannot read the frame. canopy served on
  the framing site's own origin (the old address's `/canopy` prefix on labs) is refused,
  because there the host's scripts would BE the frame's origin.
  **Boundary:** a host's own SERVER can forge headers and fetch the shell to get a
  cookie, i.e. impersonate its visitor's click. A server that does that can already
  sign anything about its visitor — which is why trust is a superuser decision per
  site. The proof stops the realistic failure (a page script, an XSS, a careless
  integration calling routes), not a site that sets out to lie.
- **The flow.** "Remember me?" in the widget header opens one screen: what canopy
  would keep (categories), from what (this conversation with this agent), under which
  address and confirmed by which site, for how long (this conversation, ≤ 24 h),
  that keeping it longer is separate, and that they can see, download and take it
  back here. "Also let agents use what they've learned" is unticked. Allowing sets
  their policy (record available + on by default; use available only if ticked) and
  issues the per-agent SESSION grant, `grant.issued` modality
  `canopy-embed-trusted-email` with `site=<name>` in the detail; actor
  `contact:<pk>` (type `user` — the person's own act). Then the chat page's own
  toggles and grant/keep prompts work for them through the contact routes;
  "keep allowing" is the same separate act as for members.
- **Their own controls, in the panel** (they cannot sign into canopy while
  `HCP_SERVICE_AUDIENCE=internal`): "What canopy knows" lists their grants (take
  back), their entries (remove — a soft delete), downloads everything (entries +
  audit log), and "Stop remembering me" turns both features off without deleting.
- **The site-keyed person** the contact pointed at before opting in is left in
  place and holds nothing HCP.
- **Not done:** correcting an entry's wording from the panel (remove + tell the agent
  again does it); a superuser toggle in the UI (API only — the frontend has no
  superuser signal to gate it on).

## Who has memory at all — the rollout gate

Narrower than the audience below, and checked first: memory is on only for the
people `HCP_PEOPLE` names.
`HCP_PEOPLE` is a comma-separated list of canopy addresses; **empty means nobody**,
and `*` (everyone) is for tests and local dev only. `HCP_CONTACT_OPT_IN` decides
whether a trusted site's visitors are offered memory at all (production: false).
One helper, `hcp.rollout_allows(person)`, answers it.

Outside the rollout:

- **Every switch reads off** (`hcp.memory_state`), so no agent reads, records or
  recalls, and the caller-context block says so.
- **Every way to turn something on is refused** with 403 "memory isn't available
  for you yet". That covers the canopy-level switches, a session's "on", granting
  an agent, adding to a collection or pointing an agent at one, a contact's
  opt-in (refused before any person is created), the person's own new entries,
  and the OAuth consent screen.
- **Turning off, revoking, removing an entry and downloading always work.** The
  person can always see, export and delete their own data (spec 3.1, 3.3.5).
- **The UI shows nothing**, driven by `memory_enabled` on `/api/me/`, the session
  payloads and the contact state: no "Your memory" links, guide entry, chips,
  prompts or "Let canopy learn from you?". If canopy already holds some of their
  memory (`memory_paused`), "Your memory (paused)" stays reachable with only see,
  remove, revoke and download.
- **Nothing held is touched.** Adding a person to `HCP_PEOPLE` brings back exactly
  the switches, grants and entries they had.

## Internal only — the one gate

`HCP_SERVICE_AUDIENCE` (env, default `internal`) is read in exactly one place,
`hcp_oauth.audience()`.

| | internal (now) | public |
|---|---|---|
| Who may authorize a client | an address on `AUTH_ALLOWED_EMAIL_DOMAIN` (checked at the consent screen, as well as by login) | anyone who can sign in |
| Client registration | admins only | admins only (no self-registration is built) |
| Discovery | published; says registration is closed and who may authorize | the same, with the public audience |

**Before flipping to `public`, ops needs:**

- **Sign-in for people outside Dimagi.** Today login is Google, restricted to
  `AUTH_ALLOWED_EMAIL_DOMAIN`. A public service needs a login path for anyone,
  which is an allauth change outside this gate.
- **Terms of service and a privacy notice** for holding strangers' context, and a
  named data controller.
- **Abuse handling.** That covers client vetting before registration, a way for
  people to report an app, and per-person rate limits beyond the per-client one.
  Spec 4.4.2 also flags rephrased-query sweeps as a pattern to watch.
- **Support:** a channel for "an app has access I didn't mean to give".
- **Retention:** when a person with no workspace and no activity is deleted, and
  what the controller-side log (4.3.5) keeps after a hard delete.
- **Self-service client registration**, if third parties are to onboard without an
  admin. It would need its own review step.
- **Security review** of the consent screen and token endpoint by someone other than
  the author.

## Retrieval: keywords plus meaning (Vertex gemini-embedding-001)

**What runs today** (`hcp.rank`, `apps/contacts/embeddings.py`):
- One search ranks everything the grant reaches together (`workspace_slugs`), so
  the best entry wins wherever it lives — neither the envelope nor
  `searchPreferences` fills from the agent's own workspace first.
- What an agent records during a turn dispatched from a board task is filed
  against that task's project (`hcp.project_of`); a later turn on the same project
  gives those entries `HCP_PROJECT_BOOST` and admits them even with no word in common.
- Unmatched role entries come along as background, at most `ROLE_FLOOR` (every
  agent-written `work_context` entry is filed as a role, so admitting all of them
  would return the newest work_context entries, whatever was asked). Corrections always lead.
- **Embeddings, when `HCP_EMBEDDINGS_MODEL` names a model** (`vertex/gemini-embedding-001`
  today; Bedrock Cohere Embed or Titan v2 also work): each served entry carries a float32 unit
  vector on its own row (`PersonFact.embedding`, with `embedding_model`). Written
  after commit on every write, filled lazily by any search that meets an entry
  without one (`FILL_PER_SEARCH` at a time), all at once by `manage.py hcp_embed`;
  cleared when a version is superseded or deleted. The query is embedded as a
  query, entries as documents. Score = shared words + `HCP_PROJECT_BOOST` +
  `HCP_SEMANTIC_WEIGHT` × similarity (counted at or above `HCP_SEMANTIC_FLOOR`). Any failure ranks
  lexically. Declared method: `hybrid` while on, `lexical` while off.
- Comparison is done in Python over the filtered candidates rather than pgvector:
  the exact scan the constraints below call for at this scale, with no database
  extension and the same code on SQLite in tests. Move it into pgvector if one
  person's served entries reach the thousands.

**Zero retention is checked, not assumed** (`embeddings.retention_ok`). Before the
first call, and again every 15 minutes, canopy asks the provider whether it keeps
what we send; until the answer is yes, nothing leaves canopy and search stays
lexical. On Vertex that is the project's `cacheConfig` saying `disableCache: true`
(Gemini models cache inputs up to 24 h by default). On Bedrock it is the account
having no model-invocation logging (Bedrock itself stores no prompts). Neither
check covers the contract: both clouds are under Dimagi's BAA, and Google's
abuse-monitoring prompt-logging exception is granted per project — Dimagi's
covers connect-labs and dimagi-chrome-extension, not canopy-494811 (requested by
hand). It is on with only Jonathan's own memory in it.

**Turning it on — Vertex (the Dimagi-approved route).** No Google key is stored:
the ECS task's AWS role (`canopy-web-task-role`) is exchanged for a short-lived
Google token by workload identity federation. In the GCP project (here
`canopy-494811`; `NUMBER` is its project number, `gcloud projects describe`):

```
P=canopy-494811; ROLE_ARN=arn:aws:sts::858923557655:assumed-role/canopy-web-task-role
gcloud services enable aiplatform.googleapis.com iamcredentials.googleapis.com sts.googleapis.com --project $P
# 1. No caching of what we send (applies to every region of the project):
curl -X PATCH -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "Content-Type: application/json" \
  https://us-central1-aiplatform.googleapis.com/v1/projects/$P/cacheConfig \
  -d "{\"name\": \"projects/$P/cacheConfig\", \"disableCache\": true}"
# 2. A service account that can only embed and read that setting:
gcloud iam roles create canopyMemoryEmbeddings --project $P \
  --permissions=aiplatform.endpoints.predict,aiplatform.cacheConfigs.get
gcloud iam service-accounts create canopy-memory-embeddings --project $P
SA=canopy-memory-embeddings@$P.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding $P --member serviceAccount:$SA \
  --role projects/$P/roles/canopyMemoryEmbeddings
# 3. Let exactly canopy-web's task role act as it:
gcloud iam workload-identity-pools create canopy-aws --location global --project $P
gcloud iam workload-identity-pools providers create-aws canopy-web --location global \
  --workload-identity-pool canopy-aws --account-id 858923557655 --project $P \
  --attribute-condition "attribute.aws_role=='$ROLE_ARN'"
gcloud iam service-accounts add-iam-policy-binding $SA --project $P --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/NUMBER/locations/global/workloadIdentityPools/canopy-aws/attribute.aws_role/$ROLE_ARN"
```

Then in `deploy/aws/canopy-web.cfn.yaml` set `HCP_EMBEDDINGS_MODEL=vertex/gemini-embedding-001`,
`HCP_VERTEX_PROJECT`, `HCP_VERTEX_WIF_AUDIENCE=//iam.googleapis.com/projects/NUMBER/locations/global/workloadIdentityPools/canopy-aws/providers/canopy-web`
and `HCP_VERTEX_SERVICE_ACCOUNT=$SA`, deploy, and run `manage.py hcp_embed` (it
prints the retention check first and refuses on a no).

The KB reranker (`KB_RERANK_MODEL`, `apps/agents/kb_rerank.py`) calls the Vertex AI
ranking API with the same service account. It needs the API and one more permission
on the same custom role (custom-role support: SUPPORTED); `roles/discoveryengine.viewer`
also carries it but reads every Discovery Engine resource:

```bash
gcloud services enable discoveryengine.googleapis.com --project $P
gcloud iam roles update canopyMemoryEmbeddings --project $P \
  --add-permissions=discoveryengine.rankingConfigs.rank
```

It calls the US multi-region (`us-discoveryengine.googleapis.com`, `locations/us`) by
default; `KB_RERANK_LOCATION` sets `eu` or `global`. Google's HIPAA BAA lists "Agent
Search on Gemini Enterprise Agent Platform" with "Use regional APIs and resource
locations for PHI", so keep it off `global` while queries could carry PHI.

**Turning it on — Bedrock.** Deploy the task role (its `memory-embeddings` policy
grants `bedrock:InvokeModel` on embedding models only, and the read-only logging
check), enable the model under Bedrock model access, set `HCP_EMBEDDINGS_MODEL` to
its id, deploy, run `manage.py hcp_embed`.

Turning either off is clearing the variable; vectors stay but are never read.

### Seeing and tuning search

**What it did, per read — /people/me → What happened.** Every search writes one
`preference.read` per entry it returned (or one saying nothing matched). Each
shows the question asked, the entry handed over, and why: `meaning 0.71`,
`words: dashboard`, `this project`, `a correction (always included)`, or
`background: who they are (role)`; an empty search says `nothing matched
(closest meaning 0.55, cutoff 0.60)`. Only the person can read their audit — no
agent can. Stored in the event's `detail` (`…; why=…; query=…`), so the API's
`/v1/audit` has it too.

**How it is running — the log.** One INFO line per search, in CloudWatch
`/ecs/labs-jj-canopy-web` (kept 30 days; the audit rows above are kept for
good), never with what an entry says:

```
hcp search: person=7 actor=agent:ace method=hybrid candidates=4 compared=4 returned=2 best_meaning=0.71 floor=0.60 embed_ms=180 retrieval=…
```

`candidates` is what the grant reached and was scored; `embed_ms` is the Vertex
round trip (plus filling any missing vectors). These two answer "is it time for
pgvector?" — when `candidates` reaches the thousands, or the scan shows up next to
`embed_ms`. Find them with CloudWatch Logs Insights:
`filter @message like "hcp search:" | parse @message "candidates=* " as c | stats max(c), avg(c)`.

**Why an entry did or didn't come back — `hcp_explain`.** Scores every one of a
person's entries against a question, with each part of the score and the reason;
pass knobs to try a value before setting it:

```
aws ecs execute-command --profile labs --region us-east-1 --cluster labs-jj-cluster \
  --task $(aws ecs list-tasks --profile labs --region us-east-1 --cluster labs-jj-cluster \
           --service-name labs-jj-canopy-web --query 'taskArns[0]' --output text) \
  --container canopy-web --interactive \
  --command "python manage.py hcp_explain --person jjackson@dimagi.com 'who runs the agents?' --floor 0.55"
```

Its read is audited like any other ("Canopy read your memory — to check how
search ranks your memory").

**The knobs** — `deploy/aws/canopy-web.cfn.yaml`, then deploy; no code change:

| Setting | Default | What it does |
|---|---|---|
| `HCP_SEMANTIC_FLOOR` | 0.60 | Closeness in meaning (0–1) an entry needs before meaning counts at all. Lower finds more paraphrases and lets in more noise. gemini-embedding-001 scores unrelated questions 0.45–0.55 against everything; right answers scored 0.66–0.73. |
| `HCP_SEMANTIC_WEIGHT` | 4.0 | What a perfect meaning match is worth, in shared words (0.7 × 4 = 2.8 words). |
| `HCP_PROJECT_BOOST` | 2.0 | What being from the turn's project is worth, in shared words; such entries come back even with nothing else in common. |
| `HCP_EMBEDDINGS_MODEL` | `vertex/gemini-embedding-001` | Empty turns meaning off (words only). A different model needs the floor re-measured. |

Fixed in code (`hcp/relevance.py`), because they are rules rather than dials: corrections
always come along and come first; at most `ROLE_FLOOR` (2) unmatched role entries
ride along; at most 20 entries per search.

**Tuning, in order:** find a read that was wrong on /people/me (copy its question);
run `hcp_explain` with it to see each entry's meaning score; try `--floor` /
`--weight` until the right entries come back and the wrong ones don't; check two or
three unrelated questions still return nothing; set the value in the cfn file and
deploy.

**Design constraints** (hold these when changing retrieval):
- **Stay in Postgres; no separate vector or graph store.** Filter first, then
  rank: grant categories (4.4.1), reach restrictions (4.1.5.1), status and person
  are ordinary `WHERE` clauses, and ranking happens only inside what the grant
  reaches — a store that ranks then filters can silently drop the best matches.
  Scale is one person's few hundred sentences per search, so compare against every
  row (exact, no approximate index).
- **Deletion stays total.** The vector is a column on the entry row, so a hard
  delete removes it in the same transaction. Vectors are personal data (much of the
  sentence can be recovered from one): same permissions as the text. The sentence
  is the source of truth; vectors are only for finding.
- **Vectors from different models never mix.** To change models: embed everything
  with the new model alongside the old, check it on the test set, switch searches
  in one step, drop the old vectors. A provider's model-retirement notice becomes a
  dated fleet task to re-embed.
- **Pick models and ranking by a small fixed test set** of real queries over real
  memory with the entries each should return, not by benchmarks.
- **The agent-facing contract does not change with the ranker:** ≤20 results, no
  paging, provenance redacted (4.4.4), no counts of what was withheld (4.4.5). An
  LLM re-rank of the top 10–20, if added, declares `llm`.
