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
