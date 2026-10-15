"""PREPARE_ENGINE=canopy: the box prepares each agent with canopy's `canopy agent prepare
--runner` — the one implementation shared with laptops (canopy
docs/architecture/agent-setup-lifecycle.md, phase 2) — and keeps only the BOX's own steps.

Pinned:
- step 3 calls `canopy agent prepare --runner --slug <s>` per roster agent (with
  --credentials-only on the credentials-only pass), and an agent is READY when its clone is
  on the box — the bash path's bar, where a warning never made an agent unready;
- the CLI is brought current BEFORE agents are prepared (it is what prepares them);
- the runner's lazy path for a never-seen agent prepares THAT agent only;
- the switch is a per-box template parameter that reaches runner.env, and empty means
  "the code's default" so the fleet moves through auto-update, not a stack change.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

from test_readiness_is_observed import BASH, _fn

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
{_fn("step3_agents_canopy")}
step3_agents_canopy
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
    (0, ["step1_tooling", "step2_gog_config", "step4_claude_plugins", "step3_agents_canopy",
         "step4b_prune_caches", "step5_summary"]),
    (1, ["step2_gog_config", "reinstall_cli_from_marketplace_clone", "step3_agents_canopy"]),
])
def test_the_cli_is_current_before_it_prepares_anything(tmp_path, credentials_only, order):
    stubs = "\n".join(f'{f}() {{ echo {f}; }}' for f in (
        "step1_tooling", "step2_gog_config", "step3_agents", "step3_agents_canopy",
        "step4_claude_plugins", "step4b_prune_caches", "step5_summary",
        "reinstall_cli_from_marketplace_clone"))
    script = f"""
PREPARE_ENGINE=canopy; CREDENTIALS_ONLY={credentials_only}
log() {{ :; }}
{stubs}
{_fn("main")}
main
"""
    out = subprocess.run([BASH, "-c", script], check=True, capture_output=True, text=True,
                         timeout=60).stdout.split()
    assert out == order


def test_the_bash_engine_is_still_the_default():
    text = (HERE / "bootstrap_agents.sh").read_text()
    assert 'PREPARE_ENGINE="${PREPARE_ENGINE:-bash}"' in text


def test_the_switch_is_a_template_parameter_that_reaches_runner_env():
    cfn = (HERE / "runner.cfn.yaml").read_text()
    assert "PrepareEngine:" in cfn and "AllowedValues: ['', 'bash', 'canopy']" in cfn
    assert "PREPARE_ENGINE=${PrepareEngine}" in cfn
    assert "PrepareEngine=${PREPARE_ENGINE_ARG}" in (HERE / "up.sh").read_text()


def test_a_never_seen_agent_is_prepared_alone_with_the_canopy_engine(cloud_runner, monkeypatch):
    mod = cloud_runner
    runs, fleet = [], []
    monkeypatch.setattr(mod, "_run_teed", lambda cmd, env, timeout: (runs.append(cmd) or (0, [])))
    monkeypatch.setattr(mod, "bootstrap_agent_fleet", lambda: fleet.append(1))
    monkeypatch.setattr(mod, "PREPARE_ENGINE", "canopy")
    mod._lazy_prepare("scout")
    assert fleet == [] and runs[0][1:] == ["agent", "prepare", "--runner", "--slug", "scout"]
    monkeypatch.setattr(mod, "PREPARE_ENGINE", "bash")
    mod._lazy_prepare("scout")
    assert fleet == [1] and len(runs) == 1
