"""`wire.sh --name X` wires the box that paired by X's invitation — no guessing.

`up.sh` records the invitation it made in `<stack>-invitation.json`; once the box
has paired, canopy-web names the runner that consumed it. That is the box to
wire, whatever else of that name exists. Driven against the real script with
`curl` faked on PATH (the fleet fake from test_wire_standby.py, plus the
invitation read).
"""
from __future__ import annotations

import json
import pathlib
import subprocess

from test_wire_standby import NEW, _fleet  # noqa: F401 — the shared fake fleet

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "wire.sh"
INV_ID = "0b4f7f8e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
INVITED = "44444444-4444-4444-4444-444444444444"


def _with_invitation(tmp: pathlib.Path, *, status: str, name: str = "cloud-ec2-test") -> pathlib.Path:
    state = _fleet(tmp)
    fleet_curl = tmp / "bin" / "curl"
    fleet_curl.rename(tmp / "bin" / "curl-fleet")
    reply = {"id": INV_ID, "name": name, "status": status,
             "runner_id": INVITED if status == "consumed" else None}
    (tmp / "bin" / "curl").write_text(f'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
url = next(x for x in a if x.startswith("http"))
if "/api/harness/runner-invitations/" in url:
    with open({str(state / "calls")!r}, "a") as fh:
        fh.write("GET /" + url.split("://", 1)[1].split("/", 1)[1] + chr(10))
    print(json.dumps({reply!r}))
else:
    os.execv({str(tmp / "bin" / "curl-fleet")!r}, [{str(tmp / "bin" / "curl-fleet")!r}] + a)
''')
    (tmp / "bin" / "curl").chmod(0o755)
    keys = tmp / "keys"
    keys.mkdir(exist_ok=True)
    (keys / "canopy-cloud-runner-test-invitation.json").write_text(
        json.dumps({"id": INV_ID, "runner_id": INVITED}))
    return state


def _wire(tmp: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    home = tmp / "home"
    (home / ".claude" / "canopy").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "canopy" / "workbench-token").write_text("t0ken\n")
    return subprocess.run(
        ["bash", str(SCRIPT), "--probe-minutes", "0", "--name", "cloud-ec2-test", "--discover-minutes", "0", *args],
        capture_output=True, text=True, timeout=120,
        env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "HOME": str(home), "KEY_DIR": str(tmp / "keys")},
        cwd=str(SCRIPT.parent),
    )


def test_wire_uses_invitation_runner_id(tmp_path):
    state = _with_invitation(tmp_path, status="consumed")
    res = _wire(tmp_path, "--standby", "--agents", "ada")
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"==> runner: {INVITED}" in res.stdout
    assert f"runner from invitation {INV_ID}" in res.stdout
    calls = state.joinpath("calls").read_text().splitlines()
    assert f"GET /api/harness/runner-invitations/{INV_ID}" in calls
    rows = json.loads((state / "put-ada.json").read_text())["runners"]
    assert rows[-1] == {"runner_id": INVITED, "enabled": False}


def test_an_unconsumed_invitation_falls_back_to_discovery(tmp_path):
    _with_invitation(tmp_path, status="approved")
    res = _wire(tmp_path, "--standby", "--agents", "ada")
    assert "is approved, not consumed" in res.stdout
    assert f"==> runner: {INVITED}" not in res.stdout


def test_an_invitation_for_another_name_is_not_used(tmp_path):
    _with_invitation(tmp_path, status="consumed", name="cloud-ec2-other")
    res = _wire(tmp_path, "--standby", "--agents", "ada")
    assert f"==> runner: {INVITED}" not in res.stdout


UNIT_RID = "55555555-5555-5555-5555-555555555555"


def test_unit_wires_that_units_runner_from_the_boxs_one_invitation(tmp_path):
    """up.sh --units makes ONE invitation for the box; each runner's id is in
    its `units`. `--unit agent-eva` wires `cloud-ec2-test-agent-eva`."""
    state = _with_invitation(tmp_path, status="consumed")
    curl = (tmp_path / "bin" / "curl").read_text()
    units = [{"name": "cloud-ec2-test", "runner_id": INVITED},
             {"name": "cloud-ec2-test-agent-eva", "runner_id": UNIT_RID}]
    curl = curl.replace("'runner_id': '44444444-4444-4444-4444-444444444444'}",
                        f"'runner_id': '44444444-4444-4444-4444-444444444444', 'units': {units!r}}}")
    (tmp_path / "bin" / "curl").write_text(curl)
    res = _wire(tmp_path, "--unit", "agent-eva", "--standby", "--agents", "ada")
    assert f"==> runner: {UNIT_RID}" in res.stdout, res.stdout + res.stderr
    rows = json.loads((state / "put-ada.json").read_text())["runners"]
    assert rows[-1] == {"runner_id": UNIT_RID, "enabled": False}
