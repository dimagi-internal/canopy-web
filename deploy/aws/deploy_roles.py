"""Generate one scoped GitHub Actions deploy role per deployment.

Run it to print/emit the policies (to /tmp/role-<app>-{policy,trust}.json);
apply the permissions with `aws iam put-role-policy` and a TRUST change with

    aws iam update-assume-role-policy --role-name github-actions-<app>-deploy \
        --policy-document file:///tmp/role-<app>-trust.json

Nothing applies this file automatically — an edit here is not live until a
human runs those commands. This
file exists because the alternative is what was here before: the deploy role's
grants were hand-made on a shared role and tracked nowhere, so nobody could
review them and adding a resource type meant a failed deploy for whoever
deployed next (see the 2026-09-13 incident — a new `AWS::SecretsManager::Secret`
in canopy-web's template blocked ALL three deployments).

Before this, ONE role (`github-actions-labs-deploy`) was assumable by six OIDC
subjects — including two personal-account repos — and held the union of every
deployment's permissions. connect-labs' CI could modify canopy-web's stack and
delete its secrets. Now each deployment has a role trusting exactly one repo
and scoped to its own stack, secrets, ECR repo, target group and log groups.

Shared infra stays shared on purpose (one ALB, one RDS, one ECS cluster, the
two task roles) — that is a cost decision, not an accident.

**The TASK EXECUTION role is the other half, and was the bigger hole.**
`labs-jj-ecs-task-execution-role` is shared by every container in every app and
had the AWS-managed `SecretsManagerReadWrite` attached: `secretsmanager:*` on
`*`, so any container's startup role could read, overwrite or DELETE every
secret in the account — plus `lambda:CreateFunction`, `s3:GetObject` and
`cloudformation:CreateChangeSet`, none of which a task-startup role has any use
for. Someone had carefully scoped the inline policy to `labs-jj-*`; the managed
policy made that scoping decorative.

Detached 2026-09-13. Reads are now granted only by scoped inline policies:
`labs-jj-*` (SecretsManagerAccess, pre-existing), the three umami ARNs, and
`canopy-web/*` + `ace-web/*` + `labs/*` (AppPrefixedSecretsRead, added first so
there was never a window where a container could not read). Those four prefixes
are exactly what the live task definitions reference — enumerated from ECS, not
guessed. No secret uses a customer-managed KMS key, so nothing needed
`kms:Decrypt`.

Renaming the 34 unprefixed secrets was considered and is NOT the fix. Secrets
Manager has no rename API (it is create + repoint every task definition +
delete), the 34 span FOUR apps rather than one, and neither ace-web nor
connect-labs declares secrets in CloudFormation at all — they reference
pre-existing ARNs — so no deploy role needs a secrets grant for them. The
isolation people expect from prefixes was really being defeated by the shared
execution role, which is now scoped.


Built by NARROWING the existing shared policies rather than authoring from
scratch: omission is the dangerous failure (a missing permission breaks a
deploy), so every statement below traces to one that exists today. Only the
resources change.

`*` is kept exactly where the existing policies keep it, for the reason their
own Sids record: the action does not support resource-level permissions, or
(for creates) the resource has no ARN yet when the preflight simulates.
"""
import json

ACCT = "858923557655"
REGION = "us-east-1"
ECS_CLUSTER = "labs-jj-cluster"

