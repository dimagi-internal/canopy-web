"""run_command: SSM send + poll (botocore Stubber, no AWS)."""
from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber

from canopy_sdk.ondemand import SSMFailure, SSMTimeout, run_command


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


CID = "c" * 36


def _ssm():
    c = boto3.client("ssm", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    return c, Stubber(c)


def _send(st):
    st.add_response("send_command", {"Command": {"CommandId": CID}}, {
        "InstanceIds": ["i-1"], "DocumentName": "AWS-RunShellScript",
        "Parameters": {"commands": ["echo hi"]}, "TimeoutSeconds": 30,
    })


def _inv(status, code=0, out="", err=""):
    return {"CommandId": CID, "InstanceId": "i-1", "Status": status, "ResponseCode": code,
            "StandardOutputContent": out, "StandardErrorContent": err}


def test_invocation_not_yet_existing_is_retried_then_succeeds():
    ssm, st = _ssm()
    _send(st)
    st.add_client_error("get_command_invocation", "InvocationDoesNotExist")
    st.add_response("get_command_invocation", _inv("Success", out="hi\n"),
                    {"CommandId": CID, "InstanceId": "i-1"})
    with st:
        res = run_command(ssm, "i-1", commands=["echo hi"], timeout_seconds=30, poll_interval=0)
    assert res.ok and res.stdout == "hi\n"


def test_terminal_failure_raises_with_stderr():
    ssm, st = _ssm()
    _send(st)
    st.add_response("get_command_invocation", _inv("Failed", code=2, err="boom"),
                    {"CommandId": CID, "InstanceId": "i-1"})
    with st:
        with pytest.raises(SSMFailure, match="boom"):
            run_command(ssm, "i-1", commands=["echo hi"], timeout_seconds=30, poll_interval=0)


def test_never_terminal_raises_timeout(monkeypatch):
    ssm, st = _ssm()
    _send(st)
    for _ in range(50):
        st.add_response("get_command_invocation", _inv("InProgress"),
                        {"CommandId": CID, "InstanceId": "i-1"})
    # Fake clock: each monotonic() call advances 1s, so a 30s deadline passes quickly.
    ticks = iter(range(0, 10_000))
    monkeypatch.setattr("time.monotonic", lambda: float(next(ticks)))
    with st:
        with pytest.raises(SSMTimeout, match="InProgress"):
            run_command(ssm, "i-1", commands=["echo hi"], timeout_seconds=30, poll_interval=0)


def test_success_returns_stdout():
    ssm, st = _ssm()
    _send(st)
    st.add_response("get_command_invocation", _inv("Success", out="ok"),
                    {"CommandId": CID, "InstanceId": "i-1"})
    with st:
        res = run_command(ssm, "i-1", commands=["echo hi"], timeout_seconds=30, poll_interval=0)
    assert res.ok and res.stdout == "ok" and res.exit_code == 0
