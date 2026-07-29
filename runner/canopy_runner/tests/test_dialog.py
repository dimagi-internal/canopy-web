"""Native collision dialog — the osascript round-trip is mocked (a real dialog needs a
GUI session). We assert the argv plumbing and the fail-safe default (New session)."""
import subprocess
from types import SimpleNamespace

from canopy_runner import dialog


def _fake_run(stdout):
    captured = {}

    def run(cmd, input, capture_output, text, timeout):
        captured["cmd"] = cmd
        captured["script"] = input
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    return run, captured


def test_passes_message_and_timeout_as_argv(monkeypatch):
    run, captured = _fake_run("Clear & send\n")
    monkeypatch.setattr(dialog.subprocess, "run", run)
    choice = dialog.collision_choice("busy-session", "some leaked words", timeout=25)
    assert choice == dialog.CLEAR
    # osascript reads the script from stdin ("-") and takes msg + timeout as argv
    assert captured["cmd"][0] == "osascript" and captured["cmd"][1] == "-"
    assert captured["cmd"][3] == "25"
    assert "busy-session" in captured["cmd"][2]      # the message carries the task name


def test_each_button_round_trips(monkeypatch):
    for label in (dialog.CLEAR, dialog.NEW, dialog.CANCEL):
        run, _ = _fake_run(label + "\n")
        monkeypatch.setattr(dialog.subprocess, "run", run)
        assert dialog.collision_choice("t", "x") == label


def test_unrecognized_output_falls_back_to_new(monkeypatch):
    run, _ = _fake_run("gibberish\n")
    monkeypatch.setattr(dialog.subprocess, "run", run)
    assert dialog.collision_choice("t", "x") == dialog.NEW


def test_no_gui_session_or_missing_osascript_falls_back_to_new(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("osascript not found")
    monkeypatch.setattr(dialog.subprocess, "run", boom)
    assert dialog.collision_choice("t", "x") == dialog.NEW


def test_timeout_falls_back_to_new(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=40)
    monkeypatch.setattr(dialog.subprocess, "run", slow)
    assert dialog.collision_choice("t", "x") == dialog.NEW


def test_long_preview_is_truncated(monkeypatch):
    run, captured = _fake_run(dialog.NEW + "\n")
    monkeypatch.setattr(dialog.subprocess, "run", run)
    dialog.collision_choice("t", "x" * 500)
    assert "…" in captured["cmd"][2]                 # preview clipped, not dumped whole
