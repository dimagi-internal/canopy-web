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


# --- control-frame dispatch (make_control_handler) -------------------------
#
# Five branches on a live socket had NO coverage, and one frame type the server
# has published for months had no branch at all — an unhandled frame looks
# exactly like one that never arrived, so nothing ever failed.

import types  # noqa: E402

from canopy_runner.cancel import CANCELLED_TURNS  # noqa: E402
from canopy_runner.main import make_control_handler  # noqa: E402


def _handler():
    waker = WakeListener("http://x", "t", "r1")
    cfg = types.SimpleNamespace(cdp_port=9222, runner_id="r1", base_url="http://x",
                                token="t", state_path=None)
    return make_control_handler(cfg, waker), waker


def test_stream_frame_wakes_the_loop():
    """REGRESSION: the server publishes `runner.stream` on every backfill request
    and viewer attach, and this dispatch had no branch for it — so a "Load full
    session" sat until the next poll tick (up to poll_seconds) before the runner
    even knew. Measured end-to-end on labs at 14.6s."""
    on_control, waker = _handler()
    assert not waker.event.is_set()
    on_control({"type": "stream", "session_id": "s", "session_key": "k", "desired": None})
    assert waker.event.is_set()


def test_cancel_frame_records_the_turn():
    on_control, _w = _handler()
    on_control({"type": "cancel", "turn_id": "t-123"})
    assert "t-123" in CANCELLED_TURNS
    CANCELLED_TURNS.discard("t-123")


def test_check_inbox_frame_rings_the_mailbox():
    from canopy_runner import inbox_due

    on_control, _w = _handler()
    on_control({"type": "check_inbox", "mailbox": "hal@dimagi-ai.com"})
    assert "hal@dimagi-ai.com" in inbox_due.take_pending()


def test_an_unknown_frame_is_ignored_rather_than_raising():
    """This runs on the socket that also carries wake and cancel: a raise here
    would cost the runner its liveness."""
    on_control, waker = _handler()
    on_control({"type": "something_new_from_a_future_server"})
    on_control({})
    assert not waker.event.is_set()


def test_a_malformed_frame_never_reaches_a_branch_that_raises():
    on_control, _w = _handler()
    on_control({"type": "cancel"})              # no turn_id
    on_control({"type": "check_inbox"})         # no mailbox
    on_control({"type": "menu_answer"})         # no session_key
    on_control({"type": "close_session"})       # no session_key


# --- the control frame must RETIRE the answer it just pressed ---------------


class _RecordingClient:
    def __init__(self):
        self.retired = []

    def post_menu_answer_result(self, runner_id, session_id, answer_id, outcome):
        self.retired.append((session_id, answer_id, outcome))


def _menu_handler(monkeypatch, client):
    from canopy_runner import hooks, sessions

    pressed = []
    monkeypatch.setattr(hooks, "answer_menu",
                        lambda key, option, **kw: (pressed.append((key, option, kw)),
                                                   ("answered", None))[1])
    monkeypatch.setattr(hooks, "note_answer_outcome", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "request_report_now", lambda: None)

    waker = WakeListener("http://x", "t", "r1")
    cfg = types.SimpleNamespace(cdp_port=9222, runner_id="r1", base_url="http://x",
                                token="t", state_path=None)
    return make_control_handler(cfg, waker, client), pressed


def test_the_control_frame_retires_the_answer_it_pressed(monkeypatch):
    """The server holds an answer until a runner REPORTS on it — that is what
    makes it survive a dead control channel. A fast path that pressed the key and
    reported nothing would leave the answer queued, and the next poll tick (5s
    later) would press it a SECOND time.

    Harmless on a permission prompt, destructive on a multi-select, where a
    number key toggles: one tap, two presses, checkbox back where it started.
    """
    client = _RecordingClient()
    on_control, pressed = _menu_handler(monkeypatch, client)
    on_control({"type": "menu_answer", "session_key": "k", "option": 1,
                "session_id": "s-1", "answer_id": "a-1"})
    assert len(pressed) == 1
    assert client.retired == [("s-1", "a-1", "answered")]


def test_selections_reach_the_runner_from_the_control_frame(monkeypatch):
    """A multi-select answer cannot be expressed as one option, so the frame's
    `selections` has to survive the dispatch or the whole feature is inert."""
    on_control, pressed = _menu_handler(monkeypatch, _RecordingClient())
    on_control({"type": "menu_answer", "session_key": "k", "option": 1,
                "selections": [[1, 3], [2]], "session_id": "s", "answer_id": "a"})
    assert pressed[0][2]["selections"] == [[1, 3], [2]]


