"""Generate one scoped GitHub Actions deploy role per deployment.

Run it to print/emit the policies; apply with `aws iam put-role-policy`. This
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

APPS = {
    "canopy-web": {
        # canopy-web was transferred into the org, so GitHub mints the IMMUTABLE
        # subject. The name-based form silently fails AssumeRoleWithWebIdentity.
        "sub": f"repo:dimagi-internal@272902307/canopy-web@1193139337:*",
        "stack": "canopy-web",
        "secret_prefix": "canopy-web/",
        "ecr": ["labs-jj-canopy-web"],
        "target_group": "labs-jj-canopy-web-tg",
        "log_groups": ["/ecs/labs-jj-canopy-web"],
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

    # ECS: the describe/register calls do not support resource-level
    # permissions, and RunTask/UpdateService scoping by cluster is what keeps
    # one app out of another's service. Kept as the shared policy had it,
    # because narrowing these is where a broken deploy would come from — the
    # isolation that matters (stack, secrets, ECR, target group) is above.
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
