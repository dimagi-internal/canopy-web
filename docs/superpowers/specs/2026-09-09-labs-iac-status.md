# Labs infrastructure-as-code: where this stands

**Written 2026-09-09 at a deliberate stopping point.** Read this first if you are
picking the work up cold. The design reasoning is in
`2026-09-09-labs-infrastructure-as-code-design.md`; this file is only "what is
done, what is not, and what to be careful of".

## Done and live

| | |
| --- | --- |
| **Deploy preflight** (canopy-web #732) | Merged and deployed. Before CloudFormation applies a change set, the deploy asks IAM whether the deploying role may perform the actions CFN will call, and refuses with the missing action named. |
| **Drift detection** (canopy-web #732) | Runs daily at 08:00 UTC, and on demand from the Actions tab: **Infra drift**. |
| **Mobile emulator under CloudFormation** (ace-web #761, #762) | Stack `ace-mobile` owns all 7 resources. Drift `IN_SYNC`. Terraform still present — deleting it is the one leftover task. |
| **Emulator verified working** | Booted 2026-09-09 after 3 months stopped. Maestro drove it to launch CommCare, exit 0. Stopped again. |

## Not done, and deliberately so

- **The bootstrap/app stack split**, for both canopy-web and ace-web. Plans are
  written (`docs/plans/2026-09-09-ace-web-bootstrap-split.md` in ace-web; the
  canopy-web equivalent is NOT yet written). This is the change that makes a
  permissions failure structurally impossible rather than caught by a check.
- **The shared core platform** — ALB, ECS cluster, RDS, VPC and the three IAM
  roles are still click-ops, owned by no stack. Phase 2, needs its own spec.
- **`infra/mobile/` in ace-web** — the dead Terraform. Safe to delete now that
  `ace-mobile` owns the resources; kept only because it had not been reviewed.
- **connect-labs** — untouched by design. Filed as agent task #172 for Hal.

## What to be careful of

**Four IAM grants were made BY HAND on 2026-09-09 and are tracked nowhere.**
They are on the shared `github-actions-labs-deploy` role, which is not in any
repo. If someone rebuilds that role from scratch, these vanish and deploys break:

| Grant | Why | Policy |
| --- | --- | --- |
| `elasticloadbalancing:ModifyTargetGroup` | the original 2026-09-09 deploy failure | `GitHubActionsLabsDeployPolicy` |
| `elasticloadbalancing:DescribeTargetGroups` on `*` | the CFN handler calls it; not resource-scopable | `GitHubActionsLabsDeployPolicy` |
| `cloudformation:ListChangeSets` | the preflight locates its change set with it | `CanopyWebCloudFormationDeploy` |
| `iam:SimulatePrincipalPolicy` on `*` | the preflight IS this call | `GitHubActionsLabsDeployPolicy` |
| `cloudformation:DetectStackDrift` | the drift workflow | `CanopyWebCloudFormationDeploy` |
| `cloudformation:DescribeStackResourceDrifts` | the drift workflow | `CanopyWebCloudFormationDeploy` |
| `cloudformation:DescribeStackDriftDetectionStatus` on `*` | the drift workflow; takes a DETECTION ID, not a stack ARN, so a stack-scoped grant matches nothing | `CanopyWebCloudFormationDeploy` |

**Seven, not four.** Two actions here (`DescribeTargetGroups`,
`DescribeStackDriftDetectionStatus`) cannot be resource-scoped and must be
granted on `*`. Both were first written stack-scoped, both silently denied, and
both cost a debugging round. If you move these into a bootstrap stack, keep
those two on `*` — "tidying" them to a stack scope breaks them without an error
anyone will read.

**These seven are the debt the whole spec exists to repay.** They belong in the
per-app bootstrap stacks, which is what the unwritten plan 4 and the written
plan 3 are for. Until then they are invisible.

A side effect worth knowing: adding `ModifyTargetGroup` also granted it to
**ace-web**, because that policy statement's Resource list already carried
ace-web's target group ARN. Benign — it fixes the same latent bug there — but
nobody asked for it.

**The drift workflow now runs daily** (08:00 UTC). It shipped dispatch-only
because the deploy role lacked the three drift actions; those were granted by
hand on 2026-09-09 and the schedule turned on in the same change.

**The `labs-jj-audit-analytics` drift is FIXED** (connect-labs #1691). It was
not an out-of-band edit to revert: `UMAMI_BUILD_VERSION` was declared TWICE in
the template by the Umami upgrade work, ECS kept one, and the running task had
three environment variables where the template described four. The live side was
right; the template was wrong. Since Umami upgrades recur, a duplicate left in
place would have re-drifted the stack on every one.

## The rule the whole thing turns on

> **A stack's deployer must be able to write every resource in it.**

canopy-web's stack violates it 11 times out of 13, and only appears to work
because those eleven never change. That is what the split fixes. Check any new
stack against this rule before shipping it.

## Running things by hand

```bash
export AWS_PROFILE=labs AWS_DEFAULT_REGION=us-east-1

# Is a deploy applicable? (read-only)
aws cloudformation deploy --stack-name canopy-web \
  --template-file deploy/aws/canopy-web.cfn.yaml \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset --no-execute-changeset \
  --parameter-overrides ImageTag="$(git rev-parse HEAD)"
CS=$(aws cloudformation list-change-sets --stack-name canopy-web \
       --query 'sort_by(Summaries,&CreationTime)[-1].ChangeSetId' --output text)
uv run python -m deploy.aws.preflight --stack canopy-web --change-set "$CS" \
  --principal arn:aws:iam::858923557655:role/github-actions-labs-deploy
aws cloudformation delete-change-set --change-set-name "$CS"   # clean up

# Boot the mobile emulator, test it, stop it
aws ec2 start-instances --instance-ids i-0c447ed51374a4871
# ... SSM in, run maestro ...
aws ec2 stop-instances --instance-ids i-0c447ed51374a4871      # ALWAYS
```
