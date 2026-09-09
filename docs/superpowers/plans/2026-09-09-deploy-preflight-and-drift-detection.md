# Deploy Preflight and Drift Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a deploy before it starts when it would change a resource the CI role cannot write, and detect stack drift on a schedule instead of by hand.

**Architecture:** The deploy step splits into *build a change set → inspect it → execute it*. Between inspect and execute, a Python preflight maps each changed resource to the IAM actions CloudFormation will call for that specific change action, asks `iam:SimulatePrincipalPolicy` whether the deploying principal may perform them, and fails with the missing action named. A second, scheduled workflow runs `detect-stack-drift` and fails when a stack has drifted.

**Tech Stack:** Python 3.12, boto3 (already a runtime dependency), pytest, GitHub Actions, AWS CloudFormation + IAM.

**Spec:** `docs/superpowers/specs/2026-09-09-labs-infrastructure-as-code-design.md`

## Global Constraints

- **Nothing in this plan applies changes to AWS.** The preflight and drift jobs are read-only (`describe-change-set`, `simulate-principal-policy`, `detect-stack-drift`). Task 4 changes *how* the existing deploy runs and therefore takes effect on the next deploy — it needs Jonathan's explicit go-ahead before merge, and he wants infra changes applied off-hours.
- **The preflight must not produce false failures.** A normal image-tag-only deploy modifies `TaskDefinition` and `Service` and must pass. This is why actions are mapped per change action (`Add`/`Modify`/`Remove`), not per resource type: the role can `ecs:UpdateService` but not `ecs:CreateService`, and a type-level mapping would fail every deploy.
- **The preflight fails closed on an unknown resource type.** A type with no mapping is an error naming the type, not a silent pass.
- **Simulate against the physical ARN when there is one.** Grants here are resource-scoped: `elasticloadbalancing:ModifyTargetGroup` evaluates `implicitDeny` against `*` and `allowed` against the real target-group ARN. Simulating against `*` alone would report false failures.
- Existing repo conventions: `uv run python -m pytest` for tests, `ruff check . --select F --ignore F403,F405` for lint, four-space indent, type hints on new Python.

## File Structure

| File | Responsibility |
| --- | --- |
| `deploy/aws/preflight.py` (new) | The whole preflight. Pure decision functions at the top (mapping, evaluation, rendering), a thin `main()` at the bottom that does the AWS calls. Split this way so everything worth testing is testable without AWS. |
| `tests/test_deploy_preflight.py` (new) | Unit tests for the pure functions plus `main()` driven by a fake boto3 client. |
| `.github/workflows/deploy-labs.yml` (modify) | The single `aws cloudformation deploy` step becomes create-change-set → preflight → execute-change-set. |
| `.github/workflows/infra-drift.yml` (new) | Scheduled drift detection over the stacks this repo owns. |

`deploy/aws/` already holds `canopy-web.cfn.yaml`, so deploy tooling has a home. The preflight is deliberately one file: it is ~150 lines and splitting it across modules would cost more in navigation than it buys.

---

### Task 1: Map a resource change to the IAM actions CloudFormation will call

**Files:**
- Create: `deploy/aws/preflight.py`
- Test: `tests/test_deploy_preflight.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces:
  - `class UnmappedResourceType(Exception)`
  - `RESOURCE_ACTIONS: dict[str, dict[str, tuple[str, ...]]]` — resource type → change action (`"Add"`/`"Modify"`/`"Remove"`) → IAM actions
  - `def actions_for(resource_type: str, change_action: str, replacement: bool = False) -> tuple[str, ...]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deploy_preflight.py
"""The deploy preflight: can the CI role write what this change set touches?

Written after a deploy failed on 2026-09-09 with a 403 on ModifyTargetGroup,
five minutes into a rollout. The role could write 2 of the 13 resources in the
stack it deploys; the other eleven had simply never changed.
"""
import pytest

from deploy.aws.preflight import (
    RESOURCE_ACTIONS,
    UnmappedResourceType,
    actions_for,
)