def test_a_failed_retire_does_not_cost_the_socket(monkeypatch):
    """The poll tick is the backstop; a raise here would take down wake and
    cancel with it."""
    class _Boom:
        def post_menu_answer_result(self, *a, **k):
            raise RuntimeError("server down")

    on_control, pressed = _menu_handler(monkeypatch, _Boom())
    on_control({"type": "menu_answer", "session_key": "k", "option": 1,
                "session_id": "s", "answer_id": "a"})
    assert len(pressed) == 1


def test_session_interrupt_frame_rings_without_touching_cdp():
    """A stop aimed at a SESSION — the only route that reaches an agent/board/
    scheduled turn, which is terminal seconds after its prompt is delivered.

    It must only MARK the work due. This runs on the wake-listener thread, which
    also carries cancel, wake and menu_answer; a CDP round trip here would block
    the socket that keeps the runner alive for as long as emdash takes to answer.
    """
    from canopy_runner import cdp_control, session_interrupt

    session_interrupt._pending.clear()
    on_control, waker = _handler()

    def _must_not_run(*a, **k):  # pragma: no cover - the assertion is that it isn't
        raise AssertionError("interrupt must not be pressed on the wake thread")

    real = cdp_control.interrupt
    cdp_control.interrupt = _must_not_run
    try:
        on_control({"type": "session_interrupt", "session_id": "s1", "session_key": "hal-1",
                    "project": "hal", "interrupt_id": "i1"})
    finally:
        cdp_control.interrupt = real

    entry = session_interrupt._pending[("hal", "hal-1")]
    assert entry["session_id"] == "s1"
    assert entry["interrupt_ids"] == {"i1"}
    assert waker.event.is_set(), "the poll thread has work — don't wait out the tick"
    session_interrupt._pending.clear()


def test_session_interrupt_frame_without_a_key_is_ignored():
    from canopy_runner import session_interrupt

    session_interrupt._pending.clear()
    on_control, _w = _handler()
    on_control({"type": "session_interrupt", "session_id": "s1"})
    assert session_interrupt._pending == {}


# --- a socket that went silent ----------------------------------------------------------
#
# After a Mac sleeps, its control socket can stay open on the laptop's side and deliver
# nothing. If the listener only waited on recv(), `connected` would stay True, the loop
# would never poll for phone sessions, and a phone pairing would wait forever.

class _TimeoutError(Exception):
    pass


# websocket-client is optional for the runner (and absent in CI), so a stand-in with the
# names _serve uses. The opcodes are RFC 6455's.
websocket = types.SimpleNamespace(
    WebSocketTimeoutException=_TimeoutError,
    ABNF=types.SimpleNamespace(OPCODE_TEXT=0x1, OPCODE_CLOSE=0x8, OPCODE_PONG=0xA),
)


class _Socket:
    """Replays a script of frames; None is a recv timeout. Counts pings."""

    def __init__(self, script):
        self.script, self.pings = list(script), 0

    def recv_data(self, control_frame=False):
        assert control_frame  # pongs must reach the reader, or silence can't be told apart
        if not self.script:
            raise AssertionError("read past the end of the script")
        item = self.script.pop(0)
        if item is None:
            raise websocket.WebSocketTimeoutException("timed out")
        return item

    def ping(self):
        self.pings += 1


class _Clock:
    def __init__(self, step):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def test_a_silent_socket_is_pinged_then_given_up():
    w = WakeListener("https://x", "t", "r", recv_timeout=30)
    ws = _Socket([None] * 10)
    w._serve(ws, websocket, clock=_Clock(step=30))
    assert 1 <= ws.pings <= 3  # it asked, a few times, and then let the socket go
    assert ws.script  # and stopped reading well before the script ran out


def test_pongs_keep_an_idle_socket_open_and_frames_still_arrive():
    got = []
    w = WakeListener("https://x", "t", "r", recv_timeout=30, on_control=got.append)
    pong = (websocket.ABNF.OPCODE_PONG, b"")
    text = (websocket.ABNF.OPCODE_TEXT, b'{"type": "listen_start", "session_id": "s"}')
    close = (websocket.ABNF.OPCODE_CLOSE, b"")
    ws = _Socket([None, pong, None, pong, None, pong, None, pong, text, close])
    w._serve(ws, websocket, clock=_Clock(step=30))
    assert got == [{"type": "listen_start", "session_id": "s"}]
    assert ws.pings == 4 and not ws.script  # read to the close; never gave up
