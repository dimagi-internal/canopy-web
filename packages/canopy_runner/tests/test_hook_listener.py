"""The loopback hook listener and its user-level install.

The properties under test are the safety ones: a hook must never fail, the
forwarding switch must actually gate, and installing must not disturb hooks
canopy did not put there (emdash owns its own).
"""
import json

from canopy_runner import hook_install
from canopy_runner.hook_listener import HookListener


def _payload(cwd="/w/repo/emdash/task", **over):
    base = {
        "hook_event_name": "PostToolUse",
        "cwd": cwd,
        "tool_name": "Bash",
        "tool_use_id": "toolu_1",
        "tool_input": {"command": "ls"},
        "tool_response": {"stdout": "out"},
    }
    base.update(over)
    return base


def _listener(*, forward=True, known=("/w/repo/emdash/task",)):
    sent = []
    lis = HookListener(
        port=0, nonce="n",
        resolve_session=lambda cwd: "sess-1" if cwd in known else "",
        forward=lambda: forward,
    )
    lis.bind_sender(lambda session_id, events: sent.append((session_id, events)))
    return lis, sent


def test_forwarding_off_accepts_and_drops():
    """THE switch: the listener always accepts, but only forwards when on. Off
    means the plumbing is installed and inert."""
    lis, sent = _listener(forward=False)
    assert lis.handle_payload(_payload()) == "not-forwarding"
    assert sent == []
    assert lis.received == 1


def test_forwarding_on_ships_a_complete_tool_pair():
    lis, sent = _listener()
    assert lis.handle_payload(_payload()) == "forwarded"
    (session_id, events), = sent
    assert session_id == "sess-1"
    assert [e["kind"] for e in events] == ["tool_use", "tool_result"]
    # Live rows are view-only; persisting them would give the durable record a
    # second, unordered source.
    assert all(e["index"] == -1 for e in events)


def test_a_session_canopy_does_not_know_is_dropped_quietly():
    """User-level hooks fire for EVERY Claude Code session on the machine. Most
    are not canopy's; dropping them is the expected path, not an error."""
    lis, sent = _listener()
    assert lis.handle_payload(_payload(cwd="/somewhere/else")) == "unknown-cwd"
    assert sent == []
    assert lis.dropped_unknown_cwd == 1


def test_a_failing_transport_never_raises_at_the_hook():
    """A hook that sees a failure is a hook that can degrade the agent's loop."""
    lis, _ = _listener()
    lis.bind_sender(lambda *a: (_ for _ in ()).throw(RuntimeError("canopy down")))
    assert lis.handle_payload(_payload()) == "error"   # not an exception


def test_non_tool_events_are_ignored():
    lis, sent = _listener()
    assert lis.handle_payload(_payload(hook_event_name="Stop")) == "ignored"
    assert sent == []


# ── install ────────────────────────────────────────────────────────────────

def test_install_is_idempotent_and_refreshes_in_place(tmp_path):
    p = tmp_path / "settings.json"
    assert hook_install.install(p, port=8787, nonce="a") is True
    assert hook_install.install(p, port=9999, nonce="b") is True
    entries = json.loads(p.read_text())["hooks"]["PostToolUse"]
    assert len(entries) == 1, "a re-install must replace, not accumulate"
    assert "9999" in entries[0]["hooks"][0]["command"]
    assert "nonce" not in entries[0]["hooks"][0]["command"]


