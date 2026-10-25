#!/usr/bin/env python3
"""Render a runner fleet's stack template (`fleet.cfn.yaml` → `fleet.rendered.cfn.yaml`).

A fleet member is built exactly as a single box is: the same cloud-init, from the
same place. So the fleet template does not carry a copy of it — it has one line,
`__RUNNER_USER_DATA__`, and this replaces it with runner.cfn.yaml's own UserData,
changed in exactly two ways (`member_user_data`):

* `RUNNER_NAME` is `<fleet>-<instance id>`, read from IMDS at boot, not a parameter:
  every member is its own runner, and the fleet's join (canopy-web
  apps/harness/fleets.py) accepts an instance only under that name;
* box.env says it is a fleet member — the group and its launch lifecycle hook —
  which turns on `cloud_runner.py --fleet-ready` (it completes the hook once the box
  has bootstrapped).

Stdlib only, like everything that runs beside up.sh. Usage:

    python3 fleet_template.py fleet.cfn.yaml runner.cfn.yaml > fleet.rendered.cfn.yaml
"""
from __future__ import annotations

import re
import sys

PLACEHOLDER = "__RUNNER_USER_DATA__"
#: How the box's own name is written into box.env (runner.cfn.yaml).
NAME_LINE = "RUNNER_NAME=${RunnerName}"
#: A member's name: the fleet's, then its instance id without "i-". IMDSv2 — the
#: script runs as root, the only user IMDS is open to on a box.
MEMBER_NAME_LINE = (
    "RUNNER_NAME=${FleetName}-$(T=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token "
    "-H 'X-aws-ec2-metadata-token-ttl-seconds: 60'); curl -fsS -H \"X-aws-ec2-metadata-token: $T\" "
    "http://169.254.169.254/latest/meta-data/instance-id | sed 's/^i-//')"
)
FLEET_LINES = ("RUNNER_FLEET=${FleetName}", "RUNNER_FLEET_GROUP=${GroupName}",
               "RUNNER_FLEET_HOOK=${LifecycleHookName}")


def runner_user_data(runner_template: str) -> list[str]:
    """runner.cfn.yaml's UserData `!Sub |` body, as its lines (indentation kept)."""
    lines = runner_template.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "Fn::Base64: !Sub |")
    indent = len(lines[start + 1]) - len(lines[start + 1].lstrip())
    body = []
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line)
    while body and not body[-1].strip():
        body.pop()
    return body


def member_user_data(body: list[str]) -> list[str]:
    """The box's UserData as a fleet member's (the two changes in the module doc)."""
    names = [i for i, line in enumerate(body) if line.strip() == NAME_LINE]
    if len(names) != 1:
        raise SystemExit(f"runner.cfn.yaml must write {NAME_LINE!r} exactly once (found {len(names)})")
    i = names[0]
    pad = body[i][: len(body[i]) - len(body[i].lstrip())]
    return body[:i] + [pad + MEMBER_NAME_LINE] + [pad + line for line in FLEET_LINES] + body[i + 1:]


def render(fleet_template: str, runner_template: str) -> str:
    out = []
    for line in fleet_template.splitlines():
        if line.strip() != PLACEHOLDER:
            out.append(line)
            continue
        pad = line[: len(line) - len(line.lstrip())]
        body = member_user_data(runner_user_data(runner_template))
        base = min(len(b) - len(b.lstrip()) for b in body if b.strip())
        out += [(pad + b[base:]) if b.strip() else "" for b in body]
    text = "\n".join(out) + "\n"
    if PLACEHOLDER in text:
        raise SystemExit(f"{PLACEHOLDER} must be a line of its own")
    if re.search(r"\$\{RunnerName\}", text):
        raise SystemExit("a fleet member has no RunnerName parameter")
    return text


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    with open(sys.argv[1]) as f, open(sys.argv[2]) as g:
        sys.stdout.write(render(f.read(), g.read()))
