"""Wrapper-level tests for the emdash CDP control (the sidecar itself needs a live
emdash on the debug port — validated separately)."""
import json
import urllib.error
from types import SimpleNamespace

import pytest

from canopy_runner import cdp_control


def _fake_run(stdout, returncode=0, stderr=""):
    def run(cmd, capture_output, text, timeout):
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    return run


def test_list_tasks_parses(monkeypatch):
    monkeypatch.setattr(cdp_control.subprocess, "run",
                        _fake_run(json.dumps({"ok": True, "tasks": ["a", "b"], "projects": ["echo"]})))
    r = cdp_control.list_tasks()
    assert r["tasks"] == ["a", "b"] and r["projects"] == ["echo"]


def test_open_send_ok(monkeypatch):
    monkeypatch.setattr(cdp_control.subprocess, "run",
                        _fake_run(json.dumps({"ok": True, "action": "sent", "task": "T"})))
    assert cdp_control.open_and_send("T", "hi there")["action"] == "sent"


def test_interrupt_calls_run_with_task_and_port(monkeypatch):
    """interrupt() must reach the sidecar's `interrupt` command with {task, port} — the
    same call shape open_and_send uses (matching _run's convention: command name + a
    plain args dict; port default matches open_and_send's default of 9222)."""
    captured = {}

    def fake_run(command, args, **kwargs):
        captured["command"] = command
        captured["args"] = args
        return {"ok": True, "task": args["task"]}

    monkeypatch.setattr(cdp_control, "_run", fake_run)
    result = cdp_control.interrupt("my-task")
    assert captured["command"] == "interrupt"
    assert captured["args"] == {"task": "my-task", "port": 9222}
    assert result["task"] == "my-task"


def test_interrupt_passes_a_non_default_port(monkeypatch):
    captured = {}
    monkeypatch.setattr(cdp_control, "_run",
                        lambda command, args, **k: captured.update(args=args) or {"ok": True})
    cdp_control.interrupt("my-task", port=9333)
    assert captured["args"] == {"task": "my-task", "port": 9333}


def test_interrupt_ok(monkeypatch):
    monkeypatch.setattr(cdp_control.subprocess, "run",
                        _fake_run(json.dumps({"ok": True, "task": "T"})))
    assert cdp_control.interrupt("T")["task"] == "T"


def test_sidecar_error_raises_cdperror(monkeypatch):
    monkeypatch.setattr(cdp_control.subprocess, "run",
                        _fake_run(json.dumps({"ok": False, "error": 'no existing task "X"'})))
    with pytest.raises(cdp_control.CDPError, match="no existing task"):
        cdp_control.open_and_send("X", "hi")


def test_non_json_output_raises(monkeypatch):
    monkeypatch.setattr(cdp_control.subprocess, "run", _fake_run("kaboom not json", stderr="trace"))
    with pytest.raises(cdp_control.CDPError, match="non-JSON"):
        cdp_control.list_tasks()


def test_node_missing_raises(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(cdp_control.subprocess, "run", boom)
    with pytest.raises(cdp_control.CDPError, match="node not found"):
        cdp_control.list_tasks()


def test_host_id_has_user_and_host():
    h = cdp_control.host_id()
    assert "@" in h and len(h) > 2


# --------------------------------------------------------------------------------------
# cdp_healthy — the claim preflight; a green probe means create/reuse will connect
# --------------------------------------------------------------------------------------


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_cdp_healthy_true_on_200(monkeypatch):
    monkeypatch.setattr(cdp_control.urllib.request, "urlopen",
                        lambda url, timeout: _Resp(200))
    assert cdp_control.cdp_healthy(port=9222) is True


def test_cdp_healthy_false_on_non_200(monkeypatch):
    monkeypatch.setattr(cdp_control.urllib.request, "urlopen",
                        lambda url, timeout: _Resp(500))
    assert cdp_control.cdp_healthy() is False


def test_cdp_healthy_false_on_connection_refused(monkeypatch):
    def refused(url, timeout):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(cdp_control.urllib.request, "urlopen", refused)
    assert cdp_control.cdp_healthy() is False


def test_cdp_healthy_false_on_timeout(monkeypatch):
    def slow(url, timeout):
        raise TimeoutError("timed out")  # a TimeoutError IS an OSError — must be caught
    monkeypatch.setattr(cdp_control.urllib.request, "urlopen", slow)
    assert cdp_control.cdp_healthy() is False


# --------------------------------------------------------------------------------------
# host_id — the reuse OWNERSHIP key; must be stable or session continuity silently dies
# --------------------------------------------------------------------------------------

def test_host_id_pins_the_first_value_it_computes(monkeypatch, tmp_path):
    pin = tmp_path / "host-id"
    monkeypatch.setattr(cdp_control, "HOST_ID_PATH", pin)
    monkeypatch.setattr(cdp_control.socket, "gethostname", lambda: "Jonathans-MacBook-Pro.local")
    monkeypatch.setattr(cdp_control.getpass, "getuser", lambda: "jjackson")
    assert cdp_control.host_id() == "jjackson@Jonathans-MacBook-Pro.local"
    assert pin.read_text().strip() == "jjackson@Jonathans-MacBook-Pro.local"


def test_host_id_survives_a_macos_hostname_flap(monkeypatch, tmp_path):
    """THE bug (proved live 2026-07-15): macOS flaps gethostname() between the Bonjour
    and DHCP names. reusable_by() compares this value by EQUALITY, so every flap
    orphaned every SessionLink recorded under the other name — reuse silently returned
    false and each thread got a fresh cold session, with no error logged anywhere."""
    pin = tmp_path / "host-id"
    monkeypatch.setattr(cdp_control, "HOST_ID_PATH", pin)
    monkeypatch.setattr(cdp_control.getpass, "getuser", lambda: "jjackson")

    monkeypatch.setattr(cdp_control.socket, "gethostname", lambda: "Jonathans-MacBook-Pro.local")
    first = cdp_control.host_id()
    # ...macOS renames the host out from under us (observed 3x each way in one day)
    monkeypatch.setattr(cdp_control.socket, "gethostname", lambda: "Jonathans-MBP.localdomain")
    assert cdp_control.host_id() == first      # ownership key unchanged -> reuse survives


def test_host_id_degrades_to_the_live_value_when_the_pin_is_unwritable(monkeypatch, tmp_path):
    """An unwritable pin must not crash the runner — fall back to the flappy live value
    (no worse than before) rather than refusing to heartbeat."""
    unwritable = tmp_path / "no-such-dir" / "x" / "host-id"
    monkeypatch.setattr(cdp_control, "HOST_ID_PATH", unwritable)
    monkeypatch.setattr(cdp_control.socket, "gethostname", lambda: "H")
    monkeypatch.setattr(cdp_control.getpass, "getuser", lambda: "u")
    def boom(*a, **k):
        raise OSError("read-only fs")
    monkeypatch.setattr(cdp_control.Path, "mkdir", boom)
    assert cdp_control.host_id() == "u@H"


def test_host_id_ignores_a_blank_pin(monkeypatch, tmp_path):
    pin = tmp_path / "host-id"
    pin.write_text("   \n")
    monkeypatch.setattr(cdp_control, "HOST_ID_PATH", pin)
    monkeypatch.setattr(cdp_control.socket, "gethostname", lambda: "H2")
    monkeypatch.setattr(cdp_control.getpass, "getuser", lambda: "u2")
    assert cdp_control.host_id() == "u2@H2"
