"""The runner suite cannot read a real agent's vault, whatever machine it runs on."""
from __future__ import annotations

import shutil
import subprocess

from conftest import CANOPY_STANDIN


def test_the_name_canopy_is_the_refusing_stand_in():
    assert shutil.which("canopy") == CANOPY_STANDIN
    assert subprocess.run(["canopy", "cred", "env", "--slug", "hal"], capture_output=True).returncode == 1


def test_the_runner_finds_the_stand_in_even_when_a_test_replaces_path(cloud_runner, monkeypatch):
    monkeypatch.setenv("PATH", "/nowhere")
    assert cloud_runner._canopy_cli() == CANOPY_STANDIN


def test_resolving_a_real_agents_env_gets_nothing(cloud_runner):
    assert cloud_runner._turn_values("hal") is None
