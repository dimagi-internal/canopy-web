# Acting as the embedded visitor: host-issued grants, not canopy-minted assertions

**Status:** decided, 2026-09-26 (open questions resolved below). Replaces the canopy → host half of
`act_on_behalf_of_caller` (`apps/tokens/onbehalf.py`), which is inert today (its
signing key is a CFN `PLACEHOLDER` and no host verifies it). Also replaces the
per-page `pages:` capability model in `apps/agents/interface.py` with a per-site
ceiling.

## The problem, in the incident that raised it

Gillian, a Dimagi colleague with no canopy account, drove ACE and Hal from the
connect-labs marketplace widget and from Slack (2026-09-26). Two things surfaced:

1. **An agent's tools run as the AGENT, not the visitor.** On the marketplace page,
   ACE calls `connect_labs__marketplace_*` with its own credentials. So what a
   visitor may reach has to be enumerated by hand in ACE's interface, page by page
   (`pages: ["labs-marketplace://*"]` + a tool list). That does not scale to "turn
   the widget on for every page in Connect", and it means every visitor acts with
   ACE's access, not their own.
2. **The planned fix is a new hole.** `act_on_behalf_of_caller` has canopy sign a
   120-second JWT saying "this is Gillian" with canopy's own private key, which the
   host would then trust. Whoever holds that key can sign for *any* subject, so a
   compromise of canopy becomes the ability to impersonate every user of every
   connected site. That is a bigger prize than anything canopy holds today.

We want per-visitor permissions without inventing a protocol and without making
canopy an identity authority.

## What the industry settled on (researched 2026-09-26)

- **MCP forbids token passthrough and requires audience-bound tokens.** "MCP
  servers MUST NOT accept any tokens that were not explicitly issued for the MCP
  server" (MCP Security Best Practices, 2026-07-28). A token must be minted *for*
  the resource that receives it (RFC 8707 resource indicators, RFC 9068 `aud`).
- **MCP's answer to "a client acts for a user at a server whose login it doesn't
  own" is Enterprise-Managed Authorization (EMA).** Stable as an MCP extension since
  2026-06-18 (`modelcontextprotocol/ext-auth`). The pattern:
  1. The party that authenticated the user issues an **Identity Assertion JWT
     Authorization Grant (ID-JAG)**: `typ: oauth-id-jag+jwt`, with `iss`, `sub`
     (the user), `aud` (the resource's authorization server), `client_id` (who may
     redeem it), `resource` (the MCP server), `scope`, short `exp`, `jti`. It is
     obtained with an RFC 8693 token exchange.
  2. The client redeems it at the resource's own authorization server with the
     **RFC 7523 JWT-bearer grant**.
  3. That authorization server validates it and issues an access token that "MUST
     be audience-restricted to the MCP Server identified by the `resource` claim".

  The IETF draft is `draft-ietf-oauth-identity-assertion-authz-grant`, and its
  cross-domain generalisation is `draft-ietf-oauth-identity-chaining`. Okta is the
  only IdP shipping it (Cross App Access); Google does not.
- **Delegation is expressed with the RFC 8693 `act` claim.** `sub` = the user,
  `act.sub` = the agent acting for them. The `draft-oauth-ai-agents-on-behalf-of-user`
  flow is an expired individual draft, so don't build on it. `act` itself is RFC.
- **The 2026-07-28 MCP spec** also: requires clients to validate `iss` (RFC 9207),
  binds client credentials to the issuing authorization server, and deprecates
  Dynamic Client Registration in favour of **Client ID Metadata Documents (CIMD)**.
- **What EMA does not do:** per-call authorization. It decides whether this user
  may connect this client to this server, not whether a particular tool call should
  go through. That stays the resource server's ACL plus canopy's own ceiling.

## The principle this design takes from it

**The party that authenticated the user issues the grant. Canopy only redeems.**

Canopy never holds a key that any host trusts to assert a user's identity. A
grant names one user, one client (canopy), one resource and one short window, and
it is signed by the host's key. The host's key never leaves the host.

## Design

### Actors

| Role | Who | Holds |
|---|---|---|
| Identity issuer | The **host** (connect-labs), where Gillian is signed in | Its own signing key (already used for the inbound contact assertion) |
| Client | **canopy-web** | A client key registered with the host (`private_key_jwt`); **no** user-signing key |
| Authorization server | The **host's** existing OAuth server (connect-labs' MCP already accepts OAuth) | Validates grants and issues access tokens for its MCP |
| Resource server | The host's **MCP server** | Its own ACL: what Gillian can see as Gillian |
| Actor | The agent (`ace`) running on a runner | Nothing at rest; gets one access token per turn |

### Flow

