# Workspace email domains — design

**Status:** design approved in conversation 2026-10-12 (Jonathan); this spec awaits review.
**First tenant:** `marshellis` (`marshellis.com`, DNS on Cloudflare, mail on Zoho today).

## Intent

A workspace owner or admin can bring their own domain to canopy-web and run its email
through it — no Google Workspace, no per-seat mailbox fees:

- **Agent addresses** (`ada@marshellis.com`) receive mail that becomes the agent's
  email turn, and the agent sends as that address. Threads are readable on canopy-web.
- **Every other address** is forwarded to a canopy user (`jjackson@marshellis.com` →
  `jjackson@dimagi.com`), with a catch-all so registration addresses nobody remembers
  still arrive.
- Fully configurable from the UI; first-class for any workspace, not a marshellis special.

**Success:** marshellis.com's MX points at SES, Zoho is gone, mail to `jjackson@` and
`support@` (and anything else) lands in jjackson@dimagi.com, and an agent in the
marshellis workspace can hold a two-way email thread with nothing Google involved.

**Not in v1:** human inboxes / IMAP / webmail; replying to forwarded mail *as*
`@domain`; per-workspace AWS accounts; automatic DNS writes (Cloudflare token);
admission-rule auto-replies for SES mailboxes (they stay pending, as for any mailbox
canopy-web cannot send from); bulk/marketing mail. The model allows several domains
per workspace; the UI ships handling one.

## Shape: one removable product app

New app **`apps/mail/`**, **product tier**. Framework apps never import it (enforced by
`tests/test_architecture_boundary.py`); it imports framework (`harness`, `workspaces`,
`agents`, `common`) freely. Ripping it out follows the ordered rip-out list in
`docs/architecture/mail.md` (`manage.py migrate mail zero` first, then the app, its
tests/fixtures/conftest block, registry lines in settings/api/route_gates/middleware/the
boundary test, the frontend api module, pages, nav tab, routes, guide descriptors and the
regenerated types, the docs lines, and the `canopy-web-mail` stack plus its retained
bucket and the `Mail*` parameters). Nothing else changes, because:

- It does **not** extend `apps/inbound` (`InboundMailbox` is Gmail-push routing and stays
  Gmail-only). SES mailboxes are a separate routing table owned by `mail`; the runner's
  `/runner-mailboxes` never lists them, so no runner polls them with `gog`.
- It gates on the existing `perms.INTEGRATIONS` capability (owner/admin) rather than
  adding a capability to `apps/workspaces`.

## Infrastructure (labs account 858923557655, us-east-1)

Verified 2026-10-12: the account has **no receipt rule sets** (canopy can own the active
one), production access, 50k/day quota, identities `canopy.dimagi.com` and
`labs.connect.dimagi.com`.

New stack **`deploy/aws/canopy-mail.cfn.yaml`** (`canopy-web-mail`), deployed by hand
like `canopy-email.cfn.yaml` (the deploy role holds no `ses:*`):

| Resource | Purpose |
|---|---|
| S3 bucket `canopy-inbound-mail-<acct>` | SES writes raw MIME to `incoming/`. Lifecycle: `incoming/` expires after **7 days**; `kept/` (agent mail, copied on ingest) has no expiry. Bucket policy lets only SES (this account, this rule) put. SSE-S3, public access blocked. |
| SNS topic `canopy-inbound-mail` | The receipt rule's S3 action notifies it (object key only — never content, so the 150 KB SNS limit is irrelevant). HTTPS subscription → canopy-web `/api/mail/ses/inbound/`. |
| Receipt rule set `canopy-inbound` (active) with ONE rule `canopy-all` | **No recipient filter**, `ScanEnabled: true`, TLS required, one S3 action (bucket + `incoming/` + topic). SES only accepts mail for verified identities, and canopy-web drops mail for any domain no workspace owns — so the rule never changes when a domain is added. |
| Configuration set `canopy-tenant-mail` | Every tenant send uses it — separate from connect-labs' `labs-jj-email`. Event destination (SEND, DELIVERY, BOUNCE, COMPLAINT, REJECT) → SNS topic `canopy-mail-events` → `/api/mail/ses/events/`. |
| IAM policy on `labs-jj-ecs-task-role` | `ses:CreateEmailIdentity`, `GetEmailIdentity`, `DeleteEmailIdentity`, `PutEmailIdentityMailFromAttributes`, `PutEmailIdentityDkimSigningAttributes` on `identity/*`; `ses:SendRawEmail`/`SendEmail` on `identity/*` with a `ses:FromAddress` condition barring `*@dimagi.com` / `*@connect.dimagi.com` (defence in depth for this policy only — IAM allows are additive; the real guard is app-level, and the `canopy-tenant-mail` configuration set is enforced in app code, `ses.send_raw` always passes it, not by IAM); `s3:GetObject`/`PutObject`/`CopyObject` on the bucket. |

