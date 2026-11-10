"""canopy-listen serve without a network: pairing, the phone's session, the meeting it starts,
and the audio it hands the recorder. A fake phone speaks relaycrypto; a fake command runner
stands in for canopy-runner; the unix socket is real."""
import asyncio
import json
import os
import struct
import subprocess
import threading
import time

import numpy as np
import pytest

rc = pytest.importorskip("canopy_listen.relaycrypto")
from canopy_listen import cli, serve, store  # noqa: E402
from canopy_listen.live import SAMPLE_RATE, Sentence  # noqa: E402
from canopy_listen.recorder import RelaySource, _speakers, record  # noqa: E402

APP_START = ["app", "start", "meeting-assistant", "--accept-notice"]


@pytest.fixture(autouse=True)
def _homes(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_LISTEN_HOME", str(tmp_path / "listen"))
    monkeypatch.setenv("CANOPY_LISTEN_CANOPY_HOME", str(tmp_path / "canopy"))
    monkeypatch.setenv("CANOPY_LISTEN_RUNNER", "/usr/bin/false")  # never this Mac's real runner
    (tmp_path / "listen").mkdir()
    (tmp_path / "canopy").mkdir()
    (tmp_path / "canopy" / "runner.json").write_text(json.dumps(
        {"runner_id": "r-1", "base_url": "https://canopy.example", "token": "never-read"}))


class Runner:
    """canopy-runner, faked: records each command and what was queued when it ran."""

    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, argv):
        queued = store.home() / "next-input.json"
        self.calls.append((argv[1:], json.loads(queued.read_text()) if queued.exists() else None))
        return self.code, ""


class Phone:
    def __init__(self):
        self.static = rc.new_private()
        self.key = rc.public_b64(self.static)

    def session(self, laptop_pub, sid):
        eph = rc.new_private()
        up, down = rc.session_keys_phone(eph, self.static, laptop_pub, sid)
        return {"phone_eph": rc.public_b64(eph), "phone_key": self.key, "label": "Pixel"}, rc.Sealer(up, sid), \
            rc.Opener(down, sid)


def make(runner=None):
    return serve.Serve(run=runner or Runner(), name="Test Mac")


async def settle(s):
    while s.controller.tasks:
        await asyncio.gather(*list(s.controller.tasks), return_exceptions=True)


def opened(opener, frames):
    out = []
    for frame in frames:
        kind, body = opener.open(frame)
        assert kind == rc.CONTROL
        out.append(json.loads(body))
    return out


async def meeting_session(s, phone, sid="sess-1"):
    hello, up, down = phone.session(s.key.public(), sid)
    sent = []

    async def send(frame):
        sent.append(frame)

    session = serve.RelaySession(s, sid, "meeting", hello, send)
    return session, up, down, sent


def fake_recorder(meeting):
    """A live 'recorder' process for a meeting, so pid_of() sees it; reaped when it dies."""
    proc = subprocess.Popen(["sleep", "30"])
    threading.Thread(target=proc.wait, daemon=True).start()
    (store.meeting_dir(meeting["id"]) / "pid").write_text(str(proc.pid))
    store.set_active(meeting["id"])
    return proc


# --- pairing ------------------------------------------------------------------------


def pair_frame(entry_secret, sid, phone_key, label="Jon's iPhone"):
    up, down = rc.pair_keys(entry_secret, sid)
    return rc.Sealer(up, sid).control({"type": "pair", "phone_key": phone_key, "label": label}), rc.Opener(down, sid)


def test_a_pairing_secret_pairs_one_phone_once_and_only_for_ten_minutes():
    async def go():
        now = [1000.0]
        s = make()
        s.pairings.clock = lambda: now[0]
        entry = s.pairings.new()
        phone = Phone()

        async def pair(sid):
            sent = []

            async def send(frame):
                sent.append(frame)

            frame, opener = pair_frame(entry["secret"], sid, phone.key)
            session = serve.RelaySession(s, sid, "pair", {"phone_eph": "x", "phone_key": None}, send)
            assert await session.open()
            assert await session.receive(frame) is False  # a pair session always closes after its answer
            return opened(opener, sent)

        assert await pair("p-1") == [{"type": "paired", "name": "Test Mac"}]
        assert s.phones.known(phone.key)["label"] == "Jon's iPhone"
        assert s.pairings.status(entry["id"]) == {"state": "paired", "label": "Jon's iPhone"}
        assert await pair("p-2") == [{"type": "refused", "reason": "used"}]

        late = s.pairings.new()
        now[0] += serve.PAIR_SECONDS + 1
        entry = late
        assert await pair("p-3") == [{"type": "refused", "reason": "expired"}]
        assert s.pairings.status(late["id"]) == {"state": "expired"}
        assert len(s.phones.all()) == 1
    asyncio.run(go())


