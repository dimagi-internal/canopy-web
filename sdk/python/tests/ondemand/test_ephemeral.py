"""EphemeralInstance: one launched-then-terminated instance per lease (botocore Stubber, no AWS)."""
from __future__ import annotations

import base64
import json

import boto3
import pytest
from botocore.stub import ANY, Stubber

from canopy_sdk.ondemand import (
    EphemeralInstance,
    InstanceGone,
    LaunchFailed,
    OnDemandError,
)

LT = "lt-0abc"
CMD = "0b5e0000-0000-4000-8000-0000000000c1"
LEASE = "6f1c0000-0000-4000-8000-000000000001"


def _clients():
    kw = {"region_name": "us-east-1", "aws_access_key_id": "x", "aws_secret_access_key": "x"}
    ec2 = boto3.client("ec2", **kw)
    ssm = boto3.client("ssm", **kw)
    return ec2, Stubber(ec2), ssm, Stubber(ssm)


def _inst(ec2, ssm):
    return EphemeralInstance(region="us-east-1", launch_template_id=LT,
                             capability="android-emulator", owner="ace", ec2=ec2, ssm=ssm)


def _tags(lease=LEASE):
    return [{"Key": "canopy:capability", "Value": "android-emulator"},
            {"Key": "canopy:owner", "Value": "ace"},
            {"Key": "canopy:lease", "Value": lease}]


def test_launch_uses_the_lease_as_client_token_and_tags_the_request():
    ec2, st, ssm, _ = _clients()
    user_data = {"lease_id": LEASE, "params": {"state": "cc-2.63.0"}, "max_lifetime_s": 3600,
                 "bucket": "b"}
    expected = {
        "LaunchTemplate": {"LaunchTemplateId": LT, "Version": "$Default"},
        "MinCount": 1, "MaxCount": 1, "ClientToken": LEASE,
        "TagSpecifications": [{"ResourceType": "instance", "Tags": _tags()},
                              {"ResourceType": "volume", "Tags": _tags()}],
        "UserData": ANY,
    }
    st.add_response("run_instances", {"Instances": [{"InstanceId": "i-1"}]}, expected)
    with st:
        assert _inst(ec2, ssm).launch(lease_id=LEASE, user_data=user_data) == "i-1"


def test_user_data_is_the_json_the_image_reads():
    ec2, st, ssm, _ = _clients()
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return {"Instances": [{"InstanceId": "i-1"}]}

    ec2.run_instances = capture  # the encoding, not the wire, is what matters here
    _inst(ec2, ssm).launch(lease_id=LEASE, user_data={"lease_id": LEASE, "params": {}})
    assert json.loads(base64.b64decode(seen["UserData"])) == {"lease_id": LEASE, "params": {}}


def test_a_retried_launch_is_the_same_launch():
    """The ClientToken is the lease id, so EC2 answers a retry with the first
    instance — the stub returns what EC2 does, and the SDK sends the same token."""
    ec2, st, ssm, _ = _clients()
    for _ in range(2):
        st.add_response("run_instances", {"Instances": [{"InstanceId": "i-1"}]},
                        {"LaunchTemplate": ANY, "MinCount": 1, "MaxCount": 1,
                         "ClientToken": LEASE, "TagSpecifications": ANY})
    inst = _inst(ec2, ssm)
    with st:
        assert inst.launch(lease_id=LEASE) == inst.launch(lease_id=LEASE) == "i-1"


@pytest.mark.parametrize("code, transient", [
    ("InsufficientInstanceCapacity", True), ("RequestLimitExceeded", True),
    ("UnauthorizedOperation", False), ("IdempotentParameterMismatch", False),
])
def test_launch_failure_says_whether_a_retry_can_help(code, transient):
    ec2, st, ssm, _ = _clients()
    st.add_client_error("run_instances", service_error_code=code)
    with st, pytest.raises(LaunchFailed) as e:
        _inst(ec2, ssm).launch(lease_id=LEASE)
    assert e.value.code == code and e.value.transient is transient


def test_launch_refuses_a_lease_id_ec2_cannot_take():
    ec2, _, ssm, _ = _clients()
    with pytest.raises(ValueError):
        _inst(ec2, ssm).launch(lease_id="x" * 65)


