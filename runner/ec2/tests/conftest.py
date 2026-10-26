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
    # A turn for an agent with no clone bootstraps the box first; in a test that
    # would clone real private repos. Tests that exercise it set their own.
    module._LAZY_BOOTSTRAPPER = lambda slug, env=None: None
    module._LAZY_CLONER = lambda slug, env: None
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


@pytest.fixture(autouse=True)
def _no_aws_device_proof(monkeypatch):
    """Every resolve tries to attach the box's IAM-role device proof (canopy-web#1549),
    which reads IMDS — through urllib.request.urlopen, which many tests fake to record
    what the runner fetched. Off by default here; tests/test_aws_device_proof.py turns it
    back on and points IMDS at a closed port."""
    monkeypatch.setenv("CANOPY_DEVICE_KEY", "0")


@pytest.fixture(autouse=True)
def _legacy_secret_files_by_default(monkeypatch):
    """Off-disk turn envs (spec 2026-10-19-runner-isolation-units §4) turn on when the
    installed canopy CLI is new enough — which `subprocess.run` fakes in many tests
    cannot answer honestly. The tests written before it pin the legacy file path, so
    it stays the default here; tests/test_turn_env_off_disk.py turns it back on."""
    monkeypatch.setenv("RUNNER_SECRET_FILES", "1")
    for name in ("CANOPY_UNIT", "CANOPY_GOG_SECRET_FILE", "CANOPY_TURN_ENV_ROOT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_credential_probe(monkeypatch):
    """Staging a credential probes it with a real `claude -p` (one short turn).
    Off by default here, so no test spends usage or waits on a CLI;
    tests/test_credential_probe.py turns it back on with subprocess faked."""
    monkeypatch.setenv("CLAUDE_PROBE", "0")


@pytest.fixture(autouse=True)
def _no_disk_sweep(monkeypatch):
    """The heartbeat starts a disk sweep (maybe_sweep_disk), which deletes old
    entries in the real /tmp. Off by default here, so no test that drives a beat
    can touch the machine it runs on; tests/test_scratch_sweep.py turns it on with
    its roots pointed into tmp_path."""
    monkeypatch.setenv("DISK_SWEEP", "0")
