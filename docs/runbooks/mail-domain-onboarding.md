# Onboarding an email domain (SES)

Design and invariants: `docs/architecture/mail.md`. Do the first two steps once per AWS account; the rest per domain.

## One-time: canopy-web first, then the AWS side

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
3. **Activate the rule set** (CloudFormation cannot):
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

4. **Add the domain** in Settings → Email (`/w/:workspace/settings/email`, workspace owner or admin).
5. **Publish DNS** at the domain's DNS host: the 3 DKIM CNAMEs, the MAIL FROM MX and SPF TXT on `bounce.<domain>`, root SPF (use the page's merged suggestion if another provider still sends), and the starter DMARC. Refresh until the rows are green and the domain is `verified` (sending works from here).
6. **Test on a subdomain first.** Publish the MX `10 inbound-smtp.us-east-1.amazonaws.com` on `mail.<domain>` only. Mail to `anything@mail.<domain>` resolves to the parent domain's routes while the root MX still points at the old provider.
7. **Add routes:** a `forward` route per human address, a `*` catch-all, and an `agent` route per agent (creating an agent offers a default `slug@domain`). Forward destinations are workspace members only. Mail the `mail.` address from outside: the forward must arrive at the member's login email; an agent route must enqueue a turn (`origin_ref.backend == "ses"`) and a reply must land threaded in the outside mailbox.
8. **Cut over the root MX** to `inbound-smtp.us-east-1.amazonaws.com`. The page's "Receiving" status confirms it; it names the old provider beforehand because switching moves all mail.
9. **Retire the old provider** once mail has flowed for a few days: remove its MX and its SPF include.

## Watching and recovering

A suspended domain (bounce/complaint rate) shows its reason on the settings page; fix the cause, then re-enable it there. Raw inbound mail in `incoming/` is deleted after 7 days.

## Remove the feature

1. Remove each domain in the UI (deletes the SES identity, disables routes), and repoint MX at the new mail host.
2. Set `MailEnabled=false` on the canopy-web stack and deploy.
3. Deactivate the rule set (`aws ses set-active-receipt-rule-set --profile labs --region us-east-1`), empty the `canopy-inbound-mail-<acct>` bucket, then `aws cloudformation delete-stack --profile labs --stack-name canopy-web-mail`.
4. Delete `apps/mail` and the other items under "Rip-out list" in `docs/architecture/mail.md`.