def test_install_preserves_hooks_canopy_did_not_write(tmp_path):
    """emdash writes its own hooks. Ours compose with them; they are not ours to
    edit."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {
        "PostToolUse": [{"hooks": [{"type": "command", "command": "emdash-thing"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "emdash-stop"}]}],
    }}))
    hook_install.install(p, port=8787, nonce="a")
    hooks = json.loads(p.read_text())["hooks"]
    commands = [h["command"] for e in hooks["PostToolUse"] for h in e["hooks"]]
    assert "emdash-thing" in commands
    assert any(hook_install.MARKER in c for c in commands)
    assert hooks["Stop"][0]["hooks"][0]["command"] == "emdash-stop"


def test_remove_takes_only_ours(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"hooks": [{"type": "command", "command": "someone-elses"}]}]}}))
    hook_install.install(p, port=8787, nonce="a")
    assert hook_install.remove(p) is True
    commands = [h["command"] for e in json.loads(p.read_text())["hooks"]["PostToolUse"]
                for h in e["hooks"]]
    assert commands == ["someone-elses"]
    assert hook_install.remove(p) is False   # nothing of ours left


def test_the_command_cannot_fail_or_hang_the_hook(tmp_path):
    cmd = hook_install.hook_command(8787, "secret")
    assert "|| true" in cmd, "a failing curl must not fail the hook"
    assert "--max-time" in cmd, "a hung canopy must not slow every tool call"
    assert "-d @-" in cmd, "the hook JSON is forwarded verbatim on stdin"
    assert "127.0.0.1" in cmd, "never off-box: this machine holds fleet credentials"


def test_a_settings_file_with_a_malformed_hooks_key_is_left_alone(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": "not-an-object"}))
    assert hook_install.install(p, port=8787, nonce="a") is False
    assert json.loads(p.read_text())["hooks"] == "not-an-object"


# ── counter reporting ───────────────────────────────────────────────────────

def test_hook_counters_are_reported_but_stay_quiet_until_something_fires(caplog):
    """Without this the live path is unobservable from the runner side — which
    is exactly the gap that showed up the first time the server logs weren't
    reachable."""
    import logging

    from canopy_runner import main as main_mod

    lis, _ = _listener()
    main_mod._hook_listener = lis
    main_mod._last_hook_report = None
    try:
        clock = [10_000.0]
        with caplog.at_level(logging.INFO, logger="canopy_runner"):
            main_mod._maybe_report_hooks(now_fn=lambda: clock[0])
            assert caplog.text == "", "nothing fired yet — silence is the honest report"

            lis.handle_payload(_payload())
            lis.handle_payload(_payload(cwd="/not/ours"))
            clock[0] += main_mod.HOOK_REPORT_SECONDS + 1
            main_mod._maybe_report_hooks(now_fn=lambda: clock[0])
        assert "2 received" in caplog.text
        assert "1 forwarded" in caplog.text
        assert "1 dropped" in caplog.text
    finally:
        main_mod._hook_listener = None


def test_hook_report_is_throttled(caplog):
    import logging

    from canopy_runner import main as main_mod

    lis, _ = _listener()
    lis.handle_payload(_payload())
    main_mod._hook_listener = lis
    main_mod._last_hook_report = None
    try:
        clock = [10_000.0]
        with caplog.at_level(logging.INFO, logger="canopy_runner"):
            main_mod._maybe_report_hooks(now_fn=lambda: clock[0])
            caplog.clear()
            clock[0] += 5  # well inside the window
            main_mod._maybe_report_hooks(now_fn=lambda: clock[0])
        assert caplog.text == "", "a busy agent must not flood the log"
    finally:
        main_mod._hook_listener = None


def test_the_first_report_is_not_throttled_out_at_process_start(caplog):
    """time.monotonic() is near zero early in a process, so seeding the
    last-report time with 0.0 read as "already reported" and swallowed the first
    report for a whole window — precisely when you are watching for it, because
    you have just enabled the feature. Clock starts where it really starts."""
    import logging

    from canopy_runner import main as main_mod

    lis, _ = _listener()
    lis.handle_payload(_payload())
    main_mod._hook_listener = lis
    main_mod._last_hook_report = None
    try:
        with caplog.at_level(logging.INFO, logger="canopy_runner"):
            main_mod._maybe_report_hooks(now_fn=lambda: 0.4)
        assert "1 received" in caplog.text
    finally:
        main_mod._hook_listener = None


def test_an_idle_tick_does_not_consume_the_report_window(caplog):
    """The stamp must be taken only when something is actually logged. Taking it
    on a no-op tick means the first tick after startup (nothing fired yet) burns
    the whole window, and the first real report waits behind it — the same
    5-minutes-of-silence symptom as the seed bug, from a different cause. Both
    were found live; both had passing tests."""
    import logging

    from canopy_runner import main as main_mod

    lis, _ = _listener()
    main_mod._hook_listener = lis
    main_mod._last_hook_report = None
    try:
        with caplog.at_level(logging.INFO, logger="canopy_runner"):
            main_mod._maybe_report_hooks(now_fn=lambda: 1.0)   # idle tick, nothing fired
            assert caplog.text == ""
            lis.handle_payload(_payload())                      # now something fires
            main_mod._maybe_report_hooks(now_fn=lambda: 6.0)    # 5s later, well inside the window
        assert "1 received" in caplog.text, "the idle tick swallowed the first real report"
    finally:
        main_mod._hook_listener = None
