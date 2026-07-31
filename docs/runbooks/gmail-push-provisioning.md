# Runbook — provisioning Gmail push for an agent mailbox

**Until this is done, push does nothing and mail is discovered by the runner's
300s poll — i.e. exactly today's behaviour.** That is the intended failure mode:
shipping the receiver with no GCP side is safe, and the event log says which
mailboxes have no watch rather than pretending they are covered.

This is the one part of `docs/superpowers/specs/2026-07-30-event-log-and-inbound-push-design.md`
that cannot be done from the repo. It needs someone with GCP access to the
project that owns the agents' OAuth clients.

## What you are building

```
Gmail (eva@dimagi-ai.com) ──watch──▶ Pub/Sub topic ──push──▶ https://labs.connect.dimagi.com/canopy/api/inbound/gmail/
```

The push carries `{emailAddress, historyId}` and **no mail**. canopy-web resolves
the mailbox to an agent, rings that agent's runners over the WS control channel,
and the runner does the `gog` read it already does.

## 1. Topic and subscription

```bash
PROJECT=<the gcp project owning the agents' OAuth clients>
TOPIC=canopy-gmail-push
ENDPOINT=https://labs.connect.dimagi.com/canopy/api/inbound/gmail/

gcloud --project "$PROJECT" pubsub topics create "$TOPIC"

# Gmail publishes as this fixed system account. Without this grant, users.watch
# returns 403 and says nothing useful about why.
gcloud --project "$PROJECT" pubsub topics add-iam-policy-binding "$TOPIC" \
  --member=serviceAccount:gmail-api-push@system.gserviceaccount.com \
  --role=roles/pubsub.publisher

# A push subscription with an OIDC token — this is what canopy-web verifies.
gcloud --project "$PROJECT" pubsub subscriptions create canopy-gmail-push-sub \
  --topic="$TOPIC" \
  --push-endpoint="$ENDPOINT" \
  --push-auth-service-account="canopy-push@${PROJECT}.iam.gserviceaccount.com" \
  --push-auth-token-audience="$ENDPOINT" \
  --ack-deadline=30
```

## 2. Tell canopy-web what to trust

Both settings, on the ECS task definition (`deploy/aws/canopy-web.cfn.yaml`):

| env | value |
|---|---|
| `INBOUND_PUSH_AUDIENCE` | the `--push-auth-token-audience` above |
| `INBOUND_PUSH_SERVICE_ACCOUNT` | the `--push-auth-service-account` above |

**`INBOUND_PUSH_AUDIENCE` empty means the endpoint refuses everything.** That is
deliberate — an unconfigured deployment must not quietly accept anonymous pushes,
and refusing costs latency (the poll still runs), not mail. Set the service
account too: audience alone is not identity, since anyone who learns the audience
string could mint a token for it from a different account.

## 3. Register the mailbox in canopy-web

One `InboundMailbox` row per mailbox, via Django admin or the shell:

```python
from apps.agents.models import Agent
from apps.inbound.models import InboundMailbox

InboundMailbox.objects.create(address="eva@dimagi-ai.com", agent=Agent.objects.get(slug="eva"))
```

Explicit rather than derived from the address: `eva@dimagi-ai.com` → agent `eva`
happens to hold today, and the day it doesn't the failure is silent.

## 4. Arm the Gmail watch

```bash
# Per mailbox, with that mailbox's own OAuth credentials.
curl -X POST "https://gmail.googleapis.com/gmail/v1/users/me/watch" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"topicName\": \"projects/${PROJECT}/topics/${TOPIC}\", \"labelIds\": [\"INBOX\"], \"labelFilterBehavior\": \"include\"}"
```

The response carries `historyId` and `expiration` (epoch ms).

**Then report the expiry back**, so forgetting is loud instead of silent:

```bash
curl -X POST "https://labs.connect.dimagi.com/canopy/api/inbound/watch/" \
  -H "Authorization: Bearer $CANOPY_TOKEN" -H "Content-Type: application/json" \
  -d '{"address": "eva@dimagi-ai.com", "expires_at": "2026-08-06T12:00:00Z"}'
```

> **A watch expires after 7 days at most and Google will not renew it**, so this
> section must be repeated weekly. The symptom of forgetting is that email
> quietly goes back to taking five minutes — which is exactly why the report call
> above matters: with an expiry on file, canopy-web logs `gmail.watch.expiring` a
> day out and `gmail.watch.expired` after, so the cliff announces itself.

`gog` has no `watch` verb and no way to print an access token — it keeps client
credentials and refresh tokens in a keyring and never exposes a bearer — which is
why step 4 is a raw `curl` a human runs rather than something the runner does.
Automating it needs either a `gog` verb for minting a token (another repo) or a
deliberate decision to hand the runner those secrets directly; that is a choice
worth making explicitly rather than a gap to paper over.

## Verifying it works

```bash
# 1. Send a mail to the mailbox, then watch the log:
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://labs.connect.dimagi.com/canopy/api/events/?source=inbound&limit=20" | jq '.items[]'
```

You want a `gmail.push` row at `info` naming the runners it rang, within seconds
of the mail. Then check the turn actually landed fast:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://labs.connect.dimagi.com/canopy/api/harness/turns/?agent=eva" | jq '.[0]'
```

`origin_ref.discovered_by` should read `"push"`. If it reads `"poll"`, push is
registered but not delivering — and the log will already be saying so with a
`gmail.push.missed` row at `error`.

## What each failure looks like in the log

| you see | it means |
|---|---|
| `gmail.push.unknown_mailbox` (warn) | step 3 not done for that address |
| `gmail.push.no_runner` (warn) | push arrived, no online runner assigned to that agent |
| `gmail.push.missed` (error) | a live watch exists but the poll found the mail first |
| `gmail.watch.expiring` / `.expired` | step 4 needs repeating (reported once the runner half lands) |
| *nothing at all* | verification is refusing every push — check both env vars in step 2 |

That last row is the one to know about: a refused push is logged by the
application logger, not the event log, because an unverified caller must not be
able to write rows. Check the ECS logs (`AWS_PROFILE=labs`,
`/ecs/labs-jj-canopy-web`) for `inbound gmail push refused`.
