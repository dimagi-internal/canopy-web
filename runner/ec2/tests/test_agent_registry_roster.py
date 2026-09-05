"""The fleet roster comes from canopy-web, not from three hardcoded copies.

Adding a sixth agent used to mean editing CloudFormation. The roster was pinned in
THREE places — the `AgentSlugs` CFN parameter, `AGENT_SLUGS` in the box's
runner.env, and this script's own default — so "create a new agent" required
access to the stack, which is the friction the Agent Runtime Registry
(docs/superpowers/specs/2026-07-20-agent-runtime-registry-design.md) exists to
remove. canopy-web already serves the answer at `GET /api/agents/{slug}/runtime`
("what a runner needs from canopy-web to run this agent"); nothing consumed it.

Now the registry is authoritative when reachable, and AGENT_SLUGS is the OFFLINE
fallback. That inversion is the whole point: register an agent in canopy-web and
the next bootstrap picks it up, with no stack change and no SSH.

Degrading safely is the load-bearing property. A box that cannot reach canopy-web
at boot must still bootstrap the agents it already knows about — so every failure
path here (no env, HTTP error, malformed body, timeout) yields an EMPTY registry
and the caller falls back, rather than aborting under `set -e` and taking the
whole fleet down with it.
"""
from __future__ import annotations

import json
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


def _extract_function(name: str) -> str:
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _fake_curl(tmp: pathlib.Path, *, slugs, repos, fail=False, garbage=False) -> pathlib.Path:
    state = tmp / "state"
    state.mkdir(exist_ok=True)
    (state / "list.json").write_text(json.dumps({"items": [{"slug": s} for s in slugs]}))
    (state / "repos.json").write_text(json.dumps(repos))
    curl = tmp / "bin" / "curl"
    curl.parent.mkdir(exist_ok=True)
    curl.write_text(f'''#!/usr/bin/env python3
import json, sys, pathlib
STATE = pathlib.Path({str(state)!r})
if {fail!r}:
    sys.exit(22)                      # curl -f on an HTTP error
url = next(a for a in sys.argv[1:] if a.startswith("http"))
if {garbage!r}:
    print("<html>not json</html>"); raise SystemExit
if url.endswith("/runtime"):
    slug = url.rstrip("/").split("/")[-2]
    repos = json.loads((STATE / "repos.json").read_text())
    print(json.dumps({{"slug": slug, "repo_url": repos.get(slug, ""), "repo_ref": "main",
                       "engine": "any", "secret_refs": [], "workspace": "connect"}}))
else:
    print((STATE / "list.json").read_text())
''')
    curl.chmod(0o755)
    return curl.parent


def _run(tmp: pathlib.Path, fn: str, call: str, *, env_extra=None, **curl_kw) -> str:
    """`fn` names the function under test for readability; all three are injected."""
    del fn
    bindir = _fake_curl(tmp, **curl_kw)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "CANOPY_BASE_URL": "https://canopy.test/canopy",
        "CANOPY_TOKEN": "t0ken",
        "AGENT_SLUGS": "ace,ada,echo,eva,hal",
        "AGENT_REPO_ORG": "dimagi-internal",
    }
    env.update(env_extra or {})
    # resolve_roster and agent_repo_url both call agent_registry, so inject the
    # whole trio rather than the one under test.
    defs = "\n".join(
        _extract_function(n) for n in ("agent_registry", "resolve_roster", "agent_repo_url")
    )
    res = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{defs}\n{call}"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert res.returncode == 0, f"exited {res.returncode}\n{res.stdout}\n{res.stderr}"
    return res.stdout.strip()


# --- the registry read ---------------------------------------------------------

def test_the_registry_yields_slug_and_repo_for_every_agent(tmp_path):
    out = _run(
        tmp_path, "agent_registry", "agent_registry",
        slugs=["ace", "echo"],
        repos={"ace": "https://github.com/dimagi-internal/ace",
               "echo": "https://github.com/dimagi-internal/echo"},
    )
    assert out.splitlines() == [
        "ace\thttps://github.com/dimagi-internal/ace",
        "echo\thttps://github.com/dimagi-internal/echo",
    ]


def test_a_NEW_agent_needs_no_stack_change(tmp_path):
    """The point of the exercise: `scout` exists only in canopy-web, and is not in
    AGENT_SLUGS, the CFN parameter, or this script."""
    out = _run(
        tmp_path, "agent_registry", "agent_registry",
        slugs=["ace", "scout"],
        repos={"ace": "https://github.com/dimagi-internal/ace",
               "scout": "https://github.com/dimagi-internal/scout"},
    )
    assert "scout\thttps://github.com/dimagi-internal/scout" in out.splitlines()


@pytest.mark.parametrize("kw,env", [
    (dict(fail=True), {}),                                    # HTTP error
    (dict(garbage=True), {}),                                 # not JSON
    ({}, {"CANOPY_BASE_URL": ""}),                            # unconfigured
    ({}, {"CANOPY_TOKEN": ""}),                               # no token
])
def test_every_failure_path_yields_an_empty_registry_and_exits_clean(tmp_path, kw, env):
    """Under `set -e` a non-zero here would abort bootstrap entirely — a box that
    cannot reach canopy-web must still provision the agents it already knows."""
    assert _run(tmp_path, "agent_registry", "agent_registry",
                slugs=["ace"], repos={"ace": "u"}, env_extra=env, **kw) == ""


# --- what the roster resolves to ----------------------------------------------

def test_the_registry_roster_wins_over_the_hardcoded_env(tmp_path):
    out = _run(
        tmp_path, "resolve_roster", "resolve_roster",
        slugs=["ace", "scout"], repos={"ace": "u1", "scout": "u2"},
    )
    assert out == "ace,scout", "AGENT_SLUGS should not override a reachable registry"


def test_an_unreachable_registry_falls_back_to_the_env_roster(tmp_path):
    out = _run(
        tmp_path, "resolve_roster", "resolve_roster",
        slugs=["ace"], repos={}, fail=True,
    )
    assert out == "ace,ada,echo,eva,hal"


def test_an_empty_registry_falls_back_rather_than_provisioning_nothing(tmp_path):
    """A canopy-web with zero agents must not silently mean "bootstrap nothing" —
    that is indistinguishable from an outage and would strand the box."""
    out = _run(tmp_path, "resolve_roster", "resolve_roster", slugs=[], repos={})
    assert out == "ace,ada,echo,eva,hal"


# --- the clone pointer ---------------------------------------------------------

def test_the_clone_url_comes_from_the_registry(tmp_path):
    out = _run(
        tmp_path, "agent_repo_url", 'agent_repo_url scout',
        slugs=["scout"], repos={"scout": "https://github.com/someone-else/scout-fork"},
    )
    assert out == "https://github.com/someone-else/scout-fork"


def test_the_clone_url_falls_back_to_the_org_convention(tmp_path):
    """No registry (or no repo_url on the record) → the historical convention."""
    out = _run(tmp_path, "agent_repo_url", "agent_repo_url hal",
               slugs=["hal"], repos={}, fail=True)
    assert out == "https://github.com/dimagi-internal/hal"
