"""`up.sh` builds a runner from commands alone — and never surprises the live box.

Driven against the real script with `aws` and `curl` faked on PATH and a local
HTTP server standing in for canopy-web, asserting on the calls it makes:

  * a named box: the stack and RunnerName come from --name, and --standby means
    the box claims nothing it is not explicitly handed;
  * the seed goes up as parts THEN manifest, and parts left over from a larger
    publish are removed;
  * every one-time human prerequisite is checked BEFORE anything is written —
    the operator's own workbench token first;
  * a new box names its workspace (--workspace), holds no PAT, and is invited:
    up.sh prints the approve link and waits for the box to pair;
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
STAGED = ["canopy/cloud-runner/claude-oauth-token"]
ROLE_ARN = "arn:aws:iam::123456789012:role/canopy-cloud-runner-test-RunnerRole-X"
INV_ID = "0b4f7f8e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
RID = "6f1c0d3e-0000-4000-8000-000000000001"
# A P-256 SubjectPublicKeyInfo, base64 — what `kms get-public-key` prints.
SPKI = ("MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE" + "A" * 86 + "==")

INSTANCE_CHANGE = {"Changes": [{"ResourceChange": {
    "Action": "Modify", "LogicalResourceId": "Instance", "Replacement": "True",
    "Details": [{"Target": {"Attribute": "Properties", "Name": "BlockDeviceMappings"}}]}}]}
SG_ONLY = {"Changes": [{"ResourceChange": {
    "Action": "Modify", "LogicalResourceId": "SecurityGroup", "Replacement": "False",
    "Details": [{"Target": {"Attribute": "Properties", "Name": "SecurityGroupIngress"}}]}}]}


@pytest.fixture
def canopy():
    """A canopy-web standing in for the token check, the workspace list, the
    runner list and the pairing invitation. `.code` is the token check's status;
    `.runners` the runner list; `.states` the invitation's status on each GET."""
    class H(http.server.BaseHTTPRequestHandler):
        code = 200
        runners: list = []
        states: list = ["pending", "consumed"]
        posted: list = []
        auth: list = []
        checked: list = []

        def _send(self, code, obj):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(obj).encode())

        def do_GET(self):  # noqa: N802
            H.auth.append(self.headers.get("Authorization", ""))
            if self.path.startswith("/api/harness/runners"):
                return self._send(H.code, H.runners if H.code == 200 else {"detail": "no"})
            if self.path.startswith("/api/workspaces"):
                return self._send(200, [{"slug": s} for s in ("dimagi", "connect", "canopy", "readonly")])
            if self.path.startswith("/api/harness/runner-invitations/check"):
                ws = self.path.split("workspace=", 1)[-1]
                H.checked.append(ws)
                if ws == "readonly":
                    return self._send(403, {"detail": "needs permission to run work there"})
                if ws in ("dimagi", "connect", "canopy"):
                    return self._send(200, {"workspace": ws})
                return self._send(404, {"detail": "not found"})
            if self.path.startswith(f"/api/harness/runner-invitations/{INV_ID}"):
                state = H.states.pop(0) if len(H.states) > 1 else H.states[0]
                return self._send(200, {"id": INV_ID, "name": "cloud-ec2-test", "status": state,
                                        "runner_id": RID if state == "consumed" else None})
            return self._send(404, {})

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            H.posted.append((self.path, body))
            if self.path == "/api/harness/runner-invitations/":
                return self._send(201, {"id": INV_ID, "status": "pending", "runner_id": None,
                                        "approve_url": f"https://canopy.example/w/{body['workspace']}"
                                                       f"/runner-invitations/{INV_ID}"})
            return self._send(404, {})

        def log_message(self, *a):
            pass

    H.runners, H.posted, H.auth, H.checked, H.states = [], [], [], [], ["pending", "consumed"]
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.handler = H
    yield srv
    srv.shutdown()


