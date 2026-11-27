# Sealed AWS role keys: set up, migrate, retire the custodial path

The rules are `apps/agents/aws_sealed.py`'s module docstring. In short: each labs agent role
(`labs-read`, `labs-readwrite`, `labs-config`) gets its own IAM user that may only assume that
role. canopy-web stores that user's access key only sealed in a person's browser, so it carries
the key but cannot use it. An approver's browser opens the key on a passkey touch, asks AWS STS
for the agent's session itself and seals the session to the agent's machines. A standing agent's
machines hold their own sealed copy and ask STS themselves.

Both paths run side by side until the custodial one is retired: a grant's `delivery` is
`custodial` (canopy-web's task role assumes the role, `apps/agents/api_aws.py`) or `sealed`.

## 1. Deploy the users and trust (connect-labs)

From connect-labs, with a labs admin login:

```bash
aws cloudformation deploy --stack-name labs-jj-canopy-sealed-keys \
  --template-file infra/canopy-sealed-keys.yml --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 --profile labs
# then redeploy the three role stacks, whose trust now names each role's own user
#   (infra/labs-agent-readwrite.yml, labs-agent-config.yml, labs-observe-readonly.yml; see infra/README.md)
```

## 2. Make each key and seal it (in the browser, no file)

For each role, make an access key and paste it straight into canopy's **Sealed AWS keys** page
(`/aws-keys`, superusers):

```bash
aws iam create-access-key --user-name canopy-sealed-labs-readwrite --profile labs
```

- **Sealed to:** *The people who approve grants* for a fenced role (`labs-readwrite`,
  `labs-config`). Tick the other superusers who should hold it, and check each one's
  fingerprint with them. Only a holder can approve a sealed grant (`aws_sealed.may_mint`), so
  pick at least two.
- **Sealed to:** *<agent>'s machines* for each agent standing for a role (`labs-read`: Ada and
  Hal). Only machines whose keys someone you trust vouched for are sealed to. A machine that
  joins later needs the key sealed again from this page.

Do not keep the secret anywhere else: the terminal output is the only other copy. Clear it.

## 3. Migrate

- The canopy CLI asks `canopy aws request --delivery auto` by default: sealed when the role has an
  approvers' key, custodial otherwise. `canopy aws credentials` tries the sealed source first
  (`POST /api/agents/{slug}/aws-credentials/sealed`) and falls back to canopy-web's own route.
  Update the CLI on every runner (`canopy update`).
- Approving a sealed grant: the approval page makes the keys in your browser. That is one passkey
  touch to open your vault key and one to approve, then the keys are handed over. A grant longer
  than the role's MaxSessionDuration (8 hours once connect-labs deploys) gets new keys from the
  same page with **Make new keys for this grant**.
- Watch CloudTrail: a sealed session's `AssumeRole` is made by `user/canopy-sealed/…`, a custodial
  one by `role/canopy-web-task-role`. Each carries the session tag `delivery=sealed` or no tag.

## 4. Retire the custodial path (a week later)

Once CloudTrail shows no `AssumeRole` by `canopy-web-task-role` on the three roles for a week:

1. connect-labs: drop `BrokerRoleName` from the three roles' trust, and canopy-web: drop the
   `assume-labs-*` statements from `deploy/aws/canopy-web-task-role.cfn.yaml`. canopy-web then
   cannot reach labs at all.
2. canopy-web: make `sealed` the only delivery, then remove `POST aws-credentials`,
   `aws_grants.live_grant` and `api_aws.sts_client` and their tests; the CLI drops its fallback.

## Rotating or revoking a key

Make a new access key, seal it on `/aws-keys` (replaces the stored one), then deactivate the old
key in IAM. Removing a holder is the same: re-seal without them, then rotate, because they could
have kept a copy. Sessions already handed out last until their own expiry. AWS's "revoke active
sessions" on a role ends them early.

## Honest limits

- The browser is trusted while it approves. A canopy-web that is compromised and serves altered
  page code to an approver while they approve could take that one key. A compromise that does not
  change the page, or that happens when nobody is approving, gets ciphertext only.
- A machine that holds a standing key holds it for good (until rotation), like any long-lived key
  on a box. Keep standing roles read-only.