def test_a_pair_frame_no_secret_opens_is_dropped_without_an_answer():
    async def go():
        s = make()
        s.pairings.new()
        sent = []

        async def send(frame):
            sent.append(frame)

        frame, _ = pair_frame(rc.new_pairing_secret(), "p-1", Phone().key)
        session = serve.RelaySession(s, "p-1", "pair", {}, send)
        assert await session.open()
        assert await session.receive(frame) is False
        assert sent == [] and s.phones.all() == []
    asyncio.run(go())


def test_no_pair_session_is_taken_before_any_code_was_made():
    async def go():
        session = serve.RelaySession(make(), "p-1", "pair", {}, None)
        assert await session.open() is False
    asyncio.run(go())


# --- pairing by code (from canopy-web, no QR) -------------------------------------------


class Asked:
    """The dialog, faked: remembers what it showed and answers `allow`."""

    def __init__(self, allow=True):
        self.allow, self.shown = allow, []

    async def __call__(self, label, code, name):
        self.shown.append((label, code, name))
        return self.allow


async def code_pairing(s, phone, sid="c-1", *, reveal_key=None, label="Chrome on Mac"):
    """The phone's half of pairing by code. Returns (the code the phone shows, what the
    laptop answered after the reveal, and the session's frames)."""
    eph, nonce = rc.new_private(), os.urandom(32)
    hello = {"phone_eph": rc.public_b64(eph), "phone_key": None, "label": label,
             "commit": rc.pair_commit(phone.key, nonce)}
    sent = []

    async def send(frame):
        sent.append(frame)

    session = serve.RelaySession(s, sid, "pair", hello, send)
    if not await session.open():
        return None, [rc.read_handshake(f) for f in sent], session
    laptop = rc.read_handshake(sent.pop(0))
    assert laptop["type"] == "hello" and laptop["name"] == "Test Mac" and laptop["laptop_key"] == s.key.public()
    code = rc.pair_code(sid, phone.key, laptop["laptop_key"], nonce, rc.unb64u(laptop["nonce"]))
    reveal = {"type": "reveal", "phone_key": reveal_key or phone.key, "nonce": rc.b64u(nonce), "label": label}
    assert await session.receive(rc.handshake_frame(reveal)) is False  # one exchange, then it closes
    answers = []
    for frame in sent:
        if frame[0] == rc.HANDSHAKE:
            answers.append(rc.read_handshake(frame))
        else:
            _, down = rc.session_keys_phone(eph, phone.static, laptop["laptop_key"], sid)
            answers.append(opened(rc.Opener(down, sid), [frame])[0])
    return code, answers, session


def test_pairing_by_code_asks_this_mac_with_the_code_the_page_shows_then_the_phone_can_record():
    async def go():
        asked = Asked(allow=True)
        s = serve.Serve(run=Runner(), name="Test Mac", confirm=asked)
        phone = Phone()
        code, answers, _ = await code_pairing(s, phone)
        assert asked.shown == [("Chrome on Mac", code, "Test Mac")] and len(code) == 6 and code.isdigit()
        assert answers == [{"type": "paired", "name": "Test Mac"}]  # sealed: only the real laptop can say it
        assert s.phones.known(phone.key)["label"] == "Chrome on Mac"
        session, _, down, sent = await meeting_session(s, phone)
        assert await session.open() and opened(down, sent)[0]["type"] == "ready"
    asyncio.run(go())


def test_a_declined_code_pairs_nothing():
    async def go():
        s = serve.Serve(run=Runner(), name="Test Mac", confirm=Asked(allow=False))
        phone = Phone()
        _, answers, _ = await code_pairing(s, phone)
        assert answers == [{"type": "refused", "reason": "declined"}] and s.phones.all() == []
    asyncio.run(go())