def _fake(tmp: pathlib.Path, *, stack_status=None, change_set=None, secrets=STAGED,
          old_parts=0, stack_workspace="dimagi", pat_secret="", ecdh_slots="",
          stack_units="", ecdh_read="", units_read="") -> pathlib.Path:
    state = tmp / "state"
    state.mkdir()
    (state / "calls").write_text("")
    cfg = {"stack_status": stack_status, "change_set": change_set, "secrets": list(secrets),
           "old_parts": old_parts, "stack_workspace": stack_workspace, "role_arn": ROLE_ARN, "spki": SPKI,
           "pat_secret": pat_secret, "ecdh_slots": ecdh_slots, "stack_units": stack_units,
           "ecdh_read": ecdh_read, "units_read": units_read}
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
elif "describe-stacks" in a and "RunnerWorkspace" in arg("--query"):
    print(cfg["stack_workspace"])
elif "describe-stacks" in a and "starts_with(ParameterKey" in arg("--query"):
    if cfg.get("ecdh_read") == "missing":
        print("An error occurred (ValidationError): Stack with id x-ecdh does not exist", file=sys.stderr)
        sys.exit(254)
    if cfg.get("ecdh_read") == "throttled":
        print("An error occurred (Throttling): Rate exceeded", file=sys.stderr)
        sys.exit(254)
    print(cfg["ecdh_slots"].replace(" ", chr(10)).replace("=", chr(9)))
elif "describe-stacks" in a and "RunnerUnits" in arg("--query"):
    if cfg.get("units_read") == "throttled":
        print("An error occurred (Throttling): Rate exceeded", file=sys.stderr)
        sys.exit(254)
    print(cfg["stack_units"] or "None")
elif "describe-stacks" in a and "CanopyTokenSecret" in arg("--query"):
    print(cfg["pat_secret"])
elif "describe-stacks" in a and "EcdhKeyArn" in arg("--query"):
    print("arn:aws:kms:us-east-1:123456789012:key/k-1")
elif "describe-stack-resource" in a:
    print("canopy-cloud-runner-test-RunnerRole-X")
elif "get-role" in a:
    print(cfg["role_arn"])
elif "get-public-key" in a:
    print(cfg["spki"])
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
    # Only the public-IP lookup is faked; canopy-web calls go to the local server.
    (bin_ / "curl").write_text('#!/bin/sh\ncase "$*" in *checkip*) echo 1.2.3.4 ;; '
                               '*) exec /usr/bin/curl "$@" ;; esac\n')
    (bin_ / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    return state


def _up(tmp, canopy, *args, token_text: str | None = "t\n") -> subprocess.CompletedProcess:
    home = tmp / "home"
    (home / ".claude" / "canopy").mkdir(parents=True, exist_ok=True)
    if token_text is not None:
        (home / ".claude" / "canopy" / "workbench-token").write_text(token_text)
    base = f"http://127.0.0.1:{canopy.server_address[1]}"
    # Each run's key file in its OWN dir: under xdist several tests run up.sh at
    # once with the same stack name, and a shared ./<stack>-key.pem was written,
    # chmod'ed and deleted by different workers mid-run (a `set -e` exit, 2026-10-17).
    keys = tmp / "keys"
    keys.mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(SCRIPT), "--base-url", base, "--allow-dirty", *args],
        capture_output=True, text=True, timeout=120, cwd=str(EC2),
        env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
             "HOME": str(home), "KEY_DIR": str(keys)})


def _calls(state):
    return (state / "calls").read_text().splitlines()


def test_a_named_standby_box_creates_its_own_stack(tmp_path, canopy):
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "connect", "--standby")
    assert res.returncode == 0, res.stdout + res.stderr
    deploy = next(c for c in _calls(state) if "cloudformation deploy" in c)
    assert "--stack-name canopy-cloud-runner-test" in deploy
    for p in ("RunnerName=cloud-ec2-test", "RunnerWorkspace=connect", "RunnerProjects=", "RunnerSessions=0"):
        assert f" {p} " in deploy + " ", (p, deploy)
    assert "--no-execute-changeset" not in deploy, "a CREATE has nothing to review"