APPS = {
    "canopy-web": {
        # canopy-web was transferred into the org, so GitHub mints the IMMUTABLE
        # subject. The name-based form silently fails AssumeRoleWithWebIdentity.
        #
        # MAIN ONLY, not `:*`. Both workflows that assume this role run from
        # main and nowhere else: deploy-labs.yml refuses any other ref in its
        # `guard` job, and infra-drift.yml runs on a schedule (default branch)
        # plus a dispatch that now refuses a branch too. With `:*` any branch
        # of this repo — a dispatch from an unmerged PR branch — could mint
        # this role and push an image or change the stack; the in-workflow
        # guard was the only thing stopping it, and the workflow file on that
        # branch is exactly what such a branch controls. Neither workflow uses
        # a GitHub `environment:`, which would change the subject's shape to
        # `:environment:<name>` — adding one means updating this.
        "sub": "repo:dimagi-internal@272902307/canopy-web@1193139337:ref:refs/heads/main",
        "stack": "canopy-web",
        "secret_prefix": "canopy-web/",
        "ecr": ["labs-jj-canopy-web"],
        "target_group": "labs-jj-canopy-web-tg",
        "log_groups": ["/ecs/labs-jj-canopy-web"],
        # canopy-web.cfn.yaml: ServiceName + TaskDefinition Family.
        "ecs_services": ["labs-jj-canopy-web"],
        "task_families": ["labs-jj-canopy-web"],
        "preflight": True,
        "metrics": True,
    },
    "ace-web": {
        "sub": "repo:dimagi-internal/ace-web:*",
        "stack": "ace-web",
        "secret_prefix": "ace-web/",
        "ecr": ["labs-jj-ace-web", "labs-jj-ace-web-frontend"],
        "target_group": "labs-jj-ace-web-tg",
        "log_groups": ["/ecs/labs-jj-ace-web"],
        "preflight": False,
        "metrics": False,
    },
    "connect-labs": {
        "sub": "repo:dimagi-internal/connect-labs:*",
        # No CloudFormation stack: connect-labs updates ECS directly.
        "stack": None,
        # Its 34 secrets have NO prefix, so there is nothing to scope to. Left
        # out entirely rather than granted on "*": its deploy does not create
        # secrets, so it needs none. See the note in the PR.
        "secret_prefix": None,
        "ecr": ["labs-jj-commcare-connect"],
        "target_group": None,
        "log_groups": ["/ecs/labs-jj-web", "/ecs/labs-jj-worker"],
        "preflight": False,
        "metrics": True,
    },
}


