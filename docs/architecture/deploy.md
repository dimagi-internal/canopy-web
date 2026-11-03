# Deploy, CI and the merge queue

Where canopy-web runs, how a change gets from a PR to production, and the AWS/IAM rules that keep the shared labs account safe. Frontend caching (why a deploy is never hidden behind a stale shell) is in [`frontend.md`](frontend.md).

## Where it runs

- **AWS ECS Fargate** on the shared labs platform (account `858923557655`, `us-east-1`), one container: Django serves the built SPA, the API and MCP. Database: the `canopy_web` DB on the shared RDS `labs-jj-postgres`.
- Served at **`https://canopy.dimagi.com/`**, at the root, behind the shared ALB via a host-header rule (`HostListenerRule`, priority 10, in `deploy/aws/canopy-web.cfn.yaml`). The certificate and the Cloudflare DNS record were made by hand and live in no repo.
- Runtime settings: `config/settings/connectlabs.py` (extends `production.py`; no `FORCE_SCRIPT_NAME` — setting it would serve canopy under a prefix everywhere).
- Infra: `deploy/aws/canopy-web.cfn.yaml` (CloudFormation).

### The vector extension

KB vectors (`apps/agents/kb_vectors.py`, dormant: `KB_VECTORS` is off, so nothing uses them) need pgvector ≥ 0.8 (iterative index scans) in the `canopy_web` database. `vector` is not a trusted extension, so canopy-web's own role (`canopy_web`, not `rds_superuser`) cannot create it, and migration `agents/0064` only runs `CREATE EXTENSION IF NOT EXISTS vector` — a no-op once it exists. On `labs-jj-postgres` (PostgreSQL 16.13, pgvector 0.8.1) it was created once by the RDS master user `connectadmin` (password: Secrets Manager `labs-jj-db-password`), from inside the canopy-web task over ECS Exec since the RDS is not public. To redo it on a new database: connect as the master user to `canopy_web` and run `CREATE EXTENSION IF NOT EXISTS vector;`, then check with `SELECT extversion FROM pg_extension WHERE extname = 'vector'`. CI's PostgreSQL job and `docker compose` use the `pgvector/pgvector:pg16` image, where the superuser creates it in the migration. On a PostgreSQL without pgvector installed, migration 0064 fails at the `halfvec` column.

### The old address still serves machines

The old address `https://labs.connect.dimagi.com/canopy/` (now `https://canopy.dimagi.com/`) still reaches the same container through the ALB's `/canopy/*` rule. `config/asgi_prefix.py` strips the prefix (`CANOPY_STRIP_PREFIX`, read in `config/asgi.py`) and `apps/common/legacy_prefix.py` decides:

- **Machine traffic** (API, WebSockets, OAuth, well-known, health) is served in place — runners, the canopy plugin, MCP clients, Slack, Gmail push and ace-web hold the old address and do not follow redirects reliably. Don't turn these into redirects.
- **Browser pages** (GET/HEAD outside machine prefixes) are 302'd to `canopy.dimagi.com`.
- Docs must not send a reader to the old address: `tests/test_old_address_in_docs.py` fails on a markdown line naming `labs.connect.dimagi.com/canopy` that does not also name `canopy.dimagi.com`.

### Connection providers

No environment or secret settings: providers (Salesforce, Google) are set up by a workspace admin in Settings → Connections, who registers the callback shown there (`https://canopy.dimagi.com/auth/connections/<provider>/<workspace>/callback/`) with the provider; the client secret is stored encrypted in the database and is the only connection secret canopy-web holds ([`vault.md`](vault.md) § OAuth connections are sealed to their owner). Cloud boxes open and refresh connections with the canopy CLI, so each box needs a CLI at or above `CONNECTIONS_ZK_MIN_CLI` (`runner/ec2/cloud_runner.py`); an older one logs one `connections.stage_failed` and stages nothing. Release order: the CLI first (boxes pick it up), then canopy-web. A new CLI against an older canopy-web gets 404 from the material route, which it treats as not connected (a quiet exit 3), so connections stop working on boxes from the CLI merge until the web deploy: do the two close together. A deploy does not carry over connections made earlier, which were server-held: the migration deletes them, and each person reconnects once and shares on the confirmation screen (`/w/<workspace>/connections/<id>/share`). The material route's `ambiguous` answer tells people to pass `--workspace`, which the canopy CLI supports.

Refreshing now happens on the box, so an egress-locked runner (`RUNNER_EGRESS`, or a sensitive-data box) reaches the provider itself. Its lock must admit the hosts its connections use: `login.salesforce.com` (or `test.salesforce.com`) and the org's My Domain host for Salesforce, `oauth2.googleapis.com` for Google. They are deliberately not in `EGRESS_BASE_HOSTS`, which every locked runner admits whether or not it holds a connection, and canopy-web does not add them for you: add them to the box's `RUNNER_EGRESS_ALLOW` or the runner's egress setting. Without them the stage fails as `connections.stage_failed` and the turn runs without a token, and that holder's revoke fails too: it keeps heartbeating, so it stays a revoker, and connecting the account again is refused as `revoking` until the 7-day sweep (`keeper.REVOKE_GRACE`).