def test_the_seed_is_parts_then_manifest_and_stale_parts_go(tmp_path, canopy):
    state = _fake(tmp_path, old_parts=6)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "dimagi")
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
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "dimagi")
    assert res.returncode != 0 and missing in res.stderr and "secrets.sh" in res.stderr
    assert not any("put-secret" in c or "create-secret" in c or "deploy" in c
                   for c in _calls(state))


def test_no_canopy_pat_secret_is_needed(tmp_path, canopy):
    """A new box pairs by invitation and holds no PAT, so up.sh neither checks
    for canopy/cloud-runner/canopy-pat nor reads it."""
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "dimagi")
    assert res.returncode == 0, res.stdout + res.stderr
    assert not any("canopy-pat" in c for c in _calls(state))


@pytest.mark.parametrize("token", ["dead", "missing"])
def test_dead_workbench_token_stops_before_aws_writes(tmp_path, canopy, token):
    state = _fake(tmp_path)
    if token == "dead":
        canopy.handler.code = 401
        res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "dimagi")
    else:
        res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "dimagi", token_text=None)
    assert res.returncode != 0
    assert "workbench token" in res.stderr and "/canopy:canopy-web-pat-mint" in res.stderr
    assert _calls(state) == [], "nothing at all was asked of AWS"


def test_new_stack_without_workspace_refuses(tmp_path, canopy):
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test")
    assert res.returncode != 0
    assert "--workspace" in res.stderr and "dimagi connect" in res.stderr, res.stderr
    assert not any("put-secret" in c or "create-secret" in c or "deploy" in c for c in _calls(state))


def test_a_workspace_you_are_not_in_is_refused(tmp_path, canopy):
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "elsewhere")
    assert res.returncode != 0 and "not a member" in res.stderr
    assert not any("deploy" in c for c in _calls(state))


def test_a_workspace_you_cannot_run_work_in_is_refused_before_aws_writes(tmp_path, canopy):
    """Membership is not enough: the invitation needs agent.work, and its gates
    are asked (GET …/runner-invitations/check) before the stack is made."""
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "readonly")
    assert res.returncode != 0 and "may not run work" in res.stderr
    assert canopy.handler.checked == ["readonly"]
    assert not any("put-secret" in c or "create-secret" in c or "deploy" in c for c in _calls(state))


def test_an_old_stack_with_a_pat_is_not_invited(tmp_path, canopy):
    """Its box has CANOPY_TOKEN and pairs the old way; waiting for an invitation
    it will never use would hang up.sh for half an hour."""
    _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=SG_ONLY, stack_workspace="connect",
          pat_secret="canopy/cloud-runner/canopy-pat")
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test")
    assert res.returncode == 0, res.stdout + res.stderr
    assert not canopy.handler.posted
    assert "still names a PAT secret" in res.stdout


def test_creates_invitation_and_prints_approve_url(tmp_path, canopy):
    import base64

    _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "connect")
    assert res.returncode == 0, res.stdout + res.stderr
    (path, body), = [p for p in canopy.handler.posted if p[0] == "/api/harness/runner-invitations/"]
    assert body["name"] == "cloud-ec2-test" and body["workspace"] == "connect"
    assert body["role_arn"] == ROLE_ARN
    # The -ecdh stack's KMS public key, as a JWK: the one approval covers it.
    der = base64.b64decode(SPKI)
    b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    assert body["ecdh_public_jwk"] == {"kty": "EC", "crv": "P-256", "x": b64u(der[-64:-32]), "y": b64u(der[-32:])}
    assert f"approve: https://canopy.example/w/connect/runner-invitations/{INV_ID}" in res.stdout
    assert f"paired: runner {RID}" in res.stdout
    saved = json.loads((tmp_path / "keys" / "canopy-cloud-runner-test-invitation.json").read_text())
    assert saved == {"id": INV_ID, "runner_id": RID}
    assert all(a == "Bearer t" for a in canopy.handler.auth), "up.sh speaks as the operator"


