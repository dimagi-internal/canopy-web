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
   A `PendingConfirmation` ARN means canopy-web was not ready when it was created. Fix: delete that subscription and re-create it (`aws sns subscribe --topic-arn <arn> --protocol https --notification-endpoint <CanopyBaseUrl parameter of the mail stack>/api/mail/ses/<inbound|events>/ --attributes DeliveryPolicy='{"healthyRetryPolicy":{"numRetries":100,"numMaxDelayRetries":30,"minDelayTarget":20,"maxDelayTarget":600,"backoffFunction":"exponential"}}'`), or redeploy the mail stack after deleting it.

## Per domain

5. **Add the domain** in Settings → Email (`/w/:workspace/settings/email`, workspace owner or admin).
6. **Publish DNS** at the domain's DNS host: the 3 DKIM CNAMEs, the MAIL FROM MX and SPF TXT on `bounce.<domain>`, root SPF (use the page's merged suggestion if another provider still sends), and the starter DMARC. Refresh until the rows are green and the domain is `verified` (sending works from here).
7. **Test on a subdomain first.** Publish the MX `10 inbound-smtp.us-east-1.amazonaws.com` on `mail.<domain>` only. Mail to `anything@mail.<domain>` resolves to the parent domain's routes while the root MX still points at the old provider.
8. **Add routes:** a `forward` route per human address, a `*` catch-all (forward or drop — the catch-all cannot belong to an agent), and an `agent` route per agent. Forward destinations are workspace members only. Mail the `mail.` address from outside: the forward must arrive at the member's login email; an agent route must enqueue a turn (`origin_ref.backend == "ses"`) and a reply must land threaded in the outside mailbox.
   After the first live send, confirm the SES event's `mail.source` equals the From address exactly: the reputation guard finds the sending domain by an exact match on it and ignores any other event (a `bounce.<domain>` source would never count). The quick proof is the sent message turning `delivered` in the agent's Mail tab — delivery status is only updated after that match. A message stuck on `sent` means the source did not match: inspect a raw event (e.g. a temporary email subscription on the `canopy-mail-events` topic) before relying on the guard.
9. **Cut over the root MX** to `inbound-smtp.us-east-1.amazonaws.com`. The page's "Receiving" status confirms it; it names the old provider beforehand because switching moves all mail.
10. **Retire the old provider** once mail has flowed for a few days: remove its MX and its SPF include.

## Watching and recovering

A suspended domain (bounce/complaint rate) shows its reason on the settings page; fix the cause, then re-enable it there. Raw inbound mail in `incoming/` is deleted after 7 days.

## Remove the feature

1. Remove each domain in the UI (deletes the SES identity and its routes; threads are kept), and repoint MX at the new mail host.
2. Set `MailEnabled=false` on the canopy-web stack and deploy.
3. Deactivate the rule set (`aws ses set-active-receipt-rule-set --profile labs --region us-east-1`), then `aws cloudformation delete-stack --profile labs --stack-name canopy-web-mail`. The bucket has `DeletionPolicy: Retain`, so it survives the stack: once nothing in `kept/` is needed, empty it and delete it by hand (`aws s3 rb --profile labs s3://canopy-inbound-mail-<acct> --force`).
4. Follow the "Rip-out list" in `docs/architecture/mail.md` (it starts with `manage.py migrate mail zero`).
