"""RC3 — WakeListener frame handling + URL (pure logic; no socket, no WS lib)."""
from __future__ import annotations

from canopy_runner.wake import WakeListener, ws_url


def test_ws_url_builds_the_control_channel_url():
    assert ws_url("https://labs.connect.dimagi.com/canopy", "abc") == \
        "wss://labs.connect.dimagi.com/canopy/ws/runner/abc/"
    assert ws_url("http://localhost:8000", "x") == "ws://localhost:8000/ws/runner/x/"


def test_handle_sets_event_only_on_wake():
    w = WakeListener("https://x", "t", "r")
    assert not w.event.is_set()
    w._handle('{"type": "heartbeat.ack"}')   # unrelated frame
    assert not w.event.is_set()
    w._handle('{"type": "wake"}')
    assert w.event.is_set()


def test_handle_ignores_malformed_frames():
    w = WakeListener("https://x", "t", "r")
    w._handle("not json")
    w._handle("")
    assert not w.event.is_set()


def test_wake_listener_routes_control_frames():
    got = []
    wl = WakeListener("http://x", "t", "r1", on_control=got.append)
    wl._handle('{"type": "wake"}')
    assert wl.event.is_set()
    wl._handle('{"type": "cancel", "turn_id": "abc"}')
    assert got == [{"type": "cancel", "turn_id": "abc"}]


def test_handle_without_on_control_ignores_non_wake_frames():
    w = WakeListener("https://x", "t", "r")  # on_control defaults to None
    w._handle('{"type": "cancel", "turn_id": "abc"}')  # should not raise
    assert not w.event.is_set()
