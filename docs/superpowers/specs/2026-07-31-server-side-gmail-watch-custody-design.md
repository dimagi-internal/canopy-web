# Keeping a Gmail watch armed without a runner

**Date:** 2026-07-31
**Status:** decided, not built
**Builds on:** `2026-07-30-event-log-and-inbound-push-design.md` (the doorbell)

## The constraint

A Gmail `users.watch` registration expires within 7 days and Google will not
renew it. Something must re-arm it, forever, as the mailbox. Today that is the
**runner** (`gmail_watch.py`), for a reason recorded in the doorbell spec: the
call must be made AS the mailbox, a service account can only do that via
domain-wide delegation, and `dimagi-ai.com` is a *secondary domain inside the
dimagi.com Workspace org*, so DWD there would cover every `dimagi.com` mailbox.
Per-mailbox OAuth grants are strictly narrower, and they live where the runner is.

That is correct for us and does not generalize. It makes push a property of
*having a laptop turned on*, which is not something we can ask of anyone else.

## Why this is not a 7-day emergency today

Worth stating so nobody builds this in a panic. If no runner runs for seven days,
nobody is reading mail anyway — the read is also the runner's job — so a lapsed
watch adds no failure mode in that window. On return, the stored expiry is in the
past, the mailbox is due, and it re-arms on the next tick. It self-heals with no
repair step. The problem this spec solves is **productization**, not fragility.

## Rejected: domain-wide delegation

The obvious server-side answer, and wrong for everyone rather than just for us.
DWD is granted per Workspace and cannot be scoped to a domain, an OU, or a user
list. Asking a prospective tenant to grant canopy access to every mailbox in
their organisation — in order to watch two agent addresses — is not a trade any
serious admin makes, and should not be offered. It is off the table permanently,
not pending a security review.

## The decision: per-mailbox OAuth, granted at the individual account, held by canopy-web

The mailbox owner signs in **as that mailbox** and grants canopy-web access to it.
Canopy stores the refresh token encrypted and re-arms that mailbox's watch on a
server-side schedule. No admin console, no domain-wide anything, and the blast
radius is exactly the accounts that consented.

This is the same narrowness the runner already relies on. The only thing that
moves is *custody* — from a laptop keychain to the web app.

### The scope is `gmail.metadata`, and that is what makes it acceptable

Verified against the live Gmail discovery document: `users.watch` accepts
`https://mail.google.com/`, `gmail.modify`, `gmail.readonly`, **and
`gmail.metadata`**.

Metadata is the one to ask for. It permits headers, labels and thread ids, and
**cannot read message bodies or attachments**. So the credential canopy-web would
hold is *strictly weaker than the one the runner holds today* (`gmail.modify`,
which can read bodies and send as the agent). Moving custody server-side is
therefore not an escalation of trust in aggregate — it is a narrower credential in
a different place.

The doorbell's founding property largely survives: canopy-web still does not read
mail, and a forged push still causes at most a `gog` read that finds nothing.

**What it does expose, stated plainly:** subject lines, and who is corresponding
with whom. That is real, and for some tenants it will be the objection. It is a
very different conversation from handing over a domain, but it is not nothing, and
the consent screen should not pretend otherwise.

## What this costs

- **Google verification.** `gmail.metadata` is a restricted scope. A
  third-party-facing canopy needs the security assessment — recurring, and not
  cheap. This, not the architecture, is the real gate on "used by others."
- **A server-side scheduler.** Canopy deliberately has none: scheduled turns are
  runner-fired precisely to avoid a second execution engine. Watch renewal is a
  daily job with no per-tenant fan-out, so an ECS scheduled task or a cron'd
  management command is the whole of it — but it is a new deploy surface and
  should be named as one.
- **Token custody.** Encrypted at rest, per mailbox. Precedent exists:
  `RunnerCredential` already stores a secret bundle non-clobbering per field.
- **Renewal must be daily, not weekly.** Google's ceiling is 7 days; re-arming
  daily leaves six consecutive failures' worth of slack before push actually
  lapses, which is the same margin `gmail_watch.py` already uses.

## The shape this implies: three tiers, not one answer

1. **Poll** — the default, and the honest answer for a tenant who will grant
   nothing. Push buys ~1s instead of ~300s; it is a latency optimization, not a
   correctness requirement. The interval is a setting, and Gmail's quota is
   generous enough that a handful of mailboxes at 30s is unremarkable.
2. **Push with server-side custody** — this spec. For tenants who will grant a
   metadata-scoped token per agent mailbox.
3. **Push with self-hosted arming** — today's behaviour, for any tenant running
   their own always-on box. Nothing to build; it already works.

Tier 1 must stay first-class. A tenant should be able to refuse custody entirely
and still have working mail, slower.

## Not built

Nothing here is implemented. `watch_topic` is still per-workspace (see the known
limit in CLAUDE.md), arming is still the runner's, and for dimagi it should stay
that way — DWD is unavailable to us and we run our own boxes. Build this when a
tenant who is not us actually wants push.

The cheap intermediate, if the laptop dependency ever bites us specifically: the
**cloud runner** already has gog credentials provisioned by `bootstrap_agents.sh`
but contains no inbox or watch code at all. Teaching it the watch tick removes the
laptop dependency without moving a single credential into canopy-web.
