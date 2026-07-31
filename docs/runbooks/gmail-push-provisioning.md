# Runbook — provisioning Gmail push for a workspace

**Everything here is done in the UI at `/w/:workspace/inbound`.** This file
explains what the steps mean; the page generates the exact commands from your
workspace's own values, so you should not need to copy anything out of here.

**Until it is provisioned, push is inert and mail is found by the runner's 300s
poll — i.e. exactly today's behaviour.** That is the intended failure mode:
shipping the receiver with no GCP side is safe, and the page says which mailboxes
have no watch rather than pretending they are covered.

## What you are building

```
Gmail (eva@dimagi-ai.com) ──watch──▶ Pub/Sub topic ──push──▶ /api/inbound/gmail/<workspace>/
```

The push carries `{emailAddress, historyId}` and **no mail**. canopy-web resolves
the mailbox to an agent, rings that agent's runners over the WS control channel,
and the runner does the `gog` read it already does.

## The steps

1. **Provision GCP.** The page renders the `gcloud` block with your project,
   topic and push URL already filled in — including the publisher grant to the
   fixed `gmail-api-push@system.gserviceaccount.com`, without which `users.watch`
   returns 403 and does not say why. Console links are on the page too.

2. **Tell the workspace what to trust.** Audience (must equal the push endpoint
   exactly) and the push service account. **Blank audience refuses every push** —
   deliberate, so an unconfigured tenant never accepts anonymous callers. Pin the
   service account as well: audience alone is not identity, since anyone who
   learns the audience string could mint a token for it from another account.

3. **Register the mailboxes.** Address → agent. Explicit rather than derived from
   the address: `eva@dimagi-ai.com` → agent `eva` holds today, and the day it
   does not, the failure is silent.

4. **Set the watch topic.** Served to every runner, so onboarding a tenant needs
   no `runner.json` edit on any box. The runner arms each mailbox on its next
   tick and re-arms 24h before the 7-day expiry.

## Why the runner arms the watch

`users.watch` must be called AS the mailbox. A service account can only do that
with **domain-wide delegation**, and `dimagi-ai.com` is a *secondary domain
inside the dimagi.com Workspace org* (`C018tavmm`) rather than its own tenant —
established when `dimagi-associate.com` was added to login (PR #151): an Internal
consent screen let `dimagi.com` and `dimagi-ai.com` both sign in, while
`dimagi-associate.com`, a genuinely separate org, was blocked before the callback
returned. DWD is granted per Workspace account and cannot be scoped to a domain,
an OU, or a user list, so granting it would cover every `dimagi.com` mailbox.

The runner already holds a per-mailbox OAuth grant for exactly the agent
accounts, which is strictly narrower and needs no admin-console change. `gog` has
no `watch` verb and will not print a bearer, so `gmail_watch.py` does the
refresh-token exchange itself from gog's own client file and keyring export.
Nothing is stored anywhere new.

The cost of that choice: if every runner is off for 7 straight days the watch
lapses. The page says so, and a runner down for a week could not have read the
mail anyway.

**A third party in their own Workspace** follows the same four steps in their own
workspace, with their own GCP project and service account. Nothing here is shared
between tenants — that is what the per-workspace config and per-workspace push
URL are for.

## Verifying

The Mailboxes table shows each watch's state and last push. For the underlying
record:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  ".../api/events/?source=inbound&limit=20" | jq '.items[]'
```

You want a `gmail.push` row at `info` naming the runners it rang, within seconds
of a test email, and the resulting turn's `origin_ref.discovered_by == "push"`.
If it reads `"poll"`, push is registered but not delivering — and a
`gmail.push.missed` row at `error` will already be saying so.

| you see | it means |
|---|---|
| `gmail.push.unknown_mailbox` (warn) | step 3 not done for that address |
| `gmail.push.no_runner` (warn) | push arrived, no online runner assigned to that agent |
| `gmail.push.missed` (error) | a live watch exists but the poll found the mail first |
| `gmail.watch.expiring` / `.expired` | the runner has not re-armed; push is lapsing |
| *nothing at all* | verification is refusing every push — check audience + signer |

That last row is the one to know: a refused push is logged by the application
logger, not the event log, because an unverified caller must not be able to write
rows. Check the ECS logs (`AWS_PROFILE=labs`, `/ecs/labs-jj-canopy-web`) for
`inbound gmail push refused`.
