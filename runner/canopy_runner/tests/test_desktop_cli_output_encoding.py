"""Claude Code's output reaches the runner as UTF-8 whatever the box's codepage is.

On Windows (cp1252) text=True alone raised UnicodeDecodeError on a curly quote in
`plugin marketplace list`, and the mod was never registered.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from canopy_runner import desktop


def test_cli_text_decodes_utf8_on_any_codepage():
    child = "import sys; sys.stdout.buffer.write(b'\\xe2\\x80\\x9d')"
    out = subprocess.run([sys.executable, "-c", child], capture_output=True, **desktop.CLI_TEXT)
    assert out.stdout == "\u201d"


def test_ensure_mod_decodes_every_cli_call_as_utf8(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kw):
        calls.append(kw)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    src = tmp_path / "mod"
    src.mkdir()
    monkeypatch.setattr(desktop, "MOD_SRC", src)
    monkeypatch.setattr(desktop, "_cli", lambda: Path("claude"))
    monkeypatch.setattr(desktop, "_cli_env", lambda cli: {})
    monkeypatch.setattr(desktop.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(desktop.subprocess, "run", fake_run)

    desktop.ensure_mod()

    assert calls, "ensure_mod ran no CLI call"
    assert all(kw.get("encoding") == "utf-8" for kw in calls)
