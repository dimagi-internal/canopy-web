"""The box prepares each agent with canopy's `canopy agent prepare --runner` — the one
implementation shared with laptops (canopy docs/architecture/agent-setup-lifecycle.md) — and
bootstrap_agents.sh keeps only the BOX's own steps. Drilled on a standby (cloud-ec2-prep,
2026-10-15): readiness matched the old bash agent for agent before the bash was removed.

Pinned:
- step 3 calls `canopy agent prepare --runner --slug <s>` per roster agent (with
  --credentials-only on the credentials-only pass), and an agent is READY when its clone is
  on the box — where a warning never made an agent unready;
- the CLI is brought current BEFORE agents are prepared (it is what prepares them);
- the runner's lazy path for a never-seen agent prepares THAT agent only;
- no per-agent preparation is left in the bash.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

from _bash import BASH, _fn

HERE = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.skipif(BASH is None, reason="needs bash 4+")
@pytest.mark.parametrize("credentials_only", [0, 1])
def test_step3_prepares_each_agent_with_canopy(tmp_path, credentials_only):
    stub, log, root = tmp_path / "bin", tmp_path / "calls", tmp_path / "agents"
    stub.mkdir()
    (root / "ace" / ".git").mkdir(parents=True)          # ace is on the box, hal is not
    (stub / "canopy").write_text(f'#!/usr/bin/env bash\necho "$AGENT_ROOT $*" >> {log}\nexit 1\n')
    (stub / "canopy").chmod(0o755)
    script = f"""
set -uo pipefail
export PATH="{stub}:/usr/bin:/bin"
AGENT_ROOT={root}; AGENT_SLUGS=ace,hal; CREDENTIALS_ONLY={credentials_only}
READY_AGENTS=(); FAILED_AGENTS=()
ok() {{ :; }}; warn() {{ :; }}; log() {{ :; }}; fail() {{ :; }}
ensure_plugin_runtime() {{ :; }}
{_fn("step3_agents")}
step3_agents
echo "READY=${{READY_AGENTS[*]}} FAILED=${{FAILED_AGENTS[*]:-}}"
"""
    out = subprocess.run([BASH, "-c", script], check=True, capture_output=True, text=True,
                         timeout=60).stdout
    extra = " --credentials-only" if credentials_only else ""
    assert log.read_text().splitlines() == [
        f"{root} agent prepare --runner --slug ace{extra}",
        f"{root} agent prepare --runner --slug hal{extra}"]
    assert ("READY=ace hal FAILED=" if credentials_only else "READY=ace FAILED=hal") in out


@pytest.mark.skipif(BASH is None, reason="needs bash 4+")
@pytest.mark.parametrize("credentials_only,order", [
    (0, ["step1_tooling", "step2_gog_config", "step4_claude_plugins", "step3_agents",
         "step4b_prune_caches", "step5_summary"]),
    (1, ["step2_gog_config", "reinstall_cli_from_marketplace_clone", "step3_agents"]),
])
def test_the_cli_is_current_before_it_prepares_anything(tmp_path, credentials_only, order):
    stubs = "\n".join(f'{f}() {{ echo {f}; }}' for f in (
        "step1_tooling", "step2_gog_config", "step3_agents",
        "step4_claude_plugins", "step4b_prune_caches", "step5_summary",
        "reinstall_cli_from_marketplace_clone"))
    script = f"""
CREDENTIALS_ONLY={credentials_only}
log() {{ :; }}
{stubs}
{_fn("main")}
main
"""
    out = subprocess.run([BASH, "-c", script], check=True, capture_output=True, text=True,
                         timeout=60).stdout.split()
    assert out == order


def test_no_per_agent_preparation_is_left_in_the_bash():
    text = (HERE / "bootstrap_agents.sh").read_text()
    for gone in ("bootstrap_one_agent", "ensure_client_creds", "refresh_gmail_token",
                 "inject_agent_env", "op inject", "gog auth tokens import", "PREPARE_ENGINE"):
        assert gone not in text, gone
    assert "canopy agent prepare --runner" in text


def test_a_never_seen_agent_is_prepared_alone(cloud_runner, monkeypatch):
    mod = cloud_runner
    runs, fleet = [], []
    monkeypatch.setattr(mod, "_run_teed", lambda cmd, env, timeout: (runs.append(cmd) or (0, [])))
    monkeypatch.setattr(mod, "bootstrap_agent_fleet", lambda: fleet.append(1))
    mod.prepare_agent("scout")
    assert fleet == [] and runs[0][1:] == ["agent", "prepare", "--runner", "--slug", "scout"]
    # ...and it IS what `_ensure_agent_clone` runs (the test loader swaps in a no-op).
    assert "_LAZY_BOOTSTRAPPER = prepare_agent" in (HERE / "cloud_runner.py").read_text()