1. **Arrival (exists today, extended).** The widget loads on a host page. The host's
   server already signs an assertion about the visitor for canopy
   (`apps/tokens/assertions.py`: asymmetric only, `aud` enforced, `exp` ≤ 120s,
   single-use `jti`). **Add:** in the same call, the host also issues an **ID-JAG**
   for its own MCP:
   - `iss` = host, `sub` = the host's id for the visitor
   - `aud` = the host's authorization-server issuer, `client_id` = canopy's client id
   - `resource` = the host's MCP URL, `scope` = what this page offers (e.g.
     `marketplace:read`)
   - `exp` ≤ 5 min, `jti`
   - `act` = `{sub: "canopy:agent:<slug>"}` naming the agent the widget is for

   The host decides the scope because the host knows the page and the person. This
   replaces the owner's hand-written per-page tool list.
2. **Redeem (canopy-web, server side).** Canopy-web POSTs the ID-JAG to the host's
   token endpoint with `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`,
   authenticating with `private_key_jwt`. It gets back an access token with
   `aud` = the host MCP, `sub` = the visitor, `act.sub` = the agent, and the page's
   scope, sender-constrained with **DPoP (RFC 9449)** to canopy-web's key. Canopy
   stores it encrypted, attached to the conversation, never to the agent or runner.
   It may be refreshed only by a fresh ID-JAG from the host, i.e. while the visitor
   is still on the page.
3. **Use: canopy-web is the gateway; the token never leaves it.** In a visitor's
   turn the agent does not call the host's MCP directly. It calls the host's tools
   through canopy's own MCP (`/api/mcp/`), which checks that this turn may call
   that tool (the ceiling below), attaches the visitor's access token and a fresh
   DPoP proof server-side, and forwards the call. This is not token passthrough:
   the token was issued *to canopy* for the host's MCP (`aud`), and canopy is the
   client using it. It follows that:
   - **no runner ever holds a visitor token or the DPoP key.** On a laptop every
     session is the same OS user, so anything a runner holds is one hook away
     from the agent;
   - the ceiling is enforced **server-side**, not only by `profile_guard`;
   - every call lands in one audit log, and revoking the conversation stops it
     immediately.

   The cost is one network hop per tool call. Canopy-web is already on the path
   of every turn, so this adds no new place to fail.
4. **Enforce (the host).** The host's MCP validates `aud`, the DPoP proof, `exp`
   and scope, then runs the tool **as Gillian**. Her rows, her permissions. Its
   audit log records `sub=gillian act=ace client=canopy`.

### What a compromise of canopy can and cannot do

| Attacker holds | Can | Cannot |
|---|---|---|
| canopy-web's DB + client key | Use access tokens for visitors **with a live conversation**, until they expire, within that page's scope; redeem an ID-JAG it captured in the ≤5-min window, once | Mint a grant for anyone. There is no user-signing key to steal. Reach users who never opened the widget. Use a token against any other resource (`aud`). |
| A runner (laptop or cloud) | Make calls through the gateway for the turn **that runner is executing**, within its ceiling | Obtain any visitor token or DPoP key: they never leave canopy-web |
| A leaked access token (logs) | Nothing without canopy's DPoP key | Replay it |

Today's design fails the first row: canopy's key could sign for anyone at any
connected site.

### Strengthening the canopy ↔ host handshake

Every step reuses a standard, and each closes a specific gap:

- **Registration via CIMD.** Canopy publishes a Client ID Metadata Document at a
  stable HTTPS URL (its `client_id`), naming its JWKS. Each host allowlists that one
  URL. This replaces pasting keys between systems, and it is what MCP 2026-07-28
  recommends in place of DCR.
- **Host metadata via RFC 8414 / RFC 9728.** Canopy discovers the host's issuer,
  token endpoint and JWKS from well-known metadata instead of per-site config
  typed into canopy. Validate `iss` on every response (RFC 9207).
- **Keys stay where they belong.** Host → canopy assertions are verified with the
  host's public key (unchanged). Canopy → host is `private_key_jwt` + DPoP, both
  canopy's own keys and neither able to assert a user. Keys rotate through JWKS
  with no coordination.
- **Retire canopy's user-signing key.** Delete `onbehalf.mint`, the
  `act_on_behalf_of_caller` tool, the public on-behalf-of JWKS endpoint and the CFN
  `PLACEHOLDER` secret. It was never turned on, so nothing depends on it.

### What happens to the interface's `pages:` model

The per-page capability becomes a **per-site ceiling**, and the page no longer has
to be listed in the agent's interface at all:

```yaml
capabilities:
  connect:
    callers: [contact, member]
    sites: [connect-labs]              # a registered Connected site
    ceiling: ["mcp__*connect_labs__*"] # the most any page on it can unlock
```

At turn time the session's tools are the page's declared `backing_tool`(s),
intersected with the ceiling, plus canopy's basics (`current_page`,
`who_is_asking`). Because the calls now run as the visitor, a broad ceiling is
safe: the host's own ACL decides what each person reaches. Before step 4 exists
for a host, its ceiling must stay read-only, because the calls still run as the
agent.

### Per-call authorization

EMA deliberately stops at connection time, so per-call checks come from two
layers, each able to refuse:
1. **Canopy's ceiling + `profile_guard`**: the tool must be allowed for this site.
2. **The host's ACL, as the visitor**: the call must be something Gillian may do.

