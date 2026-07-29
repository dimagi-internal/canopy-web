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