def test_describe_reports_state_and_lease_tag():
    ec2, st, ssm, _ = _clients()
    st.add_response("describe_instances", {"Reservations": [{"Instances": [
        {"InstanceId": "i-1", "State": {"Name": "running"}, "Tags": _tags()}]}]},
        {"InstanceIds": ["i-1"]})
    with st:
        d = _inst(ec2, ssm).describe("i-1")
    assert (d.state, d.lease_id) == ("running", LEASE)


def test_describe_of_a_gone_instance_raises_instance_gone():
    ec2, st, ssm, _ = _clients()
    st.add_client_error("describe_instances", service_error_code="InvalidInstanceID.NotFound")
    with st, pytest.raises(InstanceGone):
        _inst(ec2, ssm).describe("i-1")


def test_terminate_tolerates_an_already_gone_instance():
    ec2, st, ssm, _ = _clients()
    st.add_client_error("terminate_instances", service_error_code="InvalidInstanceID.NotFound")
    with st:
        _inst(ec2, ssm).terminate("i-1")


def test_terminate_surfaces_a_real_failure():
    ec2, st, ssm, _ = _clients()
    st.add_client_error("terminate_instances", service_error_code="UnauthorizedOperation")
    with st, pytest.raises(OnDemandError):
        _inst(ec2, ssm).terminate("i-1")


def test_list_tagged_filters_by_capability_owner_and_live_state_across_pages():
    ec2, st, ssm, _ = _clients()
    filters = [{"Name": "tag:canopy:capability", "Values": ["android-emulator"]},
               {"Name": "tag:canopy:owner", "Values": ["ace"]},
               {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
    st.add_response("describe_instances", {"NextToken": "n", "Reservations": [{"Instances": [
        {"InstanceId": "i-1", "State": {"Name": "running"}, "Tags": _tags()}]}]},
        {"Filters": filters})
    st.add_response("describe_instances", {"Reservations": [{"Instances": [
        {"InstanceId": "i-2", "State": {"Name": "pending"}, "Tags": _tags("other")}]}]},
        {"Filters": filters, "NextToken": "n"})
    with st:
        found = _inst(ec2, ssm).list_tagged()
    assert [(d.instance_id, d.lease_id) for d in found] == [("i-1", LEASE), ("i-2", "other")]


def test_run_async_returns_the_command_id_and_sends_output_to_s3():
    ec2, _, ssm, sst = _clients()
    sst.add_response("send_command", {"Command": {"CommandId": CMD}}, {
        "InstanceIds": ["i-1"], "DocumentName": "AWS-RunShellScript",
        "Parameters": {"commands": ["ace-mobile diagnose"], "executionTimeout": ["120"]},
        "TimeoutSeconds": 120, "OutputS3BucketName": "bkt", "OutputS3KeyPrefix": "leases/L/exec",
    })
    with sst:
        cid = _inst(ec2, ssm).run_async("i-1", ["ace-mobile diagnose"], timeout_s=120,
                                        output_bucket="bkt", output_prefix="leases/L/exec/")
    assert cid == CMD


def test_invocation_reads_state_back_from_ssm():
    ec2, _, ssm, sst = _clients()
    sst.add_response("get_command_invocation", {
        "CommandId": CMD, "InstanceId": "i-1", "Status": "InProgress", "ResponseCode": -1,
        "StandardOutputContent": "booting", "StandardErrorContent": ""},
        {"CommandId": CMD, "InstanceId": "i-1"})
    sst.add_response("get_command_invocation", {
        "CommandId": CMD, "InstanceId": "i-1", "Status": "Failed", "ResponseCode": 3,
        "StandardOutputContent": "out", "StandardErrorContent": "err",
        "StandardOutputUrl": "https://s3.us-east-1.amazonaws.com/b/leases/L/exec/" + CMD + "/i-1/stdout"},
        {"CommandId": CMD, "InstanceId": "i-1"})
    inst = _inst(ec2, ssm)
    with sst:
        running = inst.invocation(CMD, "i-1")
        done = inst.invocation(CMD, "i-1")
    assert (running.status, running.exit_code, running.done) == ("InProgress", None, False)
    assert (done.status, done.exit_code, done.done) == ("Failed", 3, True)
    assert done.stdout_url.endswith("/stdout")


def test_invocation_of_an_unknown_command_is_none():
    ec2, _, ssm, sst = _clients()
    sst.add_client_error("get_command_invocation", service_error_code="InvocationDoesNotExist")
    with sst:
        assert _inst(ec2, ssm).invocation(CMD, "i-1") is None
