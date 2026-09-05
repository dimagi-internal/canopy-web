"""A rebuild must carry the agent's ROUTING RULES across, not just its runner order.

The sibling of test_wire_preserves_enabled.py, and the same failure mode one layer
up. `wire.sh` snapshots and swaps `/api/agents/{slug}/runners` — the DEFAULT ordered
list — and never touches `/api/agents/{slug}/runner-rules`. Retiring the predecessor
cascades ALL of its assignment rows, and a source/actor rule is an assignment row,
so a recycle silently deletes every rule pointing at the box being replaced.

That was harmless while rules did not exist. It stopped being harmless on
2026-09-05, when actor rules became the mechanism that lets a cloud runner take
other people's work while the operator's own work stays local (spec
2026-09-05-actor-aware-runner-routing-design). Losing them on rebuild does not
degrade routing — it silently reverts the fleet to "everything runs on the
operator's laptop", which is the exact state that feature exists to end, and
nothing in the wiring output would say so.

Same method as the sibling test: run the real script with `curl` faked on PATH and
assert on the calls it makes.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "wire.sh"
NEW = "11111111-1111-1111-1111-111111111111"
OLD = "22222222-2222-2222-2222-222222222222"
LAPTOP = "33333333-3333-3333-3333-333333333333"

SARVESH = "stewari@dimagi.com"
JON = "jjackson@dimagi.com"


def _fake_curl(tmp: pathlib.Path) -> pathlib.Path:
    """A fleet where `ace` has BOTH a default order and two rules on the old box.

    Models the load-bearing behaviour: retiring a runner drops every one of its
    assignment rows — default rows and rule rows alike.
    """
    state = tmp / "state"
    state.mkdir()
    (state / "retired").write_text("")
    (state / "runners.json").write_text(json.dumps([
        {"id": NEW, "kind": "cloud", "name": "cloud-ec2-1", "status": "disconnected"},
        {"id": OLD, "kind": "cloud", "name": "cloud-ec2-1", "status": "online"},
    ]))
    (state / "agents.json").write_text(json.dumps({"items": [{"slug": "ace"}]}))
    (state / "rows.json").write_text(json.dumps([
        {"runner_id": LAPTOP, "runner_name": "laptop", "kind": "emdash",
         "rank": 0, "enabled": True, "online": True, "ready": True},
        {"runner_id": OLD, "runner_name": "cloud-ec2-1", "kind": "cloud",
         "rank": 1, "enabled": False, "online": True, "ready": True},
    ]))
    # The ACE day-one shape: named colleagues allowlisted onto the cloud box,
    # strictly, so their work stays off the operator's machine.
    (state / "rules.json").write_text(json.dumps([
        {"source": "email", "actor": SARVESH, "rank": 0, "runner_id": OLD,
         "runner_name": "cloud-ec2-1", "kind": "cloud", "strict": True,
         "online": True, "ready": True, "enabled": True, "queued_count": 0},
        {"source": "ace_web", "actor": SARVESH, "rank": 0, "runner_id": OLD,
         "runner_name": "cloud-ec2-1", "kind": "cloud", "strict": True,
         "online": True, "ready": True, "enabled": True, "queued_count": 0},
        # A rule that names the operator's laptop, NOT the box being replaced —
        # it must survive completely untouched.
        {"source": "email", "actor": JON, "rank": 0, "runner_id": LAPTOP,
         "runner_name": "laptop", "kind": "emdash", "strict": True,
         "online": True, "ready": True, "enabled": True, "queued_count": 0},
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
elif url.endswith("/runner-rules") and "/api/agents/" in url:
    if method == "PUT":
        (STATE / "put-rules.json").write_text(body or "")
        print("[]")
    else:
        # Retiring a runner drops its rule rows exactly as it drops its
        # default rows — they are the same table.
        rows = [r for r in json.loads((STATE / "rules.json").read_text())
                if r["runner_id"] not in retired]
        print(json.dumps(rows))
elif url.endswith("/runners") and "/api/agents/" in url:
    if method == "PUT":
        (STATE / "put.json").write_text(body or "")
        print("[]")
    else:
        rows = [r for r in json.loads((STATE / "rows.json").read_text())
                if r["runner_id"] not in retired]
        print(json.dumps(rows))
else:
    print("{{}}")
''')
    curl.chmod(0o755)
    return state


def _run_wire(tmp: pathlib.Path) -> subprocess.CompletedProcess:
    home = tmp / "home"
    (home / ".claude" / "canopy").mkdir(parents=True)
    (home / ".claude" / "canopy" / "workbench-token").write_text("t0ken\n")
    aws = tmp / "bin" / "aws"
    aws.write_text("#!/bin/sh\necho fake-token\n")
    aws.chmod(0o755)
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


def test_a_rebuild_moves_the_predecessors_rules_onto_the_new_box(tmp_path):
    state = _fake_curl(tmp_path)
    res = _run_wire(tmp_path)

    put = state / "put-rules.json"
    assert put.exists(), (
        "wire.sh never PUT the agent's routing rules — a recycle therefore drops "
        f"every rule naming the retired box.\n{res.stdout}\n{res.stderr}"
    )
    rules = json.loads(put.read_text())["rules"]
    moved = {(r["source"], r["actor"]): r for r in rules}

    assert ("email", SARVESH) in moved and ("ace_web", SARVESH) in moved, (
        f"a rule on the retired box was lost across the rebuild: {rules}")
    for key in (("email", SARVESH), ("ace_web", SARVESH)):
        runners = moved[key]["runners"]
        assert [r["runner_id"] for r in runners] == [NEW], (
            f"{key} should now name the NEW box, got {runners}")
        assert moved[key]["strict"] is True, (
            f"{key} lost its strictness — a fall-through rule routes other "
            f"people's work back onto the operator's machine")


def test_a_rule_naming_another_runner_is_carried_across_untouched(tmp_path):
    """The converse of the swap: only rows naming the RETIRED box may change.
    Rewriting an unrelated rule would be the same silent config edit in reverse."""
    state = _fake_curl(tmp_path)
    _run_wire(tmp_path)

    rules = json.loads((state / "put-rules.json").read_text())["rules"]
    mine = next(r for r in rules if r["actor"] == JON)
    assert [r["runner_id"] for r in mine["runners"]] == [LAPTOP]
    assert mine["strict"] is True
    assert mine["source"] == "email"
