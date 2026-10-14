"""ondemand-capability.cfn.yaml in its two modes.

`persistent` (the default) must deploy EXACTLY what the template deployed before
modes existed — labs-emod and every other existing stack update to a no-op. The
fixture is the 0.7.2 template verbatim; both are rendered the way CloudFormation
would (conditions evaluated, NoValue dropped) and compared whole.

`ephemeral` is the contract canopy-web's capability leases rely on
(docs/architecture/capabilities.md): no instance, a launch template whose AMI
comes from SSM at launch, terminate-on-halt, and a broker policy scoped to this
capability's and owner's tags.
"""
from __future__ import annotations

import pathlib

import pytest

from canopy_sdk.ondemand.assets import asset_path

from .cfn import load, render

FIXTURE = pathlib.Path(__file__).with_name("fixtures") / "ondemand-capability.persistent-0.7.2.cfn.yaml"
EPHEMERAL = {"Mode": "ephemeral", "Capability": "android-emulator", "Owner": "ace",
             "BrokerRoleName": "labs-jj-ecs-task-role"}


@pytest.fixture(scope="module")
def template():
    return load(asset_path("ondemand-capability.cfn.yaml").read_text())


@pytest.fixture(scope="module")
def ephemeral(template):
    return render(template, **EPHEMERAL)


@pytest.mark.parametrize("params", [
    {},
    {"NestedVirtualization": "true"},
    {"UserData": "aGk="},
    {"NestedVirtualization": "true", "UserData": "aGk="},
])
def test_persistent_mode_renders_exactly_the_pre_mode_template(template, params):
    old = render(load(FIXTURE.read_text()), **params)
    assert render(template, Mode="persistent", **params) == old
    assert render(template, **params) == old  # Mode defaults to persistent


def test_persistent_parameters_still_accept_what_existing_stacks_pass(template):
    old = load(FIXTURE.read_text())["Parameters"]
    new = template["Parameters"]
    assert set(old) <= set(new)
    # Every parameter added for ephemeral mode has a default, so an existing
    # stack's `deploy` needs no new overrides.
    for name in set(new) - set(old):
        assert "Default" in new[name], name
    assert new["Mode"]["Default"] == "persistent"


def test_each_mode_refuses_its_missing_inputs_at_deploy(template):
    rules = template["Rules"]
    persistent = str(rules["PersistentNeedsAnAmiAndAConsumer"])
    assert "AmiId" in persistent and "ConsumerTaskRoleName" in persistent
    ephemeral = str(rules["EphemeralNeedsAnOwnerAndABroker"])
    assert "Owner" in ephemeral and "BrokerRoleName" in ephemeral


def test_ephemeral_has_no_instance_and_no_per_instance_alarm(ephemeral):
    res = ephemeral["Resources"]
    assert "Instance" not in res
    # An alarm names one InstanceId; these instances do not exist at deploy.
    assert "IdleStopAlarm" not in res
    assert "ConsumerPolicy" not in res
    assert "BrokerPolicy" in res


def test_ephemeral_outputs_are_the_broker_contract(ephemeral):
    outputs = ephemeral["Outputs"]
    assert set(outputs) == {"LaunchTemplateId", "ArtifactsBucketName", "InstanceRoleArn",
                            "InstanceRoleId", "Capability", "Owner"}
    assert outputs["LaunchTemplateId"]["Value"] == {"Ref": "LaunchTemplate"}
    assert outputs["InstanceRoleId"]["Value"] == {"GetAtt": "InstanceRole.RoleId"}


def test_ephemeral_launch_template_reads_the_ami_from_ssm_and_terminates_on_halt(ephemeral):
    data = ephemeral["Resources"]["LaunchTemplate"]["Properties"]["LaunchTemplateData"]
    assert data["ImageId"] == {"Sub": "resolve:ssm:/canopy/capabilities/${Owner}/${Capability}/ami"}
    assert data["InstanceInitiatedShutdownBehavior"] == "terminate"
    assert "SecurityGroupIds" not in data  # the network interface carries them
    (eni,) = data["NetworkInterfaces"]
    assert eni["SubnetId"] == {"Ref": "SubnetId"} and eni["DeviceIndex"] == 0


def test_ephemeral_instances_are_tagged_for_the_broker(ephemeral):
    specs = ephemeral["Resources"]["LaunchTemplate"]["Properties"]["LaunchTemplateData"]["TagSpecifications"]
    for kind in ("instance", "volume"):
        (spec,) = [s for s in specs if s["ResourceType"] == kind]
        tags = {t["Key"]: t["Value"] for t in spec["Tags"]}
        assert tags["canopy:capability"] == {"Ref": "Capability"}
        assert tags["canopy:owner"] == {"Ref": "Owner"}


def test_ephemeral_names_do_not_collide_across_owners(ephemeral):
    res = ephemeral["Resources"]
    # Generated names for the account-global IAM / SG names; the launch template
    # is named for its owner AND capability.
    assert "RoleName" not in res["InstanceRole"]["Properties"]
    assert "InstanceProfileName" not in res["InstanceProfile"]["Properties"]
    assert "GroupName" not in res["SecurityGroup"]["Properties"]
    assert res["LaunchTemplate"]["Properties"]["LaunchTemplateName"] == {
        "Sub": "canopy-cap-${Owner}-${Capability}"}


