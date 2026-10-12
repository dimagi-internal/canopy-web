"""CLAUDE.md is loaded into every session, so it stays a current-state index.

It holds what an agent needs before it knows what it is working on — how to
run, test and ship, and the invariants nothing may break — each as a rule plus
a pointer. Detail lives in docs/architecture/<topic>.md; history lives in the
PR description. Claude Code warns once a CLAUDE.md passes 150k characters.
Pure stdlib; no Django setup.
"""
from __future__ import annotations

import pathlib

CLAUDE_MD = pathlib.Path(__file__).resolve().parent.parent / "CLAUDE.md"
CEILING = 60_000


def test_claude_md_stays_an_index():
    size = len(CLAUDE_MD.read_text(encoding="utf-8"))
    assert size <= CEILING, (
        f"CLAUDE.md is {size:,} chars (ceiling {CEILING:,}). Move subject detail into "
        "docs/architecture/<topic>.md and leave a one-line pointer; put history (what it "
        "replaced, what broke, when) in the PR description, not in docs."
    )
