"""The runner name is configurable — and a bare command still means the live box.

up.sh, wire.sh and down.sh all derive the stack from the runner name through
_lib.sh, so they cannot disagree about which stack `--name X` means. The default
mapping is load-bearing in the other direction: `./down.sh` with no arguments has
always meant stack canopy-cloud-runner / runner cloud-ec2-1, and silently
pointing it anywhere else would be worse than the bug this fixes.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

EC2 = pathlib.Path(__file__).resolve().parent.parent


def _lib(expr: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'source "{EC2}/_lib.sh"; {expr}'],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("name,stack", [
    ("cloud-ec2-1", "canopy-cloud-runner"),
    ("cloud-ec2-test", "canopy-cloud-runner-test"),
    ("cloud-ec2-2", "canopy-cloud-runner-2"),
    ("standby", "canopy-cloud-runner-standby"),
])
def test_stack_for_name(name, stack):
    assert _lib(f"stack_for_name {name}").stdout.strip() == stack


@pytest.mark.parametrize("name,ok", [
    ("cloud-ec2-1", True), ("cloud-ec2-test", True), ("a", True),
    ("", False), ("Cloud-EC2", False), ("cloud-ec2-", False), ("1cloud", False),
    ("has space", False), ("x" * 64, False),
])
def test_runner_name_ok(name, ok):
    assert (_lib(f"runner_name_ok '{name}'").returncode == 0) is ok


@pytest.mark.parametrize("script", ["up.sh", "wire.sh", "down.sh"])
def test_every_script_takes_name_and_defaults_to_the_live_box(script):
    text = (EC2 / script).read_text()
    assert "source ./_lib.sh" in text
    assert "--name)" in text or "--name|" in text or "|--name)" in text
    assert "$DEFAULT_RUNNER_NAME" in text, f"{script} must default to the live runner name"


@pytest.mark.parametrize("script", ["up.sh", "down.sh"])
def test_the_stack_is_derived_from_the_name(script):
    assert "stack_for_name" in (EC2 / script).read_text()


def test_up_sh_passes_the_name_to_the_template():
    """The template always HAD a RunnerName parameter; up.sh never set it, so every
    box paired as cloud-ec2-1 whatever the stack was called."""
    assert '"RunnerName=${NAME}"' in (EC2 / "up.sh").read_text()
