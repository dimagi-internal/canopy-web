"""A clone that could not be UPDATED is still provisioned; only a failed first
clone fails the agent.

Once the shared GitHub PAT was retired (#965), every agent whose owner had not
lent it a token failed `git pull` of its private repo — and bootstrap treated
that like a missing clone, skipping the agent's env, plugins and Gmail on a box
where a perfectly good clone already sat (ada, eva, hal on cloud-ec2-1,
2026-09-26). A turn already runs in a clone whose pull failed; bootstrap now
agrees with it.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bootstrap_agents.sh"

_HAS_ASSOC = subprocess.run(
    ["bash", "-c", "declare -A _t=( [k]=v ) 2>/dev/null && echo yes"],
    capture_output=True, text=True,
).stdout.strip() == "yes"
pytestmark = pytest.mark.skipif(
    not _HAS_ASSOC, reason="bash lacks associative arrays (macOS ships 3.2; the box and CI run 5)"
)

STUBS = """
declare -A GOG_CLIENT=() BOOTSTRAP_DETAIL=()
CREDENTIALS_ONLY=0; FAILED_AGENTS=(); READY_AGENTS=()
agent_vault_config() { printf 'Agent-X\\x1fkey\\x1f\\x1f\\x1f\\x1fx@dimagi-ai.com\\n'; }
agent_repo_url() { echo "https://github.com/dimagi-internal/$1"; }
clone_or_pull() { return 1; }          # GitHub refuses: no token that can read it
for f in ok warn log fail inject_agent_env install_agent_plugin install_required_plugins \\
         run_agent_provisioner ensure_client_creds refresh_gmail_token verify_mailbox \\
         verify_turn_client report_bootstrap; do eval "$f() { echo \\"$f \\$*\\"; }"; done
detail_join() { echo "$2"; }
mark() { :; }
"""


def _extract(name: str) -> str:
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _bootstrap(agent_root: pathlib.Path) -> str:
    script = f"{STUBS}\nAGENT_ROOT={agent_root}\n{_extract('bootstrap_one_agent')}\n" \
             "bootstrap_one_agent echo\necho FAILED=${FAILED_AGENTS[*]:-} READY=${READY_AGENTS[*]:-}\n"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_an_existing_clone_that_cannot_be_updated_is_still_provisioned(tmp_path):
    (tmp_path / "echo" / ".git").mkdir(parents=True)
    out = _bootstrap(tmp_path)
    assert "could not update" in out
    assert "inject_agent_env echo" in out and "report_bootstrap echo" in out
    assert "FAILED= READY=echo" in out


def test_a_first_clone_that_fails_still_fails_the_agent(tmp_path):
    out = _bootstrap(tmp_path)
    assert "clone of https://github.com/dimagi-internal/echo failed" in out
    assert "inject_agent_env" not in out
    assert "FAILED=echo READY=" in out
