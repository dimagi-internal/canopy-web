# Onboarding an email domain (SES)

Design and invariants: `docs/architecture/mail.md`. Do steps 1–4 once per AWS account; steps 5–10 per domain.

## One-time: canopy-web first, then the AWS side

Run every `aws cloudformation deploy` below from an up-to-date `main` checkout, after the feature's PR has merged — the templates you deploy must be the merged ones.

The mail stack's HTTPS subscriptions send a `SubscriptionConfirmation` the moment they are created, and canopy-web can only verify and confirm it if it already knows the topic ARNs. The ARNs and bucket name are deterministic, so set canopy-web up first. (The SNS doors verify signatures regardless of `MAIL_ENABLED`, but need the topic ARN settings.)

1. **Turn the feature on in canopy-web** with the known names, then deploy:
   ```bash
   aws cloudformation deploy --profile labs --stack-name canopy-web \
     --template-file deploy/aws/canopy-web.cfn.yaml --capabilities CAPABILITY_IAM \
     --parameter-overrides MailEnabled=true \
       MailInboundBucket=canopy-inbound-mail-858923557655 \
       MailInboundTopicArn=arn:aws:sns:us-east-1:858923557655:canopy-inbound-mail \
       MailEventsTopicArn=arn:aws:sns:us-east-1:858923557655:canopy-mail-events
   gh workflow run "Deploy to Labs (AWS)" --ref main
   ```
   The workflow overrides only `ImageTag`, so the `Mail*` values persist across later deploys. Confirm `/api/openapi.json` lists `/api/mail/domains/{workspace}`.
2. **Deploy the mail stack** with a labs admin login (the deploy role holds no `ses:*`):
   ```bash
   aws cloudformation deploy --profile labs --stack-name canopy-web-mail \
     --template-file deploy/aws/canopy-mail.cfn.yaml --capabilities CAPABILITY_NAMED_IAM
   aws cloudformation describe-stacks --profile labs --stack-name canopy-web-mail \
     --query 'Stacks[0].Outputs'
   ```
   The Outputs must equal the names used in step 1.
3. **Activate the rule set** (CloudFormation cannot). Only one receipt rule set can be active per account and region, so activating `canopy-inbound` deactivates any other. Check first:
   ```bash
   aws ses describe-active-receipt-rule-set --profile labs --region us-east-1
   ```
   If another rule set is active and still receives mail, stop: its rules must be merged into `canopy-inbound` (or it retired) before switching. Then:
   ```bash
   aws ses set-active-receipt-rule-set --profile labs --region us-east-1 --rule-set-name canopy-inbound
   ```
4. **Verify both subscriptions are `Confirmed`:**
   ```bash
   for t in canopy-inbound-mail canopy-mail-events; do
     aws sns list-subscriptions-by-topic --profile labs --region us-east-1 \
       --topic-arn arn:aws:sns:us-east-1:858923557655:$t \
       --query 'Subscriptions[].[Endpoint,SubscriptionArn]'
   done
   ```
   A `PendingConfirmation` ARN means canopy-web was not ready when it was created. Fix: delete that subscription and re-create it (`aws sns subscribe --topic-arn <arn> --protocol https --notification-endpoint <CanopyBaseUrl parameter of the mail stack>/api/mail/ses/<inbound|events>/ --attributes DeliveryPolicy='{"healthyRetryPolicy":{"numRetries":30,"numNoDelayRetries":0,"numMinDelayRetries":2,"numMaxDelayRetries":14,"minDelayTarget":20,"maxDelayTarget":120,"backoffFunction":"exponential"}}'`), or redeploy the mail stack after deleting it.

## Per domain

5. **Add the domain** in Settings → Email (`/w/:workspace/settings/email`, workspace owner or admin).
6. **Publish DNS** at the domain's DNS host: the 3 DKIM CNAMEs, the MAIL FROM MX and SPF TXT on `bounce.<domain>`, root SPF (use the page's merged suggestion if another provider still sends), and the starter DMARC. Refresh until the rows are green and the domain is `verified` (sending works from here).
7. **Test on a subdomain first.** Publish the MX `10 inbound-smtp.us-east-1.amazonaws.com` on `mail.<domain>` only. Mail to `anything@mail.<domain>` resolves to the parent domain's routes while the root MX still points at the old provider.
8. **Add routes:** a `forward` route per human address, a `*` catch-all (forward or drop — the catch-all cannot belong to an agent), and an `agent` route per agent. Forward destinations are workspace members only. Mail the `mail.` address from outside: the forward must arrive at the member's login email; an agent route must enqueue a turn (`origin_ref.backend == "ses"`) and a reply must land threaded in the outside mailbox.
   After the first live send, confirm the SES event's `mail.source` equals the From address exactly: the reputation guard finds the sending domain by an exact match on it and ignores any other event (a `bounce.<domain>` source would never count). The quick proof is the sent message turning `delivered` in the agent's Mail tab — delivery status is only updated after that match. A message stuck on `sent` means the source did not match: inspect a raw event (e.g. a temporary email subscription on the `canopy-mail-events` topic) before relying on the guard.
