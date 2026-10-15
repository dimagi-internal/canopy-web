"""The two guards an ephemeral capability stack owns itself, independent of the broker.

- **ExecDocument**: the one command document the broker may send. The script is
  run here for real (bash), as SSM would run it after substituting its
  parameters, so the allow list and the argv decoding are tested, not described.
- **BackstopFunction**: the scheduled Lambda that terminates any of the
  capability's instances older than MaxInstanceLifetimeSeconds. Its inline code
  is executed against a stub EC2 client; its role and schedule are checked as
  rendered.
"""
from __future__ import annotations

import base64
import datetime as dt
import os
import shutil
import subprocess
import types

import pytest

from canopy_sdk.ondemand.assets import asset_path

from .cfn import load, render

EPHEMERAL = {"Mode": "ephemeral", "Capability": "android-emulator", "Owner": "ace",
             "BrokerRoleName": "canopy-web-task-role", "ExecAllow": "ace-mobile,adb-shim"}


@pytest.fixture(scope="module")
def ephemeral():
    return render(load(asset_path("ondemand-capability.cfn.yaml").read_text()), **EPHEMERAL)


# ---- the exec document --------------------------------------------------------

def _encode(argv: list[str]) -> str:
    """What the broker sends as Argv (apps/capabilities/services.py)."""
    return base64.b64encode(b"".join(a.encode() + b"\0" for a in argv)).decode()


def _script(ephemeral, *, argv, lease_in="s3://bkt/vm/AROA:i-1/lease/L/in/",
            lease_out="s3://bkt/vm/AROA:i-1/lease/L/out/") -> str:
    doc = ephemeral["Resources"]["ExecDocument"]["Properties"]
    (step,) = doc["Content"]["mainSteps"]
    (cmd,) = step["inputs"]["runCommand"]
    body, subs = cmd["Sub"]
    # CloudFormation's Sub, for the one variable the script names.
    allow = " ".join(EPHEMERAL["ExecAllow"].split(","))
    assert subs["Allow"] == {"Join": [" ", {"Split": [",", {"Ref": "ExecAllow"}]}]}
    assert "${" not in body.replace("${Allow}", ""), "a ${...} in the script would be eaten by Sub"
    script = body.replace("${Allow}", allow)
    # SSM's own substitution of the document's parameters.
    for name, value in {"Argv": _encode(argv), "LeaseId": "6f1c0000-0000-4000-8000-000000000001",
                        "LeaseIn": lease_in, "LeaseOut": lease_out}.items():
        script = script.replace("{{ %s }}" % name, value)
    return script


