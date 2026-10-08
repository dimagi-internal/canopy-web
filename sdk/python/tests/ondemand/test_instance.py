"""OnDemandInstance: the stopped-when-idle EC2 lifecycle (botocore Stubber, no AWS)."""
from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber

from canopy_sdk.ondemand import InstanceGone, OnDemandInstance


def _ec2():
    c = boto3.client("ec2", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    return c, Stubber(c)


def _describe(state):
    return {"Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": state}}]}]}


def test_terminated_instance_raises_actionable_error():
    ec2, st = _ec2()
    st.add_response("describe_instances", _describe("terminated"), {"InstanceIds": ["i-1"]})
    with st:
        inst = OnDemandInstance("i-1", "us-east-1", "emod", ec2=ec2, ssm=object())
        with pytest.raises(InstanceGone) as e:
            inst.ensure_running()
    assert "emod" in str(e.value) and "redeploy" in str(e.value)


def test_stopped_instance_is_started_and_reported_cold(monkeypatch):
    ec2, st = _ec2()
    st.add_response("describe_instances", _describe("stopped"), {"InstanceIds": ["i-1"]})
    st.add_response("start_instances", {"StartingInstances": []}, {"InstanceIds": ["i-1"]})
    inst = OnDemandInstance("i-1", "us-east-1", "emod", ec2=ec2, ssm=object())
    monkeypatch.setattr(inst, "_wait_ec2_ok", lambda t: None)
    monkeypatch.setattr(inst, "_wait_ready", lambda t: None)
    monkeypatch.setattr(inst, "touch", lambda: None)
    with st:
        r = inst.ensure_running()
    assert r.cold is True and "ec2_start_s" in r.timings


def test_running_instance_is_warm(monkeypatch):
    ec2, st = _ec2()
    st.add_response("describe_instances", _describe("running"), {"InstanceIds": ["i-1"]})
    inst = OnDemandInstance("i-1", "us-east-1", "emod", ec2=ec2, ssm=object())
    monkeypatch.setattr(inst, "_wait_ready", lambda t: None)
    monkeypatch.setattr(inst, "touch", lambda: None)
    with st:
        assert inst.ensure_running().cold is False
