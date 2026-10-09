# The HCP service

canopy holds each person's **Human Context Protocol** instance (HCP v1, draft 4:
`apps/contacts/hcp.py`, `hcp_api.py`). Until 2026-10-09 only canopy's own agents
could reach it, so it declared `authorization_profile: first-party`. The **HCP
service** lets an application canopy does not operate ask a person, through
OAuth 2.0, for access to parts of their instance. That makes the profile `oauth2`
and the service eligible for HCP v1 Interop (spec 5.2.1).

Jonathan, 2026-10-09: *"fully implement the wholistic model end-to-end, but for now
we're only going to let it be used internally, but design it fully properly."*

This page records the design and every judgment call made building it. The
self-audit against the spec is `hcp-conformance.md`.

## The pieces

| Piece | Where |
|---|---|
| A person's own instance, with or without a workspace | `PersonFact.workspace` NULL = a **personal** entry (`contacts/0020`); `/people/me` |
| Client registry (admins) | `HcpClient`; `/api/hcp-admin/clients` (`hcp_clients_api.py`); page `/hcp-clients` |
| Authorization server | `hcp_oauth.py`, `hcp_oauth_views.py`; issuer `<origin>/api/hcp` |
| Consent screen | `templates/contacts/hcp_consent.html` (act 1), `hcp_keep.html` (act 2) |
| Tokens | `HcpToken` (access 1 h; refresh rotates), `HcpAuthorizationCode` (10 min, single use) |
| Bearer access | `apps/tokens/middleware.py` → `request.hcp_access`; `hcp_api._principal` |
| Revocation notices | `HcpRevocationDelivery`; signed, retried from the runner heartbeat |
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

**Workspace agents never read personal entries.** First-party agent behaviour is
exactly as before: an agent reads only its own workspace. A person's personal
entries reach only the person and the clients they authorize. Letting agents read
them too would be a reasonable later choice, but it would change what agents are
told without the person asking.

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

**Webhooks retry from the runner heartbeat.** canopy-web has no job queue, so
the retention sweep already rides the heartbeat, and revocation notices do the same.
- **First attempt:** a short background thread fires right after commit.
- **Retries:** from the heartbeat, at most once a minute across the fleet.
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

**MCP stays first-party.** The four HCP MCP tools are canopy's agents'. An OAuth
client uses the REST transport. The spec requires one transport fully implemented,
not both per client. The manifest says `authorizationScheme: first-party`, and
discovery lists `transports_for_oauth_clients: ["rest"]`. Opening MCP to OAuth
clients would need the MCP endpoint to accept `hcpat_` tokens confined to those
four tools.

**Discovery claims `HCP-v1-Core`, not Interop.** The OAuth half of Interop is
met. Known Core MUST gaps remain, listed in the document's
`canopy.conformance.known_gaps` and in `hcp-conformance.md`. Per 5.2.1 the
discovery document is authoritative, so it must not overclaim.

**Admins are superusers.** Registering a client puts its name before every person
it asks, which is a fleet-wide decision, the same tier as the fleet hold. The
registry is never on MCP: an agent registering an app that then asks people for
their context is exactly the escalation to rule out.

**Account without a workspace.** Any account that can sign in can already reach
`/people/me`. It now can also add, correct and export entries there without being
in any workspace. First run points a workspace-less person to it.

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