### canopy-web can use a connection

Per-connection switch "canopy-web can use this" ([`vault.md`](vault.md) § OAuth connections are sealed to their owner). Three pieces of deployment, rolled out in this order:

1. **The key's stack**, `deploy/aws/canopy-web-connections-key.cfn.yaml`: a KMS key (`ECC_NIST_P256`, `KEY_AGREEMENT`, alias `alias/canopy-web-connections`) whose policy gives `canopy-web-task-role` only `kms:DeriveSharedSecret` and `kms:GetPublicKey`. Deployed by hand with a labs admin login, like the role stack, and only with the owner's approval. Its `KeyAlias` output is the next step's value. The role stack grants no `kms:`; the key's policy names the role.
2. **canopy-web**: set the `ConnectionsServerKmsKey` parameter of `canopy-web.cfn.yaml` to that alias and deploy. It reaches the task as the environment variable `CONNECTIONS_SERVER_KMS_KEY` (empty, the default, means canopy-web uses no connection and every connection behaves as switched off). New Salesforce connections seal to canopy-web from then on.
3. **Confirm the cloud boxes run canopy CLI at or above 0.2.837** (`CONNECTIONS_ZK_MIN_CLI` in `runner/ec2/cloud_runner.py`). An older CLI refuses any statement carrying `canopy_web`. Then set the `ConnectionsServerStatements` parameter to `true` and deploy: it reaches the task as `CONNECTIONS_SERVER_STATEMENTS` (default `false`; `config/settings/base.py`). While false, a share that omits canopy-web turns the switch off, and a connection with the switch on cannot also be shared with machines; the Share screen says so (`ShareServerOut.statements_enabled`).

Read the settings from the running task's environment (ECS Exec, `printenv CONNECTIONS_SERVER_KMS_KEY CONNECTIONS_SERVER_STATEMENTS`) or the task definition; `GET /api/connections/{id}/share` shows `server.available` (key readable now) and `server.statements_enabled`.

- **Roll back the image, not the setting.** With the key unset, a rotated token a machine writes back no longer includes canopy-web, so canopy-web drops out of that envelope until the owner's next share. A named canopy-web entry already in an envelope is kept, not stripped, while the key is unset.
- **Cut canopy-web off at once:** remove the task role's grant from the key's policy (redeploy the key stack without it). Every open and derive then fails; nothing in the database changes. KMS use is in CloudTrail.

### Protocol identity is pinned by connected sites

canopy's protocol identity — CIMD client_id, assertion audience, own issuer, MCP resource — is `CANOPY_IDENTITY_BASE_URL` (`https://canopy.dimagi.com`). ace-web's and connect-labs' `CANOPY_CLIENT_ID` / `CANOPY_BASE_URL` pin it, so changing it is a coordinated change with every connected site. `CANOPY_FORMER_BASE_URLS` names the old address so the Slack sync can recognise and move the commands and webhooks it registered there.

## Deploying

Merging to `main` does **not** deploy. Deploys run only from GitHub, via the **Deploy to Labs (AWS)** workflow (`.github/workflows/deploy-labs.yml`, `workflow_dispatch`):

```bash
gh workflow run "Deploy to Labs (AWS)" --ref main                          # migrations run automatically
gh workflow run "Deploy to Labs (AWS)" --ref main -f skip_migrations=true  # EMERGENCY ONLY
```

The workflow hard-fails on any ref other than `main`, and deploys main's tip as of when the run starts, not the dispatch-time sha — so a dispatch that waited behind another rollout ships everything merged meanwhile, and a queued deploy can never roll prod backwards. Dispatching the commit already serving ends green with build and deploy skipped; `-f force=true` rolls it again (e.g. to pick up a changed secret).

Pipeline: build → push to ECR (`labs-jj-canopy-web`) → **migrate** on a throwaway task def → create a CloudFormation change set (`aws cloudformation deploy --no-execute-changeset --parameter-overrides ImageTag=<sha>`) → **preflight** → `aws cloudformation execute-change-set`, which registers the real task definition and rolls the ECS service.

### CloudFormation owns the task definition and service