def test_an_invitation_that_expires_says_rerun(tmp_path, canopy):
    canopy.handler.states = ["pending", "expired"]
    _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "connect")
    assert res.returncode != 0 and "expired" in res.stderr and "up.sh" in res.stderr


def test_update_keeps_existing_workspace(tmp_path, canopy):
    canopy.handler.runners = [{"id": RID, "kind": "cloud", "name": "cloud-ec2-test", "status": "online"}]
    state = _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=SG_ONLY, stack_workspace="connect")
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test")
    assert res.returncode == 0, res.stdout + res.stderr
    deploy = next(c for c in _calls(state) if "cloudformation deploy" in c and "runner.cfn.yaml" in c)
    assert " RunnerWorkspace=connect " in deploy + " ", deploy
    # Its runner exists, so a replaced instance re-attaches by its role: no invitation.
    assert not canopy.handler.posted
    assert "already paired" in res.stdout


def test_an_update_cannot_move_the_box_to_another_workspace(tmp_path, canopy):
    state = _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=SG_ONLY, stack_workspace="connect")
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "dimagi")
    assert res.returncode != 0 and "keeps its workspace" in res.stderr
    assert not any("deploy" in c for c in _calls(state))


def test_an_existing_stack_whose_box_never_paired_is_invited(tmp_path, canopy):
    _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=SG_ONLY, stack_workspace="connect")
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test")
    assert res.returncode == 0, res.stdout + res.stderr
    (path, body), = [p for p in canopy.handler.posted if p[0] == "/api/harness/runner-invitations/"]
    assert body["workspace"] == "connect"


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


# ── --test: a directed-only box that serves nothing until a chat is put on it ──

def _deploy(state):
    return next(c for c in _calls(state) if "cloudformation deploy" in c) + " "


def test_up_test_mode_params(tmp_path, canopy):
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-fork", "--test", "--workspace", "canopy")
    assert res.returncode == 0, res.stdout + res.stderr
    deploy = _deploy(state)
    for p in ("RunnerName=cloud-ec2-fork", "RunnerDirected=1", "RunnerSessions=1", "RunnerAgents=",
              "RunnerProjects=canopy-web", "RunnerWorkspace=canopy"):
        assert f" {p} " in deploy, (p, deploy)
    assert "./wire.sh --name cloud-ec2-fork --test" in res.stdout


def test_up_test_mode_takes_projects(tmp_path, canopy):
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-fork", "--test", "--workspace", "canopy",
              "--projects", "canopy-web,connect-labs")
    assert res.returncode == 0, res.stdout + res.stderr
    assert " RunnerProjects=canopy-web,connect-labs " in _deploy(state)


def test_up_test_refuses_live_name(tmp_path, canopy):
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--test", "--workspace", "canopy")
    assert res.returncode != 0 and "cloud-ec2-1" in res.stderr
    assert not any("deploy" in c or "put-secret" in c or "create-secret" in c for c in _calls(state))


def test_up_test_needs_a_workspace(tmp_path, canopy):
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-fork", "--test")
    assert res.returncode != 0 and "--workspace" in res.stderr
    assert not any("deploy" in c for c in _calls(state))


def test_up_test_and_standby_do_not_mix(tmp_path, canopy):
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-fork", "--test", "--standby",
              "--workspace", "canopy")
    assert res.returncode != 0
    assert not any("deploy" in c for c in _calls(state))


def test_up_workspace_names_where_the_box_pairs(tmp_path, canopy):
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "canopy")
    assert res.returncode == 0, res.stdout + res.stderr
    assert " RunnerWorkspace=canopy " in _deploy(state)
    assert "RunnerDirected" not in _deploy(state)


# ---- isolation units (spec 2026-10-19-runner-isolation-units, plan Task 6.6) -----

