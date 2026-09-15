# Embedded identity: users, contacts, and what a host is allowed to assert

**Status:** shipped — #793 (contacts beyond email), #794 (signed assertions +
the contact principal), #795 (contact-owned conversations), #797 (the socket),
#798 (the frame). §7's order of work was followed as written.
**Supersedes:** the identity half of `2026-09-12-embedded-agent-widget-v2-design.md`
(§3 credential path, §5a trust invariant), which assumed every widget user is
also a canopy user.
**Depends on:** `apps/contacts` (#791), which is built.

## The question this answers

The widget has one way to learn who a visitor is, and it assumed the visitor
has a canopy login. `Contact` (#791) breaks that assumption: a person can email
`ace@dimagi-ai.com`, be remembered, and be routed on — while having no account
at all. The same person will arrive at a widget.

So: what may a host assert about a visitor, how much should canopy believe it,
and what does the resulting session get to do?

## 1. There are two principals, not one shape with a flag

| | **User** | **Contact** |
| --- | --- | --- |
| Has a canopy login | yes | no |
| Memberships | yes | **never** (`apps/contacts/models.py`) |
| Scope of a widget session | their own ACL | this contact's own threads, nothing else |
| History across devices | yes | only within the host that vouched for them |
| Can drive page actions | yes | yes — the page enforces, not canopy (see §5) |

These are not two settings on one principal. A `DelegatedToken` today resolves
to a `User` and every surface downstream applies that user's ACL. A contact has
no ACL to apply, so a contact that arrives *shaped like* a user is not a smaller
permission — it is an unspecified one.

That is the exact failure this codebase already paid for once. `Agent.workspace`
was nullable, and six separate predicates independently grew a
`workspace_id IS NULL` leg meaning *allow*, because a principal with no tenant
met code written for principals that have one (`ARCHITECTURE.md`). A contact
with no membership meeting code written for members is the same shape.

**Decision.** A contact token is a distinct principal. `request.user` is not set
for one. Every surface fails closed unless it explicitly handles contacts.

## 2. An assertion is graded, never believed

`Contact` already decided this for email, and the reasoning transfers without
change:

> **IDENTITY FROM EMAIL IS AN ASSERTION, NOT A FACT.** … that verdict is stored
> per contact as a grade rather than a boolean, because partner organisations
> run mail servers of wildly varying quality and "unverified" has to remain a
> workable state rather than a rejection.

A host saying "this visitor is `abc`" is the same kind of statement as a mail
server saying "this message is from `abc`". It should join the same ladder
rather than get a parallel notion of trust:

| Grade | Email | Embedded host |
| --- | --- | --- |
| `none` | no auth headers | anonymous visitor; no assertion made |
| `spf` | envelope sender only | **shared secret** — proves the *app*, not the person |
| `dkim` | message signed | **signed JWT assertion**, per visitor, short-lived |
| `dmarc` | signature aligns with the visible `From:` | signed JWT **and** the host is the origin canopy framed |

The shared secret we have today sits at the `spf` tier: it proves a request came
from something holding the app's credential, and says nothing verifiable about
*which* visitor. That is the honest reading, and it is why the "vouch for
dimagi.com" checkbox felt too big — it was a `spf`-grade assertion being given
`dmarc`-grade consequences.

## 3. JWT in, opaque out

The word "JWT" covers two different tokens here and they should go opposite ways.

**Inbound assertion → JWT.** The host signs a short-lived assertion with a
private key; canopy holds only the public key. Standard shape, not bespoke:
RFC 7523's `jwt-bearer` grant, or RFC 8693 token exchange with
`subject_token_type=…:jwt`. Required claims: `iss` (the registered app), `sub`
(the host's own id for the visitor), `aud` (canopy), `exp` ≤ 120s, `jti`
(single-use, replay-cached). What this buys over the shared secret:

- canopy stores nothing that can impersonate anyone. Today it stores a hash that
  verifies a secret which can assert any identity in a domain.
- **Non-repudiation** — which is the point, given the audit trail (#790). Today
  a row says "connect-labs asserted X"; anyone with the secret could have. With
  a signature you can prove which key made that specific claim at that time.
- Rotation without downtime: publish two keys, retire one.

**Session token → stays opaque.** `DelegatedToken` is DB-backed, hashed, TTL'd
and revocable, and #789 made revocation immediate. A self-contained JWT here
would trade revocability for statelessness and then need a revocation list —
a database lookup, which is the opaque token again with extra steps. Contacts
strengthen this: cutting off a contact must be instant.

## 4. Contacts make the boundary *smaller*, not larger

An app asserting **emails** reaches into canopy's own user population, which is
why it needed a domain allowlist and why that grant is uncomfortable. An app
asserting **its own contacts** reaches only its own namespace: `(app, sub)`
cannot collide with canopy's users or another app's contacts.

That is a stronger and more natural bound than domain vouching, and it removes
the setting nobody could explain. **`allowed_delegation_domains` and the
"this site has its own sign-in" checkbox go away** for the widget path; the
domain question survives only for whatever server-to-server callers still use
`/api/auth/token-exchange`.

Note the tenancy consequence, inherited from `Contact` and correct: a contact is
per workspace. One human dealt with by two workspaces is two contacts, "because
merging them would leak one tenant's dealings into another." A host embedding
agents from two workspaces therefore vouches for two contacts, not one.

## 5. What a contact session may actually do

The default is nothing, and each addition is deliberate:

- **Talk to an agent** the app is allowed to offer, in a session owned by that
  contact. Not the workspace's session list — theirs.
- **Read its own history** with that app. Not across apps: the vouching is
  per-app, so the memory is too.
- **Page context and page actions.** Unchanged in kind, because these were never
  authorized by canopy — the page runs the callback in the visitor's own
  browser, under the host's own session, and refuses what it dislikes
  (`apps/canopy_sessions/page_actions.py`). A contact can do on the page exactly
  what the host already lets them do, which is the host's call and not canopy's.
- **Nothing else.** No insights, no agent index, no other sessions, no workspace
  surfaces. Enforced by the principal type, not by each view remembering.

Routing may *read* `Contact.attributes` — the use case that prompted this is
routing to a different agent based on org or opportunity membership — but the
model already forbids authorizing from it: it is "a CACHE of what other systems
say, never the authority … nothing here may be the thing that grants." An agent
resolving what a contact may see calls the source system and honours its ACL in
real time.

## 6. Becoming a user

Already modelled: `Contact.user` plus `services.promote_to_user`, which links
and deliberately grants nothing — "being known and being let in stay two
decisions".

For the widget: when a visitor authenticates to canopy for real, link the
contact and switch the session's principal to the user. The conversation
continues; the scope widens. Nothing merges automatically on a matching email
address, because a matching address is an assertion (§2) and the whole point of
`promote_to_user` being separate is that person-level identity needs an
out-of-band step.

This is also the answer to "what does a full user unlock in-app": not a
different product, the same widget with a wider scope — history across devices
and hosts, their workspace's data, and agents beyond the ones this app offers.

## 7. Order of work

1. **Signed assertions** (§3). Worth doing on its own merits, independent of
   contacts, and it is the smallest self-contained piece.
2. **The contact principal** (§1, §5). The security crux and the largest piece:
   a principal type through the bearer middleware, and every surface failing
   closed for it.
3. **Retire domain vouching** from the widget (§4), once 1 and 2 land.

## Non-goals

- Anonymous widget sessions with no assertion at all. Reasonable eventually
  (`none` is on the ladder); nothing needs it yet, and it would be the first
  surface canopy exposes to a wholly unauthenticated visitor.
- Cross-tenant contact identity. Deliberately excluded by `Contact`.
- Replacing `token-exchange` for server-to-server callers. ace-web uses it; it
  is out of scope here and keeps its domain allowlist.

## What this does not resolve

Whether a contact should be able to reach a page action that mutates *tenant*
data — an insight, a session — rather than host data. The page enforces today,
which is right for host data and unexamined for canopy's own pages, where the
host and the tenant are the same thing. canopy embedding itself has no contacts
today, so this is not urgent, and it will be the moment it does.