def test_a_key_swapped_after_the_commitment_is_dropped_before_anyone_is_asked():
    async def go():
        asked = Asked()
        s = serve.Serve(run=Runner(), name="Test Mac", confirm=asked)
        _, answers, _ = await code_pairing(s, Phone(), reveal_key=Phone().key)  # a relay substituting its own key
        assert answers == [] and asked.shown == [] and s.phones.all() == []
    asyncio.run(go())


def test_one_code_on_screen_at_a_time():
    async def go():
        s = serve.Serve(run=Runner(), name="Test Mac", confirm=Asked())
        s.confirming = True
        code, answers, _ = await code_pairing(s, Phone())
        assert code is None and answers == [{"type": "refused", "reason": "busy"}]
    asyncio.run(go())


def test_the_dialog_gets_the_label_as_an_argument_never_as_script(monkeypatch):
    seen = {}

    async def fake_exec(*argv, **_):
        seen["argv"] = argv

        class Proc:
            async def communicate(self):
                return b"yes\n", b""
        return Proc()

    monkeypatch.setattr(serve.shutil, "which", lambda _: "/usr/bin/osascript")
    monkeypatch.setattr(serve.asyncio, "create_subprocess_exec", fake_exec)
    label = 'Evil" & do shell script "rm -rf ~'
    assert asyncio.run(serve.ask_on_screen(label, "123456", "Test Mac")) is True
    assert label not in seen["argv"][2] and label in seen["argv"][3] and "123 456" in seen["argv"][3]


def test_the_laptop_key_is_made_once_and_kept_private():
    s = make()
    first = s.key.public()
    assert serve.LaptopKey().public() == first
    assert oct(os.stat(store.home() / "relay-key.json").st_mode & 0o777) == "0o600"


# --- the meeting session -----------------------------------------------------------


def test_an_unpaired_phone_is_refused_and_told_nothing():
    async def go():
        s = make()
        session, _, _, sent = await meeting_session(s, Phone())
        assert await session.open() is False
        assert sent == []
    asyncio.run(go())


def test_ready_comes_first_and_says_who_this_is():
    async def go():
        s, phone = make(), Phone()
        s.phones.add(phone.key, "Pixel")
        session, _, down, sent = await meeting_session(s, phone)
        assert await session.open()
        assert opened(down, sent) == [{"type": "ready", "name": "Test Mac"}]
    asyncio.run(go())


def test_a_forged_or_replayed_frame_drops_the_session():
    async def go():
        s, phone = make(), Phone()
        s.phones.add(phone.key, "Pixel")
        session, up, _, _ = await meeting_session(s, phone)
        await session.open()
        frame = up.control({"type": "rename", "speaker": 0, "name": "x"})
        assert await session.receive(frame)
        assert await session.receive(frame) is False  # replayed

        session, up, _, _ = await meeting_session(s, phone, "sess-2")
        await session.open()
        forged = bytearray(up.control({"type": "end"}))
        forged[-1] ^= 1
        assert await session.receive(bytes(forged)) is False

        session, up, _, _ = await meeting_session(s, phone, "sess-3")
        await session.open()
        other_session = rc.Sealer(rc.unb64u(rc.b64u(os.urandom(32))), "sess-3")
        assert await session.receive(other_session.control({"type": "start", "notice_accepted": True})) is False
    asyncio.run(go())


def test_start_queues_the_relay_input_then_starts_meeting_assistant():
    async def go():
        runner = Runner()
        s, phone = make(runner), Phone()
        s.phones.add(phone.key, "Pixel")
        session, up, down, sent = await meeting_session(s, phone)
        await session.open()
        assert await session.receive(up.control({"type": "start", "notice_accepted": True}))
        await settle(s)
        argv, queued = runner.calls[0]
        assert argv == APP_START
        assert queued == {"relay": session.input.id, "source": "phone"}
        assert opened(down, sent)[1] == {"type": "state", "state": "starting", "detail": ""}
        await session.close()
        assert session.input.ended
    asyncio.run(go())


def test_start_needs_the_notice_accepted_and_one_meeting_at_a_time():
    async def go():
        runner = Runner()
        s, phone = make(runner), Phone()
        s.phones.add(phone.key, "Pixel")
        session, up, down, sent = await meeting_session(s, phone)
        await session.open()
        await session.receive(up.control({"type": "start"}))
        assert opened(down, sent)[-1] == {"type": "state", "state": "failed", "detail": "notice_not_accepted"}
        assert runner.calls == []

        meeting = store.new_meeting("", "microphone")
        proc = fake_recorder(meeting)
        try:
            await session.receive(up.control({"type": "start", "notice_accepted": True}))
            assert opened(down, sent[2:])[-1] == {"type": "state", "state": "failed", "detail": "busy"}
            assert runner.calls == []
        finally:
            proc.kill()
    asyncio.run(go())


