"""Native collision dialog — the osascript/PowerShell round-trip is mocked (a real
dialog needs a desktop session). We assert the argv plumbing on BOTH platforms and
the fail-safe default (New session)."""
import subprocess
from types import SimpleNamespace

import pytest

from canopy_runner import dialog


def _fake_run(stdout):
    captured = {}

    def run(cmd, input, capture_output, text, timeout, **kw):
        captured["cmd"] = cmd
        captured["script"] = input
        captured["env"] = kw.get("env")
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


# -- Windows: the branch that did not exist, and dropped messages because of it --
#
# `osascript` raised FileNotFoundError on Windows, the except arm returned NEW, and
# the human was never asked — so a collision there was not "ask later", it was a
# chat message the sender watched vanish. platform_jobs' docstring asserted the
# rest of the runner was platform-neutral the whole time this wasn't.

def test_windows_renders_through_powershell(monkeypatch):
    monkeypatch.setattr(dialog.platform_jobs, "is_windows", lambda: True)
    run, captured = _fake_run(dialog.CLEAR + "\n")
    monkeypatch.setattr(dialog.subprocess, "run", run)
    assert dialog.collision_choice("busy-session", "some leaked words", timeout=25) == dialog.CLEAR
    assert captured["cmd"][0] == "powershell"
    assert "-NoProfile" in captured["cmd"] and "-NonInteractive" in captured["cmd"]
    # The timeout reaches Popup via the environment, never interpolated into the script.
    assert captured["env"]["CANOPY_DIALOG_TIMEOUT"] == "25"


def test_windows_passes_the_message_on_stdin_not_in_the_script(monkeypatch):
    """A composer line is the human's own words — quotes, $, backticks and all.
    Interpolating it into the PowerShell source would let it break out."""
    monkeypatch.setattr(dialog.platform_jobs, "is_windows", lambda: True)
    run, captured = _fake_run(dialog.NEW + "\n")
    monkeypatch.setattr(dialog.subprocess, "run", run)
    nasty = 'he said "$(rm -rf /)" `and` then stopped'
    dialog.collision_choice("t", nasty)
    assert nasty in captured["script"]          # on stdin
    assert nasty not in " ".join(captured["cmd"])   # NOT in argv/source


def test_windows_message_explains_the_button_mapping(monkeypatch):
    # Windows MessageBox labels are the OS's, so Yes/No/Cancel must be decoded in
    # the body or the human is guessing which button destroys their text.
    monkeypatch.setattr(dialog.platform_jobs, "is_windows", lambda: True)
    run, captured = _fake_run(dialog.NEW + "\n")
    monkeypatch.setattr(dialog.subprocess, "run", run)
    dialog.collision_choice("t", "x")
    assert "Yes" in captured["script"] and dialog.CLEAR in captured["script"]
    assert "Cancel" in captured["script"]


def test_windows_every_button_round_trips(monkeypatch):
    monkeypatch.setattr(dialog.platform_jobs, "is_windows", lambda: True)
    for label in (dialog.CLEAR, dialog.NEW, dialog.CANCEL):
        run, _ = _fake_run(label + "\n")
        monkeypatch.setattr(dialog.subprocess, "run", run)
        assert dialog.collision_choice("t", "x") == label


def test_windows_no_desktop_falls_back_to_new(monkeypatch):
    monkeypatch.setattr(dialog.platform_jobs, "is_windows", lambda: True)
    def boom(*a, **k):
        raise FileNotFoundError("powershell not found")
    monkeypatch.setattr(dialog.subprocess, "run", boom)
    assert dialog.collision_choice("t", "x") == dialog.NEW


def test_zero_timeout_is_floored_on_both_platforms(monkeypatch):
    # `giving up after 0` (AppleScript) and Popup(…, 0, …) both mean WAIT FOREVER.
    # A dialog nobody is looking at would wedge the runner's turn loop.
    for is_win, idx in ((False, None), (True, None)):
        monkeypatch.setattr(dialog.platform_jobs, "is_windows", lambda: is_win)
        run, captured = _fake_run(dialog.NEW + "\n")
        monkeypatch.setattr(dialog.subprocess, "run", run)
        dialog.collision_choice("t", "x", timeout=0)
        got = captured["env"]["CANOPY_DIALOG_TIMEOUT"] if is_win else captured["cmd"][3]
        assert int(got) >= 1, f"is_windows={is_win} passed a wait-forever timeout"


def test_the_powershell_actually_parses():
    """`_POWERSHELL` is a string as far as Python is concerned, so nothing here can
    tell a working script from a broken one — the same blind spot that let a broken
    page.evaluate body ship in emdash_control.mjs (see
    test_every_evaluate_string_is_valid_javascript). Parse it with a real parser.
    Skips where pwsh is absent rather than asserting nothing.
    """
    import shutil
    import subprocess as sp
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if pwsh is None:
        pytest.skip("no PowerShell available to parse with")
    probe = (
        "$e = $null; "
        "[System.Management.Automation.Language.Parser]::ParseInput("
        "[Console]::In.ReadToEnd(), [ref]$null, [ref]$e) | Out-Null; "
        "if ($e) { $e | ForEach-Object { $_.Message }; exit 1 }"
    )
    out = sp.run([pwsh, "-NoProfile", "-Command", probe], input=dialog._POWERSHELL,
                 capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"_POWERSHELL does not parse: {out.stdout[:300]}"
