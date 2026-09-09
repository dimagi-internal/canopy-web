"""RUNNER_EXECUTOR must be DECLARED in the template, not hand-set on a box.

Set by hand it does not survive: `canopy-fetch-env` (ExecStartPre) rewrites
/opt/canopy-runner/runner.env from scratch on EVERY service start, so an
appended line is gone before systemd reads the file — and the turn then runs on
`claude -p` with nothing in the log to say the flag was dropped. Measured
2026-09-09 on cloud-ec2-1: exactly that, twice, before the cause was found.
"""
from __future__ import annotations

import pathlib
import re

TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "runner.cfn.yaml"


def _text() -> str:
    return TEMPLATE.read_text()


def test_the_env_file_carries_the_executor():
    """The heredoc that BUILDS runner.env has to emit it, or nothing does."""
    text = _text()
    heredoc = re.search(r"cat > /opt/canopy-runner/runner\.env <<ENV(.*?)\n\s*ENV\n",
                        text, re.S)
    assert heredoc, "runner.env heredoc not found — did canopy-fetch-env move?"
    assert "RUNNER_EXECUTOR=${RunnerExecutor}" in heredoc.group(1), (
        "runner.env is rebuilt on every start; an executor not written HERE "
        "cannot survive a restart no matter where else it is set"
    )


def test_the_parameter_exists_and_is_constrained():
    text = _text()
    block = re.search(r"^  RunnerExecutor:\n(.*?)(?=^  [A-Za-z]|\Z)", text, re.S | re.M)
    assert block, "RunnerExecutor parameter not declared"
    body = block.group(1)
    assert "AllowedValues: ['cli', 'acp']" in body, body
    # A typo'd value must fail at stack-update time, not at turn time — the
    # runner treats anything that is not exactly "acp" as cli, silently.
    assert "Default:" in body


def test_the_default_does_not_silently_revert_a_switched_on_box():
    """up.sh overrides ONLY SshCidr, so every other parameter takes its default
    on every stack update. A default of 'cli' would therefore switch a box back
    without anyone asking — the same silent-revert shape this file exists to
    prevent."""
    up = (TEMPLATE.parent / "up.sh").read_text()
    overrides = re.findall(r"--parameter-overrides\s+(.*)", up)
    assert overrides, "up.sh no longer passes parameter overrides — re-check this rule"
    if not any("RunnerExecutor" in o for o in overrides):
        block = re.search(r"^  RunnerExecutor:\n(.*?)(?=^  [A-Za-z]|\Z)",
                          _text(), re.S | re.M).group(1)
        assert "Default: 'acp'" in block, (
            "up.sh does not pass RunnerExecutor, so its default is what every "
            "stack update applies; 'cli' here silently reverts a switched-on box"
        )