def test_a_failed_app_start_is_reported_and_unqueues_the_input():
    async def go():
        runner = Runner(code=1)
        s = make(runner)
        assert s.controller.start("phone", "in-1")["ok"]
        await settle(s)
        assert not (store.home() / "next-input.json").exists()
        assert s.controller.view()["phase"] == "failed"
    asyncio.run(go())


def test_the_phone_hears_its_meeting_live_and_its_end():
    async def go():
        s, phone = make(), Phone()
        s.phones.add(phone.key, "Pixel")
        session, up, down, sent = await meeting_session(s, phone)
        await session.open()
        await session.receive(up.control({"type": "start", "notice_accepted": True}))
        await settle(s)
        meeting = store.new_meeting("", "phone")
        meeting.update(input=session.input.id, state="recording", speakers=[{"name": "Speaker 1", "number": 0}],
                       segments=[{"text": "hello there", "start": 0.0, "end": 1.2, "speaker": 0, "words": []}])
        store.save(meeting)
        proc = fake_recorder(meeting)
        await asyncio.sleep(serve.FOLLOW_SECONDS * 2.5)
        got = opened(down, sent)
        assert {"type": "segment", "start": 0.0, "end": 1.2, "speaker": 0, "text": "hello there"} in got
        assert {"type": "speakers", "speakers": [{"name": "Speaker 1", "number": 0}]} in got
        assert {"type": "state", "state": "recording", "detail": ""} in got

        app = store.home().parent / "canopy" / "apps" / "meeting-assistant"
        app.mkdir(parents=True)
        (app / "run.json").write_text(json.dumps({"started_at": time.time(), "session_url": "https://c.example/s/1"}))
        (app / "log.jsonl").write_text(json.dumps({"t": time.time(), "kind": "event", "name": "saved"}) + "\n")
        proc.kill()
        meeting["state"] = "ended"
        store.save(meeting)
        await asyncio.wait_for(session.follow, 5)
        got = opened(down, sent[len(got):])
        assert {"type": "chat", "url": "https://c.example/s/1"} in got
        assert {"type": "state", "state": "ended", "detail": ""} in got
        assert {"type": "saved", "url": "https://canopy.example/meetings"} in got
    asyncio.run(go())


def test_end_discard_and_rename_reach_the_right_commands():
    async def go():
        runner = Runner()
        s, phone = make(runner), Phone()
        s.phones.add(phone.key, "Pixel")
        session, up, _, _ = await meeting_session(s, phone)
        await session.open()
        await session.receive(up.control({"type": "start", "notice_accepted": True}))
        await settle(s)
        meeting = store.new_meeting("", "phone")
        meeting.update(input=session.input.id, state="ended", speakers=[{"name": "Speaker 1", "number": 0}],
                       segments=[{"text": "hi", "start": 0.0, "end": 1.0, "speaker": 0, "words": []}])
        store.save(meeting)

        await session.receive(up.control({"type": "rename", "speaker": 0, "name": "  Amal   K "}))
        renamed = store.load(meeting["id"])
        assert renamed["speakers"] == [{"name": "Amal K", "number": 0}]
        assert renamed["transcript"] == "Amal K: hi"

        await session.receive(up.control({"type": "end"}))
        assert runner.calls[-1][0] == ["app", "end", "meeting-assistant"]
        assert session.input.ended

        await session.receive(up.control({"type": "discard"}))
        assert runner.calls[-1][0] == ["app", "stop", "meeting-assistant"]
        assert store.load(meeting["id"]) is None
        assert s.controller.view()["phase"] == "discarded"
    asyncio.run(go())


def test_end_stops_the_recording_itself_when_no_app_run_answers():
    async def go():
        s = make(Runner(code=1))
        meeting = store.new_meeting("", "microphone")
        proc = fake_recorder(meeting)
        s.controller.current = {"source": "microphone", "input": None, "started_at": 0, "phase": "starting",
                                "detail": "", "meeting_id": meeting["id"], "ending": False, "discarding": False}
        await s.controller.end()
        assert proc.wait(5) is not None
    asyncio.run(go())


