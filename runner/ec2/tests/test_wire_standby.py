"""`wire.sh --standby` adds a box WITHOUT moving any work onto it.

The normal wire.sh is a REPLACEMENT: it retires same-name predecessors and swaps
every agent's rows, rules and workspace orders onto the new box. Proving a
SECOND box that way would hand it the fleet. Standby makes exactly one kind of
change — a DISABLED row for each named agent, which never claims routed work but
is drillable — and refuses an agent that follows its workspace's order, since a
row of its own (even a disabled one) would replace that order and strand it.

Driven against the real script with `curl` faked on PATH, like
test_wire_preserves_enabled.py.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "wire.sh"
NEW = "11111111-1111-1111-1111-111111111111"
LIVE = "22222222-2222-2222-2222-222222222222"
LAPTOP = "33333333-3333-3333-3333-333333333333"


def _fleet(tmp: pathlib.Path, *, follows: set[str] = frozenset(),
           missing_vault: set[str] = frozenset()) -> pathlib.Path:
    state = tmp / "state"
    state.mkdir()
    (state / "calls").write_text("")
    agents = {}
    for slug in ("ada", "eva", "ace"):
        own = slug not in follows
        agents[slug] = {
            "rows": ([{"runner_id": LIVE, "enabled": True}, {"runner_id": LAPTOP, "enabled": False}]
                     if own else []),
            "own": own,
            "vault": slug not in missing_vault,
        }
    (state / "agents.json").write_text(json.dumps(agents))
    (state / "drills").write_text("[]")
    curl = tmp / "bin" / "curl"
    curl.parent.mkdir(exist_ok=True)
    curl.write_text(f'''#!/usr/bin/env python3
import json, sys, pathlib
S = pathlib.Path({str(state)!r})
a = sys.argv[1:]
method = a[a.index("-X") + 1] if "-X" in a else "GET"
url = next(x for x in a if x.startswith("http"))
body = pathlib.Path(a[a.index("--data") + 1][1:]).read_text() if "--data" in a else ""
with (S / "calls").open("a") as fh:
    fh.write(method + " " + "/" + url.split("://", 1)[1].split("/", 1)[1] + chr(10))
agents = json.loads((S / "agents.json").read_text())
path = "/" + url.split("://", 1)[1].split("/", 1)[1]
def out(x): print(json.dumps(x))
if path.startswith("/api/agents/") and path.count("/") >= 4:
    slug, rest = path.split("/")[3], "/".join(path.split("/")[4:])
    ag = agents.get(slug)
    if ag is None:
        out({{"detail": "Not Found"}})
    elif rest == "":
        out({{"slug": slug, "workspace": "dimagi"}})
    elif rest == "vault":
        out({{"vault": "Agent-" + slug, "key_set": ag["vault"]}})
    elif rest == "github":
        out({{"set": True, "login": "jj"}})
    elif rest == "default-order":
        out({{"own": ag["own"], "workspace": "dimagi"}})
    elif rest == "runners" and method == "PUT":
        (S / ("put-" + slug + ".json")).write_text(body)
        out(json.loads(body)["runners"])
    elif rest == "runners":
        out(ag["rows"])
    else:
        out([])
elif path.startswith("/api/workspaces/"):
    out({{"vault": "Canopy-Shared", "key_set": True}})
elif path.endswith("/credential"):
    out({{"has_claude_token": True}})
elif path.endswith("/drill"):
    (S / "drill-body.json").write_text(body)
    out([{{"agent_slug": s, "outcome": "pending"}} for s in json.loads(body)["agents"]])
elif path.endswith("/drills"):
    out([{{"agent_slug": s, "outcome": "pass", "summary": "ok"}}
         for s in json.loads((S / "drill-body.json").read_text())["agents"]])
elif path == "/api/harness/runners/":
    out([{{"id": "{NEW}", "kind": "cloud", "name": "cloud-ec2-test", "health_bootstrapped_at": 1.0}}])
else:
    out({{}})
''')
    curl.chmod(0o755)
    aws = tmp / "bin" / "aws"
    aws.write_text("#!/bin/sh\necho fake-claude-token\n")
    aws.chmod(0o755)
    (tmp / "bin" / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (tmp / "bin" / "sleep").chmod(0o755)
    return state


def _wire(tmp: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    home = tmp / "home"
    (home / ".claude" / "canopy").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "canopy" / "workbench-token").write_text("t0ken\n")
    return subprocess.run(
        ["bash", str(SCRIPT), "--runner-id", NEW, "--name", "cloud-ec2-test", *args],
        capture_output=True, text=True, timeout=120,
        env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "HOME": str(home)},
        cwd=str(SCRIPT.parent),
    )


def _calls(state: pathlib.Path) -> list[str]:
    return state.joinpath("calls").read_text().splitlines()


def test_standby_appends_a_disabled_row_and_preserves_the_rest(tmp_path):
    state = _fleet(tmp_path)
    res = _wire(tmp_path, "--standby", "--agents", "ada,eva")
    assert res.returncode == 0, res.stdout + res.stderr
    for slug in ("ada", "eva"):
        rows = json.loads((state / f"put-{slug}.json").read_text())["runners"]
        assert rows == [
            {"runner_id": LIVE, "enabled": True},
            {"runner_id": LAPTOP, "enabled": False},  # an unrelated flag, untouched
            {"runner_id": NEW, "enabled": False},
        ]
    assert not (state / "put-ace.json").exists(), "an agent not named was touched"


def test_standby_touches_nothing_else(tmp_path):
    state = _fleet(tmp_path)
    _wire(tmp_path, "--standby", "--agents", "ada")
    writes = [c for c in _calls(state) if not c.startswith("GET ")]
    assert all(c.startswith(("PUT /api/agents/ada/runners", "POST /api/harness/runners/"))
               for c in writes), writes
    assert not any("/retire" in c for c in writes), "standby must never retire anything"
    assert not any("runner-rules" in c or "runner-order" in c for c in writes), writes


def test_standby_refuses_an_agent_that_follows_its_workspace(tmp_path):
    """ace has no order of its own. One disabled row would BECOME its order —
    a list with no enabled runner — and it would stop running anywhere."""
    state = _fleet(tmp_path, follows={"ace"})
    res = _wire(tmp_path, "--standby", "--agents", "ada,ace")
    assert res.returncode != 0
    assert "follows workspace" in res.stdout
    assert not list(state.glob("put-*.json")), "a refused preflight must change nothing"


def test_standby_refuses_an_agent_with_no_vault(tmp_path):
    state = _fleet(tmp_path, missing_vault={"eva"})
    res = _wire(tmp_path, "--standby", "--agents", "eva")
    assert res.returncode != 0
    assert "no agent vault" in res.stdout
    assert not list(state.glob("put-*.json"))


def test_standby_requires_agents(tmp_path):
    _fleet(tmp_path)
    res = _wire(tmp_path, "--standby")
    assert res.returncode != 0 and "--agents" in res.stderr


def test_standby_drill_names_only_the_given_agents_and_passes(tmp_path):
    state = _fleet(tmp_path)
    res = _wire(tmp_path, "--standby", "--agents", "ada,eva", "--drill")
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.loads((state / "drill-body.json").read_text()) == {"agents": ["ada", "eva"]}
    assert "PASS" in res.stdout


def test_rerunning_standby_is_idempotent(tmp_path):
    state = _fleet(tmp_path)
    agents = json.loads((state / "agents.json").read_text())
    agents["ada"]["rows"].append({"runner_id": NEW, "enabled": False})
    (state / "agents.json").write_text(json.dumps(agents))
    res = _wire(tmp_path, "--standby", "--agents", "ada")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "present" in res.stdout
    assert not (state / "put-ada.json").exists()
