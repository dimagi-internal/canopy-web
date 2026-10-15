# Deploy, CI and the merge queue

Where canopy-web runs, how a change gets from a PR to production, and the AWS/IAM rules that keep the shared labs account safe. Frontend caching (why a deploy is never hidden behind a stale shell) is in [`frontend.md`](frontend.md).

## Where it runs

- **AWS ECS Fargate** on the shared labs platform (account `858923557655`, `us-east-1`), one container: Django serves the built SPA, the API and MCP. Database: the `canopy_web` DB on the shared RDS `labs-jj-postgres`.
- Served at **`https://canopy.dimagi.com/`**, at the root, behind the shared ALB via a host-header rule (`HostListenerRule`, priority 10, in `deploy/aws/canopy-web.cfn.yaml`). The certificate and the Cloudflare DNS record were made by hand and live in no repo.
- Runtime settings: `config/settings/connectlabs.py` (extends `production.py`; no `FORCE_SCRIPT_NAME` — setting it would serve canopy under a prefix everywhere).
- Infra: `deploy/aws/canopy-web.cfn.yaml` (CloudFormation).

### The old address still serves machines

The old address `https://labs.connect.dimagi.com/canopy/` (now `https://canopy.dimagi.com/`) still reaches the same container through the ALB's `/canopy/*` rule. `config/asgi_prefix.py` strips the prefix (`CANOPY_STRIP_PREFIX`, read in `config/asgi.py`) and `apps/common/legacy_prefix.py` decides:

- **Machine traffic** (API, WebSockets, OAuth, well-known, health) is served in place — runners, the canopy plugin, MCP clients, Slack, Gmail push and ace-web hold the old address and do not follow redirects reliably. Don't turn these into redirects.
- **Browser pages** (GET/HEAD outside machine prefixes) are 302'd to `canopy.dimagi.com`.
- Docs must not send a reader to the old address: `tests/test_old_address_in_docs.py` fails on a markdown line naming `labs.connect.dimagi.com/canopy` that does not also name `canopy.dimagi.com`.

### Protocol identity is pinned by connected sites

canopy's protocol identity — CIMD client_id, assertion audience, own issuer, MCP resource — is `CANOPY_IDENTITY_BASE_URL` (`https://canopy.dimagi.com`). ace-web's and connect-labs' `CANOPY_CLIENT_ID` / `CANOPY_BASE_URL` pin it, so changing it is a coordinated change with every connected site. `CANOPY_FORMER_BASE_URLS` names the old address so the Slack sync can recognise and move the commands and webhooks it registered there.

## Deploying

Merging to `main` does **not** deploy. Deploys run only from GitHub, via the **Deploy to Labs (AWS)** workflow (`.github/workflows/deploy-labs.yml`, `workflow_dispatch`):

```bash
gh workflow run "Deploy to Labs (AWS)" --ref main                          # migrations run automatically
gh workflow run "Deploy to Labs (AWS)" --ref main -f skip_migrations=true  # EMERGENCY ONLY
```

