# Site sign-offs: a connected site asks a person to approve something with their passkey

Status: proposed (2026-10-27). First caller: connect-labs, before it mints a full-access MCP token.

## Why

Canopy already asks for a passkey before anything that hands over or changes a secret
(`apps/passkeys/stepup.py`). Other systems need the same thing. Labs wants a person's
passkey behind every full-access MCP token, because today any agent driving a signed-in
browser can open the token page, click approve, and mint itself full access with nobody
involved.

Labs could build its own passkeys, but that would mean a second registration for every
person, a second copy of the WebAuthn code, and a second step-up UI. Canopy has all of these,
and labs is already a connected site that canopy verifies by signature. So the site asks
canopy, canopy asks the person for their passkey, and canopy hands back a signed "yes".

The same feature answers "why may ACE have a full token when an arbitrary agent may not":
**a full-access credential needs a PERSON's passkey approval.** A person approves their own.
An agent's is approved by a person accountable for it: its owner or one of its admins in
canopy. Canopy already knows who those are, and labs does not.

## What it reuses (nothing new is shared)

| Piece | Already exists |
|---|---|
| The site proving it is the site | The site's own signing key, the one canopy verifies its visitor assertions and `GET /api/sites/agents` requests with (`host_grants/site_agents.py: site_for_request`, `verify_request`; `embedding/assertions.keys_for_app`) |
| Canopy proving the answer is canopy's | Canopy's client key (the `Canopy-Execution` key, `kid` in `/oauth/client.json`), which already signs the site agents list (`client_identity.sign_site_agents`). Sites already trust it |
| The person proving it is them, now | `stepup.require_browser`, `require_a_person` and `require_recent_step_up`, plus the SPA's `StepUpDialog` (it also offers to register a first passkey) |
| Who is accountable for an agent | `Agent.owner` and `Agent.is_admin` |
| Rate limits and replay | `embedding/rate_limit` (site-agents limits) and the jti-in-cache pattern in `site_agents.verify_request` |

The new parts are a model (`SiteSignOff`), three site endpoints, one page with two
person endpoints, and an SDK module (`canopy_sdk.sign_offs`).

## The flow

```
site (labs)                         canopy                             approver's browser
-----------                         ------                             ------------------
POST /api/sites/sign-offs  ───────► verify site JWT
  (site-signed request)             resolve subject → approvers
                          ◄──────── {id, approve_url, expires_at, approvers}
send the person to approve_url ────────────────────────────────────►  /sign-offs/<id>
                                                                       sees: site, action, subject
                                    POST /api/sign-offs/<id>/approve ◄─ Approve (passkey step-up)
                                    sign the approval (client key)
                                    303 → return_url?sign_off=<jws> ──► back on the site
verify jws, match its own pending row, mint
   (or, when the approver is not the requester: GET /api/sites/sign-offs/<id> → jws)
```

There are two cases:

- **A person approves their own request** (Jonathan mints a full token on labs). Labs
  redirects to `approve_url`, the person touches their passkey, and canopy redirects back to
  labs with the signed approval. Labs mints in that same browser.
- **An owner approves for an agent** (ACE asks for a full token as `ace@dimagi-ai.com`). The
  request names ACE's account. The approvers are ACE's owner and admins, and the request shows
  in their canopy attention list. The site may also hand `approve_url` to the requester (ACE
  emails it to its owner). After approval, the requester's own site session collects the
  result with the polling endpoint. The token never passes through the approver's browser,
  and the approver does not need to be signed in to the site.

## Wire format

All of it lives in `canopy_sdk.sign_offs`, beside `canopy_sdk.site_agents`, so a site
neither builds nor checks these by hand.

### 1. `POST /api/sites/sign-offs`: the site asks

`Authorization: Bearer <JWT signed by the site's key>`:

