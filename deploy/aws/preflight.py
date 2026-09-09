"""Refuse a deploy that would change a resource the deploying role cannot write.

On 2026-09-09 a deploy failed five minutes into a rollout with:

    github-actions-labs-deploy is not authorized to perform:
    elasticloadbalancing:ModifyTargetGroup on labs-jj-canopy-web-tg

The change was correct. The pipeline had only ever CREATED that target group, so
this was the first modify in the stack's life and the grant had never existed.
Measured afterwards: the CI role can write 2 of the 13 resources in the stack it
deploys. The other eleven had simply never changed, so nobody found out.

This asks the question up front, against the change set CloudFormation is about
to execute, and names the missing action instead of leaving a 403 in a rollout
log. See docs/superpowers/specs/2026-09-09-labs-infrastructure-as-code-design.md

The preflight itself calls two actions the CI role does not (yet) have —
iam:SimulatePrincipalPolicy and cloudformation:ListChangeSets both come back
implicitDeny, measured against the live role on 2026-09-09. A preflight that
cannot run must say so loudly rather than let a bare exception (or worse, a
silently swallowed one) read as "no findings, safe to proceed" — that would be
strictly worse than not having a preflight at all. See main()'s exit code 2.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

ALLOWED = "allowed"


@dataclass(frozen=True)
class ResourceChange:
    """One entry from `describe-change-set`, reduced to what we need."""

    logical_id: str
    resource_type: str
    change_action: str  # "Add" | "Modify" | "Remove"
    physical_id: str | None  # absent on an Add
    replacement: bool


@dataclass(frozen=True)
class Finding:
    logical_id: str
    resource_type: str
    action: str


class UnmappedResourceType(Exception):
    """A resource type with no entry in RESOURCE_ACTIONS.

    Fails the preflight rather than passing it: an unknown type is exactly the
    case where nobody has thought about whether CI may write it.
    """


class UnmappedChangeAction(Exception):
    """A change action CloudFormation reported that isn't Add/Modify/Remove.

    Fails closed for the same reason as UnmappedResourceType: `actions_for`'s
    fallthrough is meant ONLY for the deliberate, evidence-backed Remove case
    (see BLOCKING_CHANGE_ACTIONS below). Any other action — Dynamic, Import,
    or a future CloudFormation addition — must not fall through that same
    branch and silently read as "nothing to check".
    """


# Resource type -> change action -> the IAM actions CloudFormation calls.
#
# Keyed by CHANGE ACTION, not just by type, and that is load-bearing. The CI
# role may ecs:UpdateService but not ecs:CreateService; an image-tag deploy only
# ever modifies. A type-level mapping would demand CreateService on every deploy
# and fail all of them.
RESOURCE_ACTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "AWS::ECS::TaskDefinition": {
        # A task definition is immutable: every change registers a new revision.
        "Add": ("ecs:RegisterTaskDefinition",),
        "Modify": ("ecs:RegisterTaskDefinition",),
        "Remove": ("ecs:DeregisterTaskDefinition",),
    },
    "AWS::ECS::Service": {
        "Add": ("ecs:CreateService",),
        "Modify": ("ecs:UpdateService",),
        "Remove": ("ecs:DeleteService",),
    },
    "AWS::Logs::LogGroup": {
        "Add": ("logs:CreateLogGroup", "logs:PutRetentionPolicy"),
        "Modify": ("logs:PutRetentionPolicy",),
        "Remove": ("logs:DeleteLogGroup",),
    },
    "AWS::ElasticLoadBalancingV2::TargetGroup": {
        "Add": (
            "elasticloadbalancing:CreateTargetGroup",
            "elasticloadbalancing:ModifyTargetGroupAttributes",
            "elasticloadbalancing:DescribeTargetGroups",
        ),
        "Modify": (
            "elasticloadbalancing:ModifyTargetGroup",
            "elasticloadbalancing:ModifyTargetGroupAttributes",
            # ELB does not support resource-level permissions for this one, so
            # it has to be granted on "*". Leaving it out cost a second failed
            # deploy on 2026-09-09.
            "elasticloadbalancing:DescribeTargetGroups",
        ),
        "Remove": ("elasticloadbalancing:DeleteTargetGroup",),
    },
    "AWS::ElasticLoadBalancingV2::ListenerRule": {
        "Add": ("elasticloadbalancing:CreateRule",),
        "Modify": ("elasticloadbalancing:ModifyRule",),
        "Remove": ("elasticloadbalancing:DeleteRule",),
    },
    "AWS::ECR::Repository": {
        "Add": ("ecr:CreateRepository", "ecr:TagResource"),
        "Modify": ("ecr:PutLifecyclePolicy", "ecr:SetRepositoryPolicy", "ecr:TagResource"),
        "Remove": ("ecr:DeleteRepository",),
    },
    "AWS::S3::Bucket": {
        "Add": ("s3:CreateBucket", "s3:PutBucketTagging"),
        "Modify": ("s3:PutBucketTagging", "s3:PutBucketPublicAccessBlock"),
        "Remove": ("s3:DeleteBucket",),
    },
    "AWS::S3::BucketPolicy": {
        "Add": ("s3:PutBucketPolicy",),
        "Modify": ("s3:PutBucketPolicy",),
        "Remove": ("s3:DeleteBucketPolicy",),
    },
    "AWS::SecretsManager::Secret": {
        "Add": ("secretsmanager:CreateSecret", "secretsmanager:TagResource"),
        "Modify": ("secretsmanager:UpdateSecret", "secretsmanager:TagResource"),
        "Remove": ("secretsmanager:DeleteSecret",),
    },
    "AWS::IAM::Policy": {
        # Never grantable to CI: iam:PutRolePolicy on its own role is a full
        # privilege escalation reachable by anyone who can merge. Mapped so the
        # preflight REFUSES such a change loudly rather than not knowing.
        "Add": ("iam:PutRolePolicy",),
        "Modify": ("iam:PutRolePolicy",),
        "Remove": ("iam:DeleteRolePolicy",),
    },
}


# CloudFormation TOLERATES a failed cleanup delete, so a delete permission is
# never what fails a deploy. Measured on this stack 2026-09-09: TaskDefinition
# reported DELETE_FAILED on ecs:DeregisterTaskDefinition while the stack
# reported UPDATE_COMPLETE with "Update successful. One or more resources could
# not be deleted." Checking delete actions would block every deploy here, since
# the role does not have DeregisterTaskDefinition. The Remove entries above stay
# because they document what CloudFormation calls; they are simply not blocking.
BLOCKING_CHANGE_ACTIONS = ("Add", "Modify")


def actions_for(
    resource_type: str, change_action: str, replacement: bool = False
) -> tuple[str, ...]:
    """The IAM actions whose absence would FAIL this resource change.

    `replacement` is CloudFormation's own word: it reports Action="Modify" with
    Replacement=True when it will create a new physical resource and delete the
    old one. The create is checked, the delete is not.
    """
    try:
        by_action = RESOURCE_ACTIONS[resource_type]
    except KeyError:
        raise UnmappedResourceType(
            f"No IAM action mapping for {resource_type}. Add it to "
            f"RESOURCE_ACTIONS in deploy/aws/preflight.py — an unmapped type "
            f"fails closed, because it is exactly the case nobody has thought "
            f"about."
        ) from None

    if replacement:
        wanted: tuple[str, ...] = BLOCKING_CHANGE_ACTIONS
    elif change_action in BLOCKING_CHANGE_ACTIONS:
        wanted = (change_action,)
    elif change_action == "Remove":
        return ()
    else:
        raise UnmappedChangeAction(
            f"Unrecognized change action {change_action!r} on {resource_type}. "
            f"Add/Modify/Remove are handled explicitly; anything else "
            f"(Dynamic, Import, a future CFN addition, …) fails closed rather "
            f"than falling through the Remove branch and going unchecked."
        )

    merged: tuple[str, ...] = ()
    for action in wanted:
        merged += by_action.get(action, ())
    return tuple(dict.fromkeys(merged))


def evaluate(change: ResourceChange, decisions: dict[str, str]) -> list[Finding]:
    """Findings for one changed resource, given simulate's verdict per action.

    An action missing from `decisions` is a finding, not a pass — a truncated or
    surprising simulate response must never read as permission granted.
    """
    needed = actions_for(change.resource_type, change.change_action, change.replacement)
    return [
        Finding(change.logical_id, change.resource_type, action)
        for action in needed
        if decisions.get(action) != ALLOWED
    ]


def render(findings: list[Finding]) -> str:
    """The message that replaces a 403 five minutes into a rollout."""
    if not findings:
        return ""
    lines = [
        "This deploy would change resources the deploying role cannot write:",
        "",
    ]
    for f in findings:
        lines.append(f"  {f.logical_id} ({f.resource_type})")
        lines.append(f"      needs {f.action}")
    lines += [
        "",
        "No infrastructure change was applied. Note: migrations for this deploy",
        "have already run (they run before this step, against the new code) —",
        "only the CloudFormation change set was refused. Either grant the action",
        "to the deploy role, or apply this change from the bootstrap stack with",
        "admin credentials.",
        "See docs/superpowers/specs/2026-09-09-labs-infrastructure-as-code-design.md",
    ]
    return "\n".join(lines)


def changes_from_change_set(described: dict) -> list[ResourceChange]:
    """Reduce `describe-change-set` output to the resource changes."""
    changes: list[ResourceChange] = []
    for entry in described.get("Changes", []):
        if entry.get("Type") != "Resource":
            continue
        rc = entry["ResourceChange"]
        # CloudFormation returns the STRING "True"/"False"/"Conditional".
        # Conditional counts as a replacement: it means CFN may create and
        # delete, and we would rather demand a grant we do not use than skip
        # one we do.
        replacement = rc.get("Replacement") in ("True", "Conditional")
        changes.append(
            ResourceChange(
                logical_id=rc["LogicalResourceId"],
                resource_type=rc["ResourceType"],
                change_action=rc["Action"],
                physical_id=rc.get("PhysicalResourceId"),
                replacement=replacement,
            )
        )
    return changes


def _describe_change_set_all_pages(cfn, stack_name: str, change_set: str) -> dict:
    """`describe-change-set`, following NextToken to the end.

    A single call only returns the first page; a change set with more
    resources than fit on one page would otherwise leave everything past it
    unchecked, with no signal that anything was skipped.
    """
    changes: list = []
    next_token: str | None = None
    while True:
        kwargs: dict = {"StackName": stack_name, "ChangeSetName": change_set}
        if next_token:
            kwargs["NextToken"] = next_token
        page = cfn.describe_change_set(**kwargs)
        changes.extend(page.get("Changes", []))
        next_token = page.get("NextToken")
        if not next_token:
            break
    return {"Changes": changes}


def check(cfn, iam, stack_name: str, change_set: str, principal_arn: str) -> list[Finding]:
    """Findings for every resource this change set would touch."""
    described = _describe_change_set_all_pages(cfn, stack_name, change_set)
    findings: list[Finding] = []

    for change in changes_from_change_set(described):
        actions = actions_for(change.resource_type, change.change_action, change.replacement)
        if not actions:
            continue

        kwargs: dict = {
            "PolicySourceArn": principal_arn,
            "ActionNames": list(actions),
            # Region-conditioned grants (DescribeTargetGroups) evaluate as denied
            # without this, because the condition key would be absent.
            "ContextEntries": [{
                "ContextKeyName": "aws:RequestedRegion",
                "ContextKeyValues": ["us-east-1"],
                "ContextKeyType": "string",
            }],
        }
        # Only pass a resource when it is genuinely an ARN. Some physical ids are
        # names (a log group is "/ecs/canopy-web"), and simulate rejects those.
        if change.physical_id and change.physical_id.startswith("arn:"):
            kwargs["ResourceArns"] = [change.physical_id]

        results = iam.simulate_principal_policy(**kwargs)
        decisions = {
            r["EvalActionName"]: r["EvalDecision"]
            for r in results.get("EvaluationResults", [])
        }
        findings.extend(evaluate(change, decisions))

    return findings


def _clients():
    """Real boto3 clients. Patched in tests; the only AWS coupling in the file."""
    import boto3

    return boto3.client("cloudformation"), boto3.client("iam")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", required=True)
    parser.add_argument("--change-set", required=True)
    parser.add_argument("--principal", required=True,
                        help="ARN of the role that will execute the change set")
    args = parser.parse_args(argv)

    # Imported here, not at module level, so importing this module (e.g. under
    # test, or by anything that just wants the pure functions above) never
    # needs boto3/botocore to be importable — the only place this module
    # touches AWS is behind _clients() and this call.
    from botocore.exceptions import ClientError

    cfn, iam = _clients()
    try:
        findings = check(cfn, iam, args.stack, args.change_set, args.principal)
    except (UnmappedResourceType, UnmappedChangeAction) as exc:
        print(str(exc))
        return 1
    except ClientError as exc:
        # The preflight's OWN permissions are missing (measured 2026-09-09:
        # iam:SimulatePrincipalPolicy and cloudformation:ListChangeSets both
        # implicitDeny for the CI role). This is a THIRD outcome, distinct
        # from "ran and found problems" — silently treating it as either
        # "clean" (exit 0) or "blocked" (exit 1) would be worse than no
        # preflight at all: the first pretends a gate exists when it doesn't,
        # the second wedges every deploy on a permission this file cannot
        # grant itself. Exit 2 says plainly: this could not be evaluated.
        action = exc.operation_name
        print(f"Deploy preflight could not run: {action} failed.")
        print(str(exc))
        return 2

    if findings:
        print(render(findings))
        return 1

    print("Preflight OK — the deploying role can write every resource in this change set.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
