# Workspace email domains (`apps/mail`)

A workspace admin brings their own domain; canopy-web runs its email through Amazon SES. **Agent addresses** (`ada@domain`) turn incoming mail into the agent's email turn and send as that address. **Every other address** is forwarded to a canopy user's login email (a catch-all route catches the rest). Setup is Settings → Email (`/w/:workspace/settings/email`); the operator steps are `docs/runbooks/mail-domain-onboarding.md`.

Not here: human inboxes/IMAP/webmail, replying to forwarded mail *as* the domain, automatic DNS writes, bulk mail.

## Tier and removability

`apps/mail/` is a **product-tier** app: framework apps never import it (`tests/test_architecture_boundary.py`). It does not extend `apps/inbound` — `InboundMailbox` is Gmail-push routing and stays Gmail-only; SES routes are `MailRoute`, so `/runner-mailboxes` never lists them and no runner polls them with `gog`. Gate: the `perms.INTEGRATIONS` capability for domains and routes, `AGENT_WORK` for an agent's own mailbox, `READ` to view threads.

**Rip-out list** (nothing else changes): delete `apps/mail/` (its migration drops the tables); its `INSTALLED_APPS` entry and `# --- Mail domains ---` settings block in `config/settings/base.py`; its router line in `apps/api/api.py`; its `route_gates` entries; the `/api/mail/ses/` entry in `apps/common/middleware.py`'s public prefixes; the frontend page, `frontend/src/api/mail.ts` and guide descriptors; the `canopy-web-mail` stack; the `Mail*` parameters in `deploy/aws/canopy-web.cfn.yaml`. The one framework touch that is not a registry line is the server-only `authserv_id` kwarg on `apps.harness.services.enqueue_turn`; it is harmless left in.

## Model

- **`MailDomain`** — workspace FK, `name` (globally unique: SES identities are account-wide), `status` `pending` → `verified` → `suspended`, `receiving` (live MX check), `mail_from_subdomain` (`bounce`), `dkim_tokens`, `suspended_reason`, `daily_send_cap` (200). Routing is inert until `verified`; DKIM verifying is the proof of DNS control.
- **`MailRoute`** — `local_part` (`*` = catch-all; unique per domain), `kind` `agent` | `forward` | `drop`, `agent` FK, `forward_to` **User FK** (must be a workspace member), `enabled`. Exact local part beats `*`. `canopy-forwarder` (the From of forwarded mail) cannot be routed.
- **`MailThread`** / **`MailMessage`** / **`MailAttachment`** — a thread has a public `uuid` (the `thread_id` agents and turns carry). Messages dedupe on `message_id` per workspace + mailbox. Raw MIME for agent mail is copied to `kept/` in S3.
- **`MailForwardLog`** — one row per forwarded message, metadata only; the body is never stored.

Threading: an inbound message joins the thread whose messages' ids appear in its `In-Reply-To`/`References`, else starts one; replies set both headers.

## AWS side (`deploy/aws/canopy-mail.cfn.yaml`, stack `canopy-web-mail`, labs account, us-east-1)

Deployed by hand with a labs admin login (the deploy role holds no `ses:*`). Bucket `canopy-inbound-mail-<acct>`: SES writes `incoming/` (expires after **7 days**); agent mail is copied to `kept/` (no expiry). Receipt rule set `canopy-inbound`, ONE rule `canopy-all`: no recipient filter, spam/virus scan on, TLS required, S3 action notifying SNS `canopy-inbound-mail` (object key only) → `POST /api/mail/ses/inbound/`. Configuration set `canopy-tenant-mail` (every tenant send) publishes send/delivery/bounce/complaint/reject to SNS `canopy-mail-events` → `POST /api/mail/ses/events/`. A policy on the shared task role creates/inspects/deletes identities and sends only from non-Dimagi addresses. The rule set must be activated by hand (`aws ses set-active-receipt-rule-set`); `tests/test_mail_stack.py` pins the invariants.

Settings (env, from the stack Outputs via `canopy-web.cfn.yaml` parameters): `MAIL_ENABLED` (default **false**; every inbound and send path refuses when off), `MAIL_INBOUND_BUCKET`, `MAIL_INBOUND_TOPIC_ARN`, `MAIL_EVENTS_TOPIC_ARN`; also `MAIL_SES_REGION`, `MAIL_CONFIGURATION_SET`, `MAIL_SES_AUTHSERV_ID`, `MAIL_INBOUND_MX`, `MAIL_FORWARDER_LOCAL_PART`.

## Flows

- **Add a domain:** uniqueness check → `CreateEmailIdentity` (Easy DKIM) + MAIL FROM `bounce.<domain>` → row `pending`. The page lists every DNS record with a live per-row check (server-side lookup, cached ~60 s). Status flips to `verified` when SES reports DKIM success, refreshed lazily on every domain read and by `manage.py mail_refresh` (no scheduler needed). MX is its own "Receiving" status and names the current MX provider so a working mailbox is not broken by accident.
- **Inbound:** the SNS signature is verified (cert host `sns.us-east-1.amazonaws.com`, topic ARN ours) **before anything else is parsed**; a `SubscriptionConfirmation` fetches its URL once. Unknown or unverified domain, or a spam/virus FAIL verdict → drop and log, never forward or wake an agent. A subdomain resolves to its verified parent's routes. Route resolution: exact → `*` → drop. **Agent:** dedupe, copy to `kept/`, store, thread, enqueue the turn. **Forward:** re-send through SES from `"<Name> via <domain>" <canopy-forwarder@domain>` with `Reply-To` the original sender, to the user's login email; a failed send returns 500 so SNS retries (~6 h), and the dedupe makes a retry safe. **Drop:** log only.
- **Agent turn:** `harness.services.enqueue_turn` with origin `email`, key `email-<agent>-<thread uuid>-<message_count>`, prompt `/<agent>:turn --thread <uuid>`, `origin_ref.backend = "ses"`. Skipped (stored, no turn): the agent's own address as sender and automated mail (`Auto-Submitted` ≠ `no`, `Precedence: bulk|list|junk`, `List-Unsubscribe`). Sender grading uses the server-only `authserv_id` (`MAIL_SES_AUTHSERV_ID`, `amazonses.com`); runner-posted Gmail turns keep `INBOUND_EMAIL_AUTHSERV_ID`, so a runner can never choose its own trusted receiver. No runner reads the mailbox.
- **Reputation guard:** the events endpoint updates `delivery_status` and rolling counters. A domain is auto-suspended (sends refused; inbound still stored/forwarded) when, over 7 days with at least 50 sends, bounces exceed 4 % or complaints 0.08 %, or on a hard-bounce burst over 10 in an hour. The settings page shows the reason; an admin re-enables. Forwarded mail counts toward the forwarding domain.

## Safety invariants

Forward destinations are User FKs only — no free-text destination exists, so the feature cannot be a relay. Tenant sends go through `canopy-tenant-mail` and cannot use Dimagi addresses; `agent_mail.send`/`forward` only ever use a `MailDomain` address. HTML renders sandboxed (never `allow-same-origin`); attachments download via short-lived presigned URLs after a `READ` check. Removing a domain deletes the SES identity and disables routes; stored threads stay until an admin deletes them. Every write route is in `route_gates` (domain/route writes are `SETTINGS`, audited via `audit.note_change`; send, reply, inbound and events are `NOT_SETTINGS`).

## REST

`/api/mail/…`, listed in `docs/architecture/api-surface.md` (so MCP tools for free). The canopy CLI's `email` verbs ask `GET /api/mail/resolve` which backend holds a mailbox and fall back to Gmail on 404.
