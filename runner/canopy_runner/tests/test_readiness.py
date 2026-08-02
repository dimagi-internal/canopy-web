from types import SimpleNamespace

from canopy_runner import readiness


def _cfg(tmp_path):
    return SimpleNamespace(state_path=str(tmp_path / "runner-state.json"), cdp_port=9222)


def test_compute_not_ready_when_cdp_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.cdp_control, "cdp_healthy", lambda **kw: False)
    ready, note = readiness.compute(_cfg(tmp_path))
    assert ready is False
    assert "emdash" in note.lower() or "cdp" in note.lower()


def test_compute_ready_when_cdp_healthy_and_no_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.cdp_control, "cdp_healthy", lambda **kw: True)
    ready, note = readiness.compute(_cfg(tmp_path))
    assert ready is True and note == ""


def test_reactive_failure_flips_not_ready_until_cleared(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.cdp_control, "cdp_healthy", lambda **kw: True)
    cfg = _cfg(tmp_path)
    readiness.mark_failed(cfg, "Not logged in")
    ready, note = readiness.compute(cfg)
    assert ready is False and note == "Not logged in"     # CDP fine, but a turn just failed
    readiness.mark_ok(cfg)
    ready, note = readiness.compute(cfg)
    assert ready is True and note == ""                    # a clean run clears it


def test_marker_survives_process_restart(tmp_path, monkeypatch):
    """--drain-one is one-shot; the marker must persist on disk, not in memory."""
    monkeypatch.setattr(readiness.cdp_control, "cdp_healthy", lambda **kw: True)
    cfg = _cfg(tmp_path)
    readiness.mark_failed(cfg, "boom")
    # a fresh cfg pointing at the same state dir (simulates a new process)
    cfg2 = _cfg(tmp_path)
    assert readiness.compute(cfg2) == (False, "boom")


# --- the marker must not latch forever -------------------------------------

def test_a_failure_stops_holding_the_box_out_after_a_while(tmp_path, monkeypatch):
    """`mark_ok` is only ever called after a turn EXECUTES, and routing will not
    give a not-ready runner a turn — so a latching marker removed a box from the
    fleet permanently, escapable only by a human deleting a file.

    Observed 2026-08-01: a laptop shut down mid-turn, the in-flight POST failed
    with a DNS error, and the box came back online-but-unroutable. A network blip
    during shutdown says nothing about whether it can run anything.
    """
    import os
    import time as _time

    from canopy_runner import cdp_control, readiness

    monkeypatch.setattr(cdp_control, "cdp_healthy", lambda **kw: True)
    cfg = type("Cfg", (), {"state_path": str(tmp_path / "state.json"), "cdp_port": 9222})()

    readiness.mark_failed(cfg, "runner execute crashed: nodename nor servname provided")
    assert readiness.compute(cfg)[0] is False, "a fresh failure must still hold it out"

    marker = tmp_path / "not-ready"
    old = _time.time() - readiness.MARKER_TTL_SECONDS - 60
    os.utime(marker, (old, old))

    ready, note = readiness.compute(cfg)
    assert ready is True and note == ""
    assert not marker.exists(), (
        "the stale marker must be removed, or the next real failure reports a "
        "reason that already healed")


def test_expiry_never_overrides_a_dead_cdp(tmp_path, monkeypatch):
    """The proactive half is a live probe, not a memory — an old marker says
    nothing about emdash being down right now."""
    from canopy_runner import cdp_control, readiness

    monkeypatch.setattr(cdp_control, "cdp_healthy", lambda **kw: False)
    cfg = type("Cfg", (), {"state_path": str(tmp_path / "state.json"), "cdp_port": 9222})()
    assert readiness.compute(cfg) == (False, "emdash CDP unreachable")
