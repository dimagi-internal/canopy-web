"""The in-VM watchdog and the per-capability CloudFormation template ship as package data."""
import os
import subprocess
import time

import pytest
import yaml

from canopy_sdk.ondemand.assets import asset_path

TEMPLATE = "ondemand-capability.cfn.yaml"
SCRIPT = "idle-shutdown.sh"


class _CfnLoader(yaml.SafeLoader):
    """Keeps CloudFormation short-form intrinsics as {"Fn": value} so the tree stays inspectable."""


def _intrinsic(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {tag_suffix: value}


_CfnLoader.add_multi_constructor("!", _intrinsic)


@pytest.fixture(scope="module")
def template():
    return yaml.load(asset_path(TEMPLATE).read_text(), Loader=_CfnLoader)


def test_every_asset_ships():
    for name in (SCRIPT, "ondemand-idle-shutdown.service", "ondemand-idle-shutdown.timer", TEMPLATE):
        assert asset_path(name).is_file(), name


def test_asset_path_refuses_unknown_names():
    with pytest.raises(FileNotFoundError):
        asset_path("nope.sh")
    with pytest.raises(ValueError):
        asset_path("../instance.py")


def test_template_declares_the_capability_contract(template):
    for p in ("Capability", "OwnerTag", "AmiId", "InstanceType", "SubnetId", "VpcId",
              "ConsumerTaskRoleName", "UserData", "NestedVirtualization", "RootVolumeGb"):
        assert p in template["Parameters"], p
    assert template["Parameters"]["NestedVirtualization"]["Default"] == "false"
    assert template["Parameters"]["RootVolumeGb"]["Default"] == 30
    res = template["Resources"]
    assert res["Instance"]["Type"] == "AWS::EC2::Instance"
    assert res["Instance"]["DeletionPolicy"] == "Retain"
    assert res["ArtifactsBucket"]["DeletionPolicy"] == "Retain"
    assert set(template["Outputs"]) >= {"InstanceId", "ArtifactsBucketName"}


def test_instance_is_tagged_with_owner_and_capability(template):
    tags = {t["Key"]: t["Value"] for t in template["Resources"]["Instance"]["Properties"]["Tags"]}
    assert tags["owner"] == {"Ref": "OwnerTag"}
    assert tags["capability"] == {"Ref": "Capability"}


def test_launch_template_stops_on_shutdown_and_passes_user_data(template):
    data = template["Resources"]["LaunchTemplate"]["Properties"]["LaunchTemplateData"]
    assert data["InstanceInitiatedShutdownBehavior"] == "stop"
    assert data["MetadataOptions"]["HttpTokens"] == "required"
    ebs = data["BlockDeviceMappings"][0]["Ebs"]
    assert ebs["Encrypted"] is True and ebs["VolumeType"] == "gp3"
    assert ebs["VolumeSize"] == {"Ref": "RootVolumeGb"}
    assert data["UserData"] == {"If": ["HasUserData", {"Ref": "UserData"}, {"Ref": "AWS::NoValue"}]}
    # Detailed (1-minute) monitoring: basic monitoring publishes CPU every 5 min,
    # which the 60 s IdleStopAlarm would read as missing data and never fire on.
    assert data["Monitoring"] == {"Enabled": True}
    # The nested-virt CPU option is only added when the parameter is "true".
    assert "WantsNestedVirtualization" in template["Conditions"]
    assert "WantsNestedVirtualization" in str(data["CpuOptions"])


def test_launch_template_resource_is_tagged(template):
    specs = template["Resources"]["LaunchTemplate"]["Properties"]["TagSpecifications"]
    (spec,) = [s for s in specs if s["ResourceType"] == "launch-template"]
    tags = {t["Key"]: t["Value"] for t in spec["Tags"]}
    assert tags["owner"] == {"Ref": "OwnerTag"}
    assert tags["capability"] == {"Ref": "Capability"}


def test_stack_delete_retains_what_the_retained_instance_depends_on(template):
    # Deleting the SG/role/profile under a retained instance fails the delete
    # (SG still attached) or strips the instance's SSM credentials.
    for name in ("Instance", "ArtifactsBucket", "SecurityGroup", "InstanceRole", "InstanceProfile"):
        res = template["Resources"][name]
        assert res.get("DeletionPolicy") == "Retain", name
        assert res.get("UpdateReplacePolicy") == "Retain", name


def test_idle_alarm_stops_the_instance(template):
    alarm = template["Resources"]["IdleStopAlarm"]["Properties"]
    assert alarm["MetricName"] == "CPUUtilization" and alarm["Statistic"] == "Maximum"
    assert (alarm["Period"], alarm["EvaluationPeriods"], alarm["Threshold"]) == (60, 5, 5)
    assert alarm["ComparisonOperator"] == "LessThanThreshold"
    assert alarm["AlarmActions"] == [{"Sub": "arn:aws:automate:${AWS::Region}:ec2:stop"}]


def test_consumer_policy_scopes_mutations_by_capability_tag(template):
    policy = template["Resources"]["ConsumerPolicy"]["Properties"]
    assert policy["Roles"] == [{"Ref": "ConsumerTaskRoleName"}]
    tag_cond = {"StringEquals": {"aws:ResourceTag/capability": {"Ref": "Capability"}}}
    mutating = {"ec2:StartInstances", "ec2:StopInstances", "ssm:SendCommand"}
    seen = set()
    for st in policy["PolicyDocument"]["Statement"]:
        actions = set(st["Action"])
        if actions & mutating:
            if st["Resource"] == {"Sub": "arn:${AWS::Partition}:ssm:${AWS::Region}::document/AWS-RunShellScript"}:
                assert actions == {"ssm:SendCommand"}  # documents carry no tags
                continue
            assert st.get("Condition") == tag_cond, st["Sid"]
            assert "instance/*" in str(st["Resource"]), st["Sid"]
            seen |= actions & mutating
        else:
            assert "Condition" not in st, st["Sid"]  # read-only statements need no scoping
    assert seen == mutating


def _run_script(marker, idle_seconds="3600", *args):
    env = {"PATH": os.environ["PATH"], "ACTIVITY_FILE": str(marker), "IDLE_SECONDS": idle_seconds}
    return subprocess.run(["bash", str(asset_path(SCRIPT)), "--dry-run", *args],
                          env=env, capture_output=True, text=True, check=True)


def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_idle_script_stops_after_threshold(tmp_path):
    marker = tmp_path / "last-activity"
    marker.write_text("")
    _age(marker, 7200)
    assert "would shut down" in _run_script(marker).stdout


def test_idle_script_stays_up_inside_threshold(tmp_path):
    marker = tmp_path / "last-activity"
    marker.write_text("")
    _age(marker, 60)
    out = _run_script(marker).stdout
    assert "would shut down" not in out and "staying up" in out


def test_idle_script_honours_idle_seconds(tmp_path):
    marker = tmp_path / "last-activity"
    marker.write_text("")
    _age(marker, 120)
    assert "would shut down" in _run_script(marker, "60").stdout


def test_idle_script_refuses_a_malformed_threshold(tmp_path):
    marker = tmp_path / "last-activity"
    marker.write_text("")  # fresh: must never halt
    env = {"PATH": os.environ["PATH"], "ACTIVITY_FILE": str(marker), "IDLE_SECONDS": "1h"}
    out = subprocess.run(["bash", str(asset_path(SCRIPT)), "--dry-run"],
                         env=env, capture_output=True, text=True)
    assert out.returncode != 0
    assert "would shut down" not in out.stdout and "halting" not in out.stdout
    assert "bad IDLE_SECONDS" in out.stderr


def test_idle_script_treats_a_missing_marker_as_not_idle(tmp_path):
    out = _run_script(tmp_path / "never-touched").stdout
    assert "would shut down" not in out and "staying up" in out


def test_idle_script_defaults_marker_to_the_capability(tmp_path):
    env = {"PATH": os.environ["PATH"], "CAPABILITY": "emod", "IDLE_SECONDS": "60"}
    out = subprocess.run(["bash", str(asset_path(SCRIPT)), "--dry-run"],
                         env=env, capture_output=True, text=True, check=True).stdout
    assert "/var/run/emod/last-activity" in out