def _statements(policy_resource):
    return {s["Sid"]: s for s in policy_resource["Properties"]["PolicyDocument"]["Statement"]}


def test_ephemeral_instance_role_writes_only_its_own_prefix_and_the_lease_dirs(ephemeral):
    (policy,) = ephemeral["Resources"]["InstanceRole"]["Properties"]["Policies"]
    st = {s["Sid"]: s for s in policy["PolicyDocument"]["Statement"]}
    assert st["OwnPrefixWrite"]["Resource"] == {"Sub": "${ArtifactsBucket.Arn}/vm/${!aws:userid}/*"}
    assert st["LeaseInputsRead"]["Action"] == ["s3:GetObject"]
    assert st["LeaseInputsRead"]["Resource"] == {"Sub": "${ArtifactsBucket.Arn}/leases/*/in/*"}
    assert st["LeaseOutputsWrite"]["Resource"] == [
        {"Sub": "${ArtifactsBucket.Arn}/leases/*/out/*"},
        {"Sub": "${ArtifactsBucket.Arn}/leases/*/exec/*"},  # SSM's own command output
    ]
    # Nothing bucket-wide: no write and no read outside those prefixes.
    for s in st.values():
        assert s["Resource"] != {"Sub": "${ArtifactsBucket.Arn}/*"}, s["Sid"]


def test_broker_policy_is_exactly_the_contract(ephemeral):
    broker = ephemeral["Resources"]["BrokerPolicy"]
    assert broker["Properties"]["Roles"] == [{"Ref": "BrokerRoleName"}]
    st = _statements(broker)
    tags = {"aws:ResourceTag/canopy:capability": {"Ref": "Capability"},
            "aws:ResourceTag/canopy:owner": {"Ref": "Owner"}}
    lt_arn = {"Sub": "arn:${AWS::Partition}:ec2:${AWS::Region}:${AWS::AccountId}:launch-template/${LaunchTemplate}"}

    # RunInstances: from this launch template only; the instance only with both request tags.
    run = st["RunTaggedInstancesFromThisLaunchTemplate"]
    assert run["Action"] == ["ec2:RunInstances"]
    assert "instance/*" in str(run["Resource"])
    assert run["Condition"]["ArnLike"] == {"ec2:LaunchTemplate": lt_arn}
    assert run["Condition"]["StringEquals"] == {
        "aws:RequestTag/canopy:capability": {"Ref": "Capability"},
        "aws:RequestTag/canopy:owner": {"Ref": "Owner"}}
    others = st["RunFromThisLaunchTemplate"]
    assert others["Condition"] == {"ArnLike": {"ec2:LaunchTemplate": lt_arn}}
    assert "instance/*" not in str(others["Resource"])

    for sid in ("TerminateTaggedInstances", "SSMSendCommandToTaggedInstances"):
        assert st[sid]["Condition"] == {"StringEquals": tags}, sid
    assert st["TerminateTaggedInstances"]["Action"] == ["ec2:TerminateInstances"]
    assert st["PassTheInstanceRoleToEC2"]["Resource"] == {"GetAtt": "InstanceRole.Arn"}
    assert st["ReadThisStack"] == {"Sid": "ReadThisStack", "Effect": "Allow",
                                   "Action": ["cloudformation:DescribeStacks"],
                                   "Resource": {"Ref": "AWS::StackId"}}
    assert st["TagOnLaunchOnly"]["Condition"] == {"StringEquals": {"ec2:CreateAction": "RunInstances"}}
    # The launch template's ImageId is resolve:ssm:, which EC2 resolves AS THE
    # RunInstances caller — without read on exactly that parameter every launch
    # is SsmAccessDenied (first live lease, 2026-10-14).
    ami = st["ResolveTheAmiParameter"]
    assert set(ami["Action"]) == {"ssm:GetParameters", "ssm:GetParameter"}
    assert ami["Resource"] == {"Sub": "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter/canopy/capabilities/${Owner}/${Capability}/ami"}

    # Nothing else in it can mutate EC2 or SSM.
    mutating = {a for s in st.values() for a in s["Action"]
                if a.startswith(("ec2:", "ssm:")) and not a.startswith(("ec2:Describe", "ssm:Get"))}
    assert mutating == {"ec2:RunInstances", "ec2:TerminateInstances", "ec2:CreateTags", "ssm:SendCommand"}
    # S3 only on this stack's bucket.
    for s in st.values():
        if any(a.startswith("s3:") for a in s["Action"]):
            assert "ArtifactsBucket" in str(s["Resource"]), s["Sid"]


def test_ephemeral_bucket_expires_leases_and_vm_prefixes(ephemeral):
    rules = ephemeral["Resources"]["ArtifactsBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
    assert {r.get("Prefix") for r in rules if "ExpirationInDays" in r} == {"leases/", "vm/"}
