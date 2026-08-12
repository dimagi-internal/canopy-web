"""A rebuild must hand the box back to the fleet on exactly the terms it left.

`wire.sh` promises, in its own header, to replace the predecessor's assignment row
"(preserving rank + enabled)". It did not. Retiring the predecessor DROPS its
assignment rows, and step 4 then re-read each agent's list — by which point the
predecessor was gone, so nothing matched, the append path ran, and that path
hardcodes `enabled: True`.

Observed 2026-08-12, rebuilding the cloud box: the runner had been deliberately
DISABLED for all five agents, and came back enabled for all five. Nobody chose
that, and nothing reported it — the wiring output says "appended" either way. A
silent config flip on the one path you take while recovering from an outage.

Driven against the real script with `curl` faked on PATH, same approach as
test_update_runner_sh.py: the whole content of wire.sh is which calls it makes
with what, so only a real run can prove it.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "wire.sh"
NEW = "11111111-1111-1111-1111-111111111111"
OLD = "22222222-2222-2222-2222-222222222222"
LAPTOP = "33333333-3333-3333-3333-333333333333"


def _fake_curl(tmp: pathlib.Path, *, cloud_enabled: bool) -> pathlib.Path:
    """A `curl` that serves the fleet and records every PUT body.

    Models the behaviour that caused the bug: once the predecessor is retired, it
    disappears from each agent's assignment list.
    """
    state = tmp / "state"
    state.mkdir()
    (state / "retired").write_text("")
    runners = [
        {"id": NEW, "kind": "cloud", "name": "cloud-ec2-1", "status": "disconnected"},
        {"id": OLD, "kind": "cloud", "name": "cloud-ec2-1", "status": "online"},
    ]
    (state / "runners.json").write_text(json.dumps(runners))
    (state / "agents.json").write_text(json.dumps({"items": [{"slug": "eva"}]}))
    (state / "rows.json").write_text(json.dumps([
        {"runner_id": LAPTOP, "runner_name": "laptop", "kind": "emdash",
         "rank": 0, "enabled": True, "online": True, "ready": True},
        {"runner_id": OLD, "runner_name": "cloud-ec2-1", "kind": "cloud",
         "rank": 1, "enabled": cloud_enabled, "online": True, "ready": True},
    ]))

    curl = tmp / "bin" / "curl"
    curl.parent.mkdir(exist_ok=True)
    curl.write_text(f'''#!/usr/bin/env python3
import json, sys, pathlib
STATE = pathlib.Path({str(state)!r})
args = sys.argv[1:]
method = args[args.index("-X") + 1] if "-X" in args else "GET"
url = next(a for a in args if a.startswith("http"))
body = None
for a in args:
    if a.startswith("--data@") or a.startswith("@"):
        body = pathlib.Path(a.lstrip("-").lstrip("data").lstrip("@")).read_text()
if "--data" in args:
    ref = args[args.index("--data") + 1]
    if ref.startswith("@"):
        body = pathlib.Path(ref[1:]).read_text()

retired = set(filter(None, (STATE / "retired").read_text().splitlines()))

if url.endswith("/api/harness/runners/"):
    print(json.dumps(json.loads((STATE / "runners.json").read_text())))
elif url.endswith("/retire"):
    rid = url.rstrip("/").split("/")[-2]
    with (STATE / "retired").open("a") as fh:
        fh.write(rid + chr(10))
    print("{{}}")
elif "/credential" in url:
    print(json.dumps({{"has_claude_token": True}}))
elif "/api/agents/?limit=" in url:
    print((STATE / "agents.json").read_text())
elif url.endswith("/runners") and "/api/agents/" in url:
    if method == "PUT":
        (STATE / "put.json").write_text(body or "")
        print("[]")
    else:
        # THE BEHAVIOUR THAT CAUSED THE BUG: a retired runner's assignment row
        # is gone from the agent's list.
        rows = [r for r in json.loads((STATE / "rows.json").read_text())
                if r["runner_id"] not in retired]
        print(json.dumps(rows))
else:
    print("{{}}")
''')
    curl.chmod(0o755)
    return state


def _run_wire(tmp: pathlib.Path, state_dir: pathlib.Path) -> subprocess.CompletedProcess:
    home = tmp / "home"
    (home / ".claude" / "canopy").mkdir(parents=True)
    (home / ".claude" / "canopy" / "workbench-token").write_text("t0ken\n")
    # `aws` must SUCCEED: the claude token is fetched under `set -e` with no
    # fallback, so a failing stub kills the script before step 4 is ever reached.
    aws = tmp / "bin" / "aws"
    aws.write_text("#!/bin/sh\necho fake-token\n")
    aws.chmod(0o755)
    # `op` may fail — the github token is explicitly optional (`|| GITHUB_TOKEN=""`).
    op = tmp / "bin" / "op"
    op.write_text("#!/bin/sh\nexit 1\n")
    op.chmod(0o755)
    return subprocess.run(
        ["bash", str(SCRIPT), "--runner-id", NEW],
        capture_output=True, text=True, timeout=120,
        env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "HOME": str(home),
             "CLAUDE_TOKEN": "x"},
        cwd=str(SCRIPT.parent),
    )


@pytest.mark.parametrize("cloud_enabled", [False, True])
def test_rebuild_preserves_the_predecessors_enabled_flag(tmp_path, cloud_enabled):
    """Whatever the operator had set, the replacement inherits it — in BOTH
    directions. `False` is the regression that actually happened; `True` guards the
    over-correction of pinning it off for everyone."""
    state = _fake_curl(tmp_path, cloud_enabled=cloud_enabled)
    res = _run_wire(tmp_path, state)
    put = state / "put.json"
    assert put.exists(), f"no assignment PUT was made\n{res.stdout}\n{res.stderr}"
    rows = json.loads(put.read_text())["runners"]
    by_id = {r["runner_id"]: r["enabled"] for r in rows}
    assert NEW in by_id, f"new runner missing from the PUT: {rows}"
    assert OLD not in by_id, f"retired predecessor still in the PUT: {rows}"
    assert by_id[NEW] is cloud_enabled, (
        f"enabled flipped {cloud_enabled} -> {by_id[NEW]} across the rebuild")
    assert by_id[LAPTOP] is True, "an unrelated runner's row was disturbed"


def test_rebuild_keeps_the_predecessors_position(tmp_path):
    """Rank is the availability cascade's order. The replacement takes the slot its
    predecessor held, rather than being appended behind everything else."""
    state = _fake_curl(tmp_path, cloud_enabled=False)
    _run_wire(tmp_path, state)
    rows = json.loads((state / "put.json").read_text())["runners"]
    assert [r["runner_id"] for r in rows] == [LAPTOP, NEW]