9. **Cut over the root MX** to `inbound-smtp.us-east-1.amazonaws.com`. The page's "Receiving" status confirms it; it names the old provider beforehand because switching moves all mail.
10. **Retire the old provider** once mail has flowed for a few days: remove its MX and its SPF include.

## Watching and recovering

A suspended domain (bounce/complaint rate) shows its reason on the settings page; fix the cause, then re-enable it there.

**A domain stuck on `pending` with "SES gave up verifying"** means the records were not published (or not visible) within SES's 72-hour window: SES's DKIM or MAIL FROM status is `FAILED` and it never re-checks by itself, even once every row is green. Recovery: publish the records until the rows are green, then press **Re-check and restart verification** on the Email page (or `POST /api/mail/domains/<workspace>/<id>/refresh`, the `refresh_mail_domain` MCP tool). canopy restarts SES verification in place — same identity, same DKIM tokens unless SES issues new ones, in which case the page lists the new CNAMEs to publish and says so. It restarts at most every 10 minutes per domain. SES then re-checks within minutes to hours; the domain turns `verified` on a later read. Never restart it by hand in the SES console or CLI: the row's tokens and the identity must stay in step. To confirm, read (only) `aws sesv2 get-email-identity --profile labs --region us-east-1 --email-identity <domain>`: `DkimAttributes.Status` and `MailFromAttributes.MailFromDomainStatus` go `PENDING` → `SUCCESS`. Raw inbound mail in `incoming/` is deleted after 7 days.

**Replaying a message**, within those 7 days. Its key is `incoming/<SES message id>` (list them with `aws s3 ls --profile labs s3://canopy-inbound-mail-858923557655/incoming/`); run it as a one-off task on the canopy-web task definition (`aws ecs run-task` with a `containerOverrides` command, output in the `/ecs/labs-jj-canopy-web` log group — the pattern in `docs/runbooks/connecting-a-system.md`). **Always `--dry-run` first**:
```bash
python manage.py mail_replay incoming/<ses-message-id> --dry-run          # where each copy would go
python manage.py mail_replay incoming/<ses-message-id>                    # To/Cc/Delivered-To on our domains
python manage.py mail_replay incoming/<ses-message-id> --recipient ada@<domain>   # a Bcc recipient, or just one
python manage.py mail_replay incoming/<ses-message-id> --reforward        # retry a forward logged as dropped
```
What a replay can fix, after the cause is fixed (a route added, a domain verified, a bug deployed):
- `dropped:no-route`, `dropped:unknown-domain`, `dropped:drop-route` — nothing was recorded, so a replay routes it afresh.
- `error:*` — a failed agent delivery or forward left nothing behind; a replay retries it.
- Agent mail — delivered at most once per mailbox (Message-ID dedupe); a copy already stored reports `duplicate`, and a stored copy whose turn never enqueued gets its turn.
- A forward logged as **dropped** (`dropped:not-a-member`, `dropped:no-email`, `dropped:loop`) is a recorded outcome: a plain replay reports `duplicate`; `--reforward` clears that DROPPED record first and retries it.
- A forward already **forwarded** never sends again, `--reforward` or not.
- `dropped:spam` / `dropped:virus` stay dropped (the SES verdict headers in the stored message are read).

It prints `recipient: outcome` per address. A transient failure prints the outcomes and exits non-zero — run it again.

A domain left `pending` (DNS never published) holds its name for `MAIL_PENDING_DOMAIN_TTL_DAYS` (7); after that, adding the name from any workspace releases the stale row and its SES identity.

## Remove the feature

1. Remove each domain in the UI (deletes the SES identity and its routes; threads are kept), and repoint MX at the new mail host.
2. Set `MailEnabled=false` on the canopy-web stack and deploy.
3. Deactivate the rule set (`aws ses set-active-receipt-rule-set --profile labs --region us-east-1`), then `aws cloudformation delete-stack --profile labs --stack-name canopy-web-mail`. The bucket has `DeletionPolicy: Retain`, so it survives the stack: once nothing in `kept/` is needed, empty it and delete it by hand (`aws s3 rb --profile labs s3://canopy-inbound-mail-<acct> --force`).
4. Follow the "Rip-out list" in `docs/architecture/mail.md` (it starts with `manage.py migrate mail zero`).