def test_up_units_invites_every_unit_in_one_invitation(tmp_path, canopy):
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "dimagi",
              "--units", "tree:dimagi,tree:connect,agent:eva")
    assert res.returncode == 0, res.stdout + res.stderr
    assert " RunnerUnits=tree:dimagi,tree:connect,agent:eva " in _deploy(state)
    ecdh = next(c for c in _calls(state) if "cloudformation deploy" in c and "-ecdh" in c)
    for p in ("Unit1=connect", "Unit2=agent-eva", "Unit3=", "Unit4="):
        assert f" {p}" in ecdh + " ", (p, ecdh)
    # Each extra unit's key is read by the alias the -ecdh stack gives it.
    assert any("get-public-key --key-id alias/canopy-runner-ecdh/canopy-cloud-runner-test-RunnerRole-X/agent-eva"
               in c for c in _calls(state))
    [(_, body)] = [(p, b) for p, b in canopy.handler.posted if p == "/api/harness/runner-invitations/"]
    assert body["name"] == "cloud-ec2-test"
    assert [(u["name"], u["workspace"], u["kind"], u["slug"]) for u in body["units"]] == [
        ("cloud-ec2-test", "dimagi", "tree", ""), ("cloud-ec2-test-connect", "connect", "tree", ""),
        ("cloud-ec2-test-agent-eva", "dimagi", "agent", "eva")]
    assert all("ecdh_public_jwk" in u for u in body["units"])
    # Every unit workspace passed the invitation's gates before AWS was touched.
    assert {"dimagi", "connect"} <= set(canopy.handler.checked)


def test_up_units_refuses_a_workspace_you_cannot_run_work_in(tmp_path, canopy):
    state = _fake(tmp_path)
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "dimagi",
              "--units", "tree:dimagi,tree:readonly")
    assert res.returncode != 0 and "readonly" in res.stderr
    assert not any("deploy" in c for c in _calls(state))


def test_up_units_in_place_leaves_the_runner_stack_alone(tmp_path, canopy):
    """Moving a live box to units: the KMS keys and the invitation, never an
    instance stop/start or replacement — the box moves itself (--install-units)."""
    state = _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=INSTANCE_CHANGE, stack_workspace="connect")
    canopy.handler.runners = [{"kind": "cloud", "name": "cloud-ec2-test", "status": "online"}]
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--units", "tree:connect,agent:eva@connect",
              "--in-place")
    assert res.returncode == 0, res.stdout + res.stderr
    deploys = [c for c in _calls(state) if "cloudformation deploy" in c]
    assert deploys and all("-ecdh" in c for c in deploys), deploys
    assert not any("execute-change-set" in c for c in _calls(state))
    assert "--install-units" in res.stdout and "RUNNER_UNITS=tree:connect,agent:eva@connect" in res.stdout
    [(_, body)] = [(p, b) for p, b in canopy.handler.posted if p == "/api/harness/runner-invitations/"]
    assert [u["name"] for u in body["units"]] == ["cloud-ec2-test", "cloud-ec2-test-agent-eva"]



# ---- unit key slots are pinned (review: HIGH for units) ------------------------------

def _ecdh_deploy(state):
    return next(c for c in _calls(state) if "cloudformation deploy" in c and "-ecdh" in c) + " "


def test_a_plain_rerun_keeps_every_unit_key_slot(tmp_path, canopy):
    """`./up.sh --name cloud-ec2-2` after a move must not blank Unit1..4: that would
    remove the units' KMS keys and break every wrap to them."""
    state = _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=SG_ONLY, stack_workspace="connect",
                  ecdh_slots="Unit1=marsh Unit2=agent-eva Unit3= Unit4=")
    canopy.handler.runners = [{"kind": "cloud", "name": "cloud-ec2-test", "status": "online"}]
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test")
    assert res.returncode == 0, res.stdout + res.stderr
    ecdh = _ecdh_deploy(state)
    for p in ("Unit1=marsh", "Unit2=agent-eva", "Unit3=", "Unit4="):
        assert f" {p} " in ecdh, (p, ecdh)