A write the visitor may do but the page should not trigger unattended stays with
the host, e.g. its own step-up or MCP's `input_required` (MRTR) confirmation.

## Where this does not apply

- **Slack and email callers** have no host session, so no ID-JAG. Their access is
  canopy's `full:` / capability rules (Slack full members are now `slack_member`,
  verified; #982). Their tool calls run as the agent, within those rules.
- **A real enterprise IdP.** If Dimagi moves to an IdP that issues ID-JAGs (Okta),
  step 1's issuer can become the IdP with no change to steps 2–4. That is exactly
  why we use this grant shape rather than our own.

## Build order

1. **connect-labs:** add the `jwt-bearer` grant to its OAuth server (with `act`,
   `resource`, DPoP), and issue an ID-JAG alongside the existing contact assertion.
2. **canopy-web:** CIMD document + JWKS; redeem at arrival; store encrypted per
   conversation; per-turn issuance endpoint; delete `onbehalf`.
3. **canopy-web gateway:** visitor turns reach host tools through canopy's MCP,
   which attaches the token + DPoP proof. The agent's direct connection to the
   host's MCP is not in a visitor turn's profile.
4. **Interface:** `sites:` + `ceiling:` replace `pages:`; move ACE's `marketplace`
   capability over.
5. **Docs:** `embedding-a-canopy-agent.md` §8a ("per-visitor tool permissions do
   not exist yet") is rewritten, and `test_embedding_doc_is_true.py` pins the new
   contract.

## Decisions (were open questions)

**1. The visitor leaves before the agent finishes: no presence, no access, and
never a fallback.**
- The token lives only while the page is open. The widget re-obtains a fresh
  ≤5-minute ID-JAG through the host's server every few minutes, so access ends
  within minutes of the tab closing.
- **A host call in a visitor's turn uses the visitor's token or fails.** It never
  falls back to the agent's own credential, which would give the visitor whatever
  the agent can reach. This is the GitHub delegation's "409 and no shared
  fallback" rule. The agent tells the visitor it needs them back on the page, and
  the conversation continues when they return.
- Unattended work on the visitor's behalf is a later, explicit opt-in: a consent
  screen at the host ("let ACE keep working for you for 24h") issuing a narrower,
  longer-lived grant. Not built until a workflow needs it.

**2. DPoP is required from day one, per client, not globally.**
- The host's MCP enforces DPoP for tokens issued through canopy's jwt-bearer grant
  (they carry `cnf.jkt`). Ordinary tokens from the host's normal OAuth flow, and
  personal access tokens, are untouched. **People using the host's MCP directly
  from Claude Code or any other client see no change**, and a client that cannot
  do DPoP keeps working.
- Tokens from canopy's grant carry `client_id=canopy` and `act.sub=<agent>`, so
  the host can tell "Gillian directly" from "ACE acting for Gillian" and treat the
  second more narrowly.
- DPoP is cheap because of decision 3 above (the gateway): the key lives in exactly
  one place, canopy-web.

**3. The HOST decides a page's scopes, server-side, from its own route registry.**
- The page's JavaScript is untrusted, so scopes are never taken from anything the
  browser sends. When the host issues the ID-JAG, it looks up the page's route in a
  server-side registry (e.g. marketplace → `marketplace:read`). The default is
  read-only, and an unregistered page gets no grant.
- **Writes need a write scope *and* a per-call confirmation from the visitor.** The
  host returns MCP `input_required` (the MRTR pattern, 2026-07-28), showing the
  visitor exactly what will change before it does.
- A call succeeds only if three independent parties allow it: the **host's page
  scope**, the **owner's per-site ceiling**, and the **visitor's own ACL at the
  host**. Canopy records the granted scope on the turn, so an owner can see what a
  visitor's turn could do.

## Sources

- MCP Security Best Practices (2026-07-28): https://modelcontextprotocol.io/specification/2026-07-28/basic/security_best_practices
- MCP 2026-07-28 changelog: https://modelcontextprotocol.io/specification/2026-07-28/changelog
- Enterprise-Managed Authorization extension: https://github.com/modelcontextprotocol/ext-auth/blob/main/specification/stable/enterprise-managed-authorization.mdx
- EMA announcement: https://blog.modelcontextprotocol.io/posts/enterprise-managed-auth/
- What ID-JAG fixes and doesn't: https://bex.co/blog/2026/07/09/mcp-enterprise-managed-authorization-id-jag
- On-Behalf-Of for AI agents (expired individual draft): https://datatracker.ietf.org/doc/html/draft-oauth-ai-agents-on-behalf-of-user-02
- OAuth actor profile for delegation: https://datatracker.ietf.org/doc/html/draft-mcguinness-oauth-actor-profile-00
- RFC 8693 (token exchange, `act`), RFC 7523 (JWT bearer), RFC 8707 (resource indicators), RFC 9449 (DPoP), RFC 9207 (`iss`), RFC 8414 / RFC 9728 (metadata)
