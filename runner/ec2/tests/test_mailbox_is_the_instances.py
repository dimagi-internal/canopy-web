"""A box sets Gmail up for THE INSTANCE's mailbox, from canopy-web (canopy-web#984).

Bootstrap used to derive it as `<slug>@dimagi-ai.com`, and the inbox poll read it
from the agent's repo. The repo is the agent's DEFINITION, shared by every
instance of it, so two ACE instances would both point at ace@. The instance's
own record is `Agent.email`; an instance with none gets no Gmail at all.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bootstrap_agents.sh"


def _extract_function(name: str) -> str:
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _run(tmp: pathlib.Path, call: str, *, agents=None, fail=False) -> str:
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    body = json.dumps({"items": agents or []})
    script = "exit 22" if fail else "cat <<JSON\n" + body + "\nJSON"
    (bindir / "curl").write_text("#!/bin/sh\n" + script + "\n")
    (bindir / "curl").chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "CANOPY_BASE_URL": "https://canopy.test/canopy",
           "CANOPY_TOKEN": "t0ken"}
    res = subprocess.run(["bash", "-c", f"set -euo pipefail\n{_extract_function('agent_mailbox')}\n{call}"],
                         capture_output=True, text=True, timeout=60, env=env)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


AGENTS = [{"slug": "ace", "email": "ace@dimagi-ai.com"},
          {"slug": "ace-staging", "email": ""},          # a second instance, no mailbox of its own
          {"slug": "hal", "email": "hal@dimagi-ai.com"}]


def test_each_instance_gets_its_own_recorded_mailbox(tmp_path):
    assert _run(tmp_path, 'agent_mailbox ace', agents=AGENTS) == "ace@dimagi-ai.com"
    assert _run(tmp_path, 'agent_mailbox hal', agents=AGENTS) == "hal@dimagi-ai.com"


def test_an_instance_with_no_mailbox_gets_none_not_the_slugs_guess(tmp_path):
    assert _run(tmp_path, 'agent_mailbox ace-staging', agents=AGENTS) == ""
    assert _run(tmp_path, 'agent_mailbox scout', agents=AGENTS) == ""


def test_an_unreachable_canopy_means_none_known_and_exits_clean(tmp_path):
    assert _run(tmp_path, 'agent_mailbox ace', fail=True) == ""


def test_nothing_in_bootstrap_derives_an_address_from_the_slug():
    src = SCRIPT.read_text()
    assert '${slug}@dimagi-ai.com' not in src
