"""`down.sh` leaves nothing behind in canopy-web — and nothing of anyone else's gone.

Until 2026-10-04 teardown deleted the stack and stopped: the runner row stayed,
with every agent assignment, source rule and workspace-order entry still naming
a box that no longer existed (the README called that "retired the next time
wire.sh stands up a replacement" — i.e. never, for a box nobody replaces).

Now it retires the row, which drops those routes server-side, and verifies none
remain. The guards are the point of these tests:
  * only rows that went SILENT are retired — a same-name row still heartbeating
    is a different box (a replacement already swapped in) and is left alone;
  * --keep-runner leaves the row, for a recycle where wire.sh must copy its rows;
  * --purge-secrets is refused while another runner stack still reads them
    (they are shared; the live box re-reads them on every start).
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "down.sh"
GONE = "11111111-1111-1111-1111-111111111111"     # this stack's box: silent
FRESH = "22222222-2222-2222-2222-222222222222"    # same name, still heartbeating
OTHER = "33333333-3333-3333-3333-333333333333"    # a different runner


def _iso(seconds_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)).isoformat()


def _setup(tmp: pathlib.Path, *, other_stacks=(), with_fresh=False) -> pathlib.Path:
    state = tmp / "state"
    state.mkdir()
    (state / "calls").write_text("")
    (state / "retired").write_text("")
    runners = [
        {"id": GONE, "kind": "cloud", "name": "cloud-ec2-test", "status": "online",
         "last_heartbeat_at": _iso(600)},
        {"id": OTHER, "kind": "cloud", "name": "cloud-ec2-1", "status": "online",
         "last_heartbeat_at": _iso(5)},
    ]
    if with_fresh:
        runners.append({"id": FRESH, "kind": "cloud", "name": "cloud-ec2-test",
                        "status": "online", "last_heartbeat_at": _iso(5)})
    (state / "runners.json").write_text(json.dumps(runners))
    (state / "rows.json").write_text(json.dumps([
        {"runner_id": OTHER, "enabled": True}, {"runner_id": GONE, "enabled": False}]))
    (state / "stacks").write_text("\n".join(["canopy-cloud-runner-test", *other_stacks]))
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
retired = set((S / "retired").read_text().split())
if path == "/api/harness/runners/":
    print(json.dumps([r for r in json.loads((S / "runners.json").read_text()) if r["id"] not in retired]))
elif path.endswith("/retire"):
    rid = path.split("/")[-2]
    (S / "retired").write_text((S / "retired").read_text() + rid + chr(10))
    print(json.dumps({{"runner": "cloud-ec2-test", "dropped_routes": [
        {{"agent": "ada", "source": "", "actor": ""}}]}}))
elif path.startswith("/api/agents/?"):
    print(json.dumps({{"items": [{{"slug": "ada"}}]}}))
elif path.endswith("/runners"):
    print(json.dumps([r for r in json.loads((S / "rows.json").read_text()) if r["runner_id"] not in retired]))
else:
    print("[]")
''')
    (bin_ / "aws").write_text(f'''#!/usr/bin/env python3
import sys, pathlib
S = pathlib.Path({str(state)!r})
a = sys.argv[1:]
with (S / "calls").open("a") as fh:
    fh.write("aws " + " ".join(x for x in a if not x.startswith("--profile") and x not in ("labs",)) + chr(10))
stacks = [s for s in (S / "stacks").read_text().split() if s]
if "describe-stacks" in a:
    name = a[a.index("--stack-name") + 1]
    sys.exit(0 if name in stacks else 255)
if "list-stacks" in a:
    print(chr(9).join(stacks))
if "list-secrets" in a:
    print("canopy/cloud-runner/runner-seed-part-0" + chr(9) + "canopy/cloud-runner/runner-seed-part-1")
''')
    (bin_ / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    return state


def _down(tmp: pathlib.Path, *args: str, token=True) -> subprocess.CompletedProcess:
    home = tmp / "home"
    (home / ".claude" / "canopy").mkdir(parents=True, exist_ok=True)
    if token:
        (home / ".claude" / "canopy" / "workbench-token").write_text("t0ken\n")
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          timeout=120, cwd=str(SCRIPT.parent),
                          env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "HOME": str(home)})


def _calls(state):
    return (state / "calls").read_text().splitlines()


def test_teardown_deletes_the_derived_stack_then_retires_the_silent_row(tmp_path):
    state = _setup(tmp_path)
    pem = SCRIPT.parent / "canopy-cloud-runner-test-key.pem"
    pem.write_text("k")
    try:
        res = _down(tmp_path, "--name", "cloud-ec2-test")
    finally:
        existed = pem.exists()
        pem.unlink(missing_ok=True)
    assert res.returncode == 0, res.stdout + res.stderr
    calls = _calls(state)
    delete = next(i for i, c in enumerate(calls) if "delete-stack" in c)
    retire = next(i for i, c in enumerate(calls) if c.endswith("/retire"))
    assert "--stack-name canopy-cloud-runner-test" in calls[delete]
    assert delete < retire, "retire AFTER the box is gone, or it re-pairs as a new row"
    assert (state / "retired").read_text().split() == [GONE]
    assert "no assignment rows or rules name it" in res.stdout
    assert not existed, "the stack's SSH key must be removed"


def test_a_same_name_row_still_heartbeating_is_left_alone(tmp_path):
    state = _setup(tmp_path, with_fresh=True)
    res = _down(tmp_path, "--name", "cloud-ec2-test")
    assert res.returncode == 0, res.stdout + res.stderr
    assert (state / "retired").read_text().split() == [GONE]
    assert FRESH in res.stdout and "left alone" in res.stdout


def test_keep_runner_leaves_the_row_for_a_recycle(tmp_path):
    state = _setup(tmp_path)
    res = _down(tmp_path, "--name", "cloud-ec2-test", "--keep-runner", token=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert not any(c.endswith("/retire") for c in _calls(state))
    assert any("delete-stack" in c for c in _calls(state))


def test_no_token_fails_before_anything_is_deleted(tmp_path):
    state = _setup(tmp_path)
    res = _down(tmp_path, "--name", "cloud-ec2-test", token=False)
    assert res.returncode != 0 and "workbench-token" in res.stderr
    assert not any("delete-stack" in c for c in _calls(state))


def test_purge_is_refused_while_another_runner_stack_reads_the_secrets(tmp_path):
    state = _setup(tmp_path, other_stacks=["canopy-cloud-runner"])
    res = _down(tmp_path, "--name", "cloud-ec2-test", "--purge-secrets")
    assert res.returncode != 0 and "canopy-cloud-runner" in res.stderr
    calls = _calls(state)
    assert not any("delete-stack" in c or "delete-secret" in c for c in calls), calls


def test_purge_deletes_the_seed_parts_by_prefix(tmp_path):
    state = _setup(tmp_path)
    res = _down(tmp_path, "--name", "cloud-ec2-test", "--purge-secrets")
    assert res.returncode == 0, res.stdout + res.stderr
    deleted = [c for c in _calls(state) if "delete-secret" in c]
    assert any("runner-seed-part-1" in c for c in deleted), deleted
    assert any("canopy/cloud-runner/runner-seed " in c + " " for c in deleted), deleted


def test_no_args_still_means_the_live_stack(tmp_path):
    state = _setup(tmp_path)
    _down(tmp_path, "--keep-runner", token=False)
    deletes = [c for c in _calls(state) if "describe-stacks" in c]
    assert deletes and all("--stack-name canopy-cloud-runner" in c and
                           "canopy-cloud-runner-" not in c for c in deletes), deletes