The task role is shared with connect-labs; the app only ever sends from a `MailDomain` address (the IAM condition is a backstop).

## Data model (`apps/mail/models.py`)

- **`MailDomain`** — `workspace` FK (NOT NULL), `name` (globally **unique**: one workspace
  per domain, SES identities are account-global), `status` (`pending` → `verified` →
  `suspended`), `receiving` (bool, from a live MX check), `mail_from_subdomain`
  (default `bounce`), `dkim_tokens` (JSON), `suspended_reason`, `daily_send_cap`
  (default 200), timestamps. Routing is **inert until `verified`** — DKIM verifying is
  the proof of DNS control.
- **`MailRoute`** — `domain` FK, `local_part` (`*` = catch-all; unique per domain),
  `kind` (`agent` | `forward` | `drop`), `agent` FK (kind=agent), `forward_to` **User FK**
  (kind=forward; the user must be a member of the workspace — **forwarding only ever
  goes to a canopy user's own login email**, never a typed address), `enabled`.
  Exact local-part match wins over `*`. Reserved local part `canopy-forwarder`
  (the From of forwarded mail) cannot be routed.
  The catch-all can forward or drop, never belong to an agent.
- **`MailThread`** — `workspace`, `route` (the agent route it belongs to; `SET_NULL`, so
  removing a route or domain keeps the thread), `address` (snapshotted from the route), public
  `uuid` (the `thread_id` agents and turns carry), `subject`, `last_message_at`,
  `message_count`.
- **`MailMessage`** — `thread`, `direction` (`in`/`out`), `message_id` (RFC 5322,
  unique per workspace + mailbox — the dedupe key for SNS redelivery; one email to two
  agents is two rows), `in_reply_to`,
  `references`, `from_addr`, `to`, `cc`, `subject`, `text`, `html` (sanitised on render,
  never `allow-same-origin`), `raw_key` (S3, `kept/…`), `verdicts` (SES spam / virus /
  SPF / DKIM / DMARC), `delivery_status` (out: `sent`/`delivered`/`bounced`/`complained`).
- **`MailAttachment`** — `message`, `filename`, `content_type`, `size`, `s3_key`.
- **`MailForwardLog`** — one row per forwarded message: domain, original from/to/subject,
  destination user, `status`, `error`, timestamps. No body (canopy does not keep human
  mail it only passes along — the raw object expires with `incoming/`).

Threading: an inbound message joins the thread whose messages' `message_id` appears in
its `In-Reply-To`/`References`; otherwise it starts a new thread. Outbound replies set
both headers from the thread.

## Flows

### Add a domain (Settings → Email, `/w/:ws/settings/email`)
1. Admin enters the domain. Server: uniqueness check → `CreateEmailIdentity` (Easy DKIM,
   RSA 2048) + `PutEmailIdentityMailFromAttributes` (`bounce.<domain>`,
   `BehaviorOnMxFailure=USE_DEFAULT_VALUE`) → row `pending`.
2. The page shows every record with copy buttons and a live ✓/✗ per row (server-side DNS
   lookup, cached ~60 s): 3 DKIM CNAMEs; MAIL FROM MX + SPF TXT on `bounce.<domain>`;
   root SPF (a **merged** suggestion when one exists, e.g.
   `v=spf1 include:zohomail.com include:amazonses.com -all`); a starter DMARC
   `p=none; rua=` record; and the root MX → `inbound-smtp.us-east-1.amazonaws.com`.
3. Status flips to `verified` when SES reports DKIM success — refreshed lazily on every
   domain read (and `manage.py mail_refresh`); canopy-web has no periodic scheduler and
   this needs none. Sending works from here.
4. **MX is shown as its own "Receiving" status.** Before it is live the page names the
   current MX provider ("Zoho currently receives mail for this domain — switching MX
   moves all of it here") so nobody breaks a working mailbox by accident.

### Every agent gets an address (`MailDomain.agent_addresses`, `auto_addresses.py`)

The domain's owner can switch on "Give every agent an address". Every agent in a workspace
that may use the domain (its own workspace, and every workspace it is shared with and the
workspaces below those) then holds `<slug>@<domain>` as a real agent route marked `auto`.
Nothing downstream knows the difference: threads, replies, `send --from` and the agent's
address list treat it like a hand-made address, and an agent keeps any other inboxes it has.
`sync` keeps the automatic routes true when the switch flips, a share is added or removed,
an agent is created or moved (post_save, after commit), or a workspace is re-parented. It
never takes a held address (skipped, and reported to that agent's own workspace only); an
admin who edits an automatic route takes it over, and a hand removal is refused (untick
Enabled instead).

### Inbound
SES → S3 `incoming/<id>` + SNS → `POST /api/mail/ses/inbound/`:
1. Verify the SNS signature (cert URL host must be `sns.<region>.amazonaws.com`, the
   region taken from our topic ARN; topic ARN must be ours); handle `SubscriptionConfirmation` by fetching the SubscribeURL once.
   Verify before parsing anything else.
2. Fetch the raw object, parse (stdlib `email`). Look up the domain of each recipient
   (a subdomain resolves to its verified parent, so `x@mail.marshellis.com` uses
   marshellis.com's routes); unknown or unverified domain → drop + log. Spam/virus verdict FAIL → drop + log
   (never forward or wake an agent on it).
3. Resolve the route (exact → `*` → none = drop). Then:
   - **agent:** dedupe on `message_id`, copy raw to `kept/`, store message +
     attachments, thread it, then enqueue the turn (below).
   - **forward:** re-send through SES as `"<Orig Name> via <domain>"
     <canopy-forwarder@<domain>>`, `Reply-To:` original sender, original
     To/Date/subject, `X-Original-From`, body + attachments intact, to the user's login
     email. Log to `MailForwardLog`. A transient send failure returns 500 so **SNS
     retries** (the subscription's custom DeliveryPolicy: 100 retries, exponential
     backoff up to 10 min); `MAIL_ENABLED` off is 200 `dropped:mail-off`; other errors
     are acknowledged. The `message_id` dedupe makes a retry safe.
   - **drop:** log only.
4. Return 200 once the message is durably handled (stored, forwarded, or logged as
   dropped); 500 only for a transient failure worth an SNS retry.

### Agent turn
For an agent-routed message, `mail` calls `harness.services.enqueue_turn` exactly as the
runner's inbox loop does for Gmail: origin `email`, idempotency key
`email-<agent>-<thread uuid>-<message pk>`, prompt `/<agent>:turn --thread <uuid>`,
`origin_ref = {thread_id, backend: "ses", from, subject, message_id, discovered_by:
"ses", message_count, headers: [Authentication-Results]}`. Skipped (stored, no turn):
the agent's own address as sender, and automated mail (`Auto-Submitted` ≠ `no`,
`Precedence: bulk|list|junk`, `List-Unsubscribe`) — the same positive-marker,
fail-open rule as `inbox_rules`. Admission rules apply to the turn as to any other. Sender grading: `enqueue_turn` gains
 a server-only `authserv_id` kwarg (not on the REST schema) so SES mail is graded
 against `amazonses.com` (`MAIL_SES_AUTHSERV_ID`) while runner-posted Gmail turns keep
 `INBOUND_EMAIL_AUTHSERV_ID` — a runner can never choose its own trusted receiver.
No runner reads the mailbox; the doorbell is the turn itself.

### Agent reads and sends (REST, so MCP for free)
Under `/api/mail/…/{workspace}` (the `apps/inbound` path idiom; gate: `AGENT_WORK` for an agent's own mailbox; workspace
membership to view threads on canopy-web; `INTEGRATIONS` for domains/routes):
- `GET resolve?address=` → `{backend: "ses", agent}` or 404 (caller falls back to Gmail).
- `GET threads?agent=`, `GET threads/{uuid}` (messages + attachment links).
- `POST threads/{uuid}/reply`, `POST send` (new thread) — From is the agent's route
  address; recipients are free (this is the agent's outbound, governed by the agent's
  own rails), attachments by upload. Enforces `daily_send_cap` and domain status.
- `POST threads/{uuid}/archive` / `read` — bookkeeping parity with the Gmail verbs.

### Reputation guard
`/api/mail/ses/events/` updates `delivery_status` and per-domain rolling counters.
A domain is **auto-suspended** (sends refused, inbound still stored/forwarded) when over
the last 7 days, with ≥ 50 sends, bounces > 4 % or complaints > 0.08 % (below SES's own
review thresholds of 5 % / 0.1 %), or immediately on any hard-bounce burst > 10 in an
hour. Suspension shows on the settings page with the reason; an admin re-enables.
Forwarded mail counts toward the forwarding domain.

## canopy (CLI) side — separate PR in the canopy repo

`src/orchestrator/agent_email.py` gains a backend switch: before `send`, `mark-read`,
`archive` and a new `thread` (read) verb, it asks canopy-web `resolve` for the mailbox;
`ses` → call the REST routes above with the agent's canopy-web credential, else the
existing `gog` path unchanged. Agents created for an SES workspace read threads with
`canopy email thread <id>` rather than `gog gmail thread get`. Dimagi agents (all
Gmail) see no behaviour change.

## Settings audit, gates, docs

Every write route is classified in `apps/api/route_gates.py` (domain/route writes are
`SETTINGS`, recorded through `audit.note_change`; send/reply/inbound/events are
`NOT_SETTINGS`). New pages get `frontend/src/guide/surfaces.ts` descriptors and a
`docs/architecture/pages.md` entry; routes go in `api-surface.md`; a topic doc
`docs/architecture/mail.md` and a runbook `docs/runbooks/mail-domain-onboarding.md`.
`generated.ts` regenerated.

## Errors and safety

- Unknown domain / route / unverified domain → drop, logged; never forwarded.
- Forward destinations are User FKs only; there is no free-text destination field
  anywhere, so the feature cannot be used as a relay.
- SNS endpoint verifies signature + topic before reading the payload; replays dedupe on
  `message_id`.
- HTML bodies render sandboxed; attachments download via short-lived presigned URLs
  handed only to workspace members.
- Removing a domain: `DeleteEmailIdentity`, its routes deleted, stored threads and messages
  kept (route set NULL; no thread delete and no retention-rule integration in v1).

## Testing

- Unit: MIME fixtures (plain, HTML, attachments, reply chains, non-UTF-8 headers) →
  parse/thread/route; forward rewrite headers; SNS signature verification against a
  test cert; route resolution (exact / catch-all / reserved); suspension thresholds;
  send cap; non-member forward destination refused; gates per route. boto3 is stubbed
  with `botocore.stub.Stubber` (no new dependency).
- Contract: `test_architecture_boundary` (mail is product tier), route gates, guide
  coverage, regenerated `generated.ts`.
- Live (runbook, not CI): `mail.marshellis.com` MX → SES first; send in, see the
  forward arrive in jjackson@dimagi.com; route an address to a test agent and complete a
  reply round-trip; then cut the root MX over and retire Zoho.

## Rollout

1. `canopy-web-mail` stack deployed (by hand, labs admin).
2. canopy-web PRs: models + domain UI → inbound + forward → agent turns + REST → reputation
   guard. Each deployable alone; nothing routes until a domain verifies.
3. marshellis: add domain, publish DNS (Cloudflare, via a marshellis-scoped API token), test on `mail.`
   subdomain, seed routes `jjackson@` and `support@` → jjackson, `*` → jjackson; cut MX.
4. canopy CLI backend switch, then the first marshellis agent.
