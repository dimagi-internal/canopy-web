"""Shared safety net for the runner's suite.

host_id() PERSISTS its ownership pin to ~/.canopy/host-id (it must be stable across
restarts — see cdp_control.host_id). That makes it the one function here with a side
effect outside the repo, and it is reached INDIRECTLY: any test that drives the main
loop hits it via heartbeat(host=host_id()). Un-isolated, the suite writes the pin into
the developer's real home — and when the test user differs from the user the daemon runs
as (CI, sudo, a sandboxed agent), it poisons that pin with an identity the daemon then
adopts, silently breaking session-reuse ownership.

So redirect the pin to tmp for EVERY test, not just the ones that mean to touch it.
"""
import pytest

from canopy_runner import cdp_control


@pytest.fixture(autouse=True)
def _isolate_host_pin(monkeypatch, tmp_path):
    monkeypatch.setattr(cdp_control, "HOST_ID_PATH", tmp_path / "host-id")


@pytest.fixture(autouse=True)
def _no_real_transcripts(monkeypatch):
    """A reuse send now verifies delivery against the session's transcript, which
    resolves under the developer's real ~/.claude. Default it to "unresolvable" (the
    send proceeds unverified, as before) so no test reads a real session; tests of
    the verification itself point it at a tmp file."""
    from canopy_runner import execute
    monkeypatch.setattr(execute, "_resolve_transcript_path", lambda *a, **k: None)
    # And don't sit out _wait_for_transcript's 45s poll against a path that can never
    # resolve: a test that needs a transcript patches this with its tmp file.
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: None)