@pytest.fixture()
def path_with_clis(tmp_path):
    """ace-mobile on PATH: prints each argument it got, and the lease env."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "ace-mobile"
    cli.write_text('#!/bin/bash\nfor a in "$@"; do printf "<%s>\\n" "$a"; done\n'
                   'echo "id=$CANOPY_LEASE_ID in=$CANOPY_LEASE_IN out=$CANOPY_LEASE_OUT"\n')
    cli.chmod(0o755)
    marker = tmp_path / "ran"
    evil = bindir / "bash-evil"
    evil.write_text(f"#!/bin/bash\ntouch {marker}\n")
    evil.chmod(0o755)
    return {"PATH": f"{bindir}:{os.environ['PATH']}", "marker": marker}


def _run(script: str, env: dict) -> subprocess.CompletedProcess:
    # The SSM agent runs runCommand with /bin/sh; the document hands the body to bash.
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, timeout=20,
                          env={"PATH": env["PATH"], "HOME": os.environ.get("HOME", "/tmp")})


needs_bash = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("base64")),
                                reason="needs bash and base64")


@needs_bash
def test_exec_document_runs_an_allowed_argv_verbatim(ephemeral, path_with_clis):
    argv = ["ace-mobile", "run-recipe", "--recipe", "s3://b/x; rm -rf /", "", "it's $(id) `id`",
            "two\nlines", "*"]
    r = _run(_script(ephemeral, argv=argv), path_with_clis)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert "\n".join(lines[:-1]) == "\n".join(f"<{a}>" for a in argv[1:])
    assert lines[-1] == ("id=6f1c0000-0000-4000-8000-000000000001 in=s3://bkt/vm/AROA:i-1/lease/L/in/ "
                         "out=s3://bkt/vm/AROA:i-1/lease/L/out/")


@needs_bash
@pytest.mark.parametrize("argv0", ["bash-evil", "/bin/sh", "ace-mobile ", "ace", "ace-mobile;id", "*"])
def test_exec_document_refuses_anything_off_the_allow_list(ephemeral, path_with_clis, argv0):
    r = _run(_script(ephemeral, argv=[argv0, "-c", "id"]), path_with_clis)
    assert r.returncode == 126
    assert "not on this capability's exec allow list (ace-mobile adb-shim)" in r.stderr
    assert not path_with_clis["marker"].exists()


def test_exec_document_parameters_admit_no_shell_syntax(ephemeral):
    import re

    params = ephemeral["Resources"]["ExecDocument"]["Properties"]["Content"]["parameters"]
    assert set(params) == {"Argv", "LeaseId", "LeaseIn", "LeaseOut", "ExecutionTimeout"}
    hostile = ["'", '"', "$", "`", ";", " ", "\n", "\\", "|", "&", "(", ")", "<", ">", "{", "}"]
    for name, spec in params.items():
        pat = re.compile(spec["allowedPattern"])
        for ch in hostile:
            # fullmatch: an engine whose `$` admits one trailing newline would let
            # only that through, and a newline inside the script's single quotes
            # is inert. No pattern admits a quote, so nothing can leave them.
            assert not pat.fullmatch(f"s3://bucket-name/a{ch}b") and not pat.fullmatch(f"QUJD{ch}"), (name, ch)
    assert re.match(params["Argv"]["allowedPattern"], _encode(["ace-mobile", "it's"]))
    assert re.match(params["LeaseIn"]["allowedPattern"], "s3://canopy-cap-x-artifactsbucket-1/vm/AROA4P:i-0ab/lease/"
                    "6f1c0000-0000-4000-8000-000000000001/in/")
    # The step's timeout is the document's own parameter, as AWS-RunShellScript does it.
    (step,) = ephemeral["Resources"]["ExecDocument"]["Properties"]["Content"]["mainSteps"]
    assert step["action"] == "aws:runShellScript" and step["inputs"]["timeoutSeconds"] == "{{ ExecutionTimeout }}"


def test_exec_document_keeps_its_name_across_updates(ephemeral):
    doc = ephemeral["Resources"]["ExecDocument"]["Properties"]
    assert doc["Name"] == {"Sub": "canopy-cap-${Owner}-${Capability}-exec"}
    assert doc["UpdateMethod"] == "NewVersion" and doc["DocumentType"] == "Command"


# ---- the lifetime backstop ----------------------------------------------------

def _backstop_module(ephemeral):
    code = ephemeral["Resources"]["BackstopFunction"]["Properties"]["Code"]["ZipFile"]
    mod = types.ModuleType("index")
    exec(compile(code, "index.py", "exec"), mod.__dict__)  # noqa: S102 — the template's own code
    return mod


class _StubEc2:
    def __init__(self, pages):
        self.pages = pages
        self.filters = None
        self.terminated: list[list[str]] = []

    def get_paginator(self, name):
        assert name == "describe_instances"
        stub = self

        class _P:
            def paginate(self, Filters):  # noqa: N803 — boto3's name
                stub.filters = Filters
                return iter(stub.pages)

        return _P()

    def terminate_instances(self, InstanceIds):  # noqa: N803
        self.terminated.append(list(InstanceIds))


def test_backstop_terminates_only_instances_past_the_lifetime(ephemeral, monkeypatch):
    mod = _backstop_module(ephemeral)
    now = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setenv("CAPABILITY", "android-emulator")
    monkeypatch.setenv("OWNER", "ace")
    monkeypatch.setenv("MAX_INSTANCE_LIFETIME_S", "16200")
    pages = [
        {"Reservations": [{"Instances": [
            {"InstanceId": "i-old", "LaunchTime": now - dt.timedelta(seconds=16201)},
            {"InstanceId": "i-young", "LaunchTime": now - dt.timedelta(seconds=600)},
        ]}]},
        {"Reservations": [{"Instances": [
            {"InstanceId": "i-ancient", "LaunchTime": now - dt.timedelta(days=3)},
        ]}]},
    ]
    ec2 = _StubEc2(pages)
    out = mod.handler({}, None, ec2=ec2)
    assert out == {"checked": 3, "terminated": ["i-ancient", "i-old"]}
    assert ec2.terminated == [["i-ancient", "i-old"]]
    assert {f["Name"]: f["Values"] for f in ec2.filters} == {
        "tag:canopy:capability": ["android-emulator"], "tag:canopy:owner": ["ace"],
        "instance-state-name": ["pending", "running", "stopping", "stopped"]}


def test_backstop_with_nothing_overdue_terminates_nothing(ephemeral, monkeypatch):
    mod = _backstop_module(ephemeral)
    for k, v in {"CAPABILITY": "android-emulator", "OWNER": "ace", "MAX_INSTANCE_LIFETIME_S": "16200"}.items():
        monkeypatch.setenv(k, v)
    ec2 = _StubEc2([{"Reservations": []}])
    assert mod.handler({}, None, ec2=ec2) == {"checked": 0, "terminated": []}
    assert ec2.terminated == []


def test_backstop_is_least_privilege_and_scheduled(ephemeral):
    res = ephemeral["Resources"]
    fn = res["BackstopFunction"]["Properties"]
    assert fn["Environment"]["Variables"] == {"CAPABILITY": {"Ref": "Capability"}, "OWNER": {"Ref": "Owner"},
                                              "MAX_INSTANCE_LIFETIME_S": {"Ref": "MaxInstanceLifetimeSeconds"}}
    assert fn["LoggingConfig"]["LogGroup"] == {"Ref": "BackstopLogGroup"}
    (policy,) = res["BackstopRole"]["Properties"]["Policies"]
    st = {s["Sid"]: s for s in policy["PolicyDocument"]["Statement"]}
    assert {a for s in st.values() for a in s["Action"]} == {
        "ec2:DescribeInstances", "ec2:TerminateInstances", "logs:CreateLogStream", "logs:PutLogEvents"}
    assert st["TerminateThisCapabilitysInstances"]["Condition"] == {"StringEquals": {
        "aws:ResourceTag/canopy:capability": {"Ref": "Capability"},
        "aws:ResourceTag/canopy:owner": {"Ref": "Owner"}}}
    assert st["WriteItsOwnLogs"]["Resource"] == {"GetAtt": "BackstopLogGroup.Arn"}
    rule = res["BackstopSchedule"]["Properties"]
    assert rule["ScheduleExpression"] == "rate(5 minutes)" and rule["State"] == "ENABLED"
    assert rule["Targets"] == [{"Id": "backstop", "Arn": {"GetAtt": "BackstopFunction.Arn"}}]
    perm = res["BackstopInvokePermission"]["Properties"]
    assert perm["Principal"] == "events.amazonaws.com" and perm["SourceArn"] == {"GetAtt": "BackstopSchedule.Arn"}


def test_backstop_default_outlives_the_longest_default_lease():
    params = load(asset_path("ondemand-capability.cfn.yaml").read_text())["Parameters"]
    assert params["MaxInstanceLifetimeSeconds"]["Default"] == 14400 + 1800


def test_persistent_mode_has_no_ephemeral_guards():
    persistent = render(load(asset_path("ondemand-capability.cfn.yaml").read_text()))
    for name in ("ExecDocument", "BackstopFunction", "BackstopRole", "BackstopSchedule",
                 "BackstopInvokePermission", "BackstopLogGroup"):
        assert name not in persistent["Resources"]