class TestActionsFor:
    def test_modifying_a_service_needs_update_not_create(self):
        # THE false-positive that would break every deploy: the CI role can
        # UpdateService but not CreateService, and an image-tag deploy only
        # ever modifies. A type-level mapping would fail every single deploy.
        assert actions_for("AWS::ECS::Service", "Modify") == ("ecs:UpdateService",)
        assert actions_for("AWS::ECS::Service", "Add") == ("ecs:CreateService",)

    def test_a_replacement_needs_create_and_delete_as_well(self):
        # CFN reports Action=Modify with Replacement=True when it will create a
        # new physical resource and delete the old one. Checking only the modify
        # actions would wave that through.
        actions = actions_for("AWS::ECS::Service", "Modify", replacement=True)
        assert set(actions) == {
            "ecs:CreateService",
            "ecs:UpdateService",
            "ecs:DeleteService",
        }

    def test_the_target_group_actions_are_the_ones_that_failed(self):
        actions = actions_for("AWS::ElasticLoadBalancingV2::TargetGroup", "Modify")
        assert "elasticloadbalancing:ModifyTargetGroup" in actions
        assert "elasticloadbalancing:ModifyTargetGroupAttributes" in actions
        # Not resource-scopable in ELB, so it must be granted on "*" — the
        # second grant the 2026-09-09 incident needed.
        assert "elasticloadbalancing:DescribeTargetGroups" in actions

    def test_an_unmapped_type_fails_closed_and_says_what_to_add(self):
        with pytest.raises(UnmappedResourceType) as exc:
            actions_for("AWS::Kinesis::Stream", "Add")
        assert "AWS::Kinesis::Stream" in str(exc.value)
        assert "RESOURCE_ACTIONS" in str(exc.value)

    def test_every_type_in_the_live_stacks_is_mapped(self):
        # The resource types canopy-web and ace-web actually contain. If one of
        # these is missing the preflight would fail closed on a normal deploy.
        for resource_type in [
            "AWS::ECS::TaskDefinition",
            "AWS::ECS::Service",
            "AWS::Logs::LogGroup",
            "AWS::ElasticLoadBalancingV2::TargetGroup",
            "AWS::ElasticLoadBalancingV2::ListenerRule",
            "AWS::ECR::Repository",
            "AWS::S3::Bucket",
            "AWS::S3::BucketPolicy",
            "AWS::SecretsManager::Secret",
            "AWS::IAM::Policy",
        ]:
            assert resource_type in RESOURCE_ACTIONS
            assert actions_for(resource_type, "Modify")

    def test_every_mapping_covers_all_three_change_actions(self):
        for resource_type, by_action in RESOURCE_ACTIONS.items():
            assert set(by_action) == {"Add", "Modify", "Remove"}, resource_type
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_deploy_preflight.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'deploy.aws.preflight'`

- [ ] **Step 3: Write minimal implementation**

Create the package markers first so `deploy.aws.preflight` is importable:

```bash
touch deploy/__init__.py deploy/aws/__init__.py
```

```python
# deploy/aws/preflight.py
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
"""
from __future__ import annotations


class UnmappedResourceType(Exception):
    """A resource type with no entry in RESOURCE_ACTIONS.

    Fails the preflight rather than passing it: an unknown type is exactly the
    case where nobody has thought about whether CI may write it.
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