- `typ` `canopy-sign-off-request+jwt`; `iss` the site's connected-site name; `aud` this URL;
  `exp` at most 60 s after `iat`; a single-use `jti`; `canopy_workspace` / `canopy_agent` as
  for `GET /api/sites/agents` (which tenant's site this is).
- `subject`: the email the site knows the account by (`ace@dimagi-ai.com`,
  `jjackson@dimagi.com`).
- `action`: `{key, title, detail?}`.
  - `key` is a stable machine name the site chooses (`labs.mcp_token.full`).
  - `title` is the plain sentence the approver reads ("Create a full-access labs MCP token
    for ace@dimagi-ai.com"). At most 200 characters, shown as text, never as HTML.
- `site_ref`: the site's own id for the thing waiting on this (opaque, at most 128 characters).
  It comes back in the approval, so an approval for one pending mint cannot complete another.
- `return_url`: where the approver's browser goes after deciding. It must be https and on an
  origin the site already registered (its frame or redirect origins). Anything else is
  422 `bad_return_url`.

Canopy answers `201 {id, approve_url, expires_at, approvers}`:

- `approvers` is `"subject"` or `"agent_admins"`.
- A request stays open for 24 hours: long enough for an owner to answer an agent's request
  later, and short enough that a stale one is not lying around.

Refusals use the site-agents error codes (`unknown_issuer`, `replayed`, `malformed`,
`rate_limited`), plus `no_approver` (422) when nobody in canopy may approve for that subject
(see below). **The site decides what `no_approver` means for it**; canopy only says it.

### 2. Who may approve: `sign_offs.approvers_for(app, subject)`, the one answer

1. **The subject is a canopy person.** A user with that email who passes
   `login_test_key.is_person`, and is a member of the site's workspace tree, gets
   `"subject"`. Only that person may approve.
2. **Otherwise, the subject is an agent's account.** An `Agent` in the site's tenant whose
   `email` or whose login's (`Agent.user`) email is the subject gets `"agent_admins"`. Its
   owner and anyone `Agent.is_admin` may approve, as long as they are a person. The agent's
   own login never may.
3. **Otherwise** canopy answers `no_approver`.

This is evaluated again when someone approves, not just when the request is made: an admin
removed in between can no longer approve.

### 3. The approval page: `/sign-offs/<id>` (SPA)

The page shows the site's name, the `action.title`, the subject, when it was asked, and, for
an agent, "asked for **ace**, an agent you own/administer". It has two buttons:

- **Approve**: `POST /api/sign-offs/{id}/approve`, `@gate("sign-off-approver", setting=True, step_up=True)`.
  - Requires a browser (`require_browser`), a person (`require_a_person`), and a passkey
    step-up within the last 5 minutes (`require_recent_step_up`). The SPA's existing
    step-up middleware turns the 403 into the passkey dialog and retries the request.
  - Answers 303 to `return_url?sign_off=<jws>&id=<id>`. A JSON caller gets `{state, sign_off}`.
  - A token is refused (403 `browser_session_required`). **No agent or script can approve.**
- **Decline**: `POST /api/sign-offs/{id}/decline`. It needs no step-up and answers 303 to
  `return_url?declined=<id>`.

Anyone who is not an approver gets 404, the same as a missing request. Approving or
declining twice, or after the request expired, is 409 `closed`.

`sign-off-approver` is a new label in `route_gates.VOCABULARY`: "may approve this
sign-off, per `sign_offs.approvers_for`".

### 4. The approval: what the site verifies

A JWS signed by canopy's client key (`client_identity`, the same key and `kid` as the site
agents list):

- `typ` `canopy-sign-off+jwt`; `iss` canopy's client id; `aud` the site's name; `sub` the
  subject; `iat`; `exp` 10 minutes after approval; a `jti`.
- `sign_off`: `{id, action_key, site_ref, approver: {email, name}, approved_at,
  step_up: "passkey"}`.

`canopy_sdk.sign_offs.verify(jws, expect_action=..., expect_site_ref=...)` checks the
signature against canopy's published client key, `aud`, `exp`, the action and the
`site_ref`. The site then:

- spends the `jti` once, on its own side;
- checks `sub` is the account it is minting for.

The approval is not a credential. It completes exactly one pending action that the site
itself created.

### 5. `GET /api/sites/sign-offs/{id}`: the site polls

Signed like (1). It answers `{state: open|approved|declined|expired, sign_off?}`. The JWS
is included once the request is approved, until it expires. This covers the agent case,
where the approver's browser goes back to the site but the requester is somewhere else.

## Records and audit

- **`SiteSignOff`** (in `apps/host_grants`) records:
  - the request: `app`, `subject`, `action_key`, `action_title`, `site_ref`, `return_url`;
  - the outcome: `approvers` kind, `state`, `decided_by`, `decided_at`, `created_at`,
    `expires_at`.
- Every decision is recorded as a `SettingChange` (the route is `setting=True`) and as an
  event `site.sign_off.approved` or `site.sign_off.declined`.
- Approvers see open requests in their attention list, and a "Sign-offs you gave" list in
  Settings → Passkeys.

## Tests

- **Site requests:** a site-signed request makes a sign-off, and an unsigned or replayed one
  is refused. A `return_url` on an origin the site has not registered is refused.
- **Who approves:**
  - A person's own request may be approved by that person only.
  - An agent's request may be approved by its owner or an admin. The agent's own login may
    not approve it, and neither may a member who is not an admin.
  - An admin removed after the request was made can no longer approve.
- **The approve route:**
  - Without a step-up it answers 403 `step_up_required`.
  - With a token it answers 403 `browser_session_required`.
  - After approval, the JWS verifies with the SDK against canopy's published key and carries
    the request's `site_ref` and action.
- **Closing:** declining, a request that has expired, or a second decision all close the
  request.
- **Route gates:** the route-gates test passes with the new label.

## Out of scope here (the other two PRs)

- **connect-labs.** It calls this before minting any full-access credential: the token page's
  create and rotate, the `labs-token-setup` browser page, and the MCP OAuth consent when the
  client asks for the full `mcp` scope. It holds a pending row keyed by `site_ref` and mints
  only after a verified approval. Existing tokens, using tokens, and no-user-visit tokens are
  untouched.
- **ACE.** `ace:labs-token-mint` stops clicking through. It starts the mint as ACE, sends
  the `approve_url` to its owner, waits until the site has the approval, and stores the
  minted token in its canopy vault as it does today.

## Open questions

1. **Labs users with no canopy account.** Most labs users (partner staff) are not in
   canopy, so for them the answer is `no_approver`. Labs decides what happens then. My
   suggestion is that they keep self-service no-user-visit tokens, and a full token is for
   people and agents canopy can vouch for. That is a labs ruling, and canopy only reports it.
2. **Workspace membership for rule 1.** Is "member of the site's workspace tree" the right
   bar for a person approving their own request, or is any canopy person with that email
   enough? Requiring membership is the safer default.
3. **Lifetime of an open request.** 24 hours suits the agent case. Should the person case,
   which redirects at once, be shorter (say 15 minutes)?
