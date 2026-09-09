"""The deploy preflight: can the CI role write what this change set touches?

Written after a deploy failed on 2026-09-09 with a 403 on ModifyTargetGroup,
five minutes into a rollout. The role could write 2 of the 13 resources in the
stack it deploys; the other eleven had simply never changed.
"""
import pytest

from deploy.aws.preflight import (
    Finding,
    ResourceChange,
    RESOURCE_ACTIONS,
    UnmappedResourceType,
    actions_for,
    changes_from_change_set,
    check,
    evaluate,
    main,
    render,
)


class TestActionsFor:
    def test_modifying_a_service_needs_update_not_create(self):
        # THE false-positive that would break every deploy: the CI role can
        # UpdateService but not CreateService, and an image-tag deploy only
        # ever modifies. A type-level mapping would fail every single deploy.
        assert actions_for("AWS::ECS::Service", "Modify") == ("ecs:UpdateService",)
        assert actions_for("AWS::ECS::Service", "Add") == ("ecs:CreateService",)

    def test_a_replacement_needs_the_create_as_well_but_not_the_delete(self):
        # CFN reports Action=Modify with Replacement=True when it will create a
        # new physical resource and delete the old one. The CREATE must be
        # checked. The DELETE must not — see the next test.
        actions = actions_for("AWS::ECS::Service", "Modify", replacement=True)
        assert set(actions) == {"ecs:CreateService", "ecs:UpdateService"}
        assert "ecs:DeleteService" not in actions

    def test_a_delete_never_blocks_because_cfn_tolerates_a_failed_cleanup(self):
        # Measured on this stack 2026-09-09: TaskDefinition reported
        # DELETE_FAILED on ecs:DeregisterTaskDefinition while the stack still
        # reported UPDATE_COMPLETE, reason "Update successful. One or more
        # resources could not be deleted." A delete permission is therefore
        # never what fails a deploy — and demanding it would block EVERY deploy
        # here, because the role does not have DeregisterTaskDefinition.
        assert actions_for("AWS::ECS::Service", "Remove") == ()
        assert actions_for("AWS::ECS::TaskDefinition", "Remove") == ()

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
