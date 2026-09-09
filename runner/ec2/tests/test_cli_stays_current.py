"""The canopy CLI on a cloud box must track the marketplace clone.

`reinstall_cli_from_marketplace_clone` was provenance-only: its guard asked
"does the uv receipt record a directory requirement?", so once the CLI had been
re-pointed at the clone ONCE it returned early forever and could never update.
The clone kept advancing (Claude Code pulls the marketplace on SessionStart);
the CLI did not. A laptop hides this because a human runs /canopy:update — on a
cloud box nobody does.

Measured 2026-09-09: CLI 0.2.471 against a 0.2.479 clone, and `bin/hal-email`'s
engine-staleness guard refused every send, so the box could think but could not
answer anyone. Surfaced by a readiness drill, not by the box.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "bootstrap_agents.sh"


def _function_body() -> str:
    """Extract the one function — the script runs `main "$@"` at the bottom, so
    it cannot simply be sourced."""
    text = SOURCE.read_text()
    m = re.search(r"^reinstall_cli_from_marketplace_clone\(\) \{.*?^\}", text, re.S | re.M)
    assert m, "function not found — did it get renamed?"
    return m.group(0)


def _run(tmp_path, *, clone_version: str, installed_version: str, receipt_has_dir: bool):
    home = tmp_path / "home"
    clone = home / ".claude/plugins/marketplaces/canopy"
    clone.mkdir(parents=True)
    (clone / "pyproject.toml").write_text(f'[project]\nname = "canopy"\nversion = "{clone_version}"\n')
    receipt = home / ".local/share/uv/tools/canopy/uv-receipt.toml"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('[tool]\ndirectory = "/somewhere"\n' if receipt_has_dir else '[tool]\ngit = "https://x"\n')

    binv = tmp_path / "bin"
    binv.mkdir()
    (binv / "canopy").write_text(f"#!/bin/sh\necho 'canopy {installed_version}'\n")
    marker = tmp_path / "uv-was-called"
    (binv / "uv").write_text(f"#!/bin/sh\necho \"$@\" >> {marker}\nexit 0\n")
    for f in ("canopy", "uv"):
        (binv / f).chmod(0o755)

    script = "\n".join([
        "set -uo pipefail",
        "ok()   { echo \"OK: $*\"; }",
        "warn() { echo \"WARN: $*\"; }",
        "log()  { echo \"LOG: $*\"; }",
        _function_body(),
        "reinstall_cli_from_marketplace_clone",
    ])
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env={"HOME": str(home), "PATH": f"{binv}:/usr/bin:/bin"},
    )
    return proc.stdout + proc.stderr, marker.exists()


def test_a_lagging_cli_is_reinstalled(tmp_path):
    """The regression: same provenance, older version — must NOT short-circuit."""
    out, reinstalled = _run(tmp_path, clone_version="0.2.479",
                            installed_version="0.2.471", receipt_has_dir=True)
    assert reinstalled, f"CLI was left stale; output was:\n{out}"
    assert "0.2.471" in out and "0.2.479" in out, out


def test_a_current_cli_is_left_alone(tmp_path):
    """Idempotence is what makes this safe to run on a timer."""
    out, reinstalled = _run(tmp_path, clone_version="0.2.479",
                            installed_version="0.2.479", receipt_has_dir=True)
    assert not reinstalled, f"needless reinstall; output was:\n{out}"
    assert "matching the marketplace clone" in out, out


def test_a_vcs_install_is_still_re_pointed(tmp_path):
    """The original provenance repair must survive the new version check."""
    _, reinstalled = _run(tmp_path, clone_version="0.2.479",
                          installed_version="0.2.479", receipt_has_dir=False)
    assert reinstalled


def test_the_timer_path_actually_calls_it(tmp_path):
    """A staleness check wired only into full bootstrap never runs: step4 needs a
    service restart, and nothing restarts this service. The timer
    (--credentials-only) is the only thing that runs regularly."""
    text = SOURCE.read_text()
    # `main()`'s branch specifically — step3_agents has its own CREDENTIALS_ONLY
    # early-return, and matching the first one in the file tests the wrong thing.
    m = re.search(r"^main\(\) \{.*?^\}", text, re.S | re.M)
    assert m, "main() not found"
    branch = re.search(r"if \(\( CREDENTIALS_ONLY \)\); then(.*?)\n  fi", m.group(0), re.S)
    assert branch, "credentials-only branch not found inside main()"
    assert "reinstall_cli_from_marketplace_clone" in branch.group(1), \
        "the timer path does not sync the CLI, so drift can never self-heal"
