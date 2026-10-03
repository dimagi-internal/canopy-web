"""A rebuild swaps the new box into every workspace default order that named the old.

Agents with no order of their own FOLLOW their workspace's (2026-10-03), so the
fleet's cloud routing lives mostly there. Retiring the predecessor drops it from
those orders; without the swap, every following agent loses the cloud box on each
rebuild and nothing says so. An order that never named the predecessor is left
alone — putting a box in a workspace's order is a routing decision, not a rebuild.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "wire.sh"
NEW = "11111111-1111-1111-1111-111111111111"
OLD = "22222222-2222-2222-2222-222222222222"
LAPTOP = "33333333-3333-3333-3333-333333333333"


def _fake_curl(tmp: pathlib.Path) -> pathlib.Path:
    state = tmp / "state"
    state.mkdir()
    (state / "retired").write_text("")
    (state / "runners.json").write_text(json.dumps([
        {"id": NEW, "kind": "cloud", "name": "cloud-ec2-1", "status": "disconnected"},
        {"id": OLD, "kind": "cloud", "name": "cloud-ec2-1", "status": "online"},
    ]))
    orders = {
        "dimagi": [{"runner_id": LAPTOP, "rank": 0, "enabled": True},
                   {"runner_id": OLD, "rank": 1, "enabled": False}],
        "connect": [{"runner_id": LAPTOP, "rank": 0, "enabled": True}],
    }
    (state / "orders.json").write_text(json.dumps(orders))
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
    print((STATE / "runners.json").read_text())
elif url.endswith("/retire"):
    with (STATE / "retired").open("a") as fh:
        fh.write(url.rstrip("/").split("/")[-2] + chr(10))
    print("{{}}")
elif "/credential" in url:
    print(json.dumps({{"has_claude_token": True}}))
elif "/api/agents/?limit=" in url:
    print(json.dumps({{"items": [{{"slug": "hal"}}]}}))
elif "/api/agents/" in url:
    print("[]")  # hal has no order of its own: it follows its workspace
elif url.endswith("/api/workspaces/"):
    print(json.dumps([{{"slug": "dimagi"}}, {{"slug": "connect"}}]))
elif url.endswith("/runner-order"):
    ws = url.rstrip("/").split("/")[-2]
    if method == "PUT":
        (STATE / f"put-{{ws}}.json").write_text(body or "")
        print("[]")
    else:
        rows = [r for r in json.loads((STATE / "orders.json").read_text())[ws]
                if r["runner_id"] not in retired]
        print(json.dumps(rows))
else:
    print("{{}}")
''')
    curl.chmod(0o755)
    return state


def _run(tmp: pathlib.Path) -> subprocess.CompletedProcess:
    home = tmp / "home"
    (home / ".claude" / "canopy").mkdir(parents=True)
    (home / ".claude" / "canopy" / "workbench-token").write_text("t0ken\n")
    for name, body in (("aws", "#!/bin/sh\necho fake-token\n"), ("op", "#!/bin/sh\nexit 1\n")):
        p = tmp / "bin" / name
        p.write_text(body)
        p.chmod(0o755)
    return subprocess.run(
        ["bash", str(SCRIPT), "--runner-id", NEW], capture_output=True, text=True, timeout=120,
        env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "HOME": str(home), "CLAUDE_TOKEN": "x"},
        cwd=str(SCRIPT.parent),
    )


def test_the_new_box_takes_the_old_ones_place_rank_and_enabled_kept(tmp_path):
    state = _fake_curl(tmp_path)
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    put = json.loads((state / "put-dimagi.json").read_text())
    assert put == {"runners": [{"runner_id": LAPTOP, "enabled": True},
                               {"runner_id": NEW, "enabled": False}]}


def test_an_order_that_never_named_the_old_box_is_left_alone(tmp_path):
    state = _fake_curl(tmp_path)
    assert _run(tmp_path).returncode == 0
    assert not (state / "put-connect.json").exists()
