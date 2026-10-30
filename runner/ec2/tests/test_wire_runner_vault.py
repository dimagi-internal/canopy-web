"""wire.sh never holds a Claude credential: it gives the new box a vault and says where
its owner signs it in (canopy-web spec 2026-10-23-runner-claude-credentials-in-the-vault-design.md).

It used to copy a live box's whole bundle — tokens and API key, in plaintext, through
this script — and fall back to a months-old Secrets Manager token. A box's credentials
now live in a vault only its own device key opens, so there is nothing to copy: the box
gets its own vault (or, with --vault, shares one inside its isolation boundary), and its
owner makes the key and signs it in on the runner's Credentials tab.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "wire.sh"
NEW = "11111111-1111-1111-1111-111111111111"
LIVE = "22222222-2222-2222-2222-222222222222"
OTHER = "33333333-3333-3333-3333-333333333333"
VAULT = "44444444-4444-4444-4444-444444444444"


def _setup(tmp: pathlib.Path, runners: list[dict], statuses: dict | None = None) -> pathlib.Path:
    """`statuses` maps a runner id to its GET /credential/status body. The fake
    records the vault it was asked to make (`vaulted.json`) or attach (`attached.json`)."""
    state = tmp / "state"
    state.mkdir()
    (state / "calls").write_text("")
    (state / "runners.json").write_text(json.dumps(runners))
    (state / "statuses.json").write_text(json.dumps(statuses or {}))
    bin_ = tmp / "bin"
    bin_.mkdir()
    (bin_ / "curl").write_text(f'''#!/usr/bin/env python3
import json, sys, pathlib
S = pathlib.Path({str(state)!r})
a = sys.argv[1:]
method = a[a.index("-X") + 1] if "-X" in a else "GET"
url = next(x for x in a if x.startswith("http"))
path = "/" + url.split("://", 1)[1].split("/", 1)[1]
with (S / "calls").open("a") as fh:
    fh.write(method + " " + path + chr(10))
def body():
    return pathlib.Path(a[a.index("--data") + 1][1:]).read_text() if "--data" in a else "{{}}"
if path == "/api/harness/runners/":
    print((S / "runners.json").read_text())
elif method == "GET" and path.endswith("/credential/status"):
    rid = path.split("/")[-3]
    statuses = json.loads((S / "statuses.json").read_text())
    print(json.dumps(statuses.get(rid) or {{"has_claude_token": True}}))
elif method == "POST" and path.endswith("/vault"):
    (S / "vaulted.json").write_text(json.dumps({{"runner": path.split("/")[-2]}}))
    print(json.dumps({{"vault_id": "v-own", "name": "box Claude credentials", "kv": 0}}))
elif method == "POST" and path.startswith("/api/vaults/") and path.endswith("/attachments"):
    (S / "attached.json").write_text(json.dumps({{"vault": path.split("/")[3], **json.loads(body())}}))
    print("{{}}")
elif method == "GET" and path.startswith("/api/harness/runners/") and path.count("/") == 5:
    print(json.dumps({{"id": path.split("/")[-2], "workspace": "dimagi"}}))
elif path.startswith("/api/agents/?"):
    print(json.dumps({{"items": []}}))
elif path == "/api/workspaces/" or path.endswith("/runners"):
    print("[]")
else:
    print("{{}}")
''')
    # No AWS at all: there is no Secrets Manager fallback to reach for any more.
    (bin_ / "aws").write_text("#!/bin/sh\necho aws-was-called >&2\nexit 9\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    return state


def _wire(tmp, *args, runner_id=NEW, probe_minutes="0"):
    home = tmp / "home"
    (home / ".claude" / "canopy").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "canopy" / "workbench-token").write_text("t\n")
    lead = ["--probe-minutes", probe_minutes] + (["--runner-id", runner_id] if runner_id else [])
    return subprocess.run(["bash", str(SCRIPT), *lead, *args],
                          capture_output=True, text=True, timeout=120, cwd=str(SCRIPT.parent),
                          env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "HOME": str(home),
                               "WIRE_POLL_SECONDS": "0"})


def _row(rid, name, hb="2026-10-04T12:00:00Z", **over):
    return {"id": rid, "kind": "cloud", "name": name, "status": "online", "last_heartbeat_at": hb,
            "ready": True, "paused": False, "workspace": "dimagi", **over}


def test_a_new_box_gets_its_own_vault_and_no_credential_passes_through(tmp_path):
    state = _setup(tmp_path, [_row(NEW, "cloud-ec2-test", None), _row(LIVE, "cloud-ec2-1")])
    res = _wire(tmp_path, "--name", "cloud-ec2-test")
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.loads((state / "vaulted.json").read_text()) == {"runner": NEW}
    calls = (state / "calls").read_text()
    # Nothing read a credential bundle, and nothing wrote one.
    assert "/credential\n" not in calls
    assert "aws-was-called" not in res.stderr
    assert f"/w/dimagi/runners/{NEW}/credentials" in res.stdout
    assert "Make its key" in res.stdout


def test_a_shared_vault_is_attached_instead(tmp_path):
    state = _setup(tmp_path, [_row(NEW, "cloud-ec2-test", None)])
    res = _wire(tmp_path, "--name", "cloud-ec2-test", "--vault", VAULT)
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.loads((state / "attached.json").read_text()) == {"vault": VAULT, "runner": NEW}
    assert not (state / "vaulted.json").exists()


def test_credentials_are_never_copied_from_another_box(tmp_path):
    state = _setup(tmp_path, [_row(NEW, "cloud-ec2-test", None), _row(LIVE, "cloud-ec2-1")])
    res = _wire(tmp_path, "--name", "cloud-ec2-test", "--credential-from", "cloud-ec2-1")
    assert res.returncode == 2
    assert "never copied" in res.stderr
    assert not (state / "vaulted.json").exists()