def policy_for(app: str, cfg: dict) -> dict:
    st = []

    if cfg["stack"]:
        st.append({
            "Sid": "OwnStackChangeSetsOnly",
            "Effect": "Allow",
            "Action": [
                "cloudformation:CreateChangeSet", "cloudformation:DescribeChangeSet",
                "cloudformation:ExecuteChangeSet", "cloudformation:DeleteChangeSet",
                "cloudformation:ListChangeSets", "cloudformation:DescribeStacks",
                "cloudformation:DescribeStackEvents", "cloudformation:DescribeStackResource",
                "cloudformation:DescribeStackResources", "cloudformation:GetTemplate",
                "cloudformation:UpdateStack", "cloudformation:CreateStack",
                "cloudformation:DetectStackDrift", "cloudformation:DescribeStackResourceDrifts",
                "cloudformation:CreateUploadBucket",
            ],
            "Resource": [
                f"arn:aws:cloudformation:{REGION}:{ACCT}:stack/{cfg['stack']}/*",
                # A change-set ARN carries no stack, so it cannot be scoped to one.
                f"arn:aws:cloudformation:{REGION}:{ACCT}:changeSet/*/*",
            ],
        })
        st.append({
            "Sid": "CfnCallsThatTakeNoResource",
            "Effect": "Allow",
            "Action": [
                "cloudformation:GetTemplateSummary",
                "cloudformation:DescribeStackDriftDetectionStatus",
            ],
            "Resource": "*",
        })

    st.append({
        "Sid": "OwnEcrReposOnly",
        "Effect": "Allow",
        "Action": [
            "ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer",
            "ecr:BatchGetImage", "ecr:PutImage", "ecr:InitiateLayerUpload",
            "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
            "ecr:DescribeImages", "ecr:ListImages",
        ],
        "Resource": [f"arn:aws:ecr:{REGION}:{ACCT}:repository/{r}" for r in cfg["ecr"]],
    })
    st.append({
        "Sid": "EcrLoginTakesNoResource",
        "Effect": "Allow",
        "Action": "ecr:GetAuthorizationToken",
        "Resource": "*",
    })

    if cfg.get("ecs_services"):
        # Scoped: this deployment may roll ITS service and run ITS task family,
        # and nothing else on the shared cluster. With these on "*" (below, and
        # what ace-web/connect-labs still have), canopy-web's deploy credentials
        # could UpdateService ace-web's or connect-labs' service, or RunTask any
        # task definition in the account with the shared task roles — i.e. a
        # bad workflow on canopy-web reached every labs app. Agents ship to
        # main without a human review, so "it had to merge first" is not the
        # boundary; what the role can touch is.
        #
        # Every call the pipeline makes is still covered (deploy-labs.yml):
        #   describe/register the migrate task def  → EcsCallsThatTakeNoResource
        #   run-task + describe-tasks + wait        → RunOwnTaskFamilyOnly / no-resource
        #   CFN rolling the service (UpdateService) → OwnServiceOnly
        #   the failure report's describe-services  → OwnServiceOnly
        # The migrate task def is registered from the LIVE one with only the
        # image swapped, so it is the same family. preflight.py simulates
        # UpdateService against the service's real ARN, so it sees this grant.
        cluster_arn = f"arn:aws:ecs:{REGION}:{ACCT}:cluster/{ECS_CLUSTER}"
        st.append({
            "Sid": "OwnServiceOnly",
            "Effect": "Allow",
            "Action": ["ecs:UpdateService", "ecs:DescribeServices"],
            # The long, cluster-qualified ARN — what `describe-services`
            # returns for this service (checked 2026-10-06).
            "Resource": [f"arn:aws:ecs:{REGION}:{ACCT}:service/{ECS_CLUSTER}/{s}"
                         for s in cfg["ecs_services"]],
        })
        st.append({
            "Sid": "RunOwnTaskFamilyOnly",
            "Effect": "Allow",
            "Action": "ecs:RunTask",
            "Resource": [f"arn:aws:ecs:{REGION}:{ACCT}:task-definition/{f}:*"
                         for f in cfg["task_families"]],
            "Condition": {"ArnEquals": {"ecs:cluster": cluster_arn}},
        })
        # Register/DescribeTaskDefinition take no resource. Registering alone
        # runs nothing (RunTask/UpdateService above are what would), and
        # DescribeTasks/ListTasks are reads.
        st.append({
            "Sid": "EcsCallsThatTakeNoResource",
            "Effect": "Allow",
            "Action": [
                "ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition",
                "ecs:DescribeTasks", "ecs:ListTasks",
            ],
            "Resource": "*",
        })
    else:
        # Unscoped, as the shared policy had it: the describe/register calls do
        # not support resource-level permissions, and narrowing RunTask/
        # UpdateService needs this deployment's service and task family names,
        # which are not recorded here — a guess would be a broken deploy.
        st.append({
            "Sid": "EcsDeployActions",
            "Effect": "Allow",
            "Action": [
                "ecs:UpdateService", "ecs:DescribeServices", "ecs:DescribeTaskDefinition",
                "ecs:DescribeTasks", "ecs:ListTasks", "ecs:RunTask",
                "ecs:RegisterTaskDefinition",
            ],
            "Resource": "*",
        })
    st.append({
        "Sid": "PassSharedTaskRolesToEcs",
        "Effect": "Allow",
        "Action": "iam:PassRole",
        "Resource": [
            f"arn:aws:iam::{ACCT}:role/labs-jj-ecs-task-execution-role",
            f"arn:aws:iam::{ACCT}:role/labs-jj-ecs-task-role",
        ],
    })
    st.append({
        "Sid": "NetworkDescribesForRunTask",
        "Effect": "Allow",
        "Action": [
            "ec2:DescribeVpcs", "ec2:DescribeSubnets",
            "ec2:DescribeSecurityGroups", "ec2:DescribeNetworkInterfaces",
        ],
        "Resource": "*",
    })

    if cfg["target_group"]:
        st.append({
            "Sid": "OwnTargetGroupOnly",
            "Effect": "Allow",
            "Action": [
                "elasticloadbalancing:DescribeTargetGroupAttributes",
                "elasticloadbalancing:ModifyTargetGroup",
                "elasticloadbalancing:ModifyTargetGroupAttributes",
            ],
            "Resource": [
                f"arn:aws:elasticloadbalancing:{REGION}:{ACCT}:targetgroup/{cfg['target_group']}/*"
            ],
        })
        st.append({
            "Sid": "ElbDescribeTakesNoResource",
            "Effect": "Allow",
            "Action": "elasticloadbalancing:DescribeTargetGroups",
            "Resource": "*",
        })

    if cfg["secret_prefix"]:
        st.append({
            "Sid": "CreateTimeSecretActionsCannotBeScoped",
            "Effect": "Allow",
            "Action": [
                "secretsmanager:CreateSecret", "secretsmanager:TagResource",
                "secretsmanager:GetRandomPassword",
            ],
            "Resource": "*",
        })
        st.append({
            "Sid": "MutateOwnSecretsOnly",
            "Effect": "Allow",
            "Action": [
                "secretsmanager:DescribeSecret", "secretsmanager:UpdateSecret",
                "secretsmanager:PutSecretValue", "secretsmanager:DeleteSecret",
                "secretsmanager:RestoreSecret", "secretsmanager:UntagResource",
            ],
            "Resource": [
                f"arn:aws:secretsmanager:{REGION}:{ACCT}:secret:{cfg['secret_prefix']}*"
            ],
        })
        # Deliberately NOT GetSecretValue: the deploy role writes secrets, the
        # ECS task execution role reads them at runtime.

    if cfg["log_groups"]:
        st.append({
            "Sid": "OwnLogGroupsOnly",
            "Effect": "Allow",
            "Action": ["logs:GetLogEvents", "logs:FilterLogEvents", "logs:DescribeLogStreams"],
            "Resource": [f"arn:aws:logs:{REGION}:{ACCT}:log-group:{g}:*" for g in cfg["log_groups"]],
        })
        # Hand-added to all three live roles before this file recorded it
        # (found 2026-10-06 diffing live against generated); without it here,
        # re-applying the generated policy would silently drop it.
        st.append({
            "Sid": "DescribeLogGroupsTakesNoResource",
            "Effect": "Allow",
            "Action": "logs:DescribeLogGroups",
            "Resource": "*",
        })

    if cfg["metrics"]:
        st.append({
            "Sid": "DeployMetricTakesNoResource",
            "Effect": "Allow",
            "Action": "cloudwatch:PutMetricData",
            "Resource": "*",
        })

    if cfg["preflight"]:
        st.append({
            "Sid": "PreflightSelfCheckTakesNoResource",
            "Effect": "Allow",
            "Action": "iam:SimulatePrincipalPolicy",
            "Resource": "*",
        })

    return {"Version": "2012-10-17", "Statement": st}


def trust_for(cfg: dict) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Federated": f"arn:aws:iam::{ACCT}:oidc-provider/token.actions.githubusercontent.com"},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {
                "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
                # ONE repo. This is the whole point.
                "StringLike": {"token.actions.githubusercontent.com:sub": [cfg["sub"]]},
            },
        }],
    }


for app, cfg in APPS.items():
    pol = policy_for(app, cfg)
    json.dump(pol, open(f"/tmp/role-{app}-policy.json", "w"), indent=2)
    json.dump(trust_for(cfg), open(f"/tmp/role-{app}-trust.json", "w"), indent=2)
    n_star = sum(1 for s in pol["Statement"] if s["Resource"] == "*")
    print(f"{app:14} {len(pol['Statement']):2} statements  ({n_star} on * — all documented)  sub={cfg['sub'][:52]}")
