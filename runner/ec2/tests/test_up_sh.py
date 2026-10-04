"""`up.sh` builds a runner from commands alone — and never surprises the live box.

Driven against the real script with `aws` and `curl` faked on PATH and a local
HTTP server standing in for canopy-web, asserting on the calls it makes:

  * a named box: the stack and RunnerName come from --name, and --standby means
    the box claims nothing it is not explicitly handed;
  * the seed goes up as parts THEN manifest, and parts left over from a larger
    publish are removed;
  * every one-time human prerequisite is checked BEFORE anything is written;
  * an update that would stop/start or replace the instance is refused without
    --allow-instance-change — on the live stack, a plain re-run after any
    template change would otherwise bounce (or replace) production silently.
"""
from __future__ import annotations

import http.server
import json
import pathlib
import subprocess
import threading

import pytest

EC2 = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = EC2 / "up.sh"
STAGED = ["canopy/cloud-runner/canopy-pat", "canopy/cloud-runner/claude-oauth-token",
          "canopy/cloud-runner/gog-keyring-password"]

INSTANCE_CHANGE = {"Changes": [{"ResourceChange": {
    "Action": "Modify", "LogicalResourceId": "Instance", "Replacement": "True",
    "Details": [{"Target": {"Attribute": "Properties", "Name": "BlockDeviceMappings"}}]}}]}
SG_ONLY = {"Changes": [{"ResourceChange": {
    "Action": "Modify", "LogicalResourceId": "SecurityGroup", "Replacement": "False",
    "Details": [{"Target": {"Attribute": "Properties", "Name": "SecurityGroupIngress"}}]}}]}


@pytest.fixture
def canopy():
    """A canopy-web that answers the PAT check; `.code` sets the status."""
    class H(http.server.BaseHTTPRequestHandler):
        code = 200

        def do_GET(self):  # noqa: N802
            self.send_response(H.code)
            self.end_headers()
            self.wfile.write(b"[]")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.handler = H
    yield srv
    srv.shutdown()


def _fake(tmp: pathlib.Path, *, stack_status=None, change_set=None, secrets=STAGED,
          old_parts=0) -> pathlib.Path:
    state = tmp / "state"
    state.mkdir()
    (state / "calls").write_text("")
    cfg = {"stack_status": stack_status, "change_set": change_set, "secrets": list(secrets),
           "old_parts": old_parts}
    (state / "cfg.json").write_text(json.dumps(cfg))
    bin_ = tmp / "bin"
    bin_.mkdir()
    (bin_ / "aws").write_text(f'''#!/usr/bin/env python3
import json, sys, pathlib
S = pathlib.Path({str(state)!r})
cfg = json.loads((S / "cfg.json").read_text())
a = [x for x in sys.argv[1:]]
with (S / "calls").open("a") as fh:
    fh.write(" ".join(a) + chr(10))
def arg(k):
    return a[a.index(k) + 1] if k in a else ""
if "get-caller-identity" in a:
    print("123456789012")
elif "describe-secret" in a:
    sys.exit(0 if arg("--secret-id") in cfg["secrets"] else 255)
elif "get-secret-value" in a:
    print("a-pat")
elif "list-secrets" in a:
    print(chr(9).join(f"canopy/cloud-runner/runner-seed-part-{{i}}" for i in range(cfg["old_parts"])))
elif "describe-stacks" in a and "StackStatus" in arg("--query"):
    if not cfg["stack_status"]:
        sys.exit(255)
    print(cfg["stack_status"])
elif "describe-stacks" in a:
    print(json.dumps([{{"OutputKey": "PublicIp", "OutputValue": "1.2.3.4"}},
                      {{"OutputKey": "KeyPairId", "OutputValue": "key-1"}}]))
elif "deploy" in a and "--no-execute-changeset" in a:
    print("Changeset created successfully. Run: aws cloudformation describe-change-set "
          "--change-set-name arn:aws:cloudformation:us-east-1:1:changeSet/cs/abc")
elif "describe-change-set" in a:
    print(json.dumps(cfg["change_set"] or {{"Changes": []}}))
elif "get-parameter" in a:
    print("PRIVATE KEY")
''')
    (bin_ / "curl").write_text("#!/bin/sh\necho 1.2.3.4\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    return state


def _up(tmp, canopy, *args) -> subprocess.CompletedProcess:
    home = tmp / "home"
    (home / ".claude" / "canopy").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "canopy" / "workbench-token").write_text("t\n")
    base = f"http://127.0.0.1:{canopy.server_address[1]}"
    try:
        return subprocess.run(
            ["bash", str(SCRIPT), "--base-url", base, "--allow-dirty", *args],
            capture_output=True, text=True, timeout=120, cwd=str(EC2),
            env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                 "HOME": str(home)})
    finally:
        for pem in EC2.glob("canopy-cloud-runner*-key.pem"):
            pem.unlink()