def test_phase_follows_the_meeting_and_the_app():
    c = {"phase": "starting", "detail": "", "ending": False, "discarding": False}
    idle = {"running": False, "saved": False}
    assert serve.phase_of(None, None, idle) == ("idle", "")
    assert serve.phase_of(c, None, idle) == ("starting", "")
    assert serve.phase_of(c, {"state": "recording", "segments": []}, idle)[0] == "recording"
    assert serve.phase_of(c, {"state": "recording", "segments": [1]}, idle)[0] == "listening"
    assert serve.phase_of({**c, "ending": True}, {"state": "recording"}, idle)[0] == "ending"
    assert serve.phase_of(c, {"state": "ended"}, {"running": True, "saved": False}) == ("ending", "saving")
    assert serve.phase_of(c, {"state": "ended"}, {"running": False, "saved": True})[0] == "saved"
    assert serve.phase_of(c, {"state": "failed", "error": "RelayRefusedError"}, idle) == ("failed", "RelayRefusedError")


# --- the local page's guard and API -----------------------------------------------


def test_only_this_page_may_use_the_local_server():
    ok = serve.allowed
    assert ok("127.0.0.1:7351", None, 7351, needs_origin=False)
    assert ok("localhost:7351", "http://localhost:7351", 7351, needs_origin=True)
    assert ok("127.0.0.1:7351", "http://127.0.0.1:7351", 7351, needs_origin=True)
    assert not ok("127.0.0.1:7351", None, 7351, needs_origin=True)  # a change must say where it came from
    assert not ok("127.0.0.1:7351", "https://evil.example", 7351, needs_origin=True)
    assert not ok("127.0.0.1:7351", "https://evil.example", 7351, needs_origin=False)
    assert not ok("127.0.0.1:7351", "null", 7351, needs_origin=True)
    assert not ok("evil.example:7351", None, 7351, needs_origin=False)  # DNS rebinding
    assert not ok("127.0.0.1:8000", "http://127.0.0.1:8000", 7351, needs_origin=True)


def test_the_page_starts_a_browser_meeting_and_pairs_a_phone():
    async def go():
        runner = Runner()
        s = make(runner)
        assert s.api_start({"input": "browser-tab"})[0] == 400  # the notice first
        code, body = s.api_start({"input": "browser-tab", "notice_accepted": True})
        assert code == 200 and s.hub.get(body["input"]).source == "browser-tab"
        await settle(s)
        assert runner.calls[0] == (APP_START, {"relay": body["input"], "source": "browser-tab"})
        assert s.api_live()["phase"] == "starting"

        code, pairing = s.api_pair()
        assert code == 200 and pairing["svg"].startswith("<svg")
        blob = json.loads(rc.unb64u(pairing["url"].split("#pair=")[1]))
        assert pairing["url"].startswith("https://canopy.example/listen#pair=")
        assert blob["r"] == "r-1" and blob["k"] == s.key.public() and blob["n"] == "Test Mac"
        assert len(rc.unb64u(blob["s"])) == 32 and blob["x"] > time.time() + 500
        assert s.api_pair_status(pairing["id"]) == (200, {"state": "waiting", "expires": pairing["expires"]})
    asyncio.run(go())


def test_the_microphone_start_queues_nothing_and_clears_a_stale_queue():
    async def go():
        runner = Runner()
        s = make(runner)
        (store.home() / "next-input.json").write_text(json.dumps({"relay": "old", "source": "phone"}))
        assert s.api_start({"input": "microphone", "notice_accepted": True})[0] == 200
        await settle(s)
        assert runner.calls == [(APP_START, None)]
    asyncio.run(go())


# --- audio to the recorder ---------------------------------------------------------


class Loop:
    """serve.main on a real unix socket, in a background thread."""

    def __init__(self, s):
        self.s, self.loop = s, asyncio.new_event_loop()
        self.stop = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not serve.ping() and time.monotonic() < deadline:
            time.sleep(0.02)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.stop = asyncio.Event()
        self.loop.run_until_complete(serve.main(0, self.s, self.stop, web_page=False))

    def call(self, fn, *args):
        done, box = threading.Event(), {}

        def go():
            box["v"] = fn(*args)
            done.set()

        self.loop.call_soon_threadsafe(go)
        done.wait(5)
        return box.get("v")

    def close(self):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(5)


