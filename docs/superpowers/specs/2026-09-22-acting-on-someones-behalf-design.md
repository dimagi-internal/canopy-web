# Acting on someone's behalf: a host system starting work for a person who is not there

**Status:** proposed 2026-09-22. Follows `2026-09-18-who-is-asking-initiator-identity-and-access-design.md`
(D1: no dynamic user creation; §2 arrival; D3: a schedule runs bounded by its creator).
**Replaces:** `POST /api/auth/token-exchange` and ace-web's `CANOPY_RUN_ACTOR_FALLBACK_EMAIL`.

## The problem, in one example

An ACE run for the *bednets* opportunity was started by Neal. Later, with nobody in a
browser, ace-web's run dispatcher needs to tell canopy: create the run's session, send
the turn, read the turn back, ask whether any runner can take it, stop it on resume, and
fetch its transcript for cost accounting.

Today ace-web does that by calling `token-exchange` with a **shared secret** and
`acting_as_email: neal@…`. canopy then hands back a token that **is** Neal — with Neal's
full ACL — and, if Neal has no canopy account, **creates one** (and a workspace
membership). If ace-web cannot act as Neal it acts as someone else entirely
(`CANOPY_RUN_ACTOR_FALLBACK_EMAIL`), and the run is recorded as that person's.

Three things are wrong with that:

1. **It records a person asking who did not ask.** Nobody was present. The turn says
   "Neal, signed in" when the truth is "ace-web, for Neal". Every other part of canopy
   now tells those apart (a schedule is `system`, with its creator accountable).
2. **The fallback records the wrong person.** Attributing Neal's run to whoever the
   fallback email names is exactly the confusion `initiator_*` exists to end.
3. **The credential is too big.** A static secret that becomes *any* person in a domain,
   with their full ACL, and creates accounts on the side.

The chat half of ace-web already moved to the SDK's contract (a signed per-visitor
assertion → a user or contact token). That contract is honest *because a person is
present*. It is the wrong tool here, where one is not.

## What we want

1. **The turn says what happened:** `initiator_kind = system`, `via = host:<site>`, and
   the **person the work is for** recorded as *accountable* — a user, or a contact when
   they have no canopy account. Never a fallback person; never a new account.
2. **Access is `site grant ∩ person`.** What the work may do is bounded by the person it
   is for (their relationship to the agent, as for any turn — owner/admin full, a caller
   confined to a capability) AND by what the site was granted. Not the person's whole ACL.
3. **A small, named credential.** A site signs a short-lived request for itself; canopy
   verifies it with the site's **public** key (the same key it already registered for
   chat). Nothing canopy stores can impersonate anybody.
4. **An explicit grant.** Being allowed to embed a chat is not being allowed to start
   work for people who are away. A workspace owner turns it on per site, per agent.

## 1. The principal: "a site, for a person"

A new, separate token type — `SiteDelegationToken` — beside `DelegatedToken` (a user,
present) and `ContactToken` (a contact, present). Separate on purpose, for the reason
`ContactToken` is: a third principal arriving through an existing type would not be a
smaller permission but an *unspecified* one.

| field | meaning |
| --- | --- |
| `app` | the Connected site that asked |
| `on_behalf_of_user` / `on_behalf_of_contact` | exactly one: the person the work is for |
| `purpose` | a short label the site gives (`run:bednets/2`), recorded on every turn |
| `expires_at` | ≤ 1 hour |

It sets neither `request.user` nor `request.contact`. It reaches **one surface**,
`/api/site/…` (below), in the same way `/api/contact/` is the entire surface a contact
token reaches — a tuple in the login middleware, not a decision spread over views.

Every turn it starts carries:

| initiator field | value |
| --- | --- |
| `initiator_kind` | `system` |
| `initiator_via` | `host:<site>` (e.g. `host:ace-web`) |
| `initiator_assurance` | `host_delegated` |
| `initiator_user` / `initiator_contact` | the person it is for (accountable) |

