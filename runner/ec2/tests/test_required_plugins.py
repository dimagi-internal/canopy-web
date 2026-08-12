"""`required_plugins` parsing must agree with the doctor check it mirrors.

Eva's readiness drill on the freshly rebuilt box (2026-08-12) passed identity,
gating rails, hook wiring, gog auth and the board — and failed `Required plugins`,
because bootstrap installed each agent's OWN plugin and nothing else. Her
Salesforce and Drive skills are `mcp__plugin_chrome-sales_*`, so the agent was
structurally healthy and functionally unable to do its job.

These pin the entry shapes `canopy agent doctor`'s `check_required_plugins`
accepts, because bootstrap and the doctor reading the same field differently is
worse than either alone: the doctor would fail an agent over a plugin bootstrap
never could have installed.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "required_plugins.py"
sys.path.insert(0, str(SCRIPT.parent))

from required_plugins import rows  # noqa: E402


def test_object_entry_carries_marketplace_and_note():
    got = rows({"required_plugins": [
        {"name": "chrome-sales", "marketplace": "dimagi-internal/chrome-sales",
         "note": "then run the chrome-sales:setup skill"}]})
    assert got == [("chrome-sales", "dimagi-internal/chrome-sales",
                    "chrome-sales", "then run the chrome-sales:setup skill")]


def test_marketplace_name_defaults_to_the_plugin_name():
    """True across this fleet — eva@eva, chrome-sales@chrome-sales — and the
    doctor documents the same default."""
    got = rows({"required_plugins": [{"name": "x", "marketplace": "o/r"}]})
    assert got == [("x", "o/r", "x", "")]


def test_explicit_marketplace_name_wins():
    got = rows({"required_plugins": [
        {"name": "x", "marketplace": "o/r", "marketplace_name": "other"}]})
    assert got[0][2] == "other"


def test_bare_string_entry_is_accepted():
    """The doctor accepts a bare name; so must this, or the two disagree."""
    assert rows({"required_plugins": ["plain"]}) == [("plain", "", "plain", "")]


def test_a_note_with_newlines_cannot_break_the_tsv():
    """Output is tab-separated and consumed by `read -r a b c d`. A note carrying a
    newline would split into a bogus extra row and the loop would try to install a
    plugin named after a fragment of prose."""
    got = rows({"required_plugins": [
        {"name": "x", "marketplace": "o/r", "note": "line one\nline two\ttabbed"}]})
    assert got[0][3] == "line one line two tabbed"
    assert "\n" not in got[0][3] and "\t" not in got[0][3]


def test_missing_or_malformed_declarations_are_silent():
    """An optional dependency list must never take the bootstrap down."""
    assert rows({}) == []
    assert rows({"required_plugins": None}) == []
    assert rows({"required_plugins": "not-a-list"}) == []
    assert rows({"required_plugins": [{"no_name": 1}, 42, None]}) == []


def test_cli_is_silent_on_an_unreadable_file(tmp_path):
    res = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "nope.json")],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0 and res.stdout == ""


def test_cli_emits_tab_separated_rows(tmp_path):
    cfg = tmp_path / "agent.json"
    cfg.write_text(json.dumps({"required_plugins": [
        {"name": "chrome-sales", "marketplace": "dimagi-internal/chrome-sales"}]}))
    res = subprocess.run([sys.executable, str(SCRIPT), str(cfg)],
                         capture_output=True, text=True, timeout=30)
    # Strip the NEWLINE only — not surrounding whitespace. An absent note is a
    # trailing empty field, and `.strip()` would eat the tab that carries it,
    # hiding whether the row still has the arity `read -r a b c d` expects.
    assert res.stdout.rstrip("\n").split("\t") == [
        "chrome-sales", "dimagi-internal/chrome-sales", "chrome-sales", ""]


def test_matches_evas_real_declaration():
    """The actual config that exposed the gap, if this checkout has it."""
    cfg = pathlib.Path.home() / "emdash/repositories/eva/config/agent.json"
    if not cfg.exists():
        return
    got = rows(json.loads(cfg.read_text()))
    assert any(r[0] == "chrome-sales" and r[1] for r in got), got
