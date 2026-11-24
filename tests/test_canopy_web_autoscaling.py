"""canopy-web's ECS service is scaled by Application Auto Scaling (deploy/aws/canopy-web.cfn.yaml),
and its deploy can create what that takes (deploy/aws/preflight.py, deploy/aws/deploy_roles.py)."""
from __future__ import annotations

from deploy.aws import deploy_roles
from deploy.aws.preflight import RESOURCE_ACTIONS, actions_for
from tests.fixtures.cfn import load_template

T = load_template("canopy-web.cfn.yaml")
R = T["Resources"]


def test_the_service_names_no_count_so_a_deploy_never_resets_a_scaled_one():
    assert "DesiredCount" not in R["Service"]["Properties"]
    assert "DesiredCount" not in T["Parameters"]


def test_two_to_four_tasks_by_default():
    assert T["Parameters"]["MinTasks"]["Default"] == 2
    assert T["Parameters"]["MaxTasks"]["Default"] == 4
    target = R["ScalableTarget"]["Properties"]
    assert target["ServiceNamespace"] == "ecs"
    assert target["ScalableDimension"] == "ecs:service:DesiredCount"
    assert "${Service.Name}" in str(target["ResourceId"])


def test_target_tracks_cpu_out_fast_in_slowly():
    policy = R["CpuScalingPolicy"]["Properties"]
    assert policy["PolicyType"] == "TargetTrackingScaling"
    conf = policy["TargetTrackingScalingPolicyConfiguration"]
    assert conf["PredefinedMetricSpecification"]["PredefinedMetricType"] == "ECSServiceAverageCPUUtilization"
    assert conf["TargetValue"] == 55
    assert conf["ScaleOutCooldown"] == 60 and conf["ScaleInCooldown"] == 600


def test_one_vcpu_tasks():
    props = R["TaskDefinition"]["Properties"]
    assert (props["Cpu"], props["Memory"]) == ("1024", "2048")


def test_every_resource_type_in_the_template_is_mapped_for_preflight():
    for name, res in R.items():
        assert res["Type"] in RESOURCE_ACTIONS, name


def test_the_deploy_role_grants_what_preflight_checks_for_autoscaling():
    granted = set()
    for st in deploy_roles.policy_for("canopy-web", deploy_roles.APPS["canopy-web"])["Statement"]:
        actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        granted.update(actions)
    for rtype in ("AWS::ApplicationAutoScaling::ScalableTarget", "AWS::ApplicationAutoScaling::ScalingPolicy"):
        for change in ("Add", "Modify"):
            assert set(actions_for(rtype, change)) <= granted, (rtype, change)
    assert {"cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms"} <= granted