def actions_for(
    resource_type: str, change_action: str, replacement: bool = False
) -> tuple[str, ...]:
    """The IAM actions CloudFormation needs for one resource change.

    `replacement` is CloudFormation's own word: it reports Action="Modify" with
    Replacement=True when it will create a new physical resource and delete the
    old one. Checking only the modify actions would wave that through, so a
    replacement is treated as all three.
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
        merged: tuple[str, ...] = ()
        for actions in by_action.values():
            merged += actions
        return tuple(dict.fromkeys(merged))

    return by_action.get(change_action, ())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_deploy_preflight.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add deploy/__init__.py deploy/aws/__init__.py deploy/aws/preflight.py tests/test_deploy_preflight.py
git commit -m "deploy: map a CloudFormation resource change to the IAM actions it needs"
```

---

### Task 2: Turn simulate results into a readable verdict

**Files:**
- Modify: `deploy/aws/preflight.py`
- Test: `tests/test_deploy_preflight.py`

**Interfaces:**
- Consumes: `actions_for`, `UnmappedResourceType` from Task 1
- Produces:
  - `@dataclass(frozen=True) class ResourceChange` with fields `logical_id: str`, `resource_type: str`, `change_action: str`, `physical_id: str | None`, `replacement: bool`
  - `@dataclass(frozen=True) class Finding` with fields `logical_id: str`, `resource_type: str`, `action: str`
  - `def evaluate(change: ResourceChange, decisions: dict[str, str]) -> list[Finding]`
  - `def render(findings: list[Finding]) -> str`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_deploy_preflight.py
from deploy.aws.preflight import Finding, ResourceChange, evaluate, render


def _change(**kw) -> ResourceChange:
    base = dict(
        logical_id="TargetGroup",
        resource_type="AWS::ElasticLoadBalancingV2::TargetGroup",
        change_action="Modify",
        physical_id="arn:aws:elasticloadbalancing:us-east-1:1:targetgroup/tg/abc",
        replacement=False,
    )
    base.update(kw)
    return ResourceChange(**base)


class TestEvaluate:
    def test_all_allowed_is_no_findings(self):
        decisions = {
            "elasticloadbalancing:ModifyTargetGroup": "allowed",
            "elasticloadbalancing:ModifyTargetGroupAttributes": "allowed",
            "elasticloadbalancing:DescribeTargetGroups": "allowed",
        }
        assert evaluate(_change(), decisions) == []

    def test_a_denied_action_becomes_a_finding(self):
        decisions = {
            "elasticloadbalancing:ModifyTargetGroup": "implicitDeny",
            "elasticloadbalancing:ModifyTargetGroupAttributes": "allowed",
            "elasticloadbalancing:DescribeTargetGroups": "allowed",
        }
        findings = evaluate(_change(), decisions)
        assert findings == [
            Finding(
                logical_id="TargetGroup",
                resource_type="AWS::ElasticLoadBalancingV2::TargetGroup",
                action="elasticloadbalancing:ModifyTargetGroup",
            )
        ]

    def test_explicit_deny_counts_too(self):
        decisions = {a: "explicitDeny" for a in (
            "elasticloadbalancing:ModifyTargetGroup",
            "elasticloadbalancing:ModifyTargetGroupAttributes",
            "elasticloadbalancing:DescribeTargetGroups",
        )}
        assert len(evaluate(_change(), decisions)) == 3

    def test_an_action_missing_from_the_results_is_a_finding_not_a_pass(self):
        # Fail closed: a simulate response that omits an action must never read
        # as permission granted.
        assert evaluate(_change(), {}) != []

    def test_an_image_tag_deploy_passes(self):
        # The steady state this must not break.
        td = _change(
            logical_id="TaskDefinition",
            resource_type="AWS::ECS::TaskDefinition",
            physical_id=None,
        )
        svc = _change(
            logical_id="Service",
            resource_type="AWS::ECS::Service",
            physical_id="arn:aws:ecs:us-east-1:1:service/c/s",
        )
        assert evaluate(td, {"ecs:RegisterTaskDefinition": "allowed"}) == []
        assert evaluate(svc, {"ecs:UpdateService": "allowed"}) == []


class TestRender:
    def test_it_names_the_resource_and_the_action(self):
        out = render([
            Finding("TargetGroup", "AWS::ElasticLoadBalancingV2::TargetGroup",
                    "elasticloadbalancing:ModifyTargetGroup")
        ])
        assert "TargetGroup" in out
        assert "elasticloadbalancing:ModifyTargetGroup" in out

    def test_it_says_what_to_do_about_it(self):
        out = render([
            Finding("TargetGroup", "AWS::ElasticLoadBalancingV2::TargetGroup",
                    "elasticloadbalancing:ModifyTargetGroup")
        ])
        # A message nobody can act on is the failure mode this replaces.
        assert "admin" in out.lower()

    def test_no_findings_renders_empty(self):
        assert render([]) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_deploy_preflight.py -q`
Expected: FAIL — `ImportError: cannot import name 'ResourceChange'`

- [ ] **Step 3: Write minimal implementation**

Add to `deploy/aws/preflight.py` (imports go at the top of the file):

```python
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
        "Nothing has been applied. Either grant the action to the deploy role,",
        "or apply this change from the bootstrap stack with admin credentials.",
        "See docs/superpowers/specs/2026-09-09-labs-infrastructure-as-code-design.md",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_deploy_preflight.py -q`
Expected: PASS (14 tests)

- [ ] **Step 5: Commit**

```bash
git add deploy/aws/preflight.py tests/test_deploy_preflight.py
git commit -m "deploy: turn a permissions verdict into a message someone can act on"
```

---

### Task 3: Read the change set, ask IAM, exit non-zero on findings

**Files:**
- Modify: `deploy/aws/preflight.py`
- Test: `tests/test_deploy_preflight.py`

**Interfaces:**
- Consumes: `ResourceChange`, `evaluate`, `render`, `UnmappedResourceType` from Tasks 1–2
- Produces:
  - `def changes_from_change_set(described: dict) -> list[ResourceChange]`
  - `def check(cfn, iam, stack_name: str, change_set: str, principal_arn: str) -> list[Finding]`
  - `def main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_deploy_preflight.py
from deploy.aws.preflight import changes_from_change_set, check, main

DESCRIBED = {
    "Changes": [
        {"Type": "Resource", "ResourceChange": {
            "Action": "Modify", "LogicalResourceId": "TaskDefinition",
            "ResourceType": "AWS::ECS::TaskDefinition", "Replacement": "True"}},
        {"Type": "Resource", "ResourceChange": {
            "Action": "Modify", "LogicalResourceId": "Service",
            "PhysicalResourceId": "arn:aws:ecs:us-east-1:1:service/c/s",
            "ResourceType": "AWS::ECS::Service", "Replacement": "False"}},
    ]
}


class TestChangesFromChangeSet:
    def test_it_reads_action_type_and_physical_id(self):
        changes = changes_from_change_set(DESCRIBED)
        assert [c.logical_id for c in changes] == ["TaskDefinition", "Service"]
        assert changes[0].physical_id is None
        assert changes[1].physical_id == "arn:aws:ecs:us-east-1:1:service/c/s"

    def test_replacement_is_parsed_from_cfns_string(self):
        # CloudFormation returns the string "True"/"False"/"Conditional".
        changes = changes_from_change_set(DESCRIBED)
        assert changes[0].replacement is True
        assert changes[1].replacement is False

    def test_conditional_replacement_is_treated_as_replacement(self):
        described = {"Changes": [{"Type": "Resource", "ResourceChange": {
            "Action": "Modify", "LogicalResourceId": "S", "ResourceType": "AWS::ECS::Service",
            "Replacement": "Conditional"}}]}
        assert changes_from_change_set(described)[0].replacement is True

    def test_non_resource_entries_are_ignored(self):
        described = {"Changes": [{"Type": "Something", "OtherChange": {}}]}
        assert changes_from_change_set(described) == []


class FakeCfn:
    def __init__(self, described): self._described = described
    def describe_change_set(self, **kw): return self._described


class FakeIam:
    """Returns `allowed` for every action except those in `deny`."""
    def __init__(self, deny=()): self.deny = set(deny); self.calls = []
    def simulate_principal_policy(self, **kw):
        self.calls.append(kw)
        return {"EvaluationResults": [
            {"EvalActionName": a,
             "EvalDecision": "implicitDeny" if a in self.deny else "allowed"}
            for a in kw["ActionNames"]
        ]}


class TestCheck:
    def test_clean_change_set_returns_no_findings(self):
        assert check(FakeCfn(DESCRIBED), FakeIam(), "s", "cs", "arn:role") == []

    def test_a_denied_action_is_reported(self):
        iam = FakeIam(deny={"ecs:UpdateService"})
        findings = check(FakeCfn(DESCRIBED), iam, "s", "cs", "arn:role")
        assert [f.action for f in findings] == ["ecs:UpdateService"]

    def test_it_simulates_against_the_physical_arn_when_there_is_one(self):
        # Grants here are resource-scoped: ModifyTargetGroup is implicitDeny on
        # "*" and allowed on the real ARN. Simulating against "*" would report a
        # failure that is not real.
        iam = FakeIam()
        check(FakeCfn(DESCRIBED), iam, "s", "cs", "arn:role")
        by_action = {c["ActionNames"][0]: c for c in iam.calls}
        assert by_action["ecs:UpdateService"]["ResourceArns"] == [
            "arn:aws:ecs:us-east-1:1:service/c/s"
        ]
        assert "ResourceArns" not in by_action["ecs:RegisterTaskDefinition"]

    def test_a_non_arn_physical_id_is_not_passed_as_a_resource(self):
        # Some physical ids are names, not ARNs (a log group, for one).
        described = {"Changes": [{"Type": "Resource", "ResourceChange": {
            "Action": "Modify", "LogicalResourceId": "LogGroup",
            "PhysicalResourceId": "/ecs/canopy-web",
            "ResourceType": "AWS::Logs::LogGroup", "Replacement": "False"}}]}
        iam = FakeIam()
        check(FakeCfn(described), iam, "s", "cs", "arn:role")
        assert all("ResourceArns" not in c for c in iam.calls)


class TestMain:
    def test_exit_zero_when_clean(self, monkeypatch, capsys):
        monkeypatch.setattr("deploy.aws.preflight._clients",
                            lambda: (FakeCfn(DESCRIBED), FakeIam()))
        assert main(["--stack", "s", "--change-set", "cs", "--principal", "arn:role"]) == 0

    def test_exit_one_and_print_when_denied(self, monkeypatch, capsys):
        monkeypatch.setattr("deploy.aws.preflight._clients",
                            lambda: (FakeCfn(DESCRIBED), FakeIam(deny={"ecs:UpdateService"})))
        rc = main(["--stack", "s", "--change-set", "cs", "--principal", "arn:role"])
        assert rc == 1
        assert "ecs:UpdateService" in capsys.readouterr().out

    def test_an_unmapped_type_exits_non_zero(self, monkeypatch, capsys):
        described = {"Changes": [{"Type": "Resource", "ResourceChange": {
            "Action": "Add", "LogicalResourceId": "K",
            "ResourceType": "AWS::Kinesis::Stream", "Replacement": "False"}}]}
        monkeypatch.setattr("deploy.aws.preflight._clients",
                            lambda: (FakeCfn(described), FakeIam()))
        rc = main(["--stack", "s", "--change-set", "cs", "--principal", "arn:role"])
        assert rc == 1
        assert "AWS::Kinesis::Stream" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_deploy_preflight.py -q`
Expected: FAIL — `ImportError: cannot import name 'changes_from_change_set'`

- [ ] **Step 3: Write minimal implementation**

Add to `deploy/aws/preflight.py`:

```python
import argparse
import sys


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


def check(cfn, iam, stack_name: str, change_set: str, principal_arn: str) -> list[Finding]:
    """Findings for every resource this change set would touch."""
    described = cfn.describe_change_set(StackName=stack_name, ChangeSetName=change_set)
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

    cfn, iam = _clients()
    try:
        findings = check(cfn, iam, args.stack, args.change_set, args.principal)
    except UnmappedResourceType as exc:
        print(str(exc))
        return 1

    if findings:
        print(render(findings))
        return 1

    print("Preflight OK — the deploying role can write every resource in this change set.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_deploy_preflight.py -q`
Expected: PASS (25 tests)

- [ ] **Step 5: Check lint and the whole suite still pass**

Run: `uv run ruff check . --select F --ignore F403,F405 && uv run python -m pytest -q`
Expected: `All checks passed!` and no new failures

- [ ] **Step 6: Commit**

```bash
git add deploy/aws/preflight.py tests/test_deploy_preflight.py
git commit -m "deploy: ask IAM whether this change set is writable, before executing it"
```

---

### Task 4: Split the deploy into create → preflight → execute

**Files:**
- Modify: `.github/workflows/deploy-labs.yml` (the `Deploy (CloudFormation owns the task definition + service)` step)

**Interfaces:**
- Consumes: `deploy/aws/preflight.py` `main()` via `python -m deploy.aws.preflight`
- Produces: nothing later tasks depend on

**Note:** this changes how production deploys run and takes effect on the next deploy. Do not merge without Jonathan's explicit go-ahead; he wants infra changes applied off-hours.

- [ ] **Step 1: Replace the deploy step**

Replace the whole `aws cloudformation deploy` invocation (added in #723, currently wrapped in `if ! aws cloudformation deploy ... fi`) with:

```yaml
      - name: Deploy (CloudFormation owns the task definition + service)
        run: |
          # THREE steps, not one, so the permissions question is asked BEFORE
          # anything is applied.
          #
          # `aws cloudformation deploy` builds a change set and executes it in
          # one go, which is why the 2026-09-09 failure surfaced as a 403 five
          # minutes into a rollout. --no-execute-changeset stops after building
          # it, leaving something we can inspect.
          set -o pipefail
          CS_NAME="preflight-${{ github.sha }}-${{ github.run_attempt }}"

          # --no-fail-on-empty-changeset makes this exit 0 and create nothing
          # when there is no diff, so the empty case is detected by asking for
          # the change set rather than by parsing output.
          aws cloudformation deploy \
            --stack-name "${{ env.CFN_STACK }}" \
            --template-file deploy/aws/canopy-web.cfn.yaml \
            --capabilities CAPABILITY_IAM \
            --no-fail-on-empty-changeset \
            --no-execute-changeset \
            --change-set-name "$CS_NAME" \
            --parameter-overrides ImageTag="${{ github.sha }}"

          if ! aws cloudformation describe-change-set \
                 --stack-name "${{ env.CFN_STACK }}" \
                 --change-set-name "$CS_NAME" >/dev/null 2>&1; then
            echo "No change set — nothing to deploy."
            exit 0
          fi

          # The role this job is running as; the same principal that will
          # execute the change set.
          PRINCIPAL=$(aws sts get-caller-identity --query Arn --output text \
            | sed 's|:sts:|:iam:|; s|assumed-role/\([^/]*\)/.*|role/\1|')
          echo "Preflight against: $PRINCIPAL"

          if ! python -m deploy.aws.preflight \
                 --stack "${{ env.CFN_STACK }}" \
                 --change-set "$CS_NAME" \
                 --principal "$PRINCIPAL"; then
            echo "::error::Deploy preflight failed — nothing was applied. See above."
            aws cloudformation delete-change-set \
              --stack-name "${{ env.CFN_STACK }}" --change-set-name "$CS_NAME" || true
            exit 1
          fi

          aws cloudformation execute-change-set \
            --stack-name "${{ env.CFN_STACK }}" --change-set-name "$CS_NAME"

          if ! aws cloudformation wait stack-update-complete \
                 --stack-name "${{ env.CFN_STACK }}"; then
              # Kept verbatim from #723: aws prints only "run describe-stack-events"
              # and exits, so the reason lives behind an API call nobody reading
              # the log can make.
              echo "::group::CloudFormation stack events (most recent first)"
              aws cloudformation describe-stack-events \
                --stack-name "${{ env.CFN_STACK }}" \
                --max-items 40 \
                --query 'StackEvents[?ResourceStatusReason!=`null`].{Time:Timestamp,Resource:LogicalResourceId,Status:ResourceStatus,Reason:ResourceStatusReason}' \
                --output table || echo "(could not read stack events)"
              echo "::endgroup::"
              FAILED=$(aws cloudformation describe-stack-events \
                --stack-name "${{ env.CFN_STACK }}" \
                --max-items 40 \
                --query 'StackEvents[?ResourceStatus==`UPDATE_FAILED`||ResourceStatus==`CREATE_FAILED`]|[0].ResourceStatusReason' \
                --output text 2>/dev/null || true)
              echo "::error::CloudFormation deploy failed. First failing resource said: ${FAILED:-see the stack events above}"
              exit 1
          fi
          echo "Rollout complete — new tasks running and healthy."
```

- [ ] **Step 2: Add the Python setup this step now needs**

The deploy job runs `python -m deploy.aws.preflight`, which needs boto3. Insert immediately before the deploy step:

```yaml
      - name: Install preflight dependencies
        run: pip install --quiet 'boto3>=1.34,<2.0'
```

- [ ] **Step 3: Verify the workflow still parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy-labs.yml')); print('YAML valid')"`
Expected: `YAML valid`

- [ ] **Step 4: Verify the preflight runs against the REAL stack, read-only**

This is the step that proves the plan rather than assuming it. With `AWS_PROFILE=labs`, build a change set by hand and run the preflight against it — no execute:

```bash
export AWS_PROFILE=labs AWS_DEFAULT_REGION=us-east-1
aws cloudformation deploy \
  --stack-name canopy-web \
  --template-file deploy/aws/canopy-web.cfn.yaml \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --no-execute-changeset --change-set-name preflight-manual-test \
  --parameter-overrides ImageTag="$(git rev-parse HEAD)"

uv run python -m deploy.aws.preflight \
  --stack canopy-web --change-set preflight-manual-test \
  --principal arn:aws:iam::858923557655:role/github-actions-labs-deploy

aws cloudformation delete-change-set \
  --stack-name canopy-web --change-set-name preflight-manual-test
```

Expected: exit 0 with "Preflight OK" — an image-tag-only change set touches `TaskDefinition` and `Service`, both writable. **If it reports findings, stop and read them before changing the code**: a false positive here is the one failure mode that would block every deploy.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/deploy-labs.yml
git commit -m "deploy: ask whether a change set is applicable before applying it"
```

---

### Task 5: Detect drift on a schedule

**Files:**
- Create: `.github/workflows/infra-drift.yml`

**Interfaces:**
- Consumes: nothing
- Produces: nothing

- [ ] **Step 1: Write the workflow**

```yaml
# .github/workflows/infra-drift.yml
name: Infra drift

# Nothing detected drift until someone ran detect-stack-drift by hand on
# 2026-09-09 and found labs-jj-audit-analytics had a task-definition environment
# variable changed out-of-band — a change the next `cloudformation deploy` would
# have silently reverted. A stack that has drifted is a stack whose template no
# longer describes what is running, which makes every review of that template a
# review of the wrong thing.
#
# Read-only: detect-stack-drift inspects, it does not reconcile.

on:
  schedule:
    - cron: "0 8 * * *"   # 08:00 UTC daily, after the nightly contract run
  workflow_dispatch:

permissions:
  id-token: write
  contents: read

jobs:
  drift:
    name: Detect CloudFormation drift
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: aws-actions/configure-aws-credentials@v6
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: us-east-1

      - name: Detect drift
        run: |
          set -o pipefail
          # Only the stacks this repo owns. ace-web and the connect-labs
          # platform stacks are detected from their own repos — each repo is
          # self-contained, which is the point.
          STACKS="canopy-web canopy-cloud-runner"
          FAILED=0

          for STACK in $STACKS; do
            echo "::group::$STACK"
            ID=$(aws cloudformation detect-stack-drift --stack-name "$STACK" \
                   --query StackDriftDetectionId --output text)

            # Detection is asynchronous; poll until it stops being IN_PROGRESS.
            for _ in $(seq 1 60); do
              STATUS=$(aws cloudformation describe-stack-drift-detection-status \
                         --stack-drift-detection-id "$ID" \
                         --query DetectionStatus --output text)
              [ "$STATUS" = "DETECTION_IN_PROGRESS" ] || break
              sleep 5
            done

            DRIFT=$(aws cloudformation describe-stack-drift-detection-status \
                      --stack-drift-detection-id "$ID" \
                      --query StackDriftStatus --output text)
            echo "$STACK: detection=$STATUS drift=$DRIFT"

            if [ "$DRIFT" = "DRIFTED" ]; then
              aws cloudformation describe-stack-resource-drifts \
                --stack-name "$STACK" \
                --stack-resource-drift-status-filters MODIFIED DELETED \
                --query 'StackResourceDrifts[].{Resource:LogicalResourceId,Type:ResourceType,Status:StackResourceDriftStatus,Properties:PropertyDifferences[].PropertyPath}' \
                --output json
              FAILED=1
            fi
            echo "::endgroup::"

            if [ "$DRIFT" = "DRIFTED" ]; then
              echo "::error::$STACK has drifted — the template no longer describes what is running. The next deploy will silently revert it."
            fi
          done

          exit $FAILED
```

- [ ] **Step 2: Verify the workflow parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/infra-drift.yml')); print('YAML valid')"`
Expected: `YAML valid`

- [ ] **Step 3: Verify the detection logic against the real account, read-only**

```bash
export AWS_PROFILE=labs AWS_DEFAULT_REGION=us-east-1
for STACK in canopy-web canopy-cloud-runner; do
  ID=$(aws cloudformation detect-stack-drift --stack-name "$STACK" --query StackDriftDetectionId --output text)
  sleep 20
  aws cloudformation describe-stack-drift-detection-status \
    --stack-drift-detection-id "$ID" \
    --query '{Stack:StackId,Detection:DetectionStatus,Drift:StackDriftStatus,Count:DriftedStackResourceCount}' --output json
done
```

Expected: both `IN_SYNC`. `canopy-web` was `IN_SYNC` when measured on 2026-09-09; if it now reports `DRIFTED`, that is a real finding and should be read before the workflow is merged.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/infra-drift.yml
git commit -m "infra: detect stack drift daily, instead of when someone thinks to look"
```

---

## Self-Review

**Spec coverage.** The spec's *Verification* section names three things: the permissions preflight (Tasks 1–4), drift detection (Task 5), and deploy failure diagnostics — the third already shipped in #723 and is preserved verbatim in Task 4 Step 1 rather than reimplemented. The spec's other sections (bootstrap/app split, `create-stack-refactor`, the emulator conversion) are the later plans named below, deliberately not in this one.

**Placeholders.** None: every step carries the code or command it needs.

**Type consistency.** `ResourceChange` and `Finding` field names are used identically in Tasks 2 and 3; `actions_for(resource_type, change_action, replacement)` keeps one signature throughout; `_clients()` is the single patch point the `TestMain` tests monkeypatch.

**Known gap, deliberate.** `RESOURCE_ACTIONS` is a hand-written mapping and CloudFormation publishes no machine-readable equivalent. It is wrong-by-omission rather than wrong-by-commission: an unmapped type fails closed with a message naming the type, so the mapping grows when it is found lacking instead of silently passing.

## Plans that follow this one

Written separately, in the spec's order, each shipping on its own:

1. **This plan** — verification, canopy-web.
2. **Mobile emulator** — Terraform to CloudFormation, import the orphaned resources, boot it, run a Maestro flow.
3. **ace-web** — bootstrap/app split via `create-stack-refactor`; first real use of the mechanism.
4. **canopy-web** — bootstrap/app split, including the 7 secrets.
5. **Core platform** — phase 2, its own spec first.