def pcm(seconds, value=0.25):
    return (np.full(int(seconds * SAMPLE_RATE), value * 32767)).astype("<i2").tobytes()


def test_relayed_audio_reaches_the_recorders_source_and_its_end_ends_it():
    loop = Loop(make())
    try:
        assert serve.ping()["name"] == "Test Mac"
        inp = loop.call(loop.s.hub.create, "phone")
        loop.call(inp.push, pcm(0.3))  # before the recorder connects: held for it
        source = RelaySource(inp.id, "phone")
        source.start()
        loop.call(inp.push, pcm(0.2))
        loop.call(inp.end)
        assert source.done.wait(5)
        got = []
        while not source.chunks.empty():
            got.append(source.chunks.get())
        audio = np.concatenate(got)
        assert len(audio) == int(0.5 * SAMPLE_RATE) and abs(float(audio.mean()) - 0.25) < 1e-3
        assert loop.call(lambda: inp.level) == 0.0
        with pytest.raises(Exception, match="unknown_input"):
            RelaySource("no-such-input").start()
    finally:
        loop.close()


def test_a_relayed_meeting_records_like_any_other():
    loop = Loop(make())
    try:
        inp = loop.call(loop.s.hub.create, "browser-tab")
        meeting = store.new_meeting("", "browser-tab")

        def recognise(buffer):
            n = len(buffer) / SAMPLE_RATE
            return [Sentence("we ship on monday", 0.0, min(2.0, n))] if n > 0.5 else []

        def feed():
            for _ in range(30):
                loop.call(inp.push, pcm(0.1))
            loop.call(inp.end)

        threading.Thread(target=feed, daemon=True).start()
        store.set_name(meeting["id"], 0, "Amal")
        assert record(meeting["id"], RelaySource(inp.id, "browser-tab"), recognise=recognise, keep_audio=False) == 0
        saved = store.load(meeting["id"])
        assert saved["state"] == "ended" and saved["source"] == "browser-tab"
        assert saved["segments"][0]["text"] == "we ship on monday"
        assert saved["speakers"] == [{"name": "Amal", "number": 0}]
        assert saved["transcript"].startswith("Amal: we ship")
    finally:
        loop.close()


def test_start_records_the_queued_relay_input(monkeypatch):
    spawned = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
    (store.home() / "next-input.json").write_text(json.dumps({"relay": "in-9", "source": "phone"}))
    cli.main(["start", "--wait", "0", "--json"])
    meeting = store.listing(1)[0]
    assert meeting["source"] == "phone" and meeting["input"] == "in-9"
    assert spawned[0][-4:] == ["--relay", "in-9", "--source", "phone"]
    assert not (store.home() / "next-input.json").exists()


# --- speaker names -----------------------------------------------------------------


def test_names_survive_the_speaker_list_being_recomputed():
    segments = [{"speaker": 0}, {"speaker": 1}, {"speaker": 2}]
    previous = [{"number": 0, "name": "Amal"}, {"number": 1, "name": "Speaker 2"}]
    assert _speakers(segments, previous) == [{"name": "Amal", "number": 0}, {"name": "Speaker 2", "number": 1},
                                             {"name": "Speaker 3", "number": 2}]
    assert _speakers(segments, previous, {2: "Bo", 0: ""}) == [
        {"name": "Speaker 1", "number": 0}, {"name": "Speaker 2", "number": 1}, {"name": "Bo", "number": 2}]
    assert _speakers(segments) == [{"name": f"Speaker {n + 1}", "number": n} for n in range(3)]


def test_framing_is_length_prefixed():
    assert struct.pack(">I", 3) + b"abc" == b"\x00\x00\x00\x03abc"


# --- the QR code --------------------------------------------------------------------


def test_the_qr_code_has_its_finder_patterns_and_fits_a_pairing_link():
    from canopy_listen import qr

    code = qr.QR(b"https://canopy.dimagi.com/listen#pair=" + b"x" * 300)
    n = code.size
    assert n == code.version * 4 + 17
    for cx, cy in ((0, 0), (n - 7, 0), (0, n - 7)):
        ring = [code.modules[cy][cx + i] for i in range(7)]
        assert ring == [True] * 7
        assert code.modules[cy + 1][cx + 1] is False and code.modules[cy + 3][cx + 3] is True
    assert qr.svg("hi").count("<path") == 1
