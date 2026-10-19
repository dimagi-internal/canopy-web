"""A package import tried before bootstrap no longer disables it for the process.

cloud-ec2-2: the startup credential probe ran before bootstrap had put the
clone's packages on sys.path. A probe that read like a usage cap parsed its reset
with canopy_transcript, the import failed, and `_transcript_core` cached that
None for the life of the process — so `packages.transcripts` failed and chat
sessions on the box wrote no durable rows until a restart. Only a success is
cached now; a failure is retried on the next call.
"""
from __future__ import annotations

import sys

import pytest

CAP = "You've hit your weekly limit · resets 11pm (UTC)"


def _unimportable(monkeypatch, *names):
    """Make these modules fail to import, as before bootstrap exposes the clone.
    Returns a callable that makes them importable again (the bootstrap)."""
    saved = {n: sys.modules.get(n) for n in names}
    for n in names:
        monkeypatch.setitem(sys.modules, n, None)   # `import n` -> ImportError

    def expose():
        for n, mod in saved.items():
            if mod is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = mod
    return expose


def test_a_cap_parsed_before_bootstrap_does_not_disable_transcripts(cloud_runner, monkeypatch):
    expose = _unimportable(monkeypatch, "canopy_transcript", "canopy_transcript.usage_limit")
    assert cloud_runner._cap_reset_epoch(CAP) is None      # before the clone is on sys.path
    assert cloud_runner._transcript_core() is None
    expose()                                               # _install_transcript_core
    assert cloud_runner._transcript_core() is not None
    assert cloud_runner._cap_reset_epoch(CAP) is not None


@pytest.mark.parametrize("loader,names", [
    ("_acp_core", ("canopy_acp",)),
    ("_inbox_core", ("canopy_runner", "canopy_runner.inbox", "canopy_runner.inbox_due")),
])
def test_the_other_loaders_retry_too(cloud_runner, monkeypatch, loader, names):
    expose = _unimportable(monkeypatch, *names)
    assert getattr(cloud_runner, loader)() is None
    expose()
    assert getattr(cloud_runner, loader)() is not None


def test_a_failure_is_logged_once_not_every_call(cloud_runner, monkeypatch):
    logged = []
    monkeypatch.setattr(cloud_runner, "_log", logged.append)
    _unimportable(monkeypatch, "canopy_transcript")
    for _ in range(5):
        assert cloud_runner._transcript_core() is None
    assert len([m for m in logged if "canopy_transcript unavailable" in m]) == 1


def test_a_success_is_cached(cloud_runner, monkeypatch):
    first = cloud_runner._transcript_core()
    assert first is not None
    monkeypatch.setitem(sys.modules, "canopy_transcript", None)
    assert cloud_runner._transcript_core() is first


def test_main_exposes_the_clone_before_the_startup_probes(cloud_runner, monkeypatch, tmp_path):
    """The path half of bootstrap runs first, so the probes can import."""
    clone = tmp_path / "src"
    (clone / ".git").mkdir(parents=True)
    order = []
    monkeypatch.setattr(cloud_runner, "RUNNER_SRC_DIR", str(clone))
    monkeypatch.setattr(cloud_runner, "BASE_URL", "https://canopy.test")
    monkeypatch.setattr(cloud_runner, "TOKEN", "t")
    monkeypatch.setattr(cloud_runner, "pair_or_load", lambda: "rid")
    monkeypatch.setattr(cloud_runner, "start_aws_device_key_enrollment",
                        lambda rid: type("T", (), {"join": lambda self, timeout=None: None})())
    monkeypatch.setattr(cloud_runner, "_retire_shared_github_credentials", lambda: None)
    monkeypatch.setattr(cloud_runner, "_expose_repo_packages", lambda repo: order.append("expose"))
    monkeypatch.setattr(cloud_runner, "fetch_and_stage_credential",
                        lambda rid: order.append("stage") or False)
    cloud_runner.main()
    assert order == ["expose", "stage"]
