"""A runner whose token canopy stopped accepting reconnects itself from the browser (#1705)."""
from pathlib import Path

from canopy_runner import reconnect
from canopy_runner.client import ClientError

DEAD = ClientError("POST /runners/r-1/heartbeat -> 401: b'{\"detail\": \"Authentication required\"}'")
BLIP = ClientError("POST /runners/r-1/heartbeat -> 502: b'bad gateway'")


def _watch(monkeypatch, *, key_active=False, rc=0, now=[1000.0]):
    monkeypatch.setattr(reconnect, "_key_signs_in", lambda rid: key_active)
    calls, restarts = [], []

    def pair(path, **kw):
        calls.append((path, kw))
        return rc

    w = reconnect.AuthWatch(Path("/x/runner.json"), "https://canopy", "r-1", pair_fn=pair,
                            restart=lambda: restarts.append(1), clock=lambda: now[0],
                            spawn=lambda fn: fn())
    return w, calls, restarts


def test_three_refused_heartbeats_open_a_reconnect_and_restart(monkeypatch):
    w, calls, restarts = _watch(monkeypatch)
    for _ in range(2):
        w.note(DEAD)
        assert not w.maybe_reconnect()
    w.note(DEAD)
    assert w.maybe_reconnect()
    assert calls[0][1]["reconnect"] is True and calls[0][1]["base_url"] == "https://canopy"
    assert restarts == [1]


def test_a_blip_or_a_good_tick_resets_the_count(monkeypatch):
    w, calls, _ = _watch(monkeypatch)
    w.note(DEAD)
    w.note(DEAD)
    w.note(BLIP)
    w.note(DEAD)
    w.ok()
    w.note(DEAD)
    assert not w.maybe_reconnect() and not calls


def test_an_active_device_key_means_nothing_to_reconnect(monkeypatch):
    w, calls, _ = _watch(monkeypatch, key_active=True)
    for _ in range(5):
        w.note(DEAD)
    assert not w.maybe_reconnect() and not calls


def test_a_failed_attempt_waits_before_the_next(monkeypatch):
    now = [1000.0]
    w, calls, restarts = _watch(monkeypatch, rc=1, now=now)
    for _ in range(3):
        w.note(DEAD)
    assert w.maybe_reconnect() and not restarts
    assert not w.maybe_reconnect()
    now[0] += reconnect.RETRY_SECONDS + 1
    assert w.maybe_reconnect() and len(calls) == 2


def _browser_watch(monkeypatch, *, windows):
    from canopy_runner import platform_jobs

    monkeypatch.setattr(reconnect, "_key_signs_in", lambda rid: False)
    monkeypatch.setattr(platform_jobs, "is_windows", lambda: windows)
    notices, opened = [], []

    def pair(path, **kw):
        # What run_browser_pair does with each new pairing: open its approve page.
        return 0 if kw["open_browser"]("https://canopy/pair/ABCD-EFGH") else 1

    w = reconnect.AuthWatch(Path("/x/runner.json"), "https://canopy", "r-1", pair_fn=pair,
                            restart=lambda: None, spawn=lambda fn: fn(),
                            notice=lambda code, url: notices.append(code),
                            browser=lambda url: opened.append(url) or True)
    return w, notices, opened


def test_windows_names_the_code_before_the_browser_opens_once_per_code(monkeypatch):
    w, notices, opened = _browser_watch(monkeypatch, windows=True)
    w._open_browser("https://canopy/pair/ABCD-EFGH")
    w._open_browser("https://canopy/pair/ABCD-EFGH")
    w._open_browser("https://canopy/pair/WXYZ-2345")
    assert notices == ["ABCD-EFGH", "WXYZ-2345"]
    assert len(opened) == 3


def test_a_reconnect_hands_the_noticing_opener_to_the_pairing(monkeypatch):
    w, notices, opened = _browser_watch(monkeypatch, windows=True)
    for _ in range(3):
        w.note(DEAD)
    assert w.maybe_reconnect()
    assert notices == ["ABCD-EFGH"] and opened == ["https://canopy/pair/ABCD-EFGH"]


def test_macos_shows_no_notice(monkeypatch):
    w, notices, opened = _browser_watch(monkeypatch, windows=False)
    w._open_browser("https://canopy/pair/ABCD-EFGH")
    assert notices == [] and opened == ["https://canopy/pair/ABCD-EFGH"]


def test_the_notice_does_not_wait_and_opens_no_console(monkeypatch):
    from canopy_runner import platform_jobs

    monkeypatch.setattr(platform_jobs, "is_windows", lambda: True)
    seen = {}

    class Stdin:
        @staticmethod
        def write(text):
            seen["text"] = text

        @staticmethod
        def close():
            seen["closed"] = True

    class Proc:
        stdin = Stdin

        def wait(self):  # pragma: no cover — must never be called
            raise AssertionError("the notice must not block the runner")

    def popen(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return Proc()

    assert reconnect.notify_code("ABCD-EFGH", "https://canopy/pair/ABCD-EFGH", popen=popen)
    assert seen["cmd"][0] == "powershell" and seen["kw"]["creationflags"] == 0x08000000
    assert "ABCD-EFGH" in seen["text"] and seen["closed"]
    assert "ABCD-EFGH" not in " ".join(seen["cmd"]), "the text goes on stdin, not the command line"