def _calls(state):
    return (state / "calls").read_text().splitlines()


def test_a_named_standby_box_creates_its_own_stack(tmp_path, canopy):
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--standby")
    assert res.returncode == 0, res.stdout + res.stderr
    deploy = next(c for c in _calls(state) if "cloudformation deploy" in c)
    assert "--stack-name canopy-cloud-runner-test" in deploy
    for p in ("RunnerName=cloud-ec2-test", "RunnerProjects=", "RunnerSessions=0"):
        assert f" {p} " in deploy + " ", (p, deploy)
    assert "--no-execute-changeset" not in deploy, "a CREATE has nothing to review"


def test_the_seed_is_parts_then_manifest_and_stale_parts_go(tmp_path, canopy):
    state = _fake(tmp_path, old_parts=6)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test")
    assert res.returncode == 0, res.stdout + res.stderr
    writes = [c for c in _calls(state) if "create-secret" in c or "put-secret-value" in c]
    ids = [c.split("--name ")[-1].split()[0] if "create-secret" in c
           else c.split("--secret-id ")[-1].split()[0] for c in writes]
    assert ids[-1] == "canopy/cloud-runner/runner-seed", "the manifest must be written LAST"
    parts = [i for i in ids if "-part-" in i]
    assert parts and parts == [f"canopy/cloud-runner/runner-seed-part-{n}" for n in range(len(parts))]
    deleted = [c for c in _calls(state) if "delete-secret" in c]
    assert len(deleted) == 6 - len(parts), deleted
    assert not any("runner-code" in c for c in writes), "the legacy secret must not be touched"


@pytest.mark.parametrize("missing", STAGED)
def test_a_missing_staged_secret_stops_before_anything_is_written(tmp_path, canopy, missing):
    state = _fake(tmp_path, secrets=[s for s in STAGED if s != missing])
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test")
    assert res.returncode != 0 and missing in res.stderr and "secrets.sh" in res.stderr
    assert not any("put-secret" in c or "create-secret" in c or "deploy" in c
                   for c in _calls(state))


def test_a_dead_canopy_pat_stops_before_anything_is_written(tmp_path, canopy):
    canopy.handler.code = 401
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test")
    assert res.returncode != 0 and "does not authenticate" in res.stderr
    assert not any("create-secret" in c or "deploy" in c for c in _calls(state))


def test_an_update_that_touches_the_instance_is_refused(tmp_path, canopy):
    """The live stack after any template change: the default (no-arg) path."""
    state = _fake(tmp_path, stack_status="CREATE_COMPLETE", change_set=INSTANCE_CHANGE)
    res = _up(tmp_path, canopy)
    assert res.returncode != 0
    assert "touches the instance" in res.stderr and "BlockDeviceMappings" in res.stderr
    calls = _calls(state)
    assert any("delete-change-set" in c for c in calls)
    assert not any("execute-change-set" in c for c in calls)
    assert "--stack-name canopy-cloud-runner " in next(c for c in calls if "cloudformation deploy" in c) + " "


def test_allow_instance_change_executes_it(tmp_path, canopy):
    state = _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=INSTANCE_CHANGE)
    res = _up(tmp_path, canopy, "--allow-instance-change")
    assert res.returncode == 0, res.stdout + res.stderr
    assert any("execute-change-set" in c for c in _calls(state))


def test_an_update_that_spares_the_instance_just_runs(tmp_path, canopy):
    state = _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=SG_ONLY)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test")
    assert res.returncode == 0, res.stdout + res.stderr
    assert any("execute-change-set" in c for c in _calls(state))


def test_a_failed_create_must_be_torn_down_first(tmp_path, canopy):
    state = _fake(tmp_path, stack_status="ROLLBACK_COMPLETE")
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test")
    assert res.returncode != 0 and "down.sh" in res.stderr
    assert not any("deploy" in c for c in _calls(state))