`caller_context.relationship` evaluates the **accountable person**, as D3 does for a
schedule's creator — so Neal's run is full-profile if Neal is ACE's owner or an admin (or
matches a `full:` rule), and confined to a capability if he is a caller. A `system` turn
with no accountable person stays full (canopy's own schedules), unchanged.

## 2. Getting one: a signed request, not a secret

The site signs a JWT with the key it already registered on Connected sites:

```
iss  = <site name>            aud = canopy          exp ≤ 120s    jti = single use
sub  = <site name>            # the site, asking for itself
obo  = {"email": "neal@…", "email_verified": true}   # or {"sub": "<site's own id>"}
purpose = "run:bednets/2"
```

`POST /api/auth/site-token {assertion}` verifies it exactly as `contact-token` does
(asymmetric only, `aud`, `exp` cap, single-use `jti`, per-issuer rate limit), then
resolves `obo` with **the same arrival rule** (`resolve_arrival`): an existing canopy
user the site may resolve → that user; otherwise → a contact in the site's workspace.
**No account is created.** The response names which: `{token, expires_at, kind:
"user" | "contact"}`.

Refused unless the site has the grant (§4).

## 3. What it can do: `/api/site/…`

Exactly the six things background work needs, for the agents the site may offer, on
sessions **this site** created for **this person** — nothing else:

| route | for |
| --- | --- |
| `POST /api/site/sessions` | create the run's session (agent, title, host `metadata` under `host_metadata`) |
| `POST /api/site/sessions/{id}/send` | send the turn (initiator as §1) |
| `POST /api/site/sessions/{id}/stop` | stop its in-flight turns (resume) |
| `GET  /api/site/turns/{id}` | read a turn it started |
| `GET  /api/site/turns/unclaimable` | its queued turns no runner can take |
| `GET  /api/site/turns/{id}/transcript` | the turn's transcript (ingest) |

A session made this way records `created_by = null`, the person on
`on_behalf_of_*`/`contact`, and `metadata.embed_app = <site>` — so the person sees it in
their own list when they next sign in (a user) or through the site (a contact), and no
co-tenant does.

## 4. The grant

A new setting on Connected sites: **"May start work for its people while they are away"**,
per site, with the agents it applies to (a subset of the agents the site may offer).
Owner-only, like the rest of the page, and shown with who turned it on and when. Off by
default. Without it `site-token` answers 403 with the reason.

## 5. What goes away

- `POST /api/auth/token-exchange`, `AppCredential.allowed_delegation_domains`,
  `provision_workspace`/`provision_role`, and the JIT user + membership creation.
- ace-web's `CANOPY_APP_CREDENTIAL` secret and `CANOPY_RUN_ACTOR_FALLBACK_EMAIL`.
  A run whose owner has no canopy account runs **as a contact's work** (confined per
  ACE's interface) rather than as somebody else's.

**Only after** ace-web's run dispatch and ingest have moved to `site-token`, run in
production, and the canopy log shows no `token-exchange` calls for a week.

## 6. Build order

1. canopy-web: `SiteDelegationToken`, `POST /api/auth/site-token`, the grant, the
   `/api/site/…` surface, the initiator shape; `relationship` for `system` + accountable.
2. ace-web: `site_token(owner_email, purpose)` replaces `exchange_token` in run dispatch,
   run state and ingest; the fallback email is deleted; the six calls move to `/api/site/…`.
3. Deploy both; turn the grant on for ace-web (ACE); watch a real run.
4. Remove `token-exchange` and its fields (§5).

## Decisions

- **B1 — Who grants.** *Recommended:* the site's workspace owner turns it on per site and
  per agent (§4). The alternative — each person consenting per site, OAuth-style — is
  stronger but asks every ACE user to click through a consent screen for work they
  already started themselves in ace-web.
- **B2 — A run owner with no canopy account.** *Recommended:* the work is recorded as for
  a contact and confined by ACE's interface, like any contact's. Alternative: refuse to
  dispatch. (Never: a fallback person.)
- **B3 — Reads.** *Recommended:* only turns and transcripts of sessions this site created
  for this person. Not the person's other conversations.
