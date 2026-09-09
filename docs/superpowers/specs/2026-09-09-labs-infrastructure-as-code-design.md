# Infrastructure as code for the labs estate

**Status:** designed, not built. Nothing in here has been applied to AWS.
**Scope:** `canopy-web`, `ace-web`, and the ACE mobile emulator. `connect-labs`
is deliberately out of scope — see *Out of scope* at the end.

## The problem, stated exactly

A deploy failed on 2026-09-09 with:

```
github-actions-labs-deploy is not authorized to perform:
elasticloadbalancing:ModifyTargetGroup on labs-jj-canopy-web-tg
```

The change that triggered it (#721) was correct. The pipeline had only ever
*created* that target group, so this was the first call to `ModifyTargetGroup`
in the stack's life, and the CI role had never been granted it.

That is not a one-off. Measured with `aws iam simulate-principal-policy`
against the live role:

| `canopy-web` stack resource | CI role can write it? |
| --------------------------- | --------------------- |
| `AWS::ECS::TaskDefinition`  | yes                   |
| `AWS::ECS::Service`         | yes (update only)     |
| `AWS::Logs::LogGroup`       | **no**                |
| `AWS::ElasticLoadBalancingV2::ListenerRule` | **no** |
| `AWS::ElasticLoadBalancingV2::TargetGroup`  | **no** |
| `AWS::ECR::Repository`      | **no**                |
| `AWS::S3::Bucket` + policy  | **no**                |
| `AWS::SecretsManager::Secret` × 7 | **no**          |

**The CI role can write 2 of the 13 resources in the stack it deploys.** The
other eleven exist because an admin created the stack originally, and every
deploy since has produced a no-op diff on them, so nobody found out. `ace-web`
is the same shape: 5 resources, CI can write 2.

So the invariant that is actually broken is:

> **A stack's deployer must be able to write every resource in it.**

Everything else in this document follows from restoring that.

### Why the obvious fix is wrong

"Grant CI the missing permissions" fails on the most important one:
`iam:PutRolePolicy` on its own role is a full privilege escalation, reachable by
anyone who can merge to `main`. It must stay denied. So IAM grants can never
live in a CI-deployed stack, and the split below is forced rather than chosen.

### What it has already cost

The same wall was hit twice and hand-patched twice. `ace-web` was granted
`ModifyTargetGroupAttributes` on `labs-jj-ace-web-tg` at some earlier date;
`canopy-web` was granted `ModifyTargetGroup` on its own target group on
2026-09-09. Two repos, one bug, two hand-edits, no shared code and no test.

Related divergence from the same cause: both apps' target groups were tuned
independently to different values (canopy 5s drain / 5s health-check interval,
ace 30s / 30s). Nobody decided they should differ.

## Decisions

### CloudFormation, not Terraform or CDK

Considered seriously, because the estate's history is AI-generated and prior
decisions carry no authority. OpenTofu was the leading candidate for two
reasons — modules would fix the duplication, and `import` blocks are a far
better workflow than CFN's import for the phase-2 core work.

Both reasons lost:

- **Modules were ruled out by a hard requirement.** Each repo must be
  self-contained: its own deploy, from its own GitHub Actions, with no need to
  know the other tenants exist. A shared module makes canopy-web's deploy depend
  on another repo being reachable and version-bumped. That is a worse property
  than the duplication it removes.
- **Automatic rollback matters more than it looks.** Two deploys failed on
  2026-09-09 and production was never affected, because CloudFormation rolled
  back on its own. Terraform has no equivalent; a failed `apply` leaves a
  half-applied estate for a human to reason about. In an estate where agents run
  the deploys, that difference is worth more than better `plan` output.

Self-containment also argues against Terraform independently: each repo would
need a state bucket, backend config, lock settings, provider pinning and a
lockfile — shared knowledge leaking back into repos meant to know nothing about
each other. CloudFormation has no state to site, share, lock or lose.

Revisit if the estate goes multi-cloud or the team grows past a couple of
people, and then it would be OpenTofu with an S3 backend — not a half-and-half.

### Duplication is answered by verification, not by DRY

Accepting self-containment means accepting that the same infrastructure shape is
written three times. The failure that duplication actually caused — the same IAM
gap twice — is better fixed by a check each repo carries than by code they
share:

> Every resource in a CI-deployed stack must be writable by the deploying
> principal, asserted before the deploy runs.

That is a small script per repo. Duplicating a check is a much better trade than
duplicating an entire infrastructure layer with no check at all.

### The split: bootstrap (admin) + app (CI)

Per repo, two stacks:

- **`<app>-bootstrap`** — applied by a human with admin credentials, deliberately
  and off-hours. Owns everything CI cannot write: ECR repository, secrets, S3
  bucket and policy, target group, listener rule, log group, and the deploy-role
  IAM grants.
- **`<app>`** — deployed by CI on every merge. Owns the task definition and the
  service, and nothing else. Takes the target group ARN and log group name from
  the bootstrap stack.

After this, today's failure is structurally impossible rather than caught by a
check: a change to a target group cannot reach a CI deploy, because CI's stack
does not contain one.

The IAM grants move from a hand-edited inline policy on a shared role into
`AWS::IAM::Policy` resources with `Roles: [!Ref DeployRoleName]` — the pattern
`connect-labs/infra/labs-monitoring.yml` already uses for
`DeployRoleLogReadPolicy`. The stack owns a *slice* of a role it does not own,
which is what lets each repo declare its own grants without any repo owning the
shared role.

### Moving resources between stacks: `create-stack-refactor`

Not retain-then-import. CloudFormation's stack-refactor operation exists for
exactly this and is available in the installed CLI (aws-cli 2.34.10).

| | retain-then-import | stack refactor |
| --- | --- | --- |
| Window where a resource is owned by no stack | yes | **none** |
| Depends on `DeletionPolicy: Retain` being right | yes | no |
| Physical resource deleted/recreated | only if Retain fails | **never** |
| Preview | change set | `list-stack-refactor-actions` |

The decisive property: `create-stack-refactor` followed by
`list-stack-refactor-actions` shows every action **without executing anything**.
Whether a given resource type is supported is therefore answered by looking, not
by guessing.

Fallback, per resource type, if refactor rejects it: retain-then-import. Its
backstop is that CloudFormation deletes a Secrets Manager secret with a recovery
window rather than immediately, so even a mistake is restorable.

### Mobile emulator: Terraform → CloudFormation

`ace-web/infra/mobile` is Terraform with **no backend block and no state file
anywhere**. Its `.gitignore` says so outright: *"state stays local until
promoted."* The resources are real and running —
instance `i-0c447ed51374a4871` (stopped since 2026-06-12),
bucket `ace-mobile-artifacts-labs`, role `ace-mobile-instance-role-labs` — but
Terraform has lost track of them. `terraform apply` today would try to create
duplicates and collide on the bucket and role names.

It is IaC in appearance only, and it is the estate's only Terraform. Converting
it to CloudFormation removes the second tool and the class of failure that
killed it.

The two `null_resource` provisioners (`stop_on_create`, `enable_nested_virt`)
are `local-exec` shell-outs to the AWS CLI. They are **one-shot bootstrap steps,
not continuous desired state** — Terraform models them as `null_resource` hacks
precisely because they do not fit a declarative model either. They become a
documented `bootstrap.sh` run once, which is cleaner than what exists rather
than a compromise.

## Sequence

Deliberately ordered so the risky mechanism is exercised twice on things that do
not matter before it touches a live credential.

1. **Verification first, everywhere.** The permissions preflight and drift
   detection land before any restructuring, so the rest of the work is observed
   rather than assumed. Drift detection would have caught the live drift found
   on 2026-09-09 (`labs-jj-audit-analytics` → `UmamiTaskDefinition` →
   `ContainerDefinitions/0/Environment/2`, changed out-of-band).
2. **Mobile emulator.** Lowest stakes — already broken, nothing depends on it.
   Proves plain resource import and the CFN conversion. Ends with the instance
   booted and a Maestro flow actually run, because "it converted" is not
   "it works".
3. **ace-web.** 5 resources, no secrets. First real use of `create-stack-refactor`.
4. **canopy-web.** 13 resources including the 7 secrets, by which point the
   mechanism has been used twice.
5. **Core platform** — ALB, ECS cluster, RDS, VPC, and the three IAM roles are
   still click-ops. Phase 2, deliberately deferred, its own spec.

## Verification

Three things, in each repo, none shared:

- **Permissions preflight.** Before `execute-change-set`, assert via
  `simulate-principal-policy` that the deploying principal can write every
  resource the CHANGE SET touches — not, as first written here, "every
  resource type in the CI stack": the code checks only the resource changes
  actually in the change set, and only the Add/Modify actions that would fail
  a deploy (Remove is deliberately not blocking — CloudFormation tolerates a
  failed cleanup delete, measured on this stack 2026-09-09). The literal
  "every resource type" reading would demand Create-family grants an
  image-tag deploy never calls (e.g. `ecs:CreateService` when the deploy only
  updates), and would block every deploy running today. Fails with the
  missing action named, rather than a 403 five minutes into a rollout.
- **Drift detection.** `detect-stack-drift`, currently `workflow_dispatch`
  only — the `schedule:` trigger is deferred, not shipped, until the grant
  below lands (a nightly run would fail every night with the same
  implicitDeny). See `.github/workflows/infra-drift.yml`.
- **Deploy failure diagnostics.** Shipped ahead of this spec in #723: the deploy
  step prints stack events on failure and raises the first failing resource's
  reason. Finding the cause of the 2026-09-09 failure otherwise required AWS
  credentials and a separate session.

**The preflight and drift detection both need grants the deploy role does not
have.** Measured against the live role:

    iam:SimulatePrincipalPolicy      implicitDeny
    cloudformation:ListChangeSets    implicitDeny
    cloudformation:DetectStackDrift  implicitDeny (+ the two Describe* reads)

Both belong in the bootstrap stack (a human-applied, admin-credentialed
grant), not a hand-edit of the shared CI role — that hand-edit is exactly the
debt this whole spec exists to repay. Until the bootstrap stack lands: the
preflight degrades to advisory (an unevaluable preflight logs a warning and
the deploy proceeds, rather than blocking on a permission it cannot grant
itself — see `deploy/aws/preflight.py` exit code 2), and drift detection stays
`workflow_dispatch`-only, runnable on demand by an admin locally.

## Risks

- **Moving live secrets between stacks.** Mitigated by ordering (secrets last,
  after the mechanism is proven twice), by `list-stack-refactor-actions` being
  inspectable before execution, and by Secrets Manager's recovery window as a
  final backstop. Every secret value is read before and diffed after.
- **The bootstrap stacks need admin credentials.** They are applied by a human,
  off-hours, by design. This is a feature: changing a target group or a secret
  should be deliberate. It is also a dependency — bootstrap must be applied
  before the app stack that references its exports.
- **`Fn::ImportValue` is rigid.** An exported value cannot be deleted while
  imported. The app stack should take the target group ARN and log group name as
  *parameters* supplied by the deploy workflow, read from the bootstrap stack's
  outputs, rather than via `Fn::ImportValue`, so the two stacks stay
  independently updatable.
- **Emulator conversion may surface work the Terraform hid.** The instance has
  been stopped since June and has never been tested since. Step 2 ends with a
  real Maestro run for that reason.

## Out of scope

- **`connect-labs`.** Its `labs-jj-web` and `labs-jj-worker` services deploy via
  raw `deploy/task-definitions/*.json` plus `register-task-definition` and
  `update-service`, owned by no stack — the two-writer pattern canopy-web
  abandoned after it froze `ImageTag` for ten days and 118 revisions. It is the
  biggest app and the same argument applies to it, but it is not this work.
  A canopy task is filed for Hal to review it.
- **The shared core platform.** Phase 2, above.
- **`ecs:DeregisterTaskDefinition`.** Missing from the deploy role, so
  CloudFormation cannot clean up superseded task definitions and every deploy
  logs a `DELETE_FAILED` it then ignores. Cosmetic; noted so it is not
  rediscovered as a mystery.
