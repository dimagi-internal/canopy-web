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
def _isolate_desktop_runtime(monkeypatch, tmp_path):
    """The Claude desktop runtime keeps a session index under ~/.canopy/desktop. On a
    box that has run desktop sessions, a test reading the REAL index sees them: the
    router starts resolving sessions, the session report grows rows. Point every test
    at an empty tmp dir and the default runtime."""
    from canopy_runner import desktop
    monkeypatch.setattr(desktop, "DEFAULT_DIR", tmp_path / "canopy-desktop")
    monkeypatch.setattr(desktop, "_current", desktop.EMDASH)


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


@pytest.fixture(autouse=True)
def _no_real_mailbox_probe():
    """The mailbox probe shells out to `gog` (a real keyring read — on macOS it can
    pop a Keychain prompt) from any test that drives the loop. Park it for every
    test; the probe's own tests call `mailbox_probe.reset()` and inject a runner."""
    from canopy_runner import mailbox_probe
    mailbox_probe.reset()
    mailbox_probe._state["next_at"] = float("inf")
    yield
    mailbox_probe.reset()


@pytest.fixture(autouse=True)
def _fresh_typed_turns():
    """execute._TYPED_TURNS remembers, per process, every turn whose message was
    typed — so a re-claimed turn is never typed twice. Tests reuse turn ids ("t1"),
    so each starts with an empty memory."""
    from canopy_runner import execute
    execute._TYPED_TURNS.clear()
    yield
    execute._TYPED_TURNS.clear()


@pytest.fixture(autouse=True)
def _isolate_user_settings(monkeypatch, tmp_path):
    """Never touch the developer's real ~/.claude/settings.json. A test that starts the
    hook listener installs the canopy hook there, pointed at the test's ephemeral port
    and nonce; on a runner box that silently re-pointed every live session's hooks at
    a dead listener until the daemon's next tick noticed (canopy-web#1188)."""
    from canopy_runner import hooks
    monkeypatch.setattr(hooks, "user_settings_path", lambda: tmp_path / "claude-settings.json")


@pytest.fixture(autouse=True)
def _no_real_composer_reads(monkeypatch):
    """An unconfirmed send now LOOKS at the session before giving up
    (execute._recover_delivery), which shells out to the CDP sidecar. Default that
    look to "unreadable" — recovery then leaves the session alone and the turn fails
    exactly as it did before recovery existed — so no test drives a real emdash;
    tests of the recovery ladder fake the read."""
    def unreadable(*a, **k):
        raise cdp_control.CDPError("composer read not faked in this test")
    monkeypatch.setattr(cdp_control, "read_composer", unreadable)


@pytest.fixture(autouse=True)
def _fresh_startup_watch():
    """The startup-stall watch (#1190) keeps the sessions canopy created at module
    level. Each test starts with an empty watch that is not saved to disk. Otherwise
    a CREATE in one test could mark a session in another test as stuck, and a test
    could write to the real ~/.canopy."""
    from canopy_runner import startup_watch
    startup_watch._reset_for_tests()
    yield
    startup_watch._reset_for_tests()