The workflow hard-fails on any ref other than `main`, and refuses a stale commit (the dispatched sha must still be main's tip when the run starts — otherwise re-dispatch).

Pipeline: build → push to ECR (`labs-jj-canopy-web`) → **migrate** on a throwaway task def → create a CloudFormation change set (`aws cloudformation deploy --no-execute-changeset --parameter-overrides ImageTag=<sha>`) → **preflight** → `aws cloudformation execute-change-set`, which registers the real task definition and rolls the ECS service.

### CloudFormation owns the task definition and service

- One writer per resource: the pipeline only passes `ImageTag`; CFN registers the task definition and rolls the service. Never register task-definition revisions or call `ecs update-service` outside CFN — the stack's `ImageTag` would go stale and the next plain `cloudformation deploy` would silently roll prod back.
- CFN resolves intrinsics (`!Ref`, `!Sub`) itself; don't add a step that reads the template outside CloudFormation.
- **The migration seam.** CFN rolls `Service` and its `TaskDefinition` atomically, so migrations run first on a throwaway task def built from the LIVE one with only the image swapped. Its env is therefore one deploy stale: if a deploy both changes an env var and ships a migration that reads it, deploy twice.
- Migrations are idempotent (`migrate --noinput`) and run before cutover. Write them the obvious way — destructive migrations are fine, and a brief window of errors during a deploy is accepted; don't split a one-line migration into expand/contract PRs.
- CFN's own overhead is ~10s; the ~6min deploy is the ECS rollout.

### Preflight

`deploy/aws/preflight.py` asks IAM (`iam:SimulatePrincipalPolicy`) whether the deploying role may perform every action the change set will call, and names the missing action instead of a 403 minutes into a rollout. Exit codes: 0 proceed; 1 block (the change set is deleted; migrations have already run); 2 the preflight could not evaluate — logged as a warning and the deploy proceeds, because a gate that cannot evaluate must not become an outage. A failed `ListChangeSets` hard-fails: reading it as "no changes" would produce a green deploy that shipped nothing.

### Deploys are not ordered by commit

The workflow's `concurrency` group serialises runs (never cancelled mid-flight), but a newer pending deploy cancels an older pending one, and a run whose sha is no longer main's tip fails as stale. After merging, confirm what shipped rather than assuming your run landed:

```bash
gh run list --workflow="Deploy to Labs (AWS)" --limit 3 --json headSha,conclusion
```

and check the feature is live (e.g. a new endpoint appears in `https://canopy.dimagi.com/api/openapi.json`).

## AWS IAM

### One deploy role per deployment

- Each deployment has its own GitHub OIDC role — `github-actions-{canopy-web,ace-web,connect-labs}-deploy` — generated by `deploy/aws/deploy_roles.py`, trusting exactly one repo and scoped to its own stack, secrets prefix, ECR repo, target group and log groups. The workflow assumes it through the `AWS_ROLE_ARN` secret.
- `deploy_roles.py` is applied **by hand** (`aws iam put-role-policy` / `update-assume-role-policy`); an edit there is not live until someone runs them. It includes the preflight's own grants (`iam:SimulatePrincipalPolicy`, `cloudformation:ListChangeSets`).
- The old shared `github-actions-labs-deploy` role still exists and grants nothing, so a stray consumer fails with a named AccessDenied. Don't re-grant it.
- **canopy-web's role must trust the IMMUTABLE OIDC subject, main only:** `repo:dimagi-internal@272902307/canopy-web@1193139337:ref:refs/heads/main`. The name-based form silently fails `AssumeRoleWithWebIdentity`. ace-web and connect-labs use the name-based form. Check with `gh api repos/dimagi-internal/<repo>/actions/oidc/customization/sub`.
- Outstanding work (moving hand-applied grants into a bootstrap stack): `docs/superpowers/specs/2026-09-09-labs-iac-status.md`; design: `docs/superpowers/specs/2026-09-09-labs-infrastructure-as-code-design.md`.

### The shared task-execution role is scoped

`labs-jj-ecs-task-execution-role` is shared by every container in every labs app. It reads secrets only through scoped inline policies on four prefixes — `labs-jj-*`, `canopy-web/*`, `ace-web/*`, `labs/*` (`labs/SENTRY_DSN` is the one `labs-jj-*` does not match; connect-labs' Sentry needs it). Don't attach the AWS-managed `SecretsManagerReadWrite` (or any `secretsmanager:*` on `*`) to it — that lets any container read, overwrite or delete every secret in the account. The unprefixed legacy secrets stay as they are (no rename API, they span four apps, and ace-web/connect-labs reference pre-existing ARNs rather than declaring secrets in CFN).

### canopy-web's own task role

canopy-web runs as `canopy-web-task-role` (`deploy/aws/canopy-web-task-role.cfn.yaml`), canopy-web's alone — cut over 2026-10-15. Before that it ran as `labs-jj-ecs-task-role`, which every labs container shares, so connect-labs and ace-web could also send as canopy.dimagi.com, read inbound mail, and drive any agent's cloud capability. Every grant keeps one owner: the role stack owns the role, ECS Exec (`ssmmessages`) and an identity grant on the attachments bucket; `canopy-email` (`canopy-ses-send`) and `canopy-mail` (`canopy-mail`) attach their policies through `TaskRoleName`, a comma-separated list so both roles hold them during the move; each capability stack attaches `BrokerPolicy` through `BrokerRoleName`; the attachments bucket policy names the canopy-web stack's `TaskRoleArn`. The code's AWS calls are exactly those (`apps/common/email.py`, `apps/mail/ses.py`, `apps/canopy_sessions/attachment_storage.py`, `apps/capabilities/aws.py`); secrets and logs are the execution role's. `tests/test_task_role_stack.py` pins the stacks and the deploy role's `PassOwnTaskRoleToEcs`.

**Done 2026-10-15** (all seven steps, with a labs admin login; verified from inside the running task over ECS Exec: SES send, SES identity read, inbound-mail get/put, attachments put/get/delete, and a capability lease acquire → exec → release). The steps, kept for any future app that moves off the shared role:

1. **Inventory the shared role** — `aws iam list-attached-role-policies` / `list-role-policies --role-name labs-jj-ecs-task-role`. Everything canopy uses must be one of the grants above; anything else canopy needs goes into the role stack first.
2. **Create the role:** `aws cloudformation deploy --stack-name canopy-web-task-role --template-file deploy/aws/canopy-web-task-role.cfn.yaml --capabilities CAPABILITY_NAMED_IAM`.
3. **Deploy role:** re-apply `deploy_roles.py` for canopy-web (`python deploy/aws/deploy_roles.py`, then `aws iam put-role-policy`), so the pipeline may `iam:PassRole` the new role.
4. **Grants on both roles:** redeploy `canopy-web-email` and `canopy-web-mail` with `TaskRoleName=labs-jj-ecs-task-role,canopy-web-task-role`; attach every capability stack's `BrokerPolicy` to the new role too (`aws iam attach-role-policy --role-name canopy-web-task-role --policy-arn <the stack's BrokerPolicy ARN>` — out of band for now; step 7 makes it the stack's).
5. **Move the task:** `aws cloudformation deploy --stack-name canopy-web --template-file deploy/aws/canopy-web.cfn.yaml --parameter-overrides TaskRoleArn=arn:aws:iam::858923557655:role/canopy-web-task-role ImageTag=<the live tag>` (admin: it also rewrites the attachments bucket policy, which the pipeline role cannot). Every later pipeline deploy carries `TaskRoleArn` forward.
6. **Verify on the new role:** a notification email arrives (SES send); a workspace mail domain receives and an agent replies (inbound bucket + tenant send); a chat attachment uploads and downloads; a capability lease reaches `ready`, execs and releases; `aws ecs execute-command` opens a shell.
7. **Drop the shared grants:** redeploy each capability stack with `BrokerRoleName=canopy-web-task-role` (ACE's `canopy-cap-ace-android-emulator` first), and `canopy-web-email` / `canopy-web-mail` with `TaskRoleName=canopy-web-task-role`. Then flip `TaskRoleArn`'s default in `canopy-web.cfn.yaml` and drop `labs-jj-ecs-task-role` from canopy-web's `PassSharedTaskRolesToEcs`.

Rollback before step 7: redeploy `canopy-web` with the old `TaskRoleArn` — both roles still hold every grant. After step 7 (the state since 2026-10-15) the shared role holds none of canopy's grants and the deploy role cannot pass it to canopy-web, so a rollback means re-running steps 3–4 in reverse first.

## CI and merging

CI (`.github/workflows/ci.yml`) runs on every PR, in the merge queue, and on push to `main`. Required checks: **Backend tests** (which also verifies `frontend/src/api/generated.ts` is fresh and the `dimagi-canopy` SDK version bump) and **Frontend build** (which also enforces the `canopy-ui`/`canopy-client` version bump).

### Every PR: arm auto-merge, no strategy flag

```bash
gh pr merge <n> --auto                          # arm it — main moves constantly
gh pr view <n> --json autoMergeRequest          # confirm it took (non-null)
```

- **Never pass `--squash` or any strategy flag.** The merge queue owns the strategy; `gh` answers a strategy flag with `! The merge strategy for main is set by the merge queue`, which reads like a warning but leaves auto-merge **unarmed**.
- The queue tests your branch **merged with `main`**, so it can fail on things your PR alone never shows, including someone else's flaky test. **A queue failure ejects the PR and disarms auto-merge.** Symptom: your checks green, `mergeStateStatus: CLEAN`, PR still open, `autoMergeRequest: null`. Fix: confirm the failure isn't yours (`gh run view <queue-run> --log-failed`; the queue branch is `gh-readonly-queue/main/pr-<n>-<sha>`), then re-arm with `gh pr merge <n> --auto`.

### Branch protection: one ruleset

All protection lives in the `main protection` **ruleset**: requires a PR, both CI checks, the merge queue, and blocks deletion/force-push. There is no classic branch protection — don't add any (two sources of `strict` contradict, and the stricter silently wins).

- Merge queue parameters: `SQUASH`, `ALLGREEN` grouping, up to 5 entries, no minimum wait (`min_entries_to_merge_wait_minutes: 0`) — a PR lands once its queue run is green.
- `strict_required_status_checks_policy` is **off** on purpose: the queue already re-tests the merged result. Don't turn it back on.
- A `merge_queue` rule requires an organisation-owned repo (a personal-account repo rejects the rule type with a bare 422).