- One writer per resource: the pipeline only passes `ImageTag`; CFN registers the task definition and rolls the service. Never register task-definition revisions or call `ecs update-service` outside CFN — the stack's `ImageTag` would go stale and the next plain `cloudformation deploy` would silently roll prod back.
- CFN resolves intrinsics (`!Ref`, `!Sub`) itself; don't add a step that reads the template outside CloudFormation.
- **The migration seam.** CFN rolls `Service` and its `TaskDefinition` atomically, so migrations run first on a throwaway task def built from the LIVE one with only the image swapped. Its env is therefore one deploy stale: if a deploy both changes an env var and ships a migration that reads it, deploy twice.
- Migrations are idempotent (`migrate --noinput`) and run before cutover, on every deploy whose diff from the live image touches a `migrations/` file, `pyproject.toml` or `uv.lock` (otherwise the ~1 min migrate task is skipped; an unknown live sha or `force` migrates). Write them the obvious way — destructive migrations are fine, and a brief window of errors during a deploy is accepted; don't split a one-line migration into expand/contract PRs.
- **The pipeline does that split for drops.** The window hurts when the old task reads a column a migration just dropped: on a hot table, 30-40% of requests fail for about 3.5 minutes (#2447). So a deploy whose migration diff has a `RemoveField` or `DeleteModel` runs `migrate_for_rollout`, which applies everything except a tail of pure drops and makes those columns nullable; a plain `migrate` applies the drops after a successful rollout. If anything held back is more than a drop (an `AddField` or `RunPython` behind it, or a rename), new code might need it before cutover, so it falls back to a plain `migrate` and the window stays.
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

`labs-jj-ecs-task-execution-role` is shared by every container in every labs app. It reads secrets only through scoped inline policies on four prefixes — `labs-jj-*`, `canopy-web/*`, `ace-web/*`, `labs/*` (`labs/SENTRY_DSN` is the one `labs-jj-*` does not match; connect-labs' Sentry needs it) — plus three named umami ARNs. Don't attach the AWS-managed `SecretsManagerReadWrite` (or any `secretsmanager:*` on `*`) to it — that lets any container read, overwrite or delete every secret in the account. The unprefixed legacy secrets stay as they are (no rename API, they span four apps, and ace-web/connect-labs reference pre-existing ARNs rather than declaring secrets in CFN).

### canopy-web's own task role

canopy-web runs as `canopy-web-task-role` (`deploy/aws/canopy-web-task-role.cfn.yaml`), canopy-web's alone. Don't put canopy's grants back on `labs-jj-ecs-task-role`: every labs container shares it, so connect-labs and ace-web could then send as canopy.dimagi.com, read inbound mail, and drive any agent's cloud capability. Every grant keeps one owner: the role stack owns the role, ECS Exec (`ssmmessages`) and an identity grant on the attachments bucket; `canopy-email` (`canopy-ses-send`) and `canopy-mail` (`canopy-mail`) attach their policies through `TaskRoleName`, a comma-separated list; each capability stack attaches `BrokerPolicy` through `BrokerRoleName`; each runner fleet's stack (`runner/ec2/fleet.cfn.yaml`) attaches `ScalerPolicy` — size and protect that one group — through `CanopyTaskRoleName`; the attachments bucket policy and the vault KMS key's policy (`alias/canopy-web-vault`, created only with `EnableVaultKms=true`; no code reads it any more — agent values are zero-knowledge — so leave it off) name the canopy-web stack's `TaskRoleArn`. The code's AWS calls are exactly those (`apps/common/email.py`, `apps/mail/ses.py`, `apps/canopy_sessions/attachment_storage.py`, `apps/capabilities/aws.py`, `apps/harness/fleets.py`); secrets and logs are the execution role's. `tests/test_task_role_stack.py` pins the stacks and the deploy role's `PassOwnTaskRoleToEcs`.

The shared role holds none of canopy's grants, and canopy-web's deploy role cannot pass it to canopy-web (`PassSharedTaskRolesToEcs` omits it). Verify the role from inside the running task over ECS Exec (`aws ecs execute-command`, needs a labs admin login): a notification email arrives (SES send); a workspace mail domain receives and an agent replies (inbound bucket + tenant send); a chat attachment uploads and downloads; a capability lease reaches `ready`, execs and releases.

A grant canopy needs goes into the role stack, or into the owning stack's `TaskRoleName` / `BrokerRoleName` — never onto the shared role. A new capability stack is deployed with `BrokerRoleName=canopy-web-task-role`.

## CI and merging

CI (`.github/workflows/ci.yml`) runs on every PR, in the merge queue, and on push to `main`. Required checks: **Backend tests** (which also verifies `frontend/src/api/generated.ts` is fresh and the `dimagi-canopy` SDK version bump) and **Frontend build** (which also enforces the `canopy-ui`/`canopy-client` version bump).

The full suites run once, in the merge queue, sharded so no job sets the wait alone: `Backend pytest` ×3, `Frontend tests` ×2, the PostgreSQL and runner suites under xdist. On a PR the two required checks are gates over the quick PR-only jobs: `PR fast checks` (ruff, migrations in sync, `generated.ts`, the import ratchets, and the backend test files the PR changed) and `PR frontend checks` (`tsc -b` and `vitest related` on the PR's changed frontend files; the whole vitest suite when `package.json`, the lockfile or a vite/ts config changed). A queue failure ejects the PR and rebuilds every entry behind it, so whatever a PR can fail on its own belongs in these jobs.

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
