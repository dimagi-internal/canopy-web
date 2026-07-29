"""Loads runner/ec2/cloud_runner.py by path.

cloud_runner.py is deliberately stdlib-only and lives under a hyphenated
directory (`ec2-runner`), which is not a valid Python package name — it can
never be `import`ed as `deploy.ec2_runner.cloud_runner`. Loading it via
importlib.util by its file path sidesteps that entirely and needs no
packaging changes to the runner itself.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "cloud_runner.py"


def _load_cloud_runner():
    spec = importlib.util.spec_from_file_location("cloud_runner", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cloud_runner"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def load_cloud_runner():
    """A callable, so a test can set env vars (via monkeypatch) BEFORE the
    module-level RUNNER_CAPS/RUNNER_SESSIONS/etc. reads happen at import time."""
    return _load_cloud_runner


@pytest.fixture
def cloud_runner(load_cloud_runner):
    """A freshly (re)loaded module per test with whatever env is ambient at
    fixture-setup time — cheap, and guarantees no test can leak module-level
    state (RUNNER_CAPS, _stop, ...) into another. Tests that care about
    specific env values should use `load_cloud_runner` directly instead, so
    they can set env before loading."""
    return load_cloud_runner()