def test_a_reordered_units_list_keeps_each_unit_in_its_slot(tmp_path, canopy):
    state = _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=SG_ONLY, stack_workspace="connect",
                  ecdh_slots="Unit1=dimagi Unit2=agent-eva Unit3= Unit4=")
    canopy.handler.runners = [{"kind": "cloud", "name": n, "status": "online"} for n in
                              ("cloud-ec2-test", "cloud-ec2-test-dimagi", "cloud-ec2-test-agent-eva",
                               "cloud-ec2-test-canopy")]
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--units",
              "tree:connect,tree:canopy,agent:eva@connect,tree:dimagi")
    assert res.returncode == 0, res.stdout + res.stderr
    ecdh = _ecdh_deploy(state)
    for p in ("Unit1=dimagi", "Unit2=agent-eva", "Unit3=canopy", "Unit4="):
        assert f" {p} " in ecdh, (p, ecdh)


def test_dropping_a_unit_needs_remove_unit(tmp_path, canopy):
    state = _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=SG_ONLY, stack_workspace="connect",
                  ecdh_slots="Unit1=dimagi Unit2=agent-eva Unit3= Unit4=")
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--units", "tree:connect,agent:eva@connect")
    assert res.returncode != 0 and "--remove-unit dimagi" in res.stderr
    assert not any("-ecdh" in c and "deploy" in c for c in _calls(state))
    canopy.handler.runners = [{"kind": "cloud", "name": n, "status": "online"} for n in
                              ("cloud-ec2-test", "cloud-ec2-test-agent-eva")]
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--units", "tree:connect,agent:eva@connect",
              "--remove-unit", "dimagi")
    assert res.returncode == 0, res.stdout + res.stderr
    ecdh = _ecdh_deploy(state)
    assert " Unit1= " in ecdh and " Unit2=agent-eva " in ecdh


def test_a_plain_rerun_keeps_the_stacks_units(tmp_path, canopy):
    state = _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=SG_ONLY, stack_workspace="connect",
                  stack_units="tree:connect,agent:eva@connect", ecdh_slots="Unit1=agent-eva")
    canopy.handler.runners = [{"kind": "cloud", "name": n, "status": "online"} for n in
                              ("cloud-ec2-test", "cloud-ec2-test-agent-eva")]
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test")
    assert res.returncode == 0, res.stdout + res.stderr
    assert " RunnerUnits=tree:connect,agent:eva@connect " in _deploy(state)


def test_in_place_always_invites_because_adopting_needs_approval(tmp_path, canopy):
    _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=INSTANCE_CHANGE, stack_workspace="connect")
    canopy.handler.runners = [{"kind": "cloud", "name": "cloud-ec2-test", "status": "online"}]
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--units", "tree:connect", "--in-place")
    assert res.returncode == 0, res.stdout + res.stderr
    assert any(p == "/api/harness/runner-invitations/" for p, _ in canopy.handler.posted)



def test_a_failed_slot_read_aborts_before_anything_is_deployed(tmp_path, canopy):
    """A transient AWS error must never read as "no slots" — that would blank
    Unit1..4 and remove the units' key aliases on the next -ecdh deploy."""
    state = _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=SG_ONLY, stack_workspace="connect",
                  ecdh_slots="Unit1=marsh", ecdh_read="throttled")
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test")
    assert res.returncode != 0 and "unit key slots" in res.stderr and "Throttling" in res.stderr
    assert not any("cloudformation deploy" in c for c in _calls(state))


def test_a_failed_units_read_aborts(tmp_path, canopy):
    state = _fake(tmp_path, stack_status="UPDATE_COMPLETE", change_set=SG_ONLY, stack_workspace="connect",
                  units_read="throttled")
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test")
    assert res.returncode != 0 and "RunnerUnits" in res.stderr
    assert not any("cloudformation deploy" in c for c in _calls(state))


def test_a_missing_ecdh_stack_means_no_slots_yet(tmp_path, canopy):
    state = _fake(tmp_path, ecdh_read="missing")
    res = _up(tmp_path, canopy, "--name", "cloud-ec2-test", "--workspace", "dimagi",
              "--units", "tree:dimagi,tree:connect")
    assert res.returncode == 0, res.stdout + res.stderr
    assert " Unit1=connect " in _ecdh_deploy(state)
