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
#: When the fake canopy-web says the bundle was written (the POST's updated_at).
STAGED_AT = "2026-10-19T12:00:00+00:00"
LIVE = "22222222-2222-2222-2222-222222222222"
OTHER = "33333333-3333-3333-3333-333333333333"


def _setup(tmp: pathlib.Path, runners: list[dict], statuses: dict | None = None) -> pathlib.Path:
    """`statuses` maps a runner id to its GET /credential/status body."""
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
STAGED_AT = {STAGED_AT!r}
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
    rid = path.split("/")[-3]
    statuses = json.loads((S / "statuses.json").read_text())
    print(json.dumps(statuses.get(rid) or {{"has_claude_token": True, "claude_token_label": "ACE",
                                             "claude_token_secondary_label": "JJ"}}))
elif method == "POST" and path.endswith("/credential"):
    (S / "posted.json").write_text(pathlib.Path(a[a.index("--data") + 1][1:]).read_text())
    print(json.dumps({{"has_claude_token": True, "updated_at": STAGED_AT}}))
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
            "ready": True, "paused": False, **over}


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


# ── the default source is the live box, not a name (cloud-ec2-fork, 2026-10-18) ──

def test_default_source_is_the_live_ready_box(tmp_path):
    state = _setup(tmp_path, [
        _row(NEW, "cloud-ec2-fork", None),
        _row(OTHER, "cloud-ec2-1", "2026-09-01T00:00:00Z", status="offline"),
        _row(LIVE, "cloud-ec2-2", "2026-10-18T12:00:00Z"),
    ])
    res = _wire(tmp_path, "--name", "cloud-ec2-fork")
    assert res.returncode == 0, res.stdout + res.stderr
    calls = (state / "calls").read_text()
    assert f"GET /api/harness/runners/{LIVE}/credential" in calls
    assert f"/api/harness/runners/{OTHER}/credential" not in calls
    assert "from: runner cloud-ec2-2" in res.stdout
    assert "the live cloud box" in res.stdout
    assert "copied: Token 1 (ACE), Token 2 (JJ)" in res.stdout


def test_same_name_predecessor_still_wins(tmp_path):
    state = _setup(tmp_path, [
        _row(NEW, "cloud-ec2-fork", None),
        _row(LIVE, "cloud-ec2-2", "2026-10-18T12:00:00Z"),
        _row(OTHER, "cloud-ec2-fork", "2026-10-01T12:00:00Z", status="offline"),
    ])
    res = _wire(tmp_path, "--name", "cloud-ec2-fork")
    assert json.loads((state / "posted.json").read_text())["claude_token"] == "tok-3333"
    assert "the predecessor this box replaces" in res.stdout


def test_paused_or_not_ready_box_is_skipped(tmp_path):
    state = _setup(tmp_path, [
        _row(NEW, "cloud-ec2-fork", None),
        _row(LIVE, "cloud-ec2-2", "2026-10-18T12:00:00Z", paused=True, status="paused"),
        _row(OTHER, "cloud-ec2-3", "2026-10-18T12:00:00Z", ready=False),
    ])
    res = _wire(tmp_path, "--name", "cloud-ec2-fork")
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.loads((state / "posted.json").read_text()) == {"claude_token": "secrets-manager-token"}


def test_prints_logins_that_did_not_copy(tmp_path):
    _setup(tmp_path, [_row(NEW, "cloud-ec2-fork", None), _row(LIVE, "cloud-ec2-2")], statuses={
        LIVE: {"has_claude_token": True, "claude_token_label": "ACE",
               "logins": [{"key": "default", "account": "ace@dimagi-ai.com", "signed_in": True}]}})
    res = _wire(tmp_path, "--name", "cloud-ec2-fork")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ace@dimagi-ai.com" in res.stdout and "stay on the box" in res.stdout
    assert f"/supervisor?tab=runners&runner={NEW}" in res.stdout
