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


# --- fix round 1 ---------------------------------------------------------

from botocore.exceptions import WaiterError

from canopy_sdk.ondemand import CommandResult, OnDemandError, SSMFailure
from canopy_sdk.ondemand import instance as inst_mod


def _inst(ec2):
    return OnDemandInstance("i-1", "us-east-1", "emod", ec2=ec2, ssm=object())


def _ok(stdout=""):
    return CommandResult(stdout=stdout, stderr="", status="Success", exit_code=0)


def test_describe_not_found_maps_to_instance_gone():
    ec2, st = _ec2()
    st.add_client_error("describe_instances", "InvalidInstanceID.NotFound")
    with st:
        with pytest.raises(InstanceGone, match="redeploy"):
            _inst(ec2).ensure_running()


def test_describe_empty_reservations_is_instance_gone():
    ec2, st = _ec2()
    st.add_response("describe_instances", {"Reservations": []}, {"InstanceIds": ["i-1"]})
    with st:
        with pytest.raises(InstanceGone, match="redeploy"):
            _inst(ec2).ensure_running()


def test_shutting_down_is_instance_gone():
    ec2, st = _ec2()
    st.add_response("describe_instances", _describe("shutting-down"), {"InstanceIds": ["i-1"]})
    with st:
        with pytest.raises(InstanceGone):
            _inst(ec2).ensure_running()


def test_stopping_waits_for_stopped_then_starts(monkeypatch):
    ec2, st = _ec2()
    st.add_response("describe_instances", _describe("stopping"), {"InstanceIds": ["i-1"]})
    st.add_response("start_instances", {"StartingInstances": []}, {"InstanceIds": ["i-1"]})
    inst = _inst(ec2)
    order = []
    monkeypatch.setattr(inst, "_wait_stopped", lambda t: order.append("stopped"))
    monkeypatch.setattr(inst, "_wait_ec2_ok", lambda t: order.append("ok"))
    monkeypatch.setattr(inst, "_wait_ready", lambda t: None)
    monkeypatch.setattr(inst, "touch", lambda: None)
    with st:
        r = inst.ensure_running()
    assert order == ["stopped", "ok"] and r.cold is True
    st.assert_no_pending_responses()


def test_start_instances_client_error_is_wrapped():
    ec2, st = _ec2()
    st.add_response("describe_instances", _describe("stopped"), {"InstanceIds": ["i-1"]})
    st.add_client_error("start_instances", "UnauthorizedOperation")
    with st:
        with pytest.raises(OnDemandError, match="i-1"):
            _inst(ec2).ensure_running()


def test_waiter_error_is_wrapped():
    ec2, _ = _ec2()
    inst = _inst(ec2)

    class W:
        def wait(self, **kw):
            raise WaiterError("instance_status_ok", "Max attempts exceeded", {})

    monkey = pytest.MonkeyPatch()
    monkey.setattr(ec2, "get_waiter", lambda name: W())
    try:
        with pytest.raises(OnDemandError, match="i-1"):
            inst._wait_ec2_ok(10)
    finally:
        monkey.undo()


def test_wait_ready_tolerates_transient_ssm_errors(monkeypatch):
    ec2, _ = _ec2()
    inst = _inst(ec2)
    calls = iter([SSMFailure("agent not registered"), _ok("READY")])

    def fake_run(cmds, timeout_s=600):
        r = next(calls)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(inst, "run", fake_run)
    monkeypatch.setattr(inst_mod.time, "sleep", lambda s: None)
    inst._wait_ready(60)


def test_wait_ready_deadline_mentions_last_error(monkeypatch):
    ec2, _ = _ec2()
    inst = _inst(ec2)

    def boom(cmds, timeout_s=600):
        raise SSMFailure("agent not registered")

    monkeypatch.setattr(inst, "run", boom)
    monkeypatch.setattr(inst_mod.time, "sleep", lambda s: None)
    ticks = iter(range(0, 1000))
    monkeypatch.setattr(inst_mod.time, "monotonic", lambda: float(next(ticks)))
    with pytest.raises(OnDemandError, match="agent not registered"):
        inst._wait_ready(3)


def test_stop_calls_stop_instances_and_wraps_errors():
    ec2, st = _ec2()
    st.add_response("stop_instances", {"StoppingInstances": []}, {"InstanceIds": ["i-1"]})
    st.add_client_error("stop_instances", "UnauthorizedOperation")
    with st:
        inst = _inst(ec2)
        inst.stop()
        with pytest.raises(OnDemandError, match="i-1"):
            inst.stop()


def test_run_and_touch_delegate_to_run_command(monkeypatch):
    ec2, _ = _ec2()
    inst = _inst(ec2)
    seen = []

    def fake(ssm, iid, *, commands, timeout_seconds):
        seen.append((iid, commands, timeout_seconds))
        return _ok()

    monkeypatch.setattr(inst_mod, "run_command", fake)
    inst.run(["echo hi"], timeout_s=5)
    inst.touch()
    assert seen[0] == ("i-1", ["echo hi"], 5)
    assert "touch /var/run/emod/last-activity" in seen[1][1][0]


@pytest.mark.parametrize("stdout,expected", [("42\n", 42), ("NONE\n", None), ("garbage", None), ("", None)])
def test_status_running_idle_parsing(monkeypatch, stdout, expected):
    ec2, st = _ec2()
    st.add_response("describe_instances", _describe("running"), {"InstanceIds": ["i-1"]})
    inst = _inst(ec2)
    monkeypatch.setattr(inst, "run", lambda cmds, timeout_s=600: _ok(stdout))
    with st:
        s = inst.status()
    assert s.state == "running" and s.idle_for_s == expected


def test_status_stopped_has_no_idle():
    ec2, st = _ec2()
    st.add_response("describe_instances", _describe("stopped"), {"InstanceIds": ["i-1"]})
    with st:
        s = _inst(ec2).status()
    assert s.state == "stopped" and s.idle_for_s is None


def test_ec2_ok_waits_for_instance_then_system_status():
    ec2, _ = _ec2()
    inst = _inst(ec2)
    seen = []

    class W:
        def __init__(self, name):
            self.name = name

        def wait(self, **kw):
            seen.append(self.name)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(ec2, "get_waiter", lambda name: W(name))
    try:
        inst._wait_ec2_ok(60)
    finally:
        monkey.undo()
    assert seen == ["instance_status_ok", "system_status_ok"]


def test_default_boot_budget_covers_a_measured_cold_start():
    import inspect

    # ~230 s start -> status ok measured on a real m8i.xlarge; 180 s failed live.
    default = inspect.signature(OnDemandInstance.ensure_running).parameters["boot_timeout_s"].default
    assert default >= 400


def test_waiter_client_error_is_wrapped():
    from botocore.exceptions import ClientError

    ec2, _ = _ec2()
    inst = _inst(ec2)

    class W:
        def wait(self, **kw):
            raise ClientError({"Error": {"Code": "RequestLimitExceeded", "Message": "x"}}, "DescribeInstanceStatus")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(ec2, "get_waiter", lambda name: W())
    try:
        with pytest.raises(OnDemandError, match="i-1"):
            inst._wait_ec2_ok(10)
    finally:
        monkey.undo()
