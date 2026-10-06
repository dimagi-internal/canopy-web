"""wire.sh stages the fleet's CURRENT Claude logins, not a months-old bootstrap token.

Found building a second box on 2026-10-04: wire.sh staged the new runner from
Secrets Manager's canopy/cloud-runner/claude-oauth-token (set 2026-07-21), which
had hit its weekly usage cap — every drill failed — while cloud-ec2-1 ran on two
newer logins an operator had set on the runner page. The logins live in
canopy-web; wire.sh now copies the whole bundle from a live runner, and only
falls back to Secrets Manager when there is none.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "wire.sh"
NEW = "11111111-1111-1111-1111-111111111111"
LIVE = "22222222-2222-2222-2222-222222222222"
OTHER = "33333333-3333-3333-3333-333333333333"


def _setup(tmp: pathlib.Path, runners: list[dict]) -> pathlib.Path:
    state = tmp / "state"
    state.mkdir()
    (state / "calls").write_text("")
    (state / "runners.json").write_text(json.dumps(runners))
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
if path == "/api/harness/runners/":
    print((S / "runners.json").read_text())
elif method == "GET" and path.endswith("/credential"):
    rid = path.split("/")[-2]
    print(json.dumps({{"claude_token": "tok-" + rid[:4], "claude_token_secondary": "sec-" + rid[:4],
                       "claude_api_key": ""}}))
elif method == "GET" and path.endswith("/credential/status"):
    print(json.dumps({{"claude_token_label": "ACE", "claude_token_secondary_label": "JJ"}}))
elif method == "POST" and path.endswith("/credential"):
    (S / "posted.json").write_text(pathlib.Path(a[a.index("--data") + 1][1:]).read_text())
    print("{{}}")
elif path.startswith("/api/agents/?"):
    print(json.dumps({{"items": []}}))
elif path == "/api/workspaces/" or path.endswith("/runners"):
    print("[]")
else:
    print("{{}}")
''')
    (bin_ / "aws").write_text("#!/bin/sh\necho secrets-manager-token\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    return state


def _wire(tmp, *args):
    home = tmp / "home"
    (home / ".claude" / "canopy").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "canopy" / "workbench-token").write_text("t\n")
    return subprocess.run(["bash", str(SCRIPT), "--runner-id", NEW, *args],
                          capture_output=True, text=True, timeout=120, cwd=str(SCRIPT.parent),
                          env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "HOME": str(home)})


def _row(rid, name, hb="2026-10-04T12:00:00Z"):
    return {"id": rid, "kind": "cloud", "name": name, "status": "online", "last_heartbeat_at": hb}


def test_a_second_box_copies_the_live_boxs_whole_bundle(tmp_path):
    state = _setup(tmp_path, [_row(NEW, "cloud-ec2-test", None), _row(LIVE, "cloud-ec2-1")])
    res = _wire(tmp_path, "--name", "cloud-ec2-test")
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.loads((state / "posted.json").read_text()) == {
        "claude_token": "tok-2222", "claude_token_secondary": "sec-2222",
        "claude_token_label": "ACE", "claude_token_secondary_label": "JJ"}
    assert "tok-2222" not in res.stdout + res.stderr, "a secret reached the terminal"


def test_a_replacement_copies_its_predecessor(tmp_path):
    state = _setup(tmp_path, [_row(NEW, "cloud-ec2-1", None), _row(OTHER, "cloud-ec2-9"),
                              _row(LIVE, "cloud-ec2-1")])
    _wire(tmp_path)
    assert json.loads((state / "posted.json").read_text())["claude_token"] == "tok-2222"


def test_credential_from_names_the_source(tmp_path):
    state = _setup(tmp_path, [_row(NEW, "cloud-ec2-test", None), _row(LIVE, "cloud-ec2-1"),
                              _row(OTHER, "cloud-ec2-9")])
    _wire(tmp_path, "--name", "cloud-ec2-test", "--credential-from", "cloud-ec2-9")
    assert json.loads((state / "posted.json").read_text())["claude_token"] == "tok-3333"


def test_an_unknown_source_is_an_error_not_a_silent_fallback(tmp_path):
    state = _setup(tmp_path, [_row(NEW, "cloud-ec2-test", None)])
    res = _wire(tmp_path, "--name", "cloud-ec2-test", "--credential-from", "nope")
    assert res.returncode != 0 and "nope" in res.stderr
    assert not (state / "posted.json").exists()


def test_no_runner_to_copy_falls_back_to_secrets_manager(tmp_path):
    state = _setup(tmp_path, [_row(NEW, "cloud-ec2-test", None)])
    res = _wire(tmp_path, "--name", "cloud-ec2-test")
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.loads((state / "posted.json").read_text()) == {"claude_token": "secrets-manager-token"}
    assert "Secrets Manager" in res.stdout
